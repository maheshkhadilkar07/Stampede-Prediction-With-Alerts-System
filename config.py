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

# Loads variables from a local .env file (SMTP password, secret key, etc.)
# into os.environ. Safe to call even if .env doesn't exist. See
# .env.example for the variables it expects.
load_dotenv(override=True)

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
DEBUG = os.environ.get("FLASK_DEBUG", "true").lower() == "true"
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
    "HIGH":     {"density": 5.0, "label": "High",      "color": "#dc3545"},
    "CRITICAL": {"density": 7.5, "label": "Critical",  "color": "#7d0d1b"},
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
# AUTHORITY EMAIL ALERTS
# ---------------------------------------------------------------------------
# When a HIGH or CRITICAL risk event is raised (Mode 1 or Mode 2), an email
# is sent to every user with role == "admin" ONLY — regular ("user") role
# accounts never receive these emails.
#
# All real credentials come from environment variables (see .env.example)
# so nothing sensitive is hardcoded in this file or committed to git.
ALERT_EMAIL_ENABLED = os.environ.get("ALERT_EMAIL_ENABLED", "false").lower() == "true"
SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USE_TLS = os.environ.get("SMTP_USE_TLS", "true").lower() == "true"
SMTP_USERNAME = os.environ.get("SMTP_USERNAME", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
ALERT_EMAIL_FROM = os.environ.get("ALERT_EMAIL_FROM", SMTP_USERNAME)

# Only re-email the SAME risk level for the SAME video/session after this
# many seconds, so a sustained CRITICAL stretch doesn't flood the admin's
# inbox with one email per detection cycle.
ALERT_EMAIL_COOLDOWN_SECONDS = 120

# ---------------------------------------------------------------------------
# TWO-FACTOR AUTHENTICATION (EMAIL OTP AT LOGIN)
# ---------------------------------------------------------------------------
# When enabled, after a correct username/password the user must also enter
# a one-time code emailed to their registered address before login_user()
# is called. Uses the SAME SMTP credentials configured above for alerts.
#
# DEV_MODE_LOG_OTP: if SMTP isn't configured (.env not filled in yet) AND
# Flask is running in debug mode, the OTP is written to the application
# log instead of emailed, so the login flow can still be tested locally
# without real SMTP credentials. This is intentionally logged loudly and
# is NEVER used when DEBUG is False, so it can't accidentally ship enabled
# in a real deployment.
TWO_FACTOR_ENABLED = os.environ.get("TWO_FACTOR_ENABLED", "true").lower() == "true"
OTP_LENGTH = 6
OTP_EXPIRY_SECONDS = 300          # 5 minutes
OTP_MAX_ATTEMPTS = 5              # wrong guesses allowed before the code is invalidated
OTP_RESEND_COOLDOWN_SECONDS = 30  # minimum gap between "resend code" requests

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
# WEBSITE / PROJECT INFORMATION (shown on About, Contact and the footer)
# ---------------------------------------------------------------------------
# Nothing here is hardcoded team data: set these in .env. Any value left
# blank is simply not displayed on the website.
PROJECT_NAME = os.environ.get("PROJECT_NAME", "StampedeGuard")
PROJECT_GITHUB_URL = os.environ.get("PROJECT_GITHUB_URL", "")
PROJECT_TEAM_NAME = os.environ.get("PROJECT_TEAM_NAME", "")
PROJECT_TEAM_MEMBERS = [m.strip() for m in os.environ.get("PROJECT_TEAM_MEMBERS", "").split(",") if m.strip()]
PROJECT_INSTITUTION = os.environ.get("PROJECT_INSTITUTION", "")
PROJECT_CONTACT_EMAIL = os.environ.get("PROJECT_CONTACT_EMAIL", "")

# ---------------------------------------------------------------------------
# DISPLAY / INCIDENT SETTINGS
# ---------------------------------------------------------------------------
# Timestamps are stored in UTC and converted to this timezone for display.
APP_TIMEZONE = os.environ.get("APP_TIMEZONE", "Asia/Kolkata")

# A location counts as an "active incident" on the map / admin dashboard if
# it is currently live at HIGH/CRITICAL, or raised a HIGH/CRITICAL alert
# within this many minutes.
INCIDENT_ACTIVE_MINUTES = int(os.environ.get("INCIDENT_ACTIVE_MINUTES", "60"))

# ---------------------------------------------------------------------------
# SERVER / DEPLOYMENT SETTINGS
# ---------------------------------------------------------------------------
# Bind to localhost by default: Flask's development server with debug on must
# never be exposed to a network. Production uses waitress/gunicorn (see README).
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "5000"))
SESSION_COOKIE_SECURE = os.environ.get("SESSION_COOKIE_SECURE", "false").lower() == "true"

# Serve the dev server over HTTPS with a throwaway self-signed certificate.
#
# Needed because browsers only expose the Geolocation API and service workers
# in a "secure context". http://localhost counts as secure, so the officer app
# works on the machine running Flask - but a phone pointed at
# http://192.168.1.x:5000 does not, and an officer can never go on duty.
#
# The browser will warn about the certificate; that is expected, accept it.
# Requires the `cryptography` package, which pywebpush already pulls in.
# This is for LAN demos only - a real deployment terminates TLS properly.
SSL_ADHOC = os.environ.get("SSL_ADHOC", "false").lower() == "true"

# ---------------------------------------------------------------------------
# BROWSER-PLAYABLE VIDEO OUTPUT (FFmpeg)
# ---------------------------------------------------------------------------
# OpenCV writes 'mp4v' which most browsers can't play. When FFmpeg is
# available (system install, or the bundled binary from imageio-ffmpeg) the
# processed video is re-encoded to H.264 + faststart. If FFmpeg is not found
# the original file is kept and the UI offers a download instead.
FFMPEG_ENABLED = os.environ.get("FFMPEG_ENABLED", "true").lower() == "true"
FFMPEG_BINARY = os.environ.get("FFMPEG_BINARY", "")  # optional explicit path

# ---------------------------------------------------------------------------
# OFFICER DISPATCH (Phase 3) - "nearest officer first", Uber-style
# ---------------------------------------------------------------------------
# When a HIGH/CRITICAL alert is raised at a location that has coordinates,
# the dispatch engine offers the incident to the single NEAREST on-duty
# officer and waits DISPATCH_OFFER_TIMEOUT_SECONDS for an answer. If they
# decline or let it lapse, the offer escalates to the next-nearest officer,
# and so on. Only one offer is ever live per incident, which is what makes
# a double-accept structurally impossible rather than merely unlikely.
DISPATCH_ENABLED = os.environ.get("DISPATCH_ENABLED", "true").lower() == "true"

# Only these risk levels page an officer. Deliberately the same set as
# ALERT_TRIGGER_LEVELS: if it was not worth an email, it is not worth
# sending a human across the city.
DISPATCH_TRIGGER_LEVELS = {"HIGH", "CRITICAL"}

# How long one officer gets to accept before the offer moves on. Long
# enough to pull a phone out of a pocket, short enough that a stampede
# is not waiting on someone who walked away from their phone.
DISPATCH_OFFER_TIMEOUT_SECONDS = int(os.environ.get("DISPATCH_OFFER_TIMEOUT_SECONDS", "25"))

# Give up after this many officers have been asked, and escalate to admins
# instead of silently cycling through the roster forever.
DISPATCH_MAX_OFFERS = int(os.environ.get("DISPATCH_MAX_OFFERS", "5"))

# Officers further away than this are never offered the incident: someone
# 40 km out is not a responder, they are a false sense of coverage.
DISPATCH_SEARCH_RADIUS_KM = float(os.environ.get("DISPATCH_SEARCH_RADIUS_KM", "15.0"))

# A sustained CRITICAL crowd raises many alerts but is ONE incident. A new
# alert within this window, at effectively the same spot, is folded into the
# existing dispatch instead of paging a second officer. This also covers a
# dispatch that went UNASSIGNED: "nobody was available" describes the roster
# at that moment, not the end of the crowd problem, so the next alert
# re-searches that same record rather than opening a duplicate.
DISPATCH_DEDUPE_WINDOW_SECONDS = int(os.environ.get("DISPATCH_DEDUPE_WINDOW_SECONDS", "600"))

# How often the background monitor checks for lapsed offers to escalate.
DISPATCH_MONITOR_TICK_SECONDS = 2

# NOTE: whether an incident is finished lives on the model, as
# Dispatch.is_closed / is_open / is_claimed (database/models.py). It is not
# duplicated here on purpose - dispatch dedupe needs a DIFFERENT and narrower
# rule (only RESOLVED and CANCELLED block reuse), and two near-identical sets
# in two files is how those two rules would silently drift into one.

# An officer's phone reports its position on this interval while on duty.
OFFICER_LOCATION_PING_SECONDS = int(os.environ.get("OFFICER_LOCATION_PING_SECONDS", "20"))

# A position older than this is treated as unknown, so an officer who went
# on duty and then lost signal is not dispatched based on a stale fix.
OFFICER_LOCATION_MAX_AGE_SECONDS = int(os.environ.get("OFFICER_LOCATION_MAX_AGE_SECONDS", "300"))

# Average city speed used only for the "about N min out" estimate shown on
# the officer's phone. This is an estimate from straight-line distance, NOT
# a routed ETA - no traffic data, no road network. Kept deliberately low so
# the number is more often pessimistic than optimistic.
DISPATCH_AVERAGE_SPEED_KMH = float(os.environ.get("DISPATCH_AVERAGE_SPEED_KMH", "25.0"))

# ---------------------------------------------------------------------------
# WEB PUSH (VAPID) - wakes an officer's phone with the app closed
# ---------------------------------------------------------------------------
# Generate a key pair once with:  python scripts/generate_vapid_keys.py
# then paste both values into .env. With these blank, push is simply
# disabled and the officer page falls back to polling while open - the
# feature degrades, it does not break.
VAPID_PUBLIC_KEY = os.environ.get("VAPID_PUBLIC_KEY", "")
VAPID_PRIVATE_KEY = os.environ.get("VAPID_PRIVATE_KEY", "")
VAPID_SUBJECT = os.environ.get("VAPID_SUBJECT", "mailto:admin@stampedeguard.local")

# Push messages are worthless after the offer window closes, so they are
# given a TTL just past it rather than being queued for hours.
PUSH_TTL_SECONDS = 60

# ---------------------------------------------------------------------------
# 3D DISPATCH SIMULATION
# ---------------------------------------------------------------------------
# An admin-only 3D view of the dispatch system: registered camera locations,
# on-duty officers and live incidents, rendered on a map plane.
#
# It drives the REAL engine - real nearest-officer ranking, real push
# notifications, a real officer accepting on a real phone - so it leaves real
# rows behind. Those rows carry Dispatch.is_simulated = True and can be
# purged from the simulation page.
SIMULATION_ENABLED = os.environ.get("SIMULATION_ENABLED", "true").lower() == "true"

# Demo officers the simulation can place on the map so there is somebody to
# dispatch to without needing real phones on real people. They are created
# as genuine officer accounts (the engine must see them as it sees anyone)
# but with an unusable random password and an address at the RFC 2606
# reserved .invalid TLD, so they can never be logged into and can never
# receive mail.
SIMULATION_OFFICER_PREFIX = "sim-officer-"
SIMULATION_EMAIL_DOMAIN = "simulation.invalid"

# A ceiling, so a stuck loop in the browser cannot fill the users table.
SIMULATION_MAX_OFFICERS = int(os.environ.get("SIMULATION_MAX_OFFICERS", "12"))

# Demo camera locations the simulation can seed, for a database with none
# registered yet - otherwise the map opens empty with nothing to dispatch to.
#
# These are ordinary CameraLocation rows, not fakes: registering monitored
# sites is a normal admin action, and the seeded set is a starting venue
# layout to keep, rename or delete from Admin > Locations. The prefix is what
# lets the purge find them again.
SIMULATION_LOCATION_PREFIX = "[Demo] "

# Where a seeded venue is centred when the admin does not say. Editable in
# the page, and there is a "use my location" button next to it - this is only
# the value the box starts with.
SIMULATION_DEFAULT_LAT = float(os.environ.get("SIMULATION_DEFAULT_LAT", "19.0760"))
SIMULATION_DEFAULT_LON = float(os.environ.get("SIMULATION_DEFAULT_LON", "72.8777"))
