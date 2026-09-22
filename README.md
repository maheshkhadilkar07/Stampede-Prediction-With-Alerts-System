# AI-Based Real-Time Stampede Prediction and Alert System

Final year major project — Mode 1 (Upload Video) pipeline is implemented end
to end: **Upload → YOLO Person Detection → Crowd Density → Rule-Based Risk
Analyzer → Heatmap → Dashboard**, with authentication, SQLite persistence,
alert history, admin panel, and PDF report generation.

## 1. Setup

```bash
cd Stampede-Prediction-System
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

First run will auto-download the YOLO26 nano weights (`yolo26n.pt`) into
`trained_models/` the first time `PersonDetector` is instantiated — this
requires internet access on the machine actually running the app.
YOLO26 (Ultralytics, Jan 2026) is the current recommended model —
NMS-free end-to-end inference and faster CPU inference than YOLOv8/YOLO11,
which matters on a CPU-only dev machine.

## 2. Initialize the database

The database is created automatically on first run via `init_db()` in
`database/database.py`. No manual migration step is required for SQLite.

## 3. Run

```bash
python app.py
```

Visit `http://localhost:5000`, register an account, log in, and upload a
video from the Upload page. Processing runs in a background thread; the
dashboard polls the JSON status endpoint until the video finishes.

## 4. Live CCTV (Mode 2) — Fluvio streaming

The live-CCTV pipeline (`streaming/`) is: **Camera → Fluvio Producer →
Fluvio Topic → Fluvio Consumer → YOLO → Density → Risk → Alerts →
Dashboard**, exposed at `/live` with an MJPEG preview and live-polling
stat cards.

**`fluvio` (the Python client) is a compiled Rust extension and is kept
OUT of `requirements.txt` on purpose** — installing it on plain Windows
fails with `error: can't find Rust compiler` because there's no
prebuilt Windows wheel. Because of that:

- **On native Windows without WSL2**: just run
  `pip install -r requirements.txt` as normal — Fluvio is not attempted,
  so nothing fails. The app detects that the `fluvio` package isn't
  installed and **automatically falls back to "direct mode"**: camera
  frames go straight into the YOLO pipeline in-process, skipping the
  message broker. Live CCTV still works end-to-end for demos; you just
  won't be exercising the actual Fluvio hop until Fluvio is set up.
- **To use real Fluvio streaming**, run the project inside WSL2:
  1. Install WSL2 + Ubuntu (`wsl --install` in an admin PowerShell,
     then reboot).
  2. Inside the Ubuntu shell:
     ```bash
     curl -fsS https://raw.githubusercontent.com/fluvio-community/fluvio/master/install.sh | bash
     fluvio cluster start
     fluvio topic create stampede-live-frames
     ```
  3. Clone/copy the project into the WSL2 filesystem (or open it via
     `\\wsl$\Ubuntu\...` / the VS Code "Remote - WSL" extension), create
     a venv there, run:
     ```bash
     pip install -r requirements.txt
     pip install -r requirements-fluvio.txt
     ```
     (this time `fluvio` installs correctly since a Linux wheel exists),
     then run `python app.py` from inside WSL2.
  4. Visit `/live`, pick "Laptop Webcam" (or an RTSP URL), and start the
     session — the mode badge on the page will read **FLUVIO STREAMING**
     instead of **DIRECT MODE** once the cluster is actually being used.

Either way, `/live/start` accepts:
- **Laptop Webcam** — opens local device index `0`
- **RTSP CCTV Stream** / **IP Camera** — provide a stream URL, e.g.
  `rtsp://user:pass@192.168.1.10:554/stream1`

## 5. Known limitation (next module)

OpenCV's `VideoWriter` with the `mp4v` fourcc produces an MP4 container that
Chrome/Firefox often refuse to play back directly, because the codec stream
inside isn't H.264. This is **intentionally left for the FFmpeg module**,
which will re-encode `videos/processed/*.mp4` into a browser-safe H.264 file
with `+faststart`. Until then, download the processed file and play it in
VLC, or view it via the heatmap/analytics charts on the dashboard.

## 6. Project status

**Completed**
- Folder structure, Flask app factory, routing
- User auth (register/login/logout) via Flask-Login
- SQLite persistence (Users, Videos, Alerts) via SQLAlchemy
- Video upload with extension/size validation
- YOLO26 person detection (`detector/person_detector.py`)
- Person counter + crowd density estimator (grid-based)
- Rule-based risk analyzer (density + growth-rate scoring)
- Heatmap prototype (decaying accumulation buffer + JET colormap overlay)
- Background (threaded) video processing pipeline
- Dashboard, alerts, video analytics, admin panel templates (Bootstrap 5)
- PDF report generation (ReportLab)
- **Live CCTV / RTSP / webcam streaming (Mode 2)**: `streaming/camera_reader.py`,
  `streaming/producer.py`, `streaming/consumer.py`, `streaming/stream_manager.py`
- **Fluvio producer/consumer integration**, with automatic fallback to
  direct in-process streaming when no Fluvio cluster is reachable
- Live MJPEG preview (`/live/feed`) + live-polling stat cards + live alerts

**Pending (next modules)**
- FFmpeg re-encode for browser-compatible playback of Mode 1 recordings
- CNN-based risk prediction (to cross-check the rule-based analyzer)
- Chart.js analytics wiring for the live dashboard (currently recorded-video only)
- Dark mode toggle
- Final responsive UI pass

## 7. Folder structure

```
Stampede-Prediction-System/
├── app.py                  # Flask app, routes, auth wiring
├── config.py                # Central settings (paths, thresholds, DB URI)
├── requirements.txt
├── database/
│   ├── database.py          # SQLAlchemy init
│   ├── models.py             # User, Video, Alert models
│   └── history.py            # Alert/video history queries
├── detector/
│   ├── person_detector.py    # YOLO26 wrapper
│   ├── crowd_counter.py
│   ├── density_estimator.py
│   ├── risk_analyzer.py      # Rule-based scoring
│   ├── heatmap.py
│   └── video_processor.py    # Orchestrates the full per-frame pipeline
├── cnn/                      # (pending) train.py / predict.py / dataset_loader.py
├── streaming/
│   ├── camera_reader.py       # Threaded webcam/RTSP/IP camera reader
│   ├── producer.py            # Fluvio frame producer (+ FluvioUnavailableError)
│   ├── consumer.py            # Fluvio frame consumer
│   └── stream_manager.py      # Orchestrates Mode 2 end-to-end, incl. fallback
├── utils/
│   ├── logger.py
│   ├── helpers.py
│   ├── forms.py               # WTForms (login/register/upload)
│   └── report_generator.py    # PDF report builder
├── templates/                 # Bootstrap 5 UI
├── static/
├── videos/{uploads,processed}
├── trained_models/            # YOLO weights land here
└── reports/                   # Generated PDF reports land here
```
