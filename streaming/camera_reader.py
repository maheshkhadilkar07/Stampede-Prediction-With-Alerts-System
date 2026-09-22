"""
streaming/camera_reader.py
============================
Wraps cv2.VideoCapture in its own background thread so frame grabbing
never blocks the rest of the live pipeline. Without this, a slow camera
or network RTSP stream would stall YOLO inference and the MJPEG preview
in lock-step, causing visible lag on the dashboard.

Supports three source types:
    "webcam" -> local laptop/USB camera (integer device index)
    "rtsp"   -> RTSP CCTV / IP camera stream URL
    "ip"     -> plain HTTP(S) IP camera stream URL (same handling as RTSP,
                OpenCV's FFmpeg backend resolves both transparently)

Save this file at: Stampede-Prediction-System/streaming/camera_reader.py
"""

import threading
import time

import cv2

from utils.logger import get_logger

logger = get_logger(__name__)


class CameraReader:
    """
    Continuously reads frames from a camera/stream source in a background
    thread and exposes the latest frame via `read()`. Thread-safe.
    """

    def __init__(self, source, source_type: str = "webcam"):
        """
        source: int (webcam device index) or str (RTSP/HTTP stream URL)
        source_type: 'webcam' | 'rtsp' | 'ip' — used only for logging.
        """
        self.source = source
        self.source_type = source_type
        self.capture = None
        self._latest_frame = None
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread = None
        self.is_opened = False
        self.frame_width = 0
        self.frame_height = 0
        self.fps = 0.0

    def start(self) -> bool:
        """
        Opens the video source and starts the background capture thread.
        Returns True on success, False if the source could not be opened
        (e.g. no webcam present, wrong RTSP URL/credentials).
        """
        logger.info(f"Opening camera source ({self.source_type}): {self.source}")
        self.capture = cv2.VideoCapture(self.source)

        if not self.capture.isOpened():
            logger.error(f"Failed to open camera source: {self.source}")
            self.is_opened = False
            return False

        self.frame_width = int(self.capture.get(cv2.CAP_PROP_FRAME_WIDTH)) or 640
        self.frame_height = int(self.capture.get(cv2.CAP_PROP_FRAME_HEIGHT)) or 480
        self.fps = self.capture.get(cv2.CAP_PROP_FPS) or 25.0
        self.is_opened = True

        self._stop_event.clear()
        self._thread = threading.Thread(target=self._capture_loop, daemon=True)
        self._thread.start()
        logger.info(
            f"Camera opened: {self.frame_width}x{self.frame_height} @ {self.fps:.1f} fps"
        )
        return True

    def _capture_loop(self) -> None:
        """Background loop: continuously grabs the latest frame."""
        consecutive_failures = 0
        while not self._stop_event.is_set():
            ok, frame = self.capture.read()
            if not ok or frame is None:
                consecutive_failures += 1
                if consecutive_failures > 30:
                    logger.error(
                        f"Camera source {self.source} stopped returning frames. Stopping reader."
                    )
                    break
                time.sleep(0.05)
                continue

            consecutive_failures = 0
            with self._lock:
                self._latest_frame = frame

        self.is_opened = False

    def read(self):
        """Return the most recently captured frame (or None if not ready yet)."""
        with self._lock:
            if self._latest_frame is None:
                return None
            return self._latest_frame.copy()

    def stop(self) -> None:
        """Stop the capture thread and release the underlying device/stream."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        if self.capture is not None:
            self.capture.release()
        self.is_opened = False
        logger.info(f"Camera source released: {self.source}")
