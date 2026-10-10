# Evaluation Module — Real Accuracy/Precision/Recall/F1

This module produces **genuine** classification metrics by running the
actual production pipeline (`PersonDetector` → `DensityEstimator` →
`CrowdCounter` → `RiskAnalyzer` — the exact same code used in
`detector/video_processor.py` and `streaming/stream_manager.py`)
against video windows YOU have manually labeled as RISK or NO_RISK.

**It does not generate numbers on its own.** There is no shortcut here
— you need real footage and a real human judgment call on each clip.
That's the difference between this and a fabricated results table: the
numbers this produces are only as good as the labels you give it, and
they'll be real either way.

## Step 1 — Get test footage

Options, roughly in order of effort:
- Your own recorded test videos (phone footage of a market, event,
  hallway during rush hour, etc.)
- A public crowd dataset (e.g. search "MOT20 dataset download" —
  used by the RAMSITA-2026 paper you referenced — or any public crowd
  surveillance clips you can legally use)
- Simulated crowding — a group of people intentionally clustering
  together on camera for a test clip, vs. spread out normally

Aim for **at least 15-20 labeled windows total**, with a mix of both
classes, or the accuracy/F1 numbers won't be statistically meaningful
enough to defend in a paper review.

## Step 2 — Label the footage

Watch each clip yourself. For each interesting section, decide: would
a person watching this honestly call it "risky crowding," or not?
Record it in a CSV with this exact format (see `labels_template.csv`):

```csv
video_path,start_sec,end_sec,ground_truth_label,notes
videos/test_clips/market_normal.mp4,0,20,NO_RISK,typical afternoon footfall
videos/test_clips/festival_gate.mp4,45,60,RISK,visible crowd crush at entrance
```

- `ground_truth_label` must be exactly `RISK` or `NO_RISK`
- Save your real file as `evaluation/labels.csv` (not committed —
  this is genuinely your own data, specific to your test footage)

## Step 3 — Run it

```bash
python -m evaluation.evaluate_risk_model evaluation/labels.csv
```

First run will download YOLO26 weights if not already present (same
as the rest of the app — needs internet once).

## What you get

- `evaluation/results/predictions.csv` — every window's prediction in
  detail (predicted label, peak risk level reached, peak person count,
  frames analyzed) — keep this, it's your raw evidence if a reviewer
  asks to see the underlying data
- `evaluation/results/metrics_report.txt` — Accuracy, Precision,
  Recall, F1 per class, plus the full sklearn classification report
- `evaluation/results/confusion_matrix.png` — ready to drop straight
  into your paper's Results section

## Why this matches how the prediction actually triggers in production

A window is predicted `RISK` if the risk analyzer reaches HIGH or
CRITICAL on **any** frame inside it — the exact same
`config.ALERT_TRIGGER_LEVELS` threshold that fires a real authority
alert email in the live app. This isn't a separate "evaluation-only"
metric invented for the paper; it's measuring the real decision
boundary the deployed system uses.

## Honest limitations to state in your paper

- Sample size is whatever you actually label — be upfront about `n`
  in your results table, and don't overstate confidence from a small
  test set
- The risk analyzer is currently rule-based (density + growth rate),
  not a trained classifier — so this evaluation measures how well the
  *rule-based formula's threshold* aligns with human judgment, not a
  model's learned generalization. State this explicitly; it's a
  different (and valid, just different) kind of evaluation than a
  trained-classifier's train/test accuracy.
- If you want a trained-model comparison later (e.g. a CNN risk
  classifier), the same `labels.csv` format and `predictions.csv`
  output structure can be reused — you'd just swap the prediction
  source.
