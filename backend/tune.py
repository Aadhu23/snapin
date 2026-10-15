"""Detection diagnostics.

Answers the question "why is my phone not being detected?" instead of just
returning found: false. Runs the real model over one or more images and prints
every candidate with the scores that feed the two gates, plus what would
happen at different thresholds.

Usage:
    python backend/tune.py phone.png
    python backend/tune.py frame1.jpg frame2.jpg
    python backend/tune.py webcam.jpg --sweep-imgsz
    python backend/tune.py webcam.jpg --compare-models yolov8s.pt yolo11s.pt

Exit code is 0 if any image triggers an alert, 1 if none do, so this can gate
a test run.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import cv2  # noqa: E402

import env as env_loader  # noqa: E402

# So tune.py reports the same gates the running server uses.
env_loader.load_env()


def _load_detector(conf_threshold, area_threshold, imgsz, tta=None):
    """Build a detector using the server's real defaults unless overridden.

    Previously tune.py hardcoded conf=0.1 / imgsz=640 / no-TTA, so it reported
    gates the running server was not using. That made tuning misleading.
    """
    from detector import StudyGuardDetector

    detector = StudyGuardDetector()
    if conf_threshold is not None:
        detector.conf_threshold = conf_threshold
    if area_threshold is not None:
        detector.area_threshold = area_threshold
    if imgsz is not None:
        detector.imgsz = imgsz
    if tta is not None:
        detector.use_tta = tta
    return detector


def _gates(detector) -> str:
    low, high = detector.aspect_ratio_range
    return (
        f"gates: conf >= {detector.conf_threshold}   "
        f"area >= {detector.area_threshold}   "
        f"aspect {low}-{high}   "
        f"imgsz={detector.imgsz}   tta={detector.use_tta}"
    )


def _candidates(detector, image) -> list[dict]:
    """Run the model and report every phone-class candidate with its scores."""
    from ultralytics import YOLO

    model = getattr(detector, "model", None) or YOLO(detector.model_path)
    # Run with a floor far below the configured threshold so we can see
    # candidates the current setting would silently discard.
    results = model(
        image,
        conf=0.0005,
        iou=detector.iou,
        imgsz=detector.imgsz,
        augment=detector.use_tta,
        verbose=False,
    )
    result = results[0]
    boxes = getattr(result, "boxes", None)
    if boxes is None or len(boxes) == 0:
        return []

    h, w = image.shape[:2]
    frame_area = float(w * h)
    out = []
    for cls, conf, box in zip(
        boxes.cls.tolist(), boxes.conf.tolist(), boxes.xyxy.tolist()
    ):
        if int(cls) != detector.target_class_id:
            continue
        w = max(0.0, box[2] - box[0])
        h = max(0.0, box[3] - box[1])
        out.append(
            {
                "conf": float(conf),
                "rel_area": (w * h) / frame_area if frame_area else 0.0,
                "aspect": w / h if h > 0 else 0.0,
                "bbox": [round(float(v), 1) for v in box],
            }
        )
    return sorted(out, key=lambda c: c["conf"], reverse=True)


def _top_other_classes(detector, image, limit: int = 5) -> list[tuple[str, float]]:
    """What the model actually saw, so 'no phone' is explainable."""
    from ultralytics import YOLO

    model = getattr(detector, "model", None) or YOLO(detector.model_path)
    result = model(image, conf=0.01, iou=detector.iou, imgsz=detector.imgsz,
                   augment=detector.use_tta, verbose=False)[0]
    boxes = getattr(result, "boxes", None)
    if boxes is None or len(boxes) == 0:
        return []
    names = model.names
    pairs = [
        (str(names[int(c)]), float(cf))
        for c, cf in zip(boxes.cls.tolist(), boxes.conf.tolist())
    ]
    return sorted(pairs, key=lambda p: -p[1])[:limit]


def analyse(image_path: Path, detector, verbose: bool = True) -> dict:
    image = cv2.imread(str(image_path))
    if image is None:
        return {"path": str(image_path), "error": "Could not read image"}

    h, w = image.shape[:2]
    low, high = detector.aspect_ratio_range
    cands = _candidates(detector, image)
    # Judge the strongest candidate: the one closest to passing, not merely the
    # highest-scoring box. A low-confidence phone-shaped box is more useful to
    # report than a high-confidence box that fails the shape gate.
    best = max(
        cands,
        key=lambda c: (
            (c["conf"] >= detector.conf_threshold)
            + (c["rel_area"] >= detector.area_threshold)
            + (low <= c["aspect"] <= high),
            c["conf"],
        ),
        default=None,
    )

    conf_pass = best is not None and best["conf"] >= detector.conf_threshold
    area_pass = best is not None and best["rel_area"] >= detector.area_threshold
    aspect_pass = best is not None and low <= best["aspect"] <= high
    alerts = bool(conf_pass and area_pass and aspect_pass)

    if verbose:
        print(f"\n{'=' * 68}")
        print(f"{image_path.name}  ({w}x{h})")
        print("=" * 68)
        print(_gates(detector))

        if not cands:
            print("\n  No phone-class box at all (searched down to conf 0.0005).")
            tops = _top_other_classes(detector, image)
            if tops:
                print("  Model saw instead: " + ", ".join(
                    f"{n} {c:.3f}" for n, c in tops
                ))
            print(
                "\n  Likely the phone is too small, too oblique, or too "
                "cluttered.\n  Try a larger SNAPIN_IMGSZ, or hold the phone "
                "closer to the lens."
            )
        else:
            print(f"\n  {'phone candidates:':<22}")
            for c in cands[:5]:
                marks = []
                marks.append("conf OK" if c["conf"] >= detector.conf_threshold else "conf low")
                marks.append(
                    "area OK" if c["rel_area"] >= detector.area_threshold else "area small"
                )
                marks.append(
                    "shape OK" if low <= c["aspect"] <= high else "shape wrong"
                )
                print(
                    f"    conf={c['conf']:.4f}  rel_area={c['rel_area']:.4f}  "
                    f"aspect={c['aspect']:.2f}  bbox={c['bbox']}  [{', '.join(marks)}]"
                )

            verdict = "ALERT" if alerts else "no alert"
            print(f"\n  => {verdict}")
            if not alerts and best:
                blockers = []
                if not conf_pass:
                    need = detector.conf_threshold / max(best["conf"], 1e-9)
                    blockers.append(
                        f"confidence is the blocker -- it would pass at "
                        f"SNAPIN_CONF_THRESHOLD={best['conf']:.4f} "
                        f"(~{need:.0f}x lower sensitivity)"
                    )
                if not area_pass:
                    blockers.append(
                        f"area is the blocker -- it would pass at "
                        f"SNAPIN_AREA_THRESHOLD={best['rel_area']:.4f}"
                    )
                if not aspect_pass:
                    blockers.append(
                        f"shape is the blocker -- aspect {best['aspect']:.2f} "
                        f"is outside {low}-{high}; widen SNAPIN_MIN_ASPECT / "
                        f"SNAPIN_MAX_ASPECT if this really is a phone"
                    )
                for line in blockers:
                    print(f"     {line}")

    return {
        "path": str(image_path),
        "size": (w, h),
        "alerts": alerts,
        "best_conf": best["conf"] if best else 0.0,
        "best_rel_area": best["rel_area"] if best else 0.0,
        "candidates": len(cands),
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("images", nargs="+", help="image file(s) to analyse")
    parser.add_argument("--conf", type=float, default=None,
                        help="override SNAPIN_CONF_THRESHOLD")
    parser.add_argument("--area", type=float, default=None,
                        help="override SNAPIN_AREA_THRESHOLD")
    parser.add_argument("--imgsz", type=int, default=None,
                        help="override SNAPIN_IMGSZ")
    parser.add_argument("--no-tta", action="store_true",
                        help="disable TTA for this run")
    parser.add_argument("--tta", action="store_true",
                        help="force TTA on (default matches the server)")
    parser.add_argument("--sweep-imgsz", action="store_true",
                        help="report the phone score at several imgsz values")
    parser.add_argument("--compare-models", nargs="+", metavar="MODEL",
                        help="compare phone scores across model files")
    args = parser.parse_args(argv)

    # None means "inherit the server defaults"; only an explicit flag changes it.
    tta = True if args.tta else (False if args.no_tta else None)
    detector = _load_detector(args.conf, args.area, args.imgsz, tta)

    if args.compare_models:
        from detector import StudyGuardDetector

        for model_file in args.compare_models:
            probe = StudyGuardDetector(model_path=model_file)
            if tta is not None:
                probe.use_tta = tta
            print(f"\n{'#' * 68}\n## model: {model_file}\n{'#' * 68}")
            for path in args.images:
                analyse(Path(path), probe)

        return 0

    results = [analyse(Path(p), detector) for p in args.images]

    if args.sweep_imgsz:
        from detector import StudyGuardDetector

        print(f"\n{'=' * 68}\nimgsz sweep\n{'=' * 68}")
        probe = StudyGuardDetector()
        if tta is not None:
            probe.use_tta = tta
        sizes = (320, 416, 480, 512, 576, 640, 768, 960)
        for path in args.images:
            image = cv2.imread(path)
            if image is None:
                continue
            print(f"\n  {Path(path).name}")
            for size in sizes:
                probe.imgsz = size
                cands = _candidates(probe, image)
                best = cands[0]["conf"] if cands else 0.0
                alert = best >= probe.conf_threshold
                print(f"    imgsz={size:4d}  best_phone_conf={best:.4f}  {'ALERT' if alert else ''}")

    errored = [r for r in results if r.get("error")]
    if errored:
        for r in errored:
            print(f"ERROR: {r['path']}: {r['error']}", file=sys.stderr)
        return 2

    triggered = [r for r in results if r["alerts"]]
    print(f"\n{'=' * 68}")
    print(
        f"{len(triggered)}/{len(results)} image(s) would trigger an alert"
        + ("" if triggered else " -- nothing detected")
    )
    print("=" * 68)
    return 0 if triggered else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))