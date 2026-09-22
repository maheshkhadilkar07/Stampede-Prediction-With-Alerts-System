"""
config.py
==========
Central configuration for the Stampede Prediction System.
Every module reads its settings from here so there is a single
source of truth for paths, thresholds and constants.

Save this file at: Stampede-Prediction-System/config.py
"""

import os
from dotenv import load_dotenv

load_dotenv()  # reads the .env file in this same folder into os.environ

# ---------------------------------------------------------------------------
# BASE PATHS
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

VIDEO_UPLOAD_DIR = os.path.join(BASE_DIR, "videos", "uploads")
VIDEO_PROCESSED_DIR = os.path.join(BASE_DIR, "videos", "processed")
TRAINED_MODELS_DIR = os.path.join(BASE_DIR, "trained_models")
LOG_DIR = os.path.join(BASE_DIR, "logs")
REPORTS_DIR = os.path.join(BASE_DIR, "reports")
DATABASE_DIR = os.path.join(BASE_DIR, "database")

# Create the runtime folders automatically if they do not exist yet.
for _folder in (VIDEO_UPLOAD_DIR, VIDEO_PROCESSED_DIR, TRAINED_MODELS_DIR,
                 LOG_DIR, REPORTS_DIR, DATABASE_DIR):
    os.makedirs(_folder, exist_ok=True)

# ---------------------------------------------------------------------------
# FLASK SETTINGS
# ---------------------------------------------------------------------------
SECRET_KEY = os.environ.get("STAMPEDE_SECRET_KEY", "dev-secret-key-change-in-production")
MAX_CONTENT_LENGTH = 500 * 1024 * 1024  # 500 MB max upload size
ALLOWED_VIDEO_EXTENSIONS = {"mp4", "avi", "mov", "mkv", "webm"}

# ---------------------------------------------------------------------------
# YOLO / PERSON DETECTION SETTINGS
# ---------------------------------------------------------------------------
# Path where the YOLO weights file is expected to live. If it is not found,
# PersonDetector will fall back to asking ultralytics to auto-download the
# nano model (requires internet access on the machine actually running this,
# NOT inside a network-restricted sandbox).
YOLO_MODEL_PATH = os.path.join(TRAINED_MODELS_DIR, "yolo26n.pt")
YOLO_CONFIDENCE_THRESHOLD = 0.35
YOLO_PERSON_CLASS_ID = 0          # COCO class id 0 == "person"
YOLO_IMG_SIZE = 640

# ---------------------------------------------------------------------------
# CROWD DENSITY / RISK THRESHOLDS
# ---------------------------------------------------------------------------
# Density is measured in "people per grid cell" after the frame is divided
# into a DENSITY_GRID_ROWS x DENSITY_GRID_COLS grid.
DENSITY_GRID_ROWS = 6
DENSITY_GRID_COLS = 8

# Risk levels are decided from (a) people-per-cell density and
# (b) how fast the count is rising between frames (rate of change).
RISK_THRESHOLDS = {
    "SAFE":     {"density": 0.0, "label": "Safe",     "color": "#28a745"},
    "LOW":      {"density": 1.5, "label": "Low",       "color": "#ffc107"},
    "MEDIUM":   {"density": 3.0, "label": "Medium",    "color": "#fd7e14"},
    "HIGH":     {"density": 2.0, "label": "High",      "color": "#dc3545"},
    "CRITICAL": {"density": 2.5, "label": "Critical",  "color": "#7d0d1b"},
}

# If the person count grows by more than this fraction between two
# consecutive sampled frames, the risk analyzer escalates the risk level
# by one notch even if density alone would not justify it.
SURGE_GROWTH_RATE_THRESHOLD = 0.35

# How many frames to skip between heavy YOLO inference calls (helps FPS on
# CPU-only machines). 1 == run on every frame.
FRAME_SKIP = 2

# ---------------------------------------------------------------------------
# HEATMAP SETTINGS
# ---------------------------------------------------------------------------
HEATMAP_DECAY = 0.94          # how quickly old "heat" fades each frame
HEATMAP_BLUR_KERNEL = (15, 15)
HEATMAP_OPACITY = 0.45

# ---------------------------------------------------------------------------
# VIDEO OUTPUT SETTINGS
# ---------------------------------------------------------------------------
# NOTE: OpenCV's VideoWriter with 'mp4v' fourcc produces MP4 files that many
# browsers (Chrome/Firefox) refuse to play back because the codec used
# inside the container is not H.264. This is a KNOWN limitation of this
# module and is intentionally left for the upcoming FFmpeg module, which
# will re-encode this output into a browser-safe H.264 file.
OUTPUT_FOURCC = "mp4v"
OUTPUT_FPS_FALLBACK = 25

# ---------------------------------------------------------------------------
# LOGGING
# ---------------------------------------------------------------------------
LOG_FILE = os.path.join(LOG_DIR, "stampede_system.log")
LOG_LEVEL = "INFO"

# ---------------------------------------------------------------------------
# DATABASE (SQLite now, MySQL-ready later - see database/database.py)
# ---------------------------------------------------------------------------
SQLITE_DB_PATH = os.path.join(DATABASE_DIR, "stampede.db")
SQLALCHEMY_DATABASE_URI = os.environ.get(
    "STAMPEDE_DATABASE_URL", f"sqlite:///{SQLITE_DB_PATH}"
)
SQLALCHEMY_TRACK_MODIFICATIONS = False

# ---------------------------------------------------------------------------
# ALERT THRESHOLDS
# ---------------------------------------------------------------------------
# Risk levels at/above this index in RISK_ORDER trigger a persisted Alert row.
ALERT_TRIGGER_LEVELS = {"HIGH", "CRITICAL"}

# ---------------------------------------------------------------------------
# PDF REPORT SETTINGS
# ---------------------------------------------------------------------------
REPORT_FILENAME_PREFIX = "stampede_report_"

# ---------------------------------------------------------------------------
# LIVE CCTV / FLUVIO STREAMING SETTINGS (Mode 2)
# ---------------------------------------------------------------------------
# Whether to attempt a real Fluvio connection at all. If the `fluvio`
# package is not installed, or no cluster is reachable, the app
# automatically falls back to "direct" streaming mode (camera -> YOLO
# pipeline in-process, skipping the message broker) so the live-CCTV
# feature still works for local demos/testing on machines without
# Fluvio set up (e.g. plain Windows without WSL2).
FLUVIO_ENABLED = True
FLUVIO_TOPIC_NAME = "stampede-live-frames"
FLUVIO_CONNECT_TIMEOUT_SECONDS = 3

# Default webcam index used by cv2.VideoCapture when the user selects
# "Laptop Webcam" as the live source (0 = default/built-in camera).
DEFAULT_WEBCAM_INDEX = 0

# JPEG quality (0-100) used when encoding frames for both the Fluvio
# message payloads and the MJPEG dashboard preview stream.
STREAM_JPEG_QUALITY = 75

# How often (in seconds) the live pipeline is allowed to persist a new
# Alert row for the SAME risk level, to avoid flooding the database
# with one row per frame while a stream sits at HIGH/CRITICAL.
LIVE_ALERT_COOLDOWN_SECONDS = 15

# Resize incoming camera/RTSP frames to this width before running YOLO,
# for consistent CPU inference speed (same idea as recorded-video mode).
LIVE_INFERENCE_WIDTH = 640

# ---------------------------------------------------------------------------
# HIGHER-AUTHORITY ALERT NOTIFICATIONS (email + SMS to admin users)
# ---------------------------------------------------------------------------
ADMIN_ALERT_ENABLED = os.environ.get("ADMIN_ALERT_ENABLED", "true").lower() == "true"
ALERT_NOTIFY_LEVELS = {"HIGH", "CRITICAL"}

# Email (Gmail SMTP — free; use a 16-char Gmail "App Password", not your login password)
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", 587))
SMTP_USE_TLS = os.environ.get("SMTP_USE_TLS", "true").lower() == "true"
SMTP_USERNAME = os.environ.get("SMTP_USERNAME", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
SMTP_FROM_EMAIL = os.environ.get("SMTP_FROM_EMAIL", SMTP_USERNAME)
ALERT_EMAIL_FROM = SMTP_FROM_EMAIL
# SMS (Fast2SMS quick route — free bundled credits on signup, India numbers only)
FAST2SMS_API_KEY = os.environ.get("FAST2SMS_API_KEY", "")



# ---------------------------------------------------------------------------
# TWO-FACTOR AUTHENTICATION (EMAIL OTP AT LOGIN)
# ---------------------------------------------------------------------------
TWO_FACTOR_ENABLED = os.environ.get("TWO_FACTOR_ENABLED", "true").lower() == "true"
OTP_LENGTH = 6
OTP_EXPIRY_SECONDS = 300          # 5 minutes
OTP_MAX_ATTEMPTS = 5              # wrong guesses allowed before the code is invalidated
OTP_RESEND_COOLDOWN_SECONDS = 30  # minimum gap between "resend code" requests