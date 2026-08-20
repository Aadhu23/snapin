from ultralytics import YOLO
import cv2
import numpy as np


class StudyGuardDetector:
    def __init__(self, model_path="yolov8s.pt"):
        self.model = YOLO(model_path)
        self.target_class_id = 67
        self.conf_threshold = 0.1
        self.area_threshold = 0.015

    def detect_phone(self, frame_bytes: bytes) -> dict:
        try:
            # Decode the image bytes to opencv format
            nparr = np.frombuffer(frame_bytes, np.uint8)
            frame = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
            cv2.imwrite("debug_input.jpg", frame)

            if frame is None:
                return {"found": False, "error": "Could not decode image"}

            # Run inference
            results = self.model(frame, verbose=False)
            result = results[0]

            found = False
            max_conf = 0.0

            if result.boxes:
                cls_list = result.boxes.cls.tolist()
                conf_list = result.boxes.conf.tolist()
                boxes = result.boxes.xyxy.tolist()

                h, w = frame.shape[:2]
                frame_area = w * h
                print(f"AI saw these classes: {cls_list}")

                for cls, conf, box in zip(cls_list, conf_list, boxes):
                    if int(cls) == self.target_class_id and conf >= self.conf_threshold:
                        x1, y1, x2, y2 = box
                        area = max(0.0, (x2 - x1) * (y2 - y1))
                        print(
                            f"Object Area: {area}, Required Threshold: {self.area_threshold * frame_area}"
                        )

                        # Calculate relative area (0.0 - 1.0)
                        relative_area = area / frame_area
                        if (
                            int(cls) == self.target_class_id
                            and conf >= self.conf_threshold
                        ):
                            if relative_area >= self.area_threshold:
                                return {
                                    "found": True,
                                    "confidence": conf,
                                    "rel_area": relative_area,
                                }
            return {"found": False, "confidence": 0.0}

        except Exception as e:
            print(f"Error in detection: {e}")
            return {"found": False, "error": str(e)}
