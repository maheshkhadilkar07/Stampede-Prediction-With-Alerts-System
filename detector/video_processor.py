"""
detector/video_processor.py
=============================
The orchestrator of Mode 1 (Upload Video pipeline):

    Input Video -> YOLO -> Person Count -> Crowd Density ->
    Risk Analysis -> Heatmap Overlay -> Output Video + Stats

This module reads a video file frame by frame with OpenCV, runs every
detector sub-module on each sampled frame, draws overlays (bounding
boxes, stats panel, heatmap), writes the annotated result to disk, and
returns a summary dict the Flask layer can hand to the dashboard.

Save this file at: Stampede-Prediction-System/detector/video_processor.py
"""

import time

import cv2

import config
from detector.person_detector import PersonDetector
from detector.crowd_counter import CrowdCounter
from detector.density_estimator import DensityEstimator
from detector.risk_analyzer import RiskAnalyzer
from detector.heatmap import HeatmapGenerator
from utils.logger import get_logger

logger = get_logger(__name__)


class VideoProcessor:
    """
    High-level façade that wires PersonDetector, CrowdCounter,
    DensityEstimator, RiskAnalyzer and HeatmapGenerator together to
    process an entire video file end-to-end.
    """

    def __init__(self):
        # Instantiated once and reused across the whole video for efficiency
        # (the YOLO model in particular is expensive to load).
        self.detector = PersonDetector()
        self.counter = CrowdCounter()
        self.density_estimator = DensityEstimator()
        self.risk_analyzer = RiskAnalyzer()
        self.heatmap_generator = HeatmapGenerator()

    def _draw_overlays(self, frame, detections, density_result, risk_result, fps_display):
        """Draw bounding boxes + a stats panel directly onto the frame."""
        # Bounding boxes around every detected person.
        for det in detections:
            cv2.rectangle(frame, (det.x1, det.y1), (det.x2, det.y2), (0, 255, 0), 2)

        # Semi-transparent stats panel in the top-left corner.
        panel_h = 130
        overlay = frame.copy()
        cv2.rectangle(overlay, (0, 0), (340, panel_h), (20, 20, 20), -1)
        frame = cv2.addWeighted(overlay, 0.55, frame, 0.45, 0)

        text_lines = [
            f"People Count: {density_result['total_count']}",
            f"Max Cell Density: {density_result['max_cell_density']}",
            f"Risk Level: {risk_result['label']}",
            f"FPS: {fps_display:.1f}",
        ]
        for i, line in enumerate(text_lines):
            cv2.putText(
                frame, line, (12, 28 + i * 26),
                cv2.FONT_HERSHEY_SIMPLEX, 0.65, (255, 255, 255), 2, cv2.LINE_AA
            )

        # Risk level color chip.
        risk_color_bgr = self._hex_to_bgr(risk_result["color"])
        cv2.rectangle(frame, (300, 74), (330, 100), risk_color_bgr, -1)

        return frame

    @staticmethod
    def _hex_to_bgr(hex_color: str):
        """Convert a '#rrggbb' hex string into an OpenCV-friendly BGR tuple."""
        hex_color = hex_color.lstrip("#")
        r, g, b = (int(hex_color[i:i + 2], 16) for i in (0, 2, 4))
        return (b, g, r)

    def process(self, input_path: str, output_path: str, progress_callback=None) -> dict:
        """
        Process the full video at input_path and write the annotated
        result to output_path.

        progress_callback(percent: float) is optional and called
        periodically so a caller (e.g. Flask route) can report progress.

        Returns a summary dict with aggregate statistics for the whole
        video, including a per-second risk timeline for charting.
        """
        cap = cv2.VideoCapture(input_path)
        if not cap.isOpened():
            raise IOError(f"Could not open video file: {input_path}")

        frame_width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        frame_height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        source_fps = cap.get(cv2.CAP_PROP_FPS) or config.OUTPUT_FPS_FALLBACK
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

        fourcc = cv2.VideoWriter_fourcc(*config.OUTPUT_FOURCC)
        writer = cv2.VideoWriter(output_path, fourcc, source_fps, (frame_width, frame_height))

        risk_timeline = []          # [{"second": 1, "level": "LOW"}, ...]
        last_detections = []
        last_density_result = {"grid": None, "max_cell_density": 0, "avg_cell_density": 0, "total_count": 0}
        last_risk_result = {"level": "SAFE", "label": "Safe", "color": "#28a745", "reason": "", "escalated_by_surge": False}

        frame_index = 0
        processing_start = time.time()

        logger.info(f"Starting video processing: {input_path} ({total_frames} frames @ {source_fps:.1f} fps)")

        while True:
            ret, frame = cap.read()
            if not ret:
                break

            # Only run the expensive YOLO inference every FRAME_SKIP frames;
            # re-use the last known detections on skipped frames so the
            # overlay still draws something reasonable and stays smooth.
            if frame_index % config.FRAME_SKIP == 0:
                last_detections = self.detector.detect(frame)
                last_density_result = self.density_estimator.compute(
                    last_detections, frame_width, frame_height
                )
                self.counter.update(last_density_result["total_count"])
                last_risk_result = self.risk_analyzer.analyze(
                    last_density_result, self.counter.growth_rate
                )
                self.heatmap_generator.update(last_density_result["grid"])

                current_second = int(frame_index / source_fps)
                risk_timeline.append({
                    "second": current_second,
                    "level": last_risk_result["level"],
                    "count": last_density_result["total_count"],
                })

            # Apply heatmap overlay, then bounding boxes + stats panel.
            frame = self.heatmap_generator.render_overlay(frame)
            elapsed = max(time.time() - processing_start, 1e-6)
            fps_display = (frame_index + 1) / elapsed
            frame = self._draw_overlays(
                frame, last_detections, last_density_result, last_risk_result, fps_display
            )

            writer.write(frame)
            frame_index += 1

            if progress_callback and total_frames > 0:
                progress_callback(round((frame_index / total_frames) * 100, 1))

        cap.release()
        writer.release()

        processing_time = round(time.time() - processing_start, 2)
        logger.info(f"Finished processing {input_path} in {processing_time}s")

        return {
            "input_path": input_path,
            "output_path": output_path,
            "frame_width": frame_width,
            "frame_height": frame_height,
            "total_frames": frame_index,
            "source_fps": round(source_fps, 2),
            "processing_time_seconds": processing_time,
            "peak_count": self.counter.peak_count,
            "average_count": round(self.counter.average_count, 2),
            "final_risk": last_risk_result,
            "risk_timeline": risk_timeline,
            # NOTE: playback in-browser may fail due to the mp4v codec —
            # this is fixed in the upcoming FFmpeg conversion module.
            "browser_compatible_warning": (
                "Output uses OpenCV's default 'mp4v' codec, which some "
                "browsers cannot play natively. FFmpeg re-encoding module "
                "will resolve this in the next update."
            ),
        }
