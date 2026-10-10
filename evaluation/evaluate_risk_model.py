"""
evaluation/evaluate_risk_model.py
====================================
Runs the ACTUAL production pipeline (PersonDetector -> DensityEstimator
-> CrowdCounter -> RiskAnalyzer, same modules used by
detector/video_processor.py and streaming/stream_manager.py) against a
set of manually labeled ground-truth video windows, and computes real
Accuracy / Precision / Recall / F1 / a confusion matrix from the
comparison — the same evaluation format used in published crowd-risk
papers (e.g. their Table 4-style classification report).

THIS SCRIPT PRODUCES NO NUMBERS ON ITS OWN. It only reports what the
pipeline actually predicts versus what a human labeled as ground truth
in labels.csv. If you have not labeled any clips yet, there is nothing
to run — see the "HOW TO CREATE GROUND-TRUTH LABELS" section below.

--------------------------------------------------------------------
HOW TO CREATE GROUND-TRUTH LABELS
--------------------------------------------------------------------
1. Collect a handful of short video clips (your own footage, or a
   public crowd dataset). Aim for at least ~15-20 labeled windows
   total, covering both risky and non-risky examples, for the
   accuracy numbers to mean anything statistically.
2. For each clip, watch it yourself (or as a team) and decide: does
   this window genuinely show dangerous crowd behavior (a person
   would call this "risky" if they saw it), or not?
3. Fill in a CSV with columns:
       video_path,start_sec,end_sec,ground_truth_label,notes
   ground_truth_label must be exactly RISK or NO_RISK.
   See evaluation/labels_template.csv for the exact format.
4. Save your real labels as evaluation/labels.csv (this file is NOT
   provided — you must create it from real footage; it is
   intentionally not auto-generated, because that would defeat the
   entire point of an honest evaluation).

--------------------------------------------------------------------
HOW THE COMPARISON WORKS
--------------------------------------------------------------------
For every labeled window, the pipeline runs frame-by-frame exactly as
it does in production. The window is predicted RISK if the risk
analyzer reaches HIGH or CRITICAL on ANY processed frame inside that
window (config.ALERT_TRIGGER_LEVELS), matching the same threshold
logic that actually fires an authority alert in the live app — so this
evaluation is measuring the real trigger condition, not a proxy for it.

--------------------------------------------------------------------
RUN
--------------------------------------------------------------------
    python -m evaluation.evaluate_risk_model evaluation/labels.csv

Outputs:
    evaluation/results/predictions.csv   (raw per-window prediction detail)
    evaluation/results/metrics_report.txt (classification report, printed too)
    evaluation/results/confusion_matrix.png

Save this file at: Stampede-Prediction-System/evaluation/evaluate_risk_model.py
"""

import csv
import os
import sys

import cv2
import matplotlib
matplotlib.use("Agg")  # headless-safe backend, no display required
import matplotlib.pyplot as plt
from sklearn.metrics import (
    accuracy_score, precision_recall_fscore_support,
    classification_report, confusion_matrix,
)

import config
from detector.person_detector import PersonDetector
from detector.crowd_counter import CrowdCounter
from detector.density_estimator import DensityEstimator
from detector.risk_analyzer import RiskAnalyzer
from utils.logger import get_logger

logger = get_logger(__name__)

RESULTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")
os.makedirs(RESULTS_DIR, exist_ok=True)


def load_labels(csv_path: str) -> list:
    """Read the ground-truth CSV into a list of row dicts, validating as we go."""
    if not os.path.exists(csv_path):
        raise FileNotFoundError(
            f"Labels file not found: {csv_path}\n"
            "Create it from evaluation/labels_template.csv with your own "
            "manually reviewed footage — see the module docstring for the format."
        )

    rows = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for i, row in enumerate(reader, start=2):  # start=2: header is line 1
            label = row["ground_truth_label"].strip().upper()
            if label not in ("RISK", "NO_RISK"):
                raise ValueError(
                    f"labels.csv line {i}: ground_truth_label must be RISK or "
                    f"NO_RISK, got '{row['ground_truth_label']}'"
                )
            rows.append({
                "video_path": row["video_path"].strip(),
                "start_sec": float(row["start_sec"]),
                "end_sec": float(row["end_sec"]),
                "ground_truth_label": label,
                "notes": row.get("notes", ""),
            })
    return rows


def evaluate_window(video_path: str, start_sec: float, end_sec: float,
                     detector: PersonDetector) -> dict:
    """
    Run the real production pipeline over one labeled window and return
    the predicted label plus supporting detail (peak risk level reached,
    peak person count, number of frames actually analyzed).
    """
    if not os.path.exists(video_path):
        raise FileNotFoundError(f"Video not found: {video_path}")

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise IOError(f"Could not open video: {video_path}")

    fps = cap.get(cv2.CAP_PROP_FPS) or config.OUTPUT_FPS_FALLBACK
    frame_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    frame_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    start_frame = int(start_sec * fps)
    end_frame = int(end_sec * fps)
    cap.set(cv2.CAP_PROP_POS_FRAMES, start_frame)

    density_estimator = DensityEstimator()
    counter = CrowdCounter()
    risk_analyzer = RiskAnalyzer()

    risk_rank = {"SAFE": 0, "LOW": 1, "MEDIUM": 2, "HIGH": 3, "CRITICAL": 4}
    peak_level = "SAFE"
    peak_count = 0
    frames_analyzed = 0
    frame_index = start_frame
    hit_trigger = False

    while frame_index < end_frame:
        ret, frame = cap.read()
        if not ret:
            break

        # Same frame-skip logic as production (config.FRAME_SKIP), for a
        # fair, apples-to-apples comparison with real deployed behavior.
        if (frame_index - start_frame) % config.FRAME_SKIP == 0:
            detections = detector.detect(frame)
            density_result = density_estimator.compute(detections, frame_w, frame_h)
            counter.update(density_result["total_count"])
            risk_result = risk_analyzer.analyze(density_result, counter.growth_rate)

            frames_analyzed += 1
            peak_count = max(peak_count, density_result["total_count"])
            if risk_rank[risk_result["level"]] > risk_rank[peak_level]:
                peak_level = risk_result["level"]
            if risk_result["level"] in config.ALERT_TRIGGER_LEVELS:
                hit_trigger = True

        frame_index += 1

    cap.release()

    return {
        "predicted_label": "RISK" if hit_trigger else "NO_RISK",
        "peak_risk_level": peak_level,
        "peak_person_count": peak_count,
        "frames_analyzed": frames_analyzed,
    }


def run_evaluation(labels_csv_path: str) -> None:
    labels = load_labels(labels_csv_path)
    logger.info(f"Loaded {len(labels)} labeled windows from {labels_csv_path}")

    # Load YOLO once and reuse across every window (expensive to reload).
    detector = PersonDetector()

    y_true, y_pred = [], []
    detail_rows = []

    for i, row in enumerate(labels, start=1):
        logger.info(
            f"[{i}/{len(labels)}] Evaluating {row['video_path']} "
            f"[{row['start_sec']}s-{row['end_sec']}s] (ground truth: {row['ground_truth_label']})"
        )
        try:
            result = evaluate_window(
                row["video_path"], row["start_sec"], row["end_sec"], detector
            )
        except (FileNotFoundError, IOError) as exc:
            logger.error(f"Skipping row {i}: {exc}")
            continue

        y_true.append(row["ground_truth_label"])
        y_pred.append(result["predicted_label"])
        detail_rows.append({**row, **result})

    if not y_true:
        logger.error("No windows were successfully evaluated. Check your video paths.")
        return

    _write_predictions_csv(detail_rows)
    _write_metrics_report(y_true, y_pred)
    _write_confusion_matrix_plot(y_true, y_pred)


def _write_predictions_csv(detail_rows: list) -> None:
    path = os.path.join(RESULTS_DIR, "predictions.csv")
    fieldnames = [
        "video_path", "start_sec", "end_sec", "ground_truth_label",
        "predicted_label", "peak_risk_level", "peak_person_count",
        "frames_analyzed", "notes",
    ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in detail_rows:
            writer.writerow({k: row.get(k, "") for k in fieldnames})
    logger.info(f"Wrote per-window predictions to {path}")


def _write_metrics_report(y_true: list, y_pred: list) -> None:
    labels_order = ["NO_RISK", "RISK"]

    acc = accuracy_score(y_true, y_pred)
    precision, recall, f1, support = precision_recall_fscore_support(
        y_true, y_pred, labels=labels_order, zero_division=0
    )
    report_text = classification_report(
        y_true, y_pred, labels=labels_order, zero_division=0
    )

    lines = []
    lines.append("=" * 60)
    lines.append("STAMPEDE RISK MODEL — REAL EVALUATION RESULTS")
    lines.append("(Pipeline: PersonDetector -> DensityEstimator -> RiskAnalyzer)")
    lines.append("=" * 60)
    lines.append(f"Total labeled windows evaluated: {len(y_true)}")
    lines.append(f"Overall Accuracy: {acc:.3f}")
    lines.append("")
    lines.append(f"{'Class':<10}{'Precision':<12}{'Recall':<12}{'F1-score':<12}{'Support':<10}")
    for label, p, r, f, s in zip(labels_order, precision, recall, f1, support):
        lines.append(f"{label:<10}{p:<12.3f}{r:<12.3f}{f:<12.3f}{s:<10}")
    lines.append("")
    lines.append("Full sklearn classification_report:")
    lines.append(report_text)

    report = "\n".join(lines)
    print(report)

    path = os.path.join(RESULTS_DIR, "metrics_report.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(report)
    logger.info(f"Wrote metrics report to {path}")


def _write_confusion_matrix_plot(y_true: list, y_pred: list) -> None:
    labels_order = ["NO_RISK", "RISK"]
    cm = confusion_matrix(y_true, y_pred, labels=labels_order)

    fig, ax = plt.subplots(figsize=(5, 4))
    im = ax.imshow(cm, cmap="Blues")
    ax.set_xticks(range(len(labels_order)))
    ax.set_yticks(range(len(labels_order)))
    ax.set_xticklabels(labels_order)
    ax.set_yticklabels(labels_order)
    ax.set_xlabel("Predicted label")
    ax.set_ylabel("True label")
    ax.set_title("Confusion Matrix — Stampede Risk Model")

    for i in range(cm.shape[0]):
        for j in range(cm.shape[1]):
            ax.text(j, i, str(cm[i, j]), ha="center", va="center",
                     color="white" if cm[i, j] > cm.max() / 2 else "black")

    fig.colorbar(im, ax=ax)
    fig.tight_layout()

    path = os.path.join(RESULTS_DIR, "confusion_matrix.png")
    fig.savefig(path, dpi=150)
    plt.close(fig)
    logger.info(f"Wrote confusion matrix plot to {path}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python -m evaluation.evaluate_risk_model <path_to_labels.csv>")
        sys.exit(1)
    run_evaluation(sys.argv[1])
