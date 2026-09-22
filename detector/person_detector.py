"""
detector/person_detector.py
============================
Wraps an Ultralytics YOLO model to detect PEOPLE ONLY in a video frame.

Save this file at: Stampede-Prediction-System/detector/person_detector.py
"""

import os

import numpy as np
from ultralytics import YOLO

import config
from utils.logger import get_logger

logger = get_logger(__name__)


class Detection:
    """
    A single person detection: bounding box + confidence.
    Kept as a lightweight class instead of a dict so downstream modules
    get IDE autocomplete and type safety.
    """

    __slots__ = ("x1", "y1", "x2", "y2", "confidence")

    def __init__(self, x1: int, y1: int, x2: int, y2: int, confidence: float):
        self.x1 = x1
        self.y1 = y1
        self.x2 = x2
        self.y2 = y2
        self.confidence = confidence

    @property
    def centroid(self):
        """Return the (cx, cy) centroid of the bounding box."""
        return (self.x1 + self.x2) // 2, (self.y1 + self.y2) // 2

    @property
    def width(self):
        return self.x2 - self.x1

    @property
    def height(self):
        return self.y2 - self.y1


class PersonDetector:
    """
    Loads a YOLO model once and exposes a detect() method that returns a
    list of Detection objects for every person found in a given frame.
    """

    def __init__(self, model_path: str = None, confidence: float = None):
        self.model_path = model_path or config.YOLO_MODEL_PATH
        self.confidence = confidence or config.YOLO_CONFIDENCE_THRESHOLD
        self.model = self._load_model()

    def _load_model(self) -> YOLO:
        """
        Load the YOLO model. If the weights file does not exist locally,
        fall back to the standard 'yolo26n.pt' identifier, which
        ultralytics will download automatically PROVIDED the machine
        running this code has internet access. (Note: this will NOT work
        inside a network-restricted sandbox — place the .pt file manually
        in trained_models/ in that case.)

        YOLO26 (released Jan 2026) is Ultralytics' current recommended
        model: NMS-free end-to-end inference, no DFL head, and faster
        CPU inference than YOLOv8/YOLO11 — a good fit for a CPU-only
        final-year-project machine. It uses the exact same Python API
        (model.predict(), results[0].boxes, box.xyxy/box.conf) as
        older Ultralytics models, so nothing else in this file changes.
        """
        if os.path.exists(self.model_path):
            logger.info(f"Loading YOLO weights from local path: {self.model_path}")
            return YOLO(self.model_path)

        logger.warning(
            f"YOLO weights not found at {self.model_path}. "
            "Falling back to 'yolo26n.pt' (requires internet to auto-download)."
        )
        return YOLO("yolo26n.pt")

    def detect(self, frame: np.ndarray) -> list:
        """
        Run YOLO inference on a single BGR frame (as returned by
        cv2.VideoCapture.read()) and return a list of Detection objects
        for the "person" class only.
        """
        results = self.model.predict(
            source=frame,
            imgsz=config.YOLO_IMG_SIZE,
            conf=self.confidence,
            classes=[config.YOLO_PERSON_CLASS_ID],
            verbose=False,
        )

        detections = []
        if not results:
            return detections

        boxes = results[0].boxes
        if boxes is None:
            return detections

        for box in boxes:
            x1, y1, x2, y2 = box.xyxy[0].tolist()
            conf = float(box.conf[0])
            detections.append(
                Detection(int(x1), int(y1), int(x2), int(y2), conf)
            )

        return detections
