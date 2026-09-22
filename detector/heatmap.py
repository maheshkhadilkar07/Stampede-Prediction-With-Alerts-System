"""
detector/heatmap.py
====================
Accumulates a "heat" map over time from per-frame density grids, then
renders it as a color overlay (blue -> green -> yellow -> red) blended on
top of the original video frame using OpenCV.

Save this file at: Stampede-Prediction-System/detector/heatmap.py
"""

import cv2
import numpy as np

import config
from utils.logger import get_logger

logger = get_logger(__name__)


class HeatmapGenerator:
    """
    Maintains a persistent heat accumulator the same shape as the density
    grid. Each frame, new density is added and old heat decays, giving a
    smooth "trail" effect rather than a flickering per-frame overlay.
    """

    def __init__(self, rows: int = None, cols: int = None):
        self.rows = rows or config.DENSITY_GRID_ROWS
        self.cols = cols or config.DENSITY_GRID_COLS
        self.decay = config.HEATMAP_DECAY
        self.heat_accumulator = np.zeros((self.rows, self.cols), dtype=np.float32)

    def update(self, density_grid: np.ndarray) -> None:
        """Decay existing heat, then add the newest density grid on top."""
        self.heat_accumulator *= self.decay
        self.heat_accumulator += density_grid.astype(np.float32)

    def render_overlay(self, frame: np.ndarray) -> np.ndarray:
        """
        Resize the low-res heat grid up to full frame size, apply a
        Gaussian blur for smoothness, colorize it with a JET colormap,
        and alpha-blend it on top of the original frame.
        """
        frame_h, frame_w = frame.shape[:2]

        # Normalise heat values to 0-255 for colormap application.
        max_val = self.heat_accumulator.max()
        if max_val > 0:
            normalized = (self.heat_accumulator / max_val * 255).astype(np.uint8)
        else:
            normalized = self.heat_accumulator.astype(np.uint8)

        # Upscale the small grid to full frame resolution.
        heat_resized = cv2.resize(
            normalized, (frame_w, frame_h), interpolation=cv2.INTER_CUBIC
        )
        heat_blurred = cv2.GaussianBlur(heat_resized, config.HEATMAP_BLUR_KERNEL, 0)

        # Apply a color map (blue = cold/empty, red = hot/crowded).
        heat_colored = cv2.applyColorMap(heat_blurred, cv2.COLORMAP_JET)

        # Blend the heat color map on top of the original frame.
        overlay = cv2.addWeighted(
            frame, 1 - config.HEATMAP_OPACITY, heat_colored, config.HEATMAP_OPACITY, 0
        )
        return overlay

    def reset(self) -> None:
        """Clear accumulated heat (e.g. when starting a new video)."""
        self.heat_accumulator = np.zeros((self.rows, self.cols), dtype=np.float32)
