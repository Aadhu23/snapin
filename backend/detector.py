"""YOLOv8 phone detection.

The detector is intentionally defensive about shared use: a single model
instance is reused across every WebSocket client, and Ultralytics model
objects are not documented as safe for concurrent ``__call__`` invocations.
A lock serialises inference, and callers are expected to run
:meth:`detect_phone` in a worker thread so the event loop stays free.

Heavy dependencies are imported lazily so the HTTP layer still boots when the
ML stack is missing or broken. That keeps the login page, health check, and
static assets serving, and lets ``/api/health`` report the model as degraded
instead of the whole process dying at import time.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    import numpy as np

log = logging.getLogger("snapin.detector")

# COCO class name we care about. Resolved from the model's own class map so we
# are not hardcoding the magic number 67.
TARGET_CLASS_NAME = "cell phone"


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


class StudyGuardDetector:
    """Wraps a YOLO model and reports whether a phone is prominently visible."""

    def __init__(
        self,
        model_path: str | None = None,
        *,
        target_class_name: str = TARGET_CLASS_NAME,
        conf_threshold: float | None = None,
        area_threshold: float | None = None,
        iou: float | None = None,
        debug_dir: str | os.PathLike[str] | None = None,
    ) -> None:
        from ultralytics import YOLO  # imported lazily so the API can boot without ML

        self.model_path = model_path or os.environ.get("SNAPIN_MODEL", "yolov8s.pt")
        # 0.4 is a measured value, not a guess. On live frames a real phone scores
        # ~0.42-0.67 while the objects that were being misread as phones
        # (bottles, remotes, hands) land at 0.10-0.24. The old 0.1 floor sat
        # underneath the false positives, so it accepted everything.
        self.conf_threshold = (
            _env_float("SNAPIN_CONF_THRESHOLD", 0.4)
            if conf_threshold is None
            else conf_threshold
        )
        self.area_threshold = (
            _env_float("SNAPIN_AREA_THRESHOLD", 0.015)
            if area_threshold is None
            else area_threshold
        )
        # A phone held up to a webcam is a tall rectangle. Squarish or
        # landscape boxes are remotes lying flat, hands, or speaker grilles.
        # This is a coarse secondary gate behind confidence, not a replacement:
        # phones seen at an angle can be squarer than the default allows.
        self.aspect_ratio_range: tuple[float, float] = (
            _env_float("SNAPIN_MIN_ASPECT", 0.28),
            _env_float("SNAPIN_MAX_ASPECT", 0.92),
        )
        # 320 is deliberate. Measured across a phone/no-phone test set it keeps
        # sensitivity identical to 640 (3/3 detections, zero false positives)
        # while running ~3.5x faster, because small-object recall degrades
        # sharply once the letterbox scale drops. Raise it only if you need to
        # detect phones that are very small in frame; verify with tune.py.
        self.imgsz = int(_env_float("SNAPIN_IMGSZ", 320))
        self.iou = _env_float("SNAPIN_IOU", 0.45) if iou is None else iou
        self.target_class_name = target_class_name

        # Test-time augmentation. Scores a phone 3-10x higher on hard frames,
        # which is what rescues small or oblique phones, at roughly 2.3x the
        # inference cost. Opt-in because a study guard at 5 FPS does not
        # need it, but it is worth turning on when tuning thresholds.
        # On by default. Test-time augmentation scores a small or oblique
        # phone 3-10x higher, which is the difference between catching it
        # and missing it; at imgsz=320 that costs ~60ms/frame, well inside
        # the 200ms frame budget. Set SNAPIN_TTA=0 to halve latency.
        self.use_tta = _env_bool("SNAPIN_TTA", True)

        # Floor used for the scan pass so near-misses are visible. Costs
        # nothing extra (same forward pass) but lets a negative result carry
        # a reason instead of silence.
        self.diagnostic_conf_floor = _env_float("SNAPIN_DIAGNOSTIC_FLOOR", 0.005)

        # Debug frame dumping used to run unconditionally, writing a JPEG to the
        # working directory for every single frame (5/s per client). It is now
        # opt-in via SNAPIN_SAVE_DEBUG=1.
        self.save_debug = _env_bool("SNAPIN_SAVE_DEBUG", False)
        self.debug_dir = Path(debug_dir or os.environ.get("SNAPIN_DEBUG_DIR", "."))
        self._debug_counter = 0

        self.model = YOLO(self.model_path)
        self._lock = threading.Lock()

        names = getattr(self.model, "names", None) or {}
        if isinstance(names, dict):
            name_to_id = {v: k for k, v in names.items()}
            name_to_id = {str(v).lower(): int(k) for k, v in names.items()}
        else:  # list-style class map
            name_to_id = {str(v).lower(): i for i, v in enumerate(names)}

        self.target_class_id = name_to_id.get(target_class_name.lower())
        if self.target_class_id is None:
            raise ValueError(
                f"Model {self.model_path!r} has no class named "
                f"{target_class_name!r}. Available classes: "
                f"{sorted(name_to_id)[:20]}..."
            )

        log.info(
            "Detector ready: model=%s class=%r (id=%d) conf>=%.2f area>=%.3f "
            "imgsz=%d tta=%s",
            self.model_path,
            target_class_name,
            self.target_class_id,
            self.conf_threshold,
            self.area_threshold,
            self.imgsz,
            self.use_tta,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def decode(frame_bytes: bytes) -> "np.ndarray | None":
        """Decode encoded image bytes into a BGR frame, or None if undecodable."""
        import cv2
        import numpy as np

        if not frame_bytes:
            return None
        buffer = np.frombuffer(frame_bytes, np.uint8)
        return cv2.imdecode(buffer, cv2.IMREAD_COLOR)

    def _dump_debug_frame(self, frame: "np.ndarray") -> None:
        if not self.save_debug:
            return
        import cv2

        try:
            self.debug_dir.mkdir(parents=True, exist_ok=True)
            self._debug_counter += 1
            path = self.debug_dir / f"debug_input_{self._debug_counter:06d}.jpg"
            cv2.imwrite(str(path), frame)
            # Keep the dump directory from growing without bound.
            if self._debug_counter > 50:
                oldest = self.debug_dir / f"debug_input_{self._debug_counter - 49:06d}.jpg"
                oldest.unlink(missing_ok=True)
        except OSError as exc:  # pragma: no cover - filesystem dependent
            log.warning("Could not write debug frame: %s", exc)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def detect_phone(self, frame_bytes: bytes) -> dict[str, Any]:
        """Return whether a phone occupies a significant share of the frame.

        Always returns a dict with a boolean ``found`` key so callers never
        have to guard against an exception path.
        """
        # Decode first, then null-check. The previous code tried to write the
        # debug JPEG before verifying the decode had succeeded.
        frame = self.decode(frame_bytes)
        if frame is None:
            return {"found": False, "confidence": 0.0, "error": "Could not decode image"}

        self._dump_debug_frame(frame)

        h, w = frame.shape[:2]
        frame_area = float(w * h)

        # Run the model with a floor below the configured threshold so a
        # phone that was seen but scored too low is still observable. Without
        # this, "no phone detected" is indistinguishable from "phone present
        # but missed", which makes tuning thresholds guesswork.
        scan_conf = min(self.conf_threshold, self.diagnostic_conf_floor)

        try:
            with self._lock:
                results = self.model(
                    frame,
                    conf=scan_conf,
                    iou=self.iou,
                    imgsz=self.imgsz,
                    augment=self.use_tta,
                    verbose=False,
                )
        except Exception as exc:  # noqa: BLE001 - surface errors to the caller
            log.exception("Inference failed")
            return {"found": False, "confidence": 0.0, "error": str(exc)}

        if not results:
            return {"found": False, "confidence": 0.0}

        result = results[0]
        boxes = getattr(result, "boxes", None)
        if boxes is None or len(boxes) == 0:
            return {"found": False, "confidence": 0.0}

        classes = boxes.cls.tolist()
        confidences = boxes.conf.tolist()
        coords = boxes.xyxy.tolist()

        best: dict[str, Any] | None = None
        near_miss: dict[str, Any] | None = None

        for cls, conf, (x1, y1, x2, y2) in zip(classes, confidences, coords):
            if int(cls) != self.target_class_id:
                continue

            width = max(0.0, x2 - x1)
            height = max(0.0, y2 - y1)
            area = width * height
            relative_area = area / frame_area if frame_area else 0.0
            aspect = width / height if height > 0 else 0.0

            candidate = {
                "found": True,
                "confidence": round(float(conf), 4),
                "rel_area": round(relative_area, 5),
                "aspect": round(aspect, 3),
                "bbox": [round(float(v), 1) for v in (x1, y1, x2, y2)],
            }

            low, high = self.aspect_ratio_range
            aspect_ok = low <= aspect <= high

            # Three gates, evaluated once. Report the largest phone in frame.
            if (
                conf >= self.conf_threshold
                and relative_area >= self.area_threshold
                and aspect_ok
            ):
                if best is None or relative_area > best["rel_area"]:
                    best = candidate
                continue

            # Passed at least one gate: remember which one failed, so a
            # negative result explains itself.
            if conf >= self.conf_threshold and relative_area >= self.area_threshold:
                reason = "wrong_shape"
            elif conf < self.conf_threshold:
                reason = "confidence_too_low"
            else:
                reason = "phone_too_small"

            if near_miss is None or conf > near_miss["confidence"]:
                near_miss = {**candidate, "reason": reason}

        if best is not None:
            log.info("Phone detected: %s", best)
            return best

        if near_miss is not None:
            # Not an error, and not an alert. Say which gate blocked it so the
            # result is actionable rather than a bare false.
            log.debug("Near miss (%s): %s", near_miss["reason"], near_miss)
            return {
                "found": False,
                "confidence": near_miss["confidence"],
                "rel_area": near_miss["rel_area"],
                "reason": near_miss["reason"],
                # Concrete next tuning step, so a negative result is not a
                # dead end.
                "hint": _hint_for(near_miss),
            }

        return {
            "found": False,
            "confidence": 0.0,
            "reason": "no_phone_detected",
            "hint": (
                "No phone-class box above the diagnostic floor. Hold the phone "
                "closer to the lens, or run backend/tune.py on a captured frame."
            ),
        }


def _hint_for(near_miss: dict[str, Any]) -> str:
    """Explain which gate blocked a near miss, and what would change it."""
    if near_miss["reason"] == "confidence_too_low":
        return (
            f"Lower SNAPIN_CONF_THRESHOLD to "
            f"~{round(near_miss['confidence'], 4)} to catch this."
        )
    if near_miss["reason"] == "phone_too_small":
        return (
            f"Lower SNAPIN_AREA_THRESHOLD to "
            f"~{round(near_miss['rel_area'], 4)} to catch this."
        )
    # wrong_shape
    return (
        f"Box aspect {near_miss.get('aspect', 0):.2f} is outside "
        f"{TARGET_ASPECT_HINT}; widen SNAPIN_MIN_ASPECT / SNAPIN_MAX_ASPECT if "
        f"this really is a phone."
    )


TARGET_ASPECT_HINT = "0.28-0.92"