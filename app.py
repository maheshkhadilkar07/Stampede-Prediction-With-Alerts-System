import json
import os
import threading
import time

from utils.notifier import notify_admins_async
from utils.otp_service import (
    generate_and_send_login_otp, verify_login_otp, seconds_until_resend_allowed,
    generate_and_send_otp, verify_otp_code,
)
from utils.forms import OtpForm

from flask import (
    Flask, render_template, redirect, session, url_for, flash, request,
    jsonify, send_from_directory, abort, Response,
)
from flask_login import (
    LoginManager, login_user, logout_user, login_required, current_user,
)

import config
from database.database import db, init_db
from database.models import User, Video, Alert
from database import history
from utils.forms import LoginForm, RegisterForm, UploadVideoForm
from utils.helpers import (
    allowed_video_file, generate_unique_filename,
    safe_join_upload_path, safe_join_processed_path,
)
from utils.logger import get_logger
from utils.report_generator import generate_video_report_pdf
from detector.video_processor import VideoProcessor
from streaming.stream_manager import LiveStreamManager

logger = get_logger(__name__)

app = Flask(__name__)
app.config["SECRET_KEY"] = config.SECRET_KEY
app.config["MAX_CONTENT_LENGTH"] = config.MAX_CONTENT_LENGTH
app.config["SQLALCHEMY_DATABASE_URI"] = config.SQLALCHEMY_DATABASE_URI
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = config.SQLALCHEMY_TRACK_MODIFICATIONS

init_db(app)

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"
login_manager.login_message = "Please log in to access the dashboard."
login_manager.login_message_category = "info"


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


_processing_lock = threading.Lock()


live_manager = LiveStreamManager()


def _live_alert_callback(level: str, message: str) -> None:
    """
    Called from LiveStreamManager's background thread whenever the live
    risk level escalates into HIGH/CRITICAL. Runs outside a Flask request,
    so it needs its own application context to touch the database.
    """
    with app.app_context():
        if live_manager.video_id is None:
            return
        video = db.session.get(Video, live_manager.video_id)
        if video is None:
            return
        alert = Alert(video_id=video.id, risk_level=level, message=message)
        db.session.add(alert)
        db.session.commit()
        logger.warning(f"LIVE ALERT [{level}] video_id={video.id}: {message}")
        notify_admins_async(video.id, level, message)


live_manager.on_alert = _live_alert_callback


@app.route("/")
def index():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))

    form = LoginForm()
    if form.validate_on_submit():
        user = User.query.filter_by(username=form.username.data.strip()).first()
        if user and user.check_password(form.password.data):
            if not user.is_verified:
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
                return redirect(next_page or url_for("dashboard"))

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

@app.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))

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
            return redirect(next_page or url_for("dashboard"))

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


@app.route("/logout")
@login_required
def logout():
    logger.info(f"User '{current_user.username}' logged out.")
    logout_user()
    return redirect(url_for("login"))


@app.route("/dashboard")
@login_required
def dashboard():
    summary = history.get_dashboard_summary()
    recent_videos = history.get_recent_videos(limit=10)
    return render_template("dashboard.html", summary=summary, recent_videos=recent_videos)



@app.route("/upload", methods=["GET", "POST"])
@login_required
def upload_video():
    form = UploadVideoForm()
    if form.validate_on_submit():
        file = form.video_file.data
        original_filename = file.filename

        if not allowed_video_file(original_filename):
            flash("Unsupported video format.", "danger")
            return redirect(url_for("upload_video"))

        stored_filename = generate_unique_filename(original_filename)
        input_path = safe_join_upload_path(stored_filename)
        file.save(input_path)

        video = Video(
            user_id=current_user.id,
            original_filename=original_filename,
            stored_filename=stored_filename,
            source_type="upload",
            processing_status="pending",
        )
        db.session.add(video)
        db.session.commit()

        logger.info(f"Video uploaded: {original_filename} -> {stored_filename} (id={video.id})")

        
        thread = threading.Thread(target=_process_video_job, args=(video.id, input_path))
        thread.daemon = True
        thread.start()

        return redirect(url_for("video_status", video_id=video.id))

    return render_template("upload.html", form=form)


def _process_video_job(video_id: int, input_path: str) -> None:
    
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
            with _processing_lock:
                processor = VideoProcessor()
                result = processor.process(input_path, output_path)

            video.processed_filename = output_filename
            video.frame_width = result["frame_width"]
            video.frame_height = result["frame_height"]
            video.total_frames = result["total_frames"]
            video.source_fps = result["source_fps"]
            video.processing_time_seconds = result["processing_time_seconds"]
            video.peak_count = result["peak_count"]
            video.average_count = result["average_count"]
            video.final_risk_level = result["final_risk"]["level"]
            video.set_risk_timeline(result["risk_timeline"])
            video.processing_status = "completed"
            db.session.commit()

            history.create_alerts_from_timeline(video)
            logger.info(f"Video {video_id} processed successfully.")

            if video.final_risk_level in config.ALERT_NOTIFY_LEVELS:
                notify_admins_async(
                    video.id, video.final_risk_level,
                    "Critical crowd risk detected in uploaded video analysis.",
                )

        except Exception as exc:  
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

   
    video = Video(
        user_id=current_user.id,
        original_filename=f"Live CCTV ({source_type})",
        stored_filename=f"live_{int(time.time())}",
        source_type="cctv",
        processing_status="processing",
    )
    db.session.add(video)
    db.session.commit()

    result = live_manager.start(source_type, source_value)

    if not result["started"]:
        video.processing_status = "failed"
        video.error_message = result.get("reason", "Unknown error starting live session.")
        db.session.commit()
        flash(f"Could not start live session: {video.error_message}", "danger")
        return redirect(url_for("live_view"))

    live_manager.video_id = video.id

    if result.get("fluvio_active"):
        flash("Live session started using Fluvio streaming.", "success")
    

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
    alert_rows = history.get_recent_alerts(limit=100)
    return render_template("alerts.html", alerts=alert_rows)


# ---------------------------------------------------------------------------
# ADMIN PANEL
# ---------------------------------------------------------------------------
@app.route("/admin")
@login_required
def admin_panel():
    if not current_user.is_admin:
        flash("Admin access required.", "danger")
        return redirect(url_for("dashboard"))

    users = User.query.order_by(User.created_at.desc()).all()
    videos = Video.query.order_by(Video.upload_time.desc()).all()
    return render_template("admin_panel.html", users=users, videos=videos)


# ---------------------------------------------------------------------------
# ERROR HANDLERS
# ---------------------------------------------------------------------------
@app.errorhandler(404)
def not_found(_error):
    return render_template("base.html"), 404


@app.errorhandler(413)
def file_too_large(_error):
    flash("File is too large. Maximum upload size is 500 MB.", "danger")
    return redirect(url_for("upload_video"))


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
