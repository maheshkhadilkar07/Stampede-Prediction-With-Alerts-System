"""
utils/mailer.py
==================
Single low-level SMTP send function used by the OTP login flow (and,
if you already have utils/email_alert.py, that file can reuse this too).

Save this file at: Stampede-Prediction-System/utils/mailer.py
"""

import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import config
from utils.logger import get_logger

logger = get_logger(__name__)


def smtp_configured() -> bool:
    """True if enough SMTP credentials are present in .env to attempt a send."""
    return bool(config.SMTP_HOST and config.SMTP_USERNAME and config.SMTP_PASSWORD)


def send_email(subject: str, body_text: str, recipients: list) -> bool:
    """
    Send a plain-text email to one or more recipients. Returns True only
    if the send actually succeeded. Never raises — callers (background
    threads, the login route) must not crash because an email failed.
    """
    if not recipients:
        logger.warning("send_email() called with no recipients — skipping.")
        return False

    if not smtp_configured():
        logger.warning(
            "SMTP not configured (SMTP_HOST/SMTP_USERNAME/SMTP_PASSWORD missing "
            "from .env) — cannot send email."
        )
        return False

    msg = MIMEMultipart()
    msg["Subject"] = subject
    msg["From"] = config.ALERT_EMAIL_FROM
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(body_text, "plain"))

    try:
        with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=10) as server:
            if config.SMTP_USE_TLS:
                server.starttls()
            server.login(config.SMTP_USERNAME, config.SMTP_PASSWORD)
            server.sendmail(config.ALERT_EMAIL_FROM, recipients, msg.as_string())
        logger.info(f"Email sent to {', '.join(recipients)}: {subject}")
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error(f"Failed to send email to {', '.join(recipients)}: {exc}")
        return False