"""
detector/crowd_counter.py
==========================
Keeps a rolling history of per-frame person counts so we can compute
statistics (average, peak, growth-rate) that feed the risk analyzer.

Save this file at: Stampede-Prediction-System/detector/crowd_counter.py
"""

from collections import deque

from utils.logger import get_logger

logger = get_logger(__name__)


class CrowdCounter:
    """
    Tracks person-count history for a video stream and derives simple
    statistics: current count, rolling average, peak count, and the
    frame-to-frame growth rate (used to detect sudden crowd surges).
    """

    def __init__(self, history_size: int = 30):
        self.history_size = history_size
        self.counts = deque(maxlen=history_size)
        self.peak_count = 0

    def update(self, count: int) -> None:
        """Record a new frame's person count."""
        self.counts.append(count)
        if count > self.peak_count:
            self.peak_count = count

    @property
    def current_count(self) -> int:
        return self.counts[-1] if self.counts else 0

    @property
    def average_count(self) -> float:
        if not self.counts:
            return 0.0
        return sum(self.counts) / len(self.counts)

    @property
    def growth_rate(self) -> float:
        """
        Returns the fractional change between the previous and current
        count, e.g. 0.5 means the crowd grew by 50% since the last frame.
        Returns 0.0 if there is not enough history or previous count is 0.
        """
        if len(self.counts) < 2:
            return 0.0
        previous, current = self.counts[-2], self.counts[-1]
        if previous == 0:
            return 0.0 if current == 0 else 1.0
        return (current - previous) / previous

    def summary(self) -> dict:
        """Return a dict snapshot of current counter stats (for dashboard/API)."""
        return {
            "current_count": self.current_count,
            "average_count": round(self.average_count, 2),
            "peak_count": self.peak_count,
            "growth_rate": round(self.growth_rate, 3),
        }
