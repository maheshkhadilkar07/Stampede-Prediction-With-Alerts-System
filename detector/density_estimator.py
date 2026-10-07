"""
detector/density_estimator.py
==============================
Divides each frame into a grid and counts how many detected people fall
into each cell (using their bounding-box centroid). This gives a spatial
crowd-density map, not just a single global count, which is what lets us
tell "20 people spread across the whole frame" apart from
"20 people crammed into one corner" (the second is far more dangerous).

Save this file at: Stampede-Prediction-System/detector/density_estimator.py
"""

import numpy as np

import config
from utils.logger import get_logger

logger = get_logger(__name__)


class DensityEstimator:
    """
    Computes a grid-based density map from a list of Detection objects
    and exposes the overall (max-cell) density value used for risk
    analysis, plus the full grid for heatmap rendering.
    """

    def __init__(self, rows: int = None, cols: int = None):
        self.rows = rows or config.DENSITY_GRID_ROWS
        self.cols = cols or config.DENSITY_GRID_COLS

    def compute(self, detections: list, frame_width: int, frame_height: int) -> dict:
        """
        Build a (rows x cols) grid of person-counts based on each
        detection's centroid, then derive density metrics from it.

        Returns a dict with:
            grid: 2D numpy array of raw counts per cell
            max_cell_density: highest count found in any single cell
            avg_cell_density: mean count per occupied cell
            total_count: total number of detections
        """
        grid = np.zeros((self.rows, self.cols), dtype=np.int32)

        cell_w = max(frame_width / self.cols, 1)
        cell_h = max(frame_height / self.rows, 1)

        for det in detections:
            cx, cy = det.centroid
            col = min(int(cx / cell_w), self.cols - 1)
            row = min(int(cy / cell_h), self.rows - 1)
            grid[row, col] += 1

        total_count = len(detections)
        occupied_cells = np.count_nonzero(grid)
        max_cell_density = int(grid.max()) if grid.size else 0
        avg_cell_density = float(grid.sum() / occupied_cells) if occupied_cells else 0.0

        return {
            "grid": grid,
            "max_cell_density": max_cell_density,
            "avg_cell_density": round(avg_cell_density, 2),
            "total_count": total_count,
        }
