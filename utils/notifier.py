"""
utils/notifier.py
==================
Sends CRITICAL/HIGH risk alerts to higher-authority (admin) users via
email (Gmail SMTP, free) and SMS (Fast2SMS quick-route, free signup
credits, India numbers).

Each channel is independently optional — if credentials aren't set in
config, that channel is skipped with a logged warning instead of
raising. Runs in a background thread so the detection pipeline (video
processing / live CCTV loop) never blocks on network I/O.
"""

import smtplib
import threading
from email.mime.text import MIMEText

import requests

import config
from utils.logger import get_logger

logger = get_logger(__name__)


def notify_admins_async(video_id, risk_level, message):
    """Fire-and-forget: notify all admin users in a background thread."""
    t = threading.Thread(target=_notify_admins, args=(video_id, risk_level, message))
    t.daemon = True
    t.start()


def _notify_admins(video_id, risk_level, message):
    if not config.ADMIN_ALERT_ENABLED or risk_level not in config.ALERT_NOTIFY_LEVELS:
        return

    from app import app  # local import — avoids circular import at module load
    from database.models import User

    with app.app_context():
        admins = User.query.filter_by(role="admin").all()
        if not admins:
            logger.warning("No admin users found to notify.")
            return

        subject = f"[StampedeGuard] {risk_level} risk alert (video/session #{video_id})"
        body = (
            f"Risk level: {risk_level}\n{message}\n\n"
            f"Open the dashboard to review this session."
        )

        emails = [a.email for a in admins if getattr(a, "email", None)]
        phones = [a.phone_number for a in admins if getattr(a, "phone_number", None)]

        if emails:
            _send_email(subject, body, emails)
        if phones:
            _send_sms(f"{subject}: {message}", phones)


def _send_email(subject, body, recipients):
    if not (config.SMTP_USERNAME and config.SMTP_PASSWORD):
        logger.warning("SMTP not configured — skipping email alert.")
        return
    try:
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = config.SMTP_FROM_EMAIL
        msg["To"] = ", ".join(recipients)
        with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT) as server:
            server.starttls()
            server.login(config.SMTP_USERNAME, config.SMTP_PASSWORD)
            server.sendmail(config.SMTP_FROM_EMAIL, recipients, msg.as_string())
        logger.info(f"Alert email sent to {len(recipients)} admin(s).")
    except Exception as exc:  # noqa: BLE001
        logger.exception(f"Failed to send alert email: {exc}")


def _send_sms(text, phone_numbers):
    if not config.FAST2SMS_API_KEY:
        logger.warning("Fast2SMS not configured — skipping SMS alert.")
        return
    try:
        resp = requests.post(
            "https://www.fast2sms.com/dev/bulkV2",
            headers={"authorization": config.FAST2SMS_API_KEY},
            data={
                "route": "q",
                "message": text[:160],
                "language": "english",
                "flash": 0,
                "numbers": ",".join(phone_numbers),
            },
            timeout=10,
        )
        resp.raise_for_status()
        logger.info(f"Alert SMS sent to {len(phone_numbers)} admin(s).")
    except Exception as exc:  # noqa: BLE001
        logger.exception(f"Failed to send alert SMS: {exc}")