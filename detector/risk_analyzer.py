"""
detector/risk_analyzer.py
==========================
Rule-based (non-ML) risk classifier. Combines:
  1. Max-cell crowd density (people crammed into one spot)
  2. Growth rate (how fast the crowd is building up)
into a single risk level: SAFE / LOW / MEDIUM / HIGH / CRITICAL.

This is intentionally rule-based for now — CNN-based prediction (trained
on labelled stampede-risk footage) is a separate, later module that will
refine or replace this analyzer's output.

Save this file at: Stampede-Prediction-System/detector/risk_analyzer.py
"""

import config
from utils.logger import get_logger

logger = get_logger(__name__)

# Ordered from safest to most dangerous - order matters for "escalate by one".
RISK_ORDER = ["SAFE", "LOW", "MEDIUM", "HIGH", "CRITICAL"]


class RiskAnalyzer:
    """
    Given the latest density metrics and crowd growth rate, decides the
    current risk level and returns a rich result dict ready for the
    dashboard / alert system to consume.
    """

    def __init__(self):
        self.thresholds = config.RISK_THRESHOLDS
        self.surge_threshold = config.SURGE_GROWTH_RATE_THRESHOLD

    def _density_to_level(self, max_cell_density: float) -> str:
        """Map a raw density number to the highest threshold it clears."""
        level = "SAFE"
        for key in RISK_ORDER:
            if max_cell_density >= self.thresholds[key]["density"]:
                level = key
        return level

    def _escalate(self, level: str) -> str:
        """Bump a risk level up by one notch (used for sudden surges)."""
        idx = RISK_ORDER.index(level)
        return RISK_ORDER[min(idx + 1, len(RISK_ORDER) - 1)]

    def analyze(self, density_result: dict, growth_rate: float) -> dict:
        """
        density_result: output of DensityEstimator.compute()
        growth_rate: output of CrowdCounter.growth_rate

        Returns:
            {
                "level": "MEDIUM",
                "label": "Medium",
                "color": "#fd7e14",
                "reason": "...",
                "escalated_by_surge": True/False
            }
        """
        base_level = self._density_to_level(density_result["max_cell_density"])
        escalated = False

        if growth_rate >= self.surge_threshold and base_level != "CRITICAL":
            escalated_level = self._escalate(base_level)
            if escalated_level != base_level:
                logger.info(
                    f"Risk escalated from {base_level} to {escalated_level} "
                    f"due to crowd surge (growth_rate={growth_rate:.2f})"
                )
            base_level = escalated_level
            escalated = True

        info = self.thresholds[base_level]
        reason_parts = [f"max cell density = {density_result['max_cell_density']} people"]
        if escalated:
            reason_parts.append(f"surge detected (+{growth_rate * 100:.0f}% growth)")

        return {
            "level": base_level,
            "label": info["label"],
            "color": info["color"],
            "reason": "; ".join(reason_parts),
            "escalated_by_surge": escalated,
        }
