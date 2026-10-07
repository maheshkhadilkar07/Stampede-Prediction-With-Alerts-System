"""
app.py
=======
Main Flask application for the AI-Based Real-Time Stampede Prediction
and Alert System.

Wires together:
    - Flask-Login authentication (login / register / logout)
    - SQLite persistence via database/database.py + models.py
    - Mode 1 (Upload Video): detector.video_processor.VideoProcessor
      (YOLO -> density -> risk -> heatmap), run in a background thread
    - Mode 2 (Live CCTV): streaming.stream_manager.LiveStreamManager
      (Camera -> Fluvio (or direct fallback) -> YOLO -> density -> risk
      -> heatmap -> MJPEG preview + JSON polling + live alerts)
    - PDF report generation
    - JSON status-polling APIs used by static/js/dashboard.js

Save this file at: Stampede-Prediction-System/app.py

Run with:
    python app.py
"""

import json
import os
import threading
import time
from datetime import datetime, timedelta

from flask import (
    Flask, render_template, redirect, url_for, flash, request,
    jsonify, send_from_directory, abort, Response, session,
)
from flask_login import (
    LoginManager, login_user, logout_user, login_required, current_user,
)
from flask_wtf.csrf import CSRFProtect
from sqlalchemy import text

import config
from database.database import db, init_db
from database.models import (
    User, Video, Alert, Authority, AlertDelivery, ContactMessage, CameraLocation,
)
from database import history
from utils.forms import (
    LoginForm, RegisterForm, UploadVideoForm, OtpForm, AuthorityForm,
    ContactForm, LocationForm,
)
from utils.helpers import (
    allowed_video_file, generate_unique_filename,
    safe_join_upload_path, safe_join_processed_path,
)
from utils.logger import get_logger
from utils.report_generator import generate_video_report_pdf
from utils.email_alert import send_alert_to_recipients, retry_failed_deliveries
from utils.video_converter import convert_to_browser_mp4
from utils.timeutils import fmt_local, iso_utc, TZ_LABEL
from utils.otp_service import (
    generate_and_send_login_otp, verify_login_otp, seconds_until_resend_allowed,
    generate_and_send_otp, verify_otp_code,
)
from detector.video_processor import VideoProcessor
from streaming.stream_manager import LiveStreamManager
from dispatch.routes import register_dispatch_blueprints
from dispatch.engine import dispatch_engine

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# APP FACTORY
# ---------------------------------------------------------------------------
app = Flask(__name__)
app.config["SECRET_KEY"] = config.SECRET_KEY
app.config["MAX_CONTENT_LENGTH"] = config.MAX_CONTENT_LENGTH
app.config["SQLALCHEMY_DATABASE_URI"] = config.SQLALCHEMY_DATABASE_URI
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = config.SQLALCHEMY_TRACK_MODIFICATIONS

app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = config.SESSION_COOKIE_SECURE

# Refuse to start in production mode with the placeholder secret key.
if not config.DEBUG and config.SECRET_KEY == "dev-secret-key-change-in-production":
    raise RuntimeError(
        "FLASK_DEBUG is false but STAMPEDE_SECRET_KEY is still the default. "
        "Set a long random STAMPEDE_SECRET_KEY in .env before deploying."
    )

init_db(app)
csrf = CSRFProtect(app)  # every POST form must carry the csrf_token

# Officer dispatch: blueprints for the officer PWA, its JSON API and the
# admin dispatch board. The engine needs no request context - it is driven
# by the alert hooks below and by its own background expiry monitor.
register_dispatch_blueprints(app)
dispatch_engine.init_app(app)

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"
login_manager.login_message = "Please log in to access the dashboard."
login_manager.login_message_category = "info"


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


@app.template_filter("localtime")
def _localtime_filter(dt, fmt="%d %b %Y, %H:%M"):
    """Render a stored UTC datetime in config.APP_TIMEZONE."""
    return fmt_local(dt, fmt)


def _landing_url(user=None):
    """
    Where a signed-in account belongs after login.

    A field officer's job happens entirely on their phone, so sending them to
    the operator dashboard would leave them with a view they cannot act on.
    Admins keep the dashboard - they need it - and reach the dispatch board
    from the sidebar.
    """
    user = user or current_user
    if getattr(user, "is_officer", False):
        return url_for("officer.home")
    return url_for("dashboard")


@app.context_processor
def _inject_site_info():
    """Project info (from .env) available to every template."""
    return {
        "site": {
            "name": config.PROJECT_NAME,
            "github": config.PROJECT_GITHUB_URL,
            "team_name": config.PROJECT_TEAM_NAME,
            "team_members": config.PROJECT_TEAM_MEMBERS,
            "institution": config.PROJECT_INSTITUTION,
            "contact_email": config.PROJECT_CONTACT_EMAIL,
            "year": datetime.utcnow().year,
            "timezone": TZ_LABEL,
        }
    }


@app.after_request
def _security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    # geolocation must stay allowed for our own origin: the field-officer PWA
    # needs the device's position to be dispatchable and to work out which
    # officer is nearest. camera and microphone remain fully blocked - nothing
    # in the app asks for either, so allowing them would only widen the
    # surface.
    response.headers.setdefault(
        "Permissions-Policy", "camera=(), microphone=(), geolocation=(self)"
    )
    return response


@app.route("/healthz")
def healthz():
    """Liveness/readiness probe for load balancers and container platforms."""
    try:
        db.session.execute(text("SELECT 1"))
        return jsonify({"status": "ok", "database": "ok"})
    except Exception as exc:  # noqa: BLE001
        logger.error(f"Health check failed: {exc}")
        return jsonify({"status": "error", "database": "unavailable"}), 503


# A single VideoProcessor instance is expensive to build (loads YOLO once)
# but VideoProcessor.process() is NOT thread-safe to call concurrently on
# the same instance, so we build a fresh one per background job instead.
# This keeps memory bounded on typical final-year-project hardware while
# still avoiding the "shared mutable state across threads" bug.
_processing_lock = threading.Lock()

# ---------------------------------------------------------------------------
# LIVE CCTV (Mode 2) MANAGER
# ---------------------------------------------------------------------------
# Single process-wide manager: only one live session runs at a time, which
# is the right constraint for a final-year-project deployment (one camera
# feed, one YOLO model loaded in memory). See streaming/stream_manager.py.
live_manager = LiveStreamManager()


def _live_alert_callback(level: str, message: str) -> None:
    """
    Called from LiveStreamManager's background thread whenever the live
    risk level escalates into HIGH/CRITICAL. Runs outside a Flask request,
    so it needs its own application context to touch the database.
    Notifies admin accounts AND any active, location-matched authorities
    (Phase 2), subject to the existing cooldown, and records a per-
    recipient AlertDelivery row for the Alert History page.
    """
    with app.app_context():
        if live_manager.video_id is None:
            return
        video = db.session.get(Video, live_manager.video_id)
        if video is None:
            return

        # A sustained HIGH/CRITICAL stream must not create an alert row (and
        # a round of notifications) every few seconds: skip if this video
        # already raised the same level inside the cooldown window.
        last_same = (
            Alert.query.filter_by(video_id=video.id, risk_level=level)
            .order_by(Alert.created_at.desc()).first()
        )
        if last_same and last_same.created_at and (
            datetime.utcnow() - last_same.created_at
        ).total_seconds() < config.ALERT_EMAIL_COOLDOWN_SECONDS:
            return

        live_stats = live_manager.get_stats()
        alert = Alert(
            video_id=video.id, risk_level=level, message=message,
            person_count=live_stats.get("person_count"),
            max_cell_density=live_stats.get("max_cell_density"),
            growth_rate=live_stats.get("growth_rate"),
        )
        db.session.add(alert)
        db.session.flush()  # assign alert.id before the email/commit

        source_label = f"Live CCTV ({live_manager.source_type or 'camera'})"
        delivery_results = send_alert_to_recipients(
            level, message, source_label=source_label, location_name=video.location_name,
        )
        for d in delivery_results:
            db.session.add(AlertDelivery(
                alert_id=alert.id, recipient_type=d["type"], recipient_id=d["id"],
                recipient_name=d["name"], recipient_email=d["email"],
                status=d["status"], sent_at=datetime.utcnow(), error_message=d["error"],
            ))
        alert.notified = any(d["status"] == "sent" for d in delivery_results)

        db.session.commit()
        logger.warning(
            f"LIVE ALERT [{level}] video_id={video.id}: {message} "
            f"({sum(1 for d in delivery_results if d['status']=='sent')}/{len(delivery_results)} notified)"
        )

        # Dispatch runs AFTER the commit on purpose: we never page a field
        # officer about an incident that failed to persist. create_dispatch_
        # for_alert is a no-op for anything below HIGH/CRITICAL, for a video
        # with no coordinates, and for a location already covered by an open
        # dispatch - so a sustained incident pages officers once, not once
        # per alert.
        try:
            dispatch_id = dispatch_engine.create_dispatch_for_alert(alert.id)
            if dispatch_id:
                logger.warning(
                    f"Dispatch #{dispatch_id} raised for alert {alert.id} ({level})."
                )
        except Exception as exc:  # noqa: BLE001
            # Alerting and paging are separate concerns: a dispatch failure
            # must not roll back or mask an alert that was already saved and
            # emailed.
            db.session.rollback()
            logger.error(f"Dispatch creation failed for alert {alert.id}: {exc}")


live_manager.on_alert = _live_alert_callback


# ---------------------------------------------------------------------------
# PUBLIC MARKETING SITE
# ---------------------------------------------------------------------------
@app.route("/")
def home():
    return render_template("home.html")


@app.route("/about")
def about():
    return render_template("about.html")


@app.route("/features")
def features_page():
    return render_template("features.html")


@app.route("/how-it-works")
def how_it_works():
    return render_template(
        "how_it_works.html",
        risk_levels=config.RISK_THRESHOLDS, surge=config.SURGE_GROWTH_RATE_THRESHOLD,
        grid_rows=config.DENSITY_GRID_ROWS, grid_cols=config.DENSITY_GRID_COLS,
        frame_skip=config.FRAME_SKIP,
    )


@app.route("/technology")
def technology_page():
    return render_template("technology.html")


@app.route("/contact", methods=["GET", "POST"])
def contact():
    form = ContactForm()
    if form.validate_on_submit():
        msg = ContactMessage(
            name=form.name.data.strip(),
            email=form.email.data.strip(),
            message=form.message.data.strip(),
        )
        db.session.add(msg)
        db.session.commit()
        logger.info(f"Contact message received from {msg.email}.")
        flash("Thanks for reaching out — we'll get back to you soon.", "success")
        return redirect(url_for("contact"))

    return render_template("contact.html", form=form)


# ---------------------------------------------------------------------------
# AUTH ROUTES
# ---------------------------------------------------------------------------
@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(_landing_url())

    form = LoginForm()
    if form.validate_on_submit():
        user = User.query.filter_by(username=form.username.data.strip()).first()
        if user and user.check_password(form.password.data):
            if not user.is_verified:
                # Password is correct, but they never finished confirming
                # their email at registration — send them back to that
                # flow instead of proceeding to login 2FA.
                result = generate_and_send_otp(user, purpose="register")
                session["pending_register_user_id"] = user.id
                if result.get("dev_code"):
                    flash(f"[DEV MODE] SMTP not configured — your verification code is: {result['dev_code']}", "warning")
                elif result["sent"]:
                    flash(f"Please verify your email first. A code has been sent to {user.email}.", "warning")
                else:
                    flash("Please verify your email before logging in.", "warning")
                return redirect(url_for("register_verify"))

            if not config.TWO_FACTOR_ENABLED:
                login_user(user)
                logger.info(f"User '{user.username}' logged in (2FA disabled).")
                next_page = request.args.get("next")
                return redirect(next_page or _landing_url(user))

            # Correct password, but NOT logged in yet — send the OTP and
            # hand off to the verification step before calling login_user().
            result = generate_and_send_login_otp(user)
            if not result["sent"] and result["wait_seconds"] == 0:
                flash(
                    "Could not send your verification code right now. "
                    "Please contact an administrator.", "danger",
                )
                return redirect(url_for("login"))

            session["pending_2fa_user_id"] = user.id
            session["pending_2fa_next"] = request.args.get("next", "")
            if result.get("dev_code"):
                flash(f"[DEV MODE] SMTP not configured — your code is: {result['dev_code']}", "warning")
            else:
                flash(f"A verification code has been sent to {user.email}.", "info")
            logger.info(f"Password OK for '{user.username}'; awaiting OTP verification.")
            return redirect(url_for("login_verify"))

        flash("Invalid username or password.", "danger")

    return render_template("login.html", form=form)


@app.route("/login/verify", methods=["GET", "POST"])
def login_verify():
    user_id = session.get("pending_2fa_user_id")
    if user_id is None:
        flash("Please sign in first.", "info")
        return redirect(url_for("login"))

    user = db.session.get(User, user_id)
    if user is None:
        session.pop("pending_2fa_user_id", None)
        return redirect(url_for("login"))

    form = OtpForm()
    if form.validate_on_submit():
        result = verify_login_otp(user_id, form.code.data)
        if result["ok"]:
            session.pop("pending_2fa_user_id", None)
            next_page = session.pop("pending_2fa_next", "") or None
            login_user(user)
            logger.info(f"User '{user.username}' completed 2FA and logged in.")
            return redirect(next_page or _landing_url(user))

        reason_messages = {
            "no_code": "No active code found — please request a new one.",
            "expired": "That code has expired. Please request a new one.",
            "max_attempts": "Too many incorrect attempts. Please request a new code.",
            "incorrect": "Incorrect code. Please try again.",
        }
        flash(reason_messages.get(result["reason"], "Verification failed."), "danger")
        if result["reason"] in ("no_code", "expired", "max_attempts"):
            return redirect(url_for("login_verify"))

    wait_seconds = seconds_until_resend_allowed(user)
    return render_template("verify_otp.html", form=form, email=user.email, wait_seconds=wait_seconds)


@app.route("/login/resend-otp", methods=["POST"])
def login_resend_otp():
    user_id = session.get("pending_2fa_user_id")
    if user_id is None:
        return redirect(url_for("login"))

    user = db.session.get(User, user_id)
    if user is None:
        session.pop("pending_2fa_user_id", None)
        return redirect(url_for("login"))

    result = generate_and_send_login_otp(user)
    if result["sent"]:
        if result.get("dev_code"):
            flash(f"[DEV MODE] SMTP not configured — your new code is: {result['dev_code']}", "warning")
        else:
            flash(f"A new verification code has been sent to {user.email}.", "info")
    elif result["wait_seconds"] > 0:
        flash(f"Please wait {result['wait_seconds']}s before requesting another code.", "warning")
    else:
        flash("Could not send a new code right now. Please try again shortly.", "danger")

    return redirect(url_for("login_verify"))


@app.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(_landing_url())

    form = RegisterForm()
    if form.validate_on_submit():
        existing = User.query.filter(
            (User.username == form.username.data.strip()) | (User.email == form.email.data.strip())
        ).first()
        if existing:
            flash("Username or email already registered.", "danger")
        else:
            user = User(
                username=form.username.data.strip(),
                email=form.email.data.strip(),
                role=form.role.data,
                badge_number=(form.badge_number.data or "").strip() or None,
                phone_number=(form.phone_number.data or "").strip() or None,
                is_verified=False,
            )
            user.set_password(form.password.data)
            db.session.add(user)
            db.session.commit()
            logger.info(f"New user registered (pending email verification): {user.username} ({user.role})")

            result = generate_and_send_otp(user, purpose="register")
            if not result["sent"] and result["wait_seconds"] == 0:
                flash(
                    "Account created, but we could not send a verification email right now. "
                    "Please contact an administrator.", "danger",
                )
                return redirect(url_for("login"))

            session["pending_register_user_id"] = user.id
            if result.get("dev_code"):
                flash(f"[DEV MODE] SMTP not configured — your verification code is: {result['dev_code']}", "warning")
            else:
                flash(f"A verification code has been sent to {user.email}.", "info")
            return redirect(url_for("register_verify"))

    return render_template("register.html", form=form)


@app.route("/register/verify", methods=["GET", "POST"])
def register_verify():
    user_id = session.get("pending_register_user_id")
    if user_id is None:
        flash("Please register first.", "info")
        return redirect(url_for("register"))

    user = db.session.get(User, user_id)
    if user is None:
        session.pop("pending_register_user_id", None)
        return redirect(url_for("register"))

    if user.is_verified:
        session.pop("pending_register_user_id", None)
        return redirect(url_for("login"))

    form = OtpForm()
    if form.validate_on_submit():
        result = verify_otp_code(user_id, form.code.data, purpose="register")
        if result["ok"]:
            user.is_verified = True
            db.session.commit()
            session.pop("pending_register_user_id", None)
            logger.info(f"User '{user.username}' verified their email.")
            flash("Email verified! You can now log in.", "success")
            return redirect(url_for("login"))

        reason_messages = {
            "no_code": "No active code found — please request a new one.",
            "expired": "That code has expired. Please request a new one.",
            "max_attempts": "Too many incorrect attempts. Please request a new code.",
            "incorrect": "Incorrect code. Please try again.",
        }
        flash(reason_messages.get(result["reason"], "Verification failed."), "danger")
        if result["reason"] in ("no_code", "expired", "max_attempts"):
            return redirect(url_for("register_verify"))

    wait_seconds = seconds_until_resend_allowed(user, purpose="register")
    return render_template("verify_register_otp.html", form=form, email=user.email, wait_seconds=wait_seconds)


@app.route("/register/resend-otp", methods=["POST"])
def register_resend_otp():
    user_id = session.get("pending_register_user_id")
    if user_id is None:
        return redirect(url_for("register"))

    user = db.session.get(User, user_id)
    if user is None:
        session.pop("pending_register_user_id", None)
        return redirect(url_for("register"))

    result = generate_and_send_otp(user, purpose="register")
    if result["sent"]:
        if result.get("dev_code"):
            flash(f"[DEV MODE] SMTP not configured — your new code is: {result['dev_code']}", "warning")
        else:
            flash(f"A new verification code has been sent to {user.email}.", "info")
    elif result["wait_seconds"] > 0:
        flash(f"Please wait {result['wait_seconds']}s before requesting another code.", "warning")
    else:
        flash("Could not send a new code right now. Please try again shortly.", "danger")

    return redirect(url_for("register_verify"))


@app.route("/logout")
@login_required
def logout():
    logger.info(f"User '{current_user.username}' logged out.")
    logout_user()
    return redirect(url_for("login"))


# ---------------------------------------------------------------------------
# DASHBOARD
# ---------------------------------------------------------------------------
@app.route("/dashboard")
@login_required
def dashboard():
    summary = history.get_dashboard_summary()
    recent_videos = history.get_recent_videos(limit=10)
    return render_template("dashboard.html", summary=summary, recent_videos=recent_videos)


# ---------------------------------------------------------------------------
# REGISTERED LOCATIONS (shared by upload + live start)
# ---------------------------------------------------------------------------
def _active_locations():
    return CameraLocation.query.filter_by(is_active=True).order_by(CameraLocation.name).all()


def _resolve_location(location_id, name, lat, lng):
    """
    A registered (admin-verified) location wins over free-typed values.
    Returns (location_name, latitude, longitude).
    """
    if location_id:
        loc = db.session.get(CameraLocation, int(location_id))
        if loc is not None and loc.is_active:
            return loc.name, loc.latitude, loc.longitude
    return (name or None), lat, lng


# ---------------------------------------------------------------------------
# VIDEO UPLOAD (Mode 1)
# ---------------------------------------------------------------------------
@app.route("/upload", methods=["GET", "POST"])
@login_required
def upload_video():
    form = UploadVideoForm()
    locations = _active_locations()
    form.location_id.choices = [(0, "— Not specified —")] + [(l.id, l.name) for l in locations]
    if form.validate_on_submit():
        file = form.video_file.data
        original_filename = file.filename

        if not allowed_video_file(original_filename):
            flash("Unsupported video format.", "danger")
            return redirect(url_for("upload_video"))

        stored_filename = generate_unique_filename(original_filename)
        input_path = safe_join_upload_path(stored_filename)
        file.save(input_path)

        loc_name, loc_lat, loc_lng = _resolve_location(
            form.location_id.data, form.location_name.data, form.latitude.data, form.longitude.data,
        )
        video = Video(
            user_id=current_user.id,
            original_filename=original_filename,
            stored_filename=stored_filename,
            source_type="upload",
            processing_status="pending",
            location_name=loc_name,
            latitude=loc_lat,
            longitude=loc_lng,
        )
        db.session.add(video)
        db.session.commit()

        logger.info(f"Video uploaded: {original_filename} -> {stored_filename} (id={video.id})")

        # Process in a background thread so the HTTP request returns
        # immediately and the user is redirected to a live-polling status
        # page instead of the browser hanging on a multi-minute request.
        thread = threading.Thread(target=_process_video_job, args=(video.id, input_path))
        thread.daemon = True
        thread.start()

        return redirect(url_for("video_status", video_id=video.id))

    return render_template("upload.html", form=form, locations=locations)


def _process_video_job(video_id: int, input_path: str) -> None:
    """
    Runs in a background thread: executes the full YOLO -> density ->
    risk -> heatmap pipeline on the uploaded video and persists results.
    Wrapped in its own app context since it runs outside a Flask request.
    """
    with app.app_context():
        video = db.session.get(Video, video_id)
        if video is None:
            logger.error(f"_process_video_job: video id {video_id} not found.")
            return

        video.processing_status = "processing"
        db.session.commit()

        output_filename = f"processed_{video.stored_filename}"
        output_path = safe_join_processed_path(output_filename)

        try:
            # Serialize heavy GPU/CPU inference jobs so several uploads in
            # quick succession don't fight over the same CPU/GPU resources.
            with _processing_lock:
                processor = VideoProcessor()
                result = processor.process(input_path, output_path)

            # Re-encode to browser-playable H.264 when FFmpeg is available.
            video.browser_playable = convert_to_browser_mp4(output_path)

            video.processed_filename = output_filename
            video.frame_width = result["frame_width"]
            video.frame_height = result["frame_height"]
            video.total_frames = result["total_frames"]
            video.source_fps = result["source_fps"]
            video.processing_time_seconds = result["processing_time_seconds"]
            video.peak_count = result["peak_count"]
            video.average_count = result["average_count"]
            video.final_risk_level = result["final_risk"]["level"]
            video.final_density = result.get("final_density")
            video.final_growth_rate = result.get("final_growth_rate")
            video.set_risk_timeline(result["risk_timeline"])
            video.processing_status = "completed"
            db.session.commit()

            history.create_alerts_from_timeline(video)
            logger.info(f"Video {video_id} processed successfully.")

        except Exception as exc:  # noqa: BLE001 - we want to persist ANY failure reason
            logger.exception(f"Video {video_id} processing failed: {exc}")
            video.processing_status = "failed"
            video.error_message = str(exc)
            db.session.commit()


@app.route("/video/<int:video_id>/status")
@login_required
def video_status(video_id):
    video = db.session.get(Video, video_id)
    if video is None:
        abort(404)
    return render_template("video_status.html", video=video)


@app.route("/api/video/<int:video_id>/status")
@login_required
def api_video_status(video_id):
    """JSON endpoint polled by static/js/dashboard.js (pollVideoStatus)."""
    video = db.session.get(Video, video_id)
    if video is None:
        return jsonify({"error": "not found"}), 404
    return jsonify({
        "id": video.id,
        "processing_status": video.processing_status,
        "error_message": video.error_message,
    })


@app.route("/video/<int:video_id>/analytics")
@login_required
def video_analytics(video_id):
    video = db.session.get(Video, video_id)
    if video is None:
        abort(404)
    if video.processing_status != "completed":
        flash("Analytics are only available once processing is complete.", "info")
        return redirect(url_for("video_status", video_id=video_id))

    timeline = video.get_risk_timeline()
    counts = [entry.get("count", 0) for entry in timeline]

    return render_template(
        "video_analytics.html",
        video=video,
        timeline_json=json.dumps(timeline),
        counts_json=json.dumps(counts),
    )


@app.route("/video/<int:video_id>/report", methods=["POST"])
@login_required
def generate_report(video_id):
    video = db.session.get(Video, video_id)
    if video is None:
        abort(404)
    if video.processing_status != "completed":
        flash("Cannot generate a report until processing is complete.", "danger")
        return redirect(url_for("video_status", video_id=video_id))

    pdf_path = generate_video_report_pdf(video)
    return send_from_directory(
        config.REPORTS_DIR, os.path.basename(pdf_path), as_attachment=True
    )


@app.route("/videos/processed/<path:filename>")
@login_required
def serve_processed_video(filename):
    """Serves annotated output videos so <video> tags in templates can play them."""
    return send_from_directory(config.VIDEO_PROCESSED_DIR, filename)


# ---------------------------------------------------------------------------
# LIVE CCTV (Mode 2)
# ---------------------------------------------------------------------------
@app.route("/live")
@login_required
def live_view():
    """Live CCTV dashboard page: source picker + MJPEG preview + live stats."""
    return render_template(
        "live_view.html",
        is_running=live_manager.is_running,
        source_type=live_manager.source_type,
        fluvio_active=live_manager.fluvio_active,
        fluvio_enabled_in_config=config.FLUVIO_ENABLED,
        locations=_active_locations(),
        live_location=(
            db.session.get(Video, live_manager.video_id).location_name
            if live_manager.is_running and live_manager.video_id else None
        ),
    )


@app.route("/live/start", methods=["POST"])
@login_required
def live_start():
    source_type = request.form.get("source_type", "webcam")

    if source_type == "webcam":
        source_value = config.DEFAULT_WEBCAM_INDEX
    elif source_type in ("rtsp", "ip"):
        source_value = request.form.get("source_url", "").strip()
        if not source_value:
            flash("Please enter a stream URL for RTSP / IP camera sources.", "danger")
            return redirect(url_for("live_view"))
    else:
        flash("Unknown camera source type.", "danger")
        return redirect(url_for("live_view"))

    # Optional location tagging for the risk map — plain form fields
    # (not Flask-WTF here since live_view.html posts a simple HTML form).
    location_name = request.form.get("location_name", "").strip() or None
    lat_raw = request.form.get("latitude", "").strip()
    lng_raw = request.form.get("longitude", "").strip()
    try:
        latitude = float(lat_raw) if lat_raw else None
        longitude = float(lng_raw) if lng_raw else None
        if latitude is not None and not -90 <= latitude <= 90:
            latitude = None
        if longitude is not None and not -180 <= longitude <= 180:
            longitude = None
    except ValueError:
        latitude, longitude = None, None
    if (latitude is None) != (longitude is None):
        latitude = longitude = None  # need both or neither

    location_name, latitude, longitude = _resolve_location(
        request.form.get("location_id", type=int), location_name, latitude, longitude,
    )

    # Create a Video row representing this live session so alerts, history
    # and the admin panel can all reference it exactly like an uploaded video.
    video = Video(
        user_id=current_user.id,
        original_filename=f"Live CCTV ({source_type})",
        stored_filename=f"live_{int(time.time())}",
        source_type="cctv",
        processing_status="processing",
        location_name=location_name,
        latitude=latitude,
        longitude=longitude,
    )
    db.session.add(video)
    db.session.commit()

    live_manager.video_id = video.id  # set before start(): the first alert may fire immediately
    result = live_manager.start(source_type, source_value)

    if not result["started"]:
        live_manager.video_id = None
        video.processing_status = "failed"
        video.error_message = result.get("reason", "Unknown error starting live session.")
        db.session.commit()
        flash(f"Could not start live session: {video.error_message}", "danger")
        return redirect(url_for("live_view"))

    if result.get("fluvio_active"):
        flash("Live session started using Fluvio streaming.", "success")
    else:
        reason = result.get("reason")
        msg = "Live session started in direct mode (Fluvio not available)."
        if reason:
            msg += f" Reason: {reason}"
        flash(msg, "warning")

    logger.info(
        f"Live CCTV session started (video_id={video.id}, source={source_type}, "
        f"fluvio_active={result.get('fluvio_active')})"
    )
    return redirect(url_for("live_view"))


@app.route("/live/stop", methods=["POST"])
@login_required
def live_stop():
    video_id = live_manager.video_id
    stats = live_manager.get_stats()
    live_manager.stop()

    if video_id is not None:
        video = db.session.get(Video, video_id)
        if video is not None:
            video.processing_status = "completed"
            video.peak_count = live_manager.crowd_counter.peak_count
            video.average_count = live_manager.crowd_counter.average_count
            video.final_risk_level = stats.get("risk_level", "SAFE")
            video.final_density = stats.get("max_cell_density")
            video.final_growth_rate = stats.get("growth_rate")
            db.session.commit()

    flash("Live session stopped.", "info")
    return redirect(url_for("live_view"))


@app.route("/live/feed")
@login_required
def live_feed():
    """
    MJPEG streaming endpoint. The <img> tag in live_view.html points its
    src directly at this route; the browser renders each JPEG part as it
    arrives, producing a live "video" without any JS polling required.
    """
    def generate():
        while True:
            frame_bytes = live_manager.get_latest_jpeg()
            if frame_bytes is not None:
                yield (
                    b"--frame\r\n"
                    b"Content-Type: image/jpeg\r\n\r\n" + frame_bytes + b"\r\n"
                )
            time.sleep(1.0 / 15.0)

    return Response(generate(), mimetype="multipart/x-mixed-replace; boundary=frame")


@app.route("/api/live/status")
@login_required
def api_live_status():
    """JSON endpoint polled by static/js/dashboard.js to update the live stat cards."""
    return jsonify(live_manager.get_stats())


# ---------------------------------------------------------------------------
# ALERTS
# ---------------------------------------------------------------------------
@app.route("/alerts")
@login_required
def alerts():
    risk = request.args.get("risk", "").strip() or None
    location = request.args.get("location", "").strip() or None
    delivery_status = request.args.get("status", "").strip() or None
    date_from_raw = request.args.get("date_from", "").strip()
    date_to_raw = request.args.get("date_to", "").strip()

    date_from = date_to = None
    try:
        if date_from_raw:
            date_from = datetime.strptime(date_from_raw, "%Y-%m-%d")
        if date_to_raw:
            date_to = datetime.strptime(date_to_raw, "%Y-%m-%d") + timedelta(days=1) - timedelta(seconds=1)
    except ValueError:
        flash("Invalid date format.", "warning")

    alert_rows = history.get_filtered_alerts(
        risk=risk, date_from=date_from, date_to=date_to,
        location=location, delivery_status=delivery_status,
    )
    location_options = history.get_alert_location_options()

    return render_template(
        "alerts.html", alerts=alert_rows, location_options=location_options,
        current_risk=risk or "", current_location=location or "",
        current_status=delivery_status or "", current_date_from=date_from_raw, current_date_to=date_to_raw,
    )


@app.route("/alerts/<int:alert_id>/retry", methods=["POST"])
@login_required
def alert_retry(alert_id):
    """Admin: re-send an alert's email to recipients whose delivery failed."""
    guard = _require_admin()
    if guard:
        return guard
    alert = db.session.get(Alert, alert_id)
    if alert is None:
        abort(404)
    result = retry_failed_deliveries(alert, db)
    if result["retried"] == 0:
        flash("Nothing to retry: this alert has no failed deliveries.", "info")
    else:
        flash(
            f"Retried {result['retried']} failed notification(s): "
            f"{result['sent']} sent, {result['failed']} still failing.",
            "success" if result["failed"] == 0 else "warning",
        )
    return redirect(url_for("alerts"))


@app.route("/api/alerts/recent")
@login_required
def api_alerts_recent():
    """Latest alerts (no contact details) for the Live Monitoring panel."""
    limit = max(1, min(request.args.get("limit", 5, type=int), 50))
    rows = history.get_recent_alerts(limit)
    return jsonify({"alerts": [
        {
            "id": a.id,
            "risk_level": a.risk_level,
            "location": a.video.location_name or a.video.original_filename,
            "timestamp": iso_utc(a.created_at),
            "people": a.person_count,
            "sent": sum(1 for d in a.deliveries if d.status == "sent"),
            "failed": sum(1 for d in a.deliveries if d.status == "failed"),
        } for a in rows
    ]})


# ---------------------------------------------------------------------------
# RISK MAP
# ---------------------------------------------------------------------------
@app.route("/map")
@login_required
def risk_map():
    """Map page showing every located video/camera, color-coded by risk level."""
    return render_template("map.html")


def _build_map_locations() -> list:
    """
    One entry per physical location. Located videos at the same place are
    merged: the live session (if any) or else the most recent video supplies
    the current state; the latest alert across all of them supplies incident
    and per-recipient notification details. Admin-registered locations with
    no footage yet are included as grey "no data" markers. No coordinates are
    ever invented - only stored/registered ones are shown.
    """
    live_stats = live_manager.get_stats() if live_manager.is_running else None
    live_vid = live_manager.video_id if live_stats else None
    now = datetime.utcnow()
    window = timedelta(minutes=config.INCIDENT_ACTIVE_MINUTES)

    videos = Video.query.filter(
        Video.latitude.isnot(None), Video.longitude.isnot(None)
    ).order_by(Video.upload_time.desc()).all()

    groups = {}
    for v in videos:
        key = ((v.location_name or "").strip().lower(), round(v.latitude, 5), round(v.longitude, 5))
        groups.setdefault(key, []).append(v)

    features, seen_names = [], set()
    for vids in groups.values():
        rep_video = next((v for v in vids if live_vid == v.id), vids[0])
        is_live = live_vid == rep_video.id

        if is_live:
            risk_level = live_stats.get("risk_level", "SAFE")
            person_count = live_stats.get("person_count", 0)
            density = live_stats.get("max_cell_density", 0)
            growth_rate = live_stats.get("growth_rate", 0.0)
        else:
            risk_level = rep_video.final_risk_level or "SAFE"
            person_count = rep_video.peak_count or 0
            density = rep_video.final_density or 0
            growth_rate = rep_video.final_growth_rate or 0.0

        risk_info = config.RISK_THRESHOLDS.get(risk_level, config.RISK_THRESHOLDS["SAFE"])
        latest_alert = (
            Alert.query.filter(Alert.video_id.in_([v.id for v in vids]))
            .order_by(Alert.created_at.desc()).first()
        )
        alert_info = None
        if latest_alert is not None:
            deliveries = latest_alert.deliveries
            alert_info = {
                "id": latest_alert.id,
                "risk_level": latest_alert.risk_level,
                "message": latest_alert.message,
                "timestamp": iso_utc(latest_alert.created_at),
                "sent_count": sum(1 for d in deliveries if d.status == "sent"),
                "failed_count": sum(1 for d in deliveries if d.status == "failed"),
                "pending_count": sum(1 for d in deliveries if d.status == "pending"),
                "recipients": [  # names/roles only - never contact details
                    {"name": d.recipient_name, "type": d.recipient_type, "status": d.status}
                    for d in deliveries
                ],
            }

        incident_active = bool(
            (is_live and risk_level in config.ALERT_TRIGGER_LEVELS)
            or (latest_alert is not None
                and latest_alert.risk_level in config.ALERT_TRIGGER_LEVELS
                and latest_alert.created_at
                and now - latest_alert.created_at <= window)
        )
        name = rep_video.location_name or rep_video.original_filename
        seen_names.add(name.strip().lower())
        features.append({
            "id": rep_video.id,
            "name": name,
            "latitude": rep_video.latitude,
            "longitude": rep_video.longitude,
            "risk_level": risk_level,
            "risk_label": risk_info["label"],
            "color": risk_info["color"],
            "person_count": person_count,
            "density": round(density, 2) if density else 0,
            "growth_rate": round(growth_rate, 3) if growth_rate else 0.0,
            "source_type": rep_video.source_type,
            "is_live": is_live,
            "status": rep_video.processing_status,
            "upload_time": iso_utc(rep_video.upload_time),
            "detail_url": url_for("live_view") if is_live else url_for("video_status", video_id=rep_video.id),
            "incident_active": incident_active,
            "no_data": False,
            "alert": alert_info,
        })

    for loc in _active_locations():
        if loc.name.strip().lower() in seen_names:
            continue
        features.append({
            "id": None, "name": loc.name, "latitude": loc.latitude, "longitude": loc.longitude,
            "risk_level": "NO_DATA", "risk_label": "No monitoring data yet", "color": "#94a3b8",
            "person_count": 0, "density": 0, "growth_rate": 0.0, "source_type": "registered",
            "is_live": False, "status": "registered", "upload_time": None, "detail_url": url_for("upload_video"),
            "incident_active": False, "no_data": True, "alert": None,
        })
    return features


@app.route("/api/map/locations")
@login_required
def api_map_locations():
    """JSON feed for the Leaflet map (see _build_map_locations)."""
    features = _build_map_locations()
    return jsonify({
        "locations": features,
        "active_incidents": sum(1 for f in features if f["incident_active"]),
    })


# ---------------------------------------------------------------------------
# ADMIN PANEL
# ---------------------------------------------------------------------------
@app.route("/admin")
@login_required
def admin_panel():
    guard = _require_admin()
    if guard:
        return guard

    active_incidents = sum(1 for f in _build_map_locations() if f["incident_active"])
    overview = history.get_admin_overview(active_incidents)
    users = User.query.order_by(User.created_at.desc()).all()
    videos = Video.query.order_by(Video.upload_time.desc()).limit(50).all()
    messages = ContactMessage.query.order_by(ContactMessage.created_at.desc()).limit(50).all()
    return render_template(
        "admin_panel.html", overview=overview, users=users, videos=videos, messages=messages,
    )


@app.route("/admin/messages/<int:message_id>/toggle-read", methods=["POST"])
@login_required
def admin_message_toggle(message_id):
    guard = _require_admin()
    if guard:
        return guard
    msg = db.session.get(ContactMessage, message_id)
    if msg is None:
        abort(404)
    msg.is_read = not msg.is_read
    db.session.commit()
    return redirect(url_for("admin_panel") + "#messages")


@app.route("/admin/messages/<int:message_id>/delete", methods=["POST"])
@login_required
def admin_message_delete(message_id):
    guard = _require_admin()
    if guard:
        return guard
    msg = db.session.get(ContactMessage, message_id)
    if msg is None:
        abort(404)
    db.session.delete(msg)
    db.session.commit()
    flash("Message deleted.", "info")
    return redirect(url_for("admin_panel") + "#messages")


# ---------------------------------------------------------------------------
# ADMIN: MONITORED LOCATIONS (real-world coordinates for cameras / sources)
# ---------------------------------------------------------------------------
def _flash_form_errors(form):
    for field_errors in form.errors.values():
        for err in field_errors:
            flash(err, "danger")


@app.route("/admin/locations")
@login_required
def admin_locations():
    guard = _require_admin()
    if guard:
        return guard
    locations = CameraLocation.query.order_by(CameraLocation.name).all()
    usage = {
        l.id: Video.query.filter(db.func.lower(Video.location_name) == l.name.lower()).count()
        for l in locations
    }
    return render_template("admin_locations.html", locations=locations, usage=usage, form=LocationForm())


@app.route("/admin/locations/add", methods=["POST"])
@login_required
def admin_location_add():
    guard = _require_admin()
    if guard:
        return guard
    form = LocationForm()
    if form.validate_on_submit():
        name = form.name.data.strip()
        if CameraLocation.query.filter(db.func.lower(CameraLocation.name) == name.lower()).first():
            flash(f"A location named '{name}' already exists.", "danger")
        else:
            db.session.add(CameraLocation(
                name=name, description=(form.description.data or "").strip() or None,
                latitude=form.latitude.data, longitude=form.longitude.data, is_active=form.is_active.data,
            ))
            db.session.commit()
            logger.info(f"Location added: {name} by '{current_user.username}'.")
            flash(f"Location '{name}' added.", "success")
    else:
        _flash_form_errors(form)
    return redirect(url_for("admin_locations"))


@app.route("/admin/locations/<int:location_id>/edit", methods=["POST"])
@login_required
def admin_location_edit(location_id):
    guard = _require_admin()
    if guard:
        return guard
    loc = db.session.get(CameraLocation, location_id)
    if loc is None:
        abort(404)
    form = LocationForm()
    if form.validate_on_submit():
        name = form.name.data.strip()
        clash = CameraLocation.query.filter(
            db.func.lower(CameraLocation.name) == name.lower(), CameraLocation.id != loc.id
        ).first()
        if clash:
            flash(f"Another location is already named '{name}'.", "danger")
        else:
            loc.name = name
            loc.description = (form.description.data or "").strip() or None
            loc.latitude = form.latitude.data
            loc.longitude = form.longitude.data
            loc.is_active = form.is_active.data
            db.session.commit()
            flash(f"Location '{loc.name}' updated.", "success")
    else:
        _flash_form_errors(form)
    return redirect(url_for("admin_locations"))


@app.route("/admin/locations/<int:location_id>/toggle-active", methods=["POST"])
@login_required
def admin_location_toggle(location_id):
    guard = _require_admin()
    if guard:
        return guard
    loc = db.session.get(CameraLocation, location_id)
    if loc is None:
        abort(404)
    loc.is_active = not loc.is_active
    db.session.commit()
    flash(f"Location '{loc.name}' {'activated' if loc.is_active else 'deactivated'}.", "info")
    return redirect(url_for("admin_locations"))


@app.route("/admin/locations/<int:location_id>/delete", methods=["POST"])
@login_required
def admin_location_delete(location_id):
    guard = _require_admin()
    if guard:
        return guard
    loc = db.session.get(CameraLocation, location_id)
    if loc is None:
        abort(404)
    name = loc.name
    db.session.delete(loc)  # existing videos keep their stored name/coordinates
    db.session.commit()
    flash(f"Location '{name}' deleted.", "info")
    return redirect(url_for("admin_locations"))


# ---------------------------------------------------------------------------
# ADMIN: AUTHORITY MANAGEMENT (Phase 1)
# ---------------------------------------------------------------------------
def _require_admin():
    """Returns a redirect response if the current user isn't an admin, else None."""
    if not current_user.is_admin:
        flash("Admin access required.", "danger")
        return redirect(url_for("dashboard"))
    return None


@app.route("/admin/authorities")
@login_required
def admin_authorities():
    guard = _require_admin()
    if guard:
        return guard

    query = Authority.query

    role_filter = request.args.get("role", "").strip()
    status_filter = request.args.get("status", "").strip()
    search = request.args.get("q", "").strip()

    if role_filter:
        query = query.filter(Authority.role == role_filter)
    if status_filter == "active":
        query = query.filter(Authority.is_active.is_(True))
    elif status_filter == "inactive":
        query = query.filter(Authority.is_active.is_(False))
    if search:
        like = f"%{search}%"
        query = query.filter(
            (Authority.name.ilike(like)) | (Authority.email.ilike(like)) | (Authority.department.ilike(like))
        )

    authorities = query.order_by(Authority.created_at.desc()).all()

    # Real database-driven stats — never hardcoded.
    all_authorities = Authority.query.all()
    stats = {
        "total": len(all_authorities),
        "active": sum(1 for a in all_authorities if a.is_active),
        "police": sum(1 for a in all_authorities if a.role == "Police"),
        "traffic_police": sum(1 for a in all_authorities if a.role == "Traffic Police"),
        "other": sum(1 for a in all_authorities if a.role not in ("Police", "Traffic Police")),
    }

    add_form = AuthorityForm()
    return render_template(
        "admin_authorities.html",
        authorities=authorities, stats=stats, add_form=add_form,
        role_choices=Authority.ROLE_CHOICES,
        alert_counts=history.get_authority_alert_counts(),
        location_names=[l.name for l in _active_locations()],
        current_role_filter=role_filter, current_status_filter=status_filter, current_search=search,
    )


@app.route("/admin/authorities/add", methods=["POST"])
@login_required
def admin_authority_add():
    guard = _require_admin()
    if guard:
        return guard

    form = AuthorityForm()
    if form.validate_on_submit():
        authority = Authority(
            name=form.name.data.strip(),
            role=form.role.data,
            department=(form.department.data or "").strip() or None,
            email=form.email.data.strip(),
            phone=(form.phone.data or "").strip() or None,
            location_area=(form.location_area.data or "").strip() or None,
            is_active=form.is_active.data,
            created_by_id=current_user.id,
        )
        db.session.add(authority)
        db.session.commit()
        logger.info(f"Authority added: {authority.name} ({authority.role}) by '{current_user.username}'.")
        flash(f"Authority '{authority.name}' added.", "success")
    else:
        for field_errors in form.errors.values():
            for err in field_errors:
                flash(err, "danger")

    return redirect(url_for("admin_authorities"))


@app.route("/admin/authorities/<int:authority_id>/edit", methods=["POST"])
@login_required
def admin_authority_edit(authority_id):
    guard = _require_admin()
    if guard:
        return guard

    authority = db.session.get(Authority, authority_id)
    if authority is None:
        abort(404)

    form = AuthorityForm()
    if form.validate_on_submit():
        authority.name = form.name.data.strip()
        authority.role = form.role.data
        authority.department = (form.department.data or "").strip() or None
        authority.email = form.email.data.strip()
        authority.phone = (form.phone.data or "").strip() or None
        authority.location_area = (form.location_area.data or "").strip() or None
        authority.is_active = form.is_active.data
        db.session.commit()
        logger.info(f"Authority updated: {authority.name} (id={authority.id}) by '{current_user.username}'.")
        flash(f"Authority '{authority.name}' updated.", "success")
    else:
        for field_errors in form.errors.values():
            for err in field_errors:
                flash(err, "danger")

    return redirect(url_for("admin_authorities"))


@app.route("/admin/authorities/<int:authority_id>/toggle-active", methods=["POST"])
@login_required
def admin_authority_toggle(authority_id):
    guard = _require_admin()
    if guard:
        return guard

    authority = db.session.get(Authority, authority_id)
    if authority is None:
        abort(404)

    authority.is_active = not authority.is_active
    db.session.commit()
    state = "activated" if authority.is_active else "deactivated"
    logger.info(f"Authority {state}: {authority.name} (id={authority.id}) by '{current_user.username}'.")
    flash(f"Authority '{authority.name}' {state}.", "info")
    return redirect(url_for("admin_authorities"))


@app.route("/admin/authorities/<int:authority_id>/delete", methods=["POST"])
@login_required
def admin_authority_delete(authority_id):
    guard = _require_admin()
    if guard:
        return guard

    authority = db.session.get(Authority, authority_id)
    if authority is None:
        abort(404)

    name = authority.name
    db.session.delete(authority)
    db.session.commit()
    logger.info(f"Authority deleted: {name} (id={authority_id}) by '{current_user.username}'.")
    flash(f"Authority '{name}' deleted.", "info")
    return redirect(url_for("admin_authorities"))


# ---------------------------------------------------------------------------
# ERROR HANDLERS
# ---------------------------------------------------------------------------
@app.errorhandler(404)
def not_found(_error):
    return render_template("404.html"), 404


@app.errorhandler(500)
def server_error(_error):
    db.session.rollback()
    return render_template("500.html"), 500


@app.errorhandler(413)
def file_too_large(_error):
    flash("File is too large. Maximum upload size is 500 MB.", "danger")
    return redirect(url_for("upload_video"))


if __name__ == "__main__":
    # Development server only. For production use waitress/gunicorn (see README).
    run_kwargs = {"debug": config.DEBUG, "host": config.HOST, "port": config.PORT}

    if config.DEBUG and config.HOST not in ("127.0.0.1", "localhost"):
        # Werkzeug's debugger serves an interactive Python console on error
        # pages. On 0.0.0.0 that is remote code execution for anyone on the
        # same network. Warned about rather than blocked, because binding
        # wide is exactly what a phone demo needs - but nobody should do it
        # with the debugger live without being told.
        logger.error(
            "UNSAFE: FLASK_DEBUG is on and HOST is %s, so the Werkzeug "
            "debugger console is reachable from the network. Set "
            "FLASK_DEBUG=false in .env before letting anyone else connect.",
            config.HOST,
        )

    if config.SSL_ADHOC:
        # Self-signed HTTPS so the officer PWA gets the secure context it
        # needs for geolocation and service workers on a phone. See
        # config.SSL_ADHOC and README section 5.3.
        try:
            import cryptography  # noqa: F401
        except ImportError:
            logger.error(
                "SSL_ADHOC=true but the 'cryptography' package is missing. "
                "Run: pip install cryptography   (or pip install pywebpush, "
                "which includes it). Starting over plain HTTP instead - the "
                "officer app will NOT be able to read location on a phone."
            )
        else:
            run_kwargs["ssl_context"] = "adhoc"
            logger.warning(
                "Serving HTTPS with a self-signed certificate. Your browser "
                "will warn about it; that is expected for a LAN demo."
            )
    app.run(**run_kwargs)
