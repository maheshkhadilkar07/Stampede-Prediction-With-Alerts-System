"""
streaming/stream_manager.py
==============================
Orchestrates Mode 2 (Live CCTV): camera -> Fluvio -> YOLO -> density ->
risk -> heatmap -> dashboard, exactly mirroring the diagram in the
project spec:

    Live CCTV Camera -> Fluvio Producer -> Fluvio Topic -> Fluvio
    Consumer -> YOLO Detection -> Crowd Density -> CNN Prediction ->
    Alert System -> Dashboard

(CNN prediction is not wired in yet — see cnn/ module, still pending —
so risk currently comes from the same rule-based RiskAnalyzer used in
Mode 1, which will be cross-checked against the CNN once it exists.)

FLUVIO FALLBACK: if a real Fluvio cluster is not reachable (e.g. Fluvio
isn't installed / WSL2 not set up yet), this manager automatically runs
in "direct" mode: camera frames go straight into the detection pipeline
in-process, skipping the message broker hop. Everything else (YOLO,
density, risk, heatmap, alerts, dashboard) behaves identically either
way, so the live-CCTV feature works today and gets the real streaming
backbone the moment Fluvio is set up, with zero code changes.

Save this file at: Stampede-Prediction-System/streaming/stream_manager.py
"""

import threading
import time

import cv2

import config
from utils.logger import get_logger
from detector.person_detector import PersonDetector
from detector.crowd_counter import CrowdCounter
from detector.density_estimator import DensityEstimator
from detector.risk_analyzer import RiskAnalyzer
from detector.heatmap import HeatmapGenerator
from streaming.camera_reader import CameraReader
from streaming.producer import FrameProducer, FluvioUnavailableError
from streaming.consumer import FrameConsumer

logger = get_logger(__name__)


class LiveStreamManager:
    """
    Singleton-style manager (one instance is created in app.py) that
    owns the full live-CCTV pipeline lifecycle: start a source, run
    detection continuously in a background thread, expose the latest
    annotated JPEG frame for MJPEG preview, and expose live stats for
    the JSON polling API.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread = None

        self.camera: CameraReader = None
        self.producer: FrameProducer = None
        self.consumer: FrameConsumer = None

        self.is_running = False
        self.fluvio_active = False
        self.source_type = None
        self.source_value = None

        self.person_detector = None  # loaded lazily (expensive)
        self.crowd_counter = CrowdCounter(history_size=60)
        self.density_estimator = DensityEstimator()
        self.risk_analyzer = RiskAnalyzer()
        self.heatmap_generator = HeatmapGenerator()

        self._latest_jpeg = None
        self._latest_stats = {
            "person_count": 0,
            "max_cell_density": 0,
            "risk_level": "SAFE",
            "risk_label": "Safe",
            "risk_color": "#28a745",
            "fps": 0.0,
            "fluvio_active": False,
        }
        self._last_alert_level = None
        self._last_alert_time = 0.0
        self.video_id = None  # DB Video row id for this live session, set by app.py

        # on_alert(level, message) callback set by app.py so this module
        # never has to import Flask/SQLAlchemy models directly.
        self.on_alert = None

    # -----------------------------------------------------------------
    # LIFECYCLE
    # -----------------------------------------------------------------
    def start(self, source_type: str, source_value) -> dict:
        """
        Start the live pipeline for the given source.
            source_type: 'webcam' | 'rtsp' | 'ip'
            source_value: int (webcam index) or str (stream URL)

        Returns a dict describing what actually started, e.g.
        {"started": True, "fluvio_active": False, "reason": "..."}
        """
        with self._lock:
            if self.is_running:
                return {"started": False, "reason": "A live session is already running."}

            self.source_type = source_type
            self.source_value = source_value

            self.camera = CameraReader(source_value, source_type=source_type)
            if not self.camera.start():
                self.camera = None
                return {
                    "started": False,
                    "reason": f"Could not open source ({source_type}: {source_value}). "
                              "Check the camera is connected / URL is correct.",
                }

            # Lazily load YOLO once per process (expensive), reused across sessions.
            if self.person_detector is None:
                self.person_detector = PersonDetector()

            self.crowd_counter = CrowdCounter(history_size=60)
            self.heatmap_generator = HeatmapGenerator()
            self._last_alert_level = None

            # Try Fluvio first; fall back to direct mode on any failure.
            fluvio_reason = None
            if config.FLUVIO_ENABLED:
                try:
                    self.producer = FrameProducer()
                    self.producer.connect()
                    self.consumer = FrameConsumer()
                    self.consumer.connect()
                    self.fluvio_active = True
                    logger.info("Live session running in FLUVIO streaming mode.")
                except FluvioUnavailableError as exc:
                    fluvio_reason = str(exc)
                    self.producer = None
                    self.consumer = None
                    self.fluvio_active = False
                    logger.warning(
                        f"Fluvio unavailable, falling back to direct mode: {exc}"
                    )
            else:
                self.fluvio_active = False

            self._stop_event.clear()
            self.is_running = True

            if self.fluvio_active:
                # Two threads: one publishes camera frames to Fluvio, one
                # consumes them back and runs the detection pipeline.
                self._producer_thread = threading.Thread(
                    target=self._producer_loop, daemon=True
                )
                self._thread = threading.Thread(
                    target=self._fluvio_consumer_loop, daemon=True
                )
                self._producer_thread.start()
            else:
                # Direct mode: a single loop reads the camera and runs
                # detection in-process (no broker hop).
                self._producer_thread = None
                self._thread = threading.Thread(target=self._direct_loop, daemon=True)

            self._thread.start()

            return {
                "started": True,
                "fluvio_active": self.fluvio_active,
                "reason": fluvio_reason,
            }

    def stop(self) -> None:
        """Stop all pipeline threads and release the camera/Fluvio connections."""
        with self._lock:
            if not self.is_running:
                return
            self._stop_event.set()

        if self._thread is not None:
            self._thread.join(timeout=3.0)
        if getattr(self, "_producer_thread", None) is not None:
            self._producer_thread.join(timeout=3.0)

        if self.camera is not None:
            self.camera.stop()
        if self.producer is not None:
            self.producer.close()
        if self.consumer is not None:
            self.consumer.close()

        self.is_running = False
        self.fluvio_active = False
        self.video_id = None
        logger.info("Live session stopped.")

    # -----------------------------------------------------------------
    # PIPELINE LOOPS
    # -----------------------------------------------------------------
    def _producer_loop(self) -> None:
        """(Fluvio mode) Continuously publish camera frames to the topic."""
        target_interval = 1.0 / 15.0  # cap publish rate at ~15 fps
        while not self._stop_event.is_set():
            frame = self.camera.read()
            if frame is not None:
                try:
                    self.producer.send_frame(frame)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"Producer publish failed: {exc}")
            time.sleep(target_interval)

    def _fluvio_consumer_loop(self) -> None:
        """(Fluvio mode) Consume frames from the topic and run detection on each."""
        try:
            for frame, captured_at in self.consumer.frames():
                if self._stop_event.is_set():
                    break
                self._process_frame(frame, latency_source_ts=captured_at)
        except Exception as exc:  # noqa: BLE001
            logger.exception(f"Fluvio consumer loop crashed: {exc}")

    def _direct_loop(self) -> None:
        """(Fallback mode) Read camera frames and run detection directly, no broker."""
        target_interval = 1.0 / 12.0  # cap inference rate at ~12 fps for CPU machines
        while not self._stop_event.is_set():
            frame = self.camera.read()
            if frame is not None:
                self._process_frame(frame)
            time.sleep(target_interval)

    # -----------------------------------------------------------------
    # CORE FRAME PROCESSING (shared by both modes)
    # -----------------------------------------------------------------
    def _process_frame(self, frame, latency_source_ts: float = None) -> None:
        """
        Runs the full YOLO -> density -> risk -> heatmap pipeline on one
        frame, updates the shared latest-frame/stats state, and fires
        alerts through the on_alert callback when risk escalates.
        """
        frame_start = time.time()
        frame_h, frame_w = frame.shape[:2]

        # Resize for consistent inference speed, matching Mode 1's approach.
        scale = config.LIVE_INFERENCE_WIDTH / frame_w if frame_w > config.LIVE_INFERENCE_WIDTH else 1.0
        infer_frame = cv2.resize(frame, (0, 0), fx=scale, fy=scale) if scale != 1.0 else frame

        detections = self.person_detector.detect(infer_frame)
        # Scale detections back up to the original frame size for drawing.
        if scale != 1.0:
            for det in detections:
                det.x1, det.y1, det.x2, det.y2 = (
                    int(det.x1 / scale), int(det.y1 / scale),
                    int(det.x2 / scale), int(det.y2 / scale),
                )

        self.crowd_counter.update(len(detections))
        density_result = self.density_estimator.compute(detections, frame_w, frame_h)
        risk_result = self.risk_analyzer.analyze(density_result, self.crowd_counter.growth_rate)
        self.heatmap_generator.update(density_result["grid"])

        annotated = self.heatmap_generator.render_overlay(frame)
        for det in detections:
            cv2.rectangle(annotated, (det.x1, det.y1), (det.x2, det.y2), (0, 255, 120), 2)

        cv2.putText(
            annotated, f"People: {len(detections)}  Risk: {risk_result['label']}",
            (16, 32), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2, cv2.LINE_AA,
        )

        ok, buffer = cv2.imencode(
            ".jpg", annotated, [cv2.IMWRITE_JPEG_QUALITY, config.STREAM_JPEG_QUALITY]
        )
        fps = 1.0 / max(time.time() - frame_start, 1e-6)

        with self._lock:
            if ok:
                self._latest_jpeg = buffer.tobytes()
            self._latest_stats = {
                "person_count": len(detections),
                "max_cell_density": density_result["max_cell_density"],
                "risk_level": risk_result["level"],
                "risk_label": risk_result["label"],
                "risk_color": risk_result["color"],
                "fps": round(fps, 1),
                "fluvio_active": self.fluvio_active,
            }

        self._maybe_fire_alert(risk_result)

    def _maybe_fire_alert(self, risk_result: dict) -> None:
        """
        Fires the on_alert callback when the risk level enters HIGH/CRITICAL,
        respecting LIVE_ALERT_COOLDOWN_SECONDS so a sustained high-risk
        stream doesn't spam one alert row per frame.
        """
        level = risk_result["level"]
        if level not in config.ALERT_TRIGGER_LEVELS:
            self._last_alert_level = level
            return

        now = time.time()
        level_changed = level != self._last_alert_level
        cooldown_elapsed = (now - self._last_alert_time) >= config.LIVE_ALERT_COOLDOWN_SECONDS

        if (level_changed or cooldown_elapsed) and self.on_alert is not None:
            self.on_alert(level, f"{risk_result['label']} risk detected on live CCTV feed.")
            self._last_alert_level = level
            self._last_alert_time = now

    # -----------------------------------------------------------------
    # PUBLIC READ ACCESSORS (used by Flask routes)
    # -----------------------------------------------------------------
    def get_latest_jpeg(self):
        """Return the latest annotated frame as JPEG bytes, or None if not ready."""
        with self._lock:
            return self._latest_jpeg

    def get_stats(self) -> dict:
        """Return a JSON-serializable snapshot of the latest live stats."""
        with self._lock:
            stats = dict(self._latest_stats)
        stats["is_running"] = self.is_running
        return stats
