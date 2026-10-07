"""
utils/email_alert.py
=======================
Sends stampede risk alert emails to "higher authority" recipients
whenever a HIGH or CRITICAL risk event is raised, from either Mode 1
(uploaded video) or Mode 2 (live CCTV). Recipients are:
    1. Every admin User account (unchanged from before Phase 2)
    2. Every ACTIVE Authority whose location_area matches the video's
       location_name (or has no location_area set, meaning "all zones")

The alert threshold itself is NOT changed by this module — it only
decides WHO gets notified once app.py/history.py has already decided
a HIGH/CRITICAL alert should fire.

Save this file at: Stampede-Prediction-System/utils/email_alert.py
"""

import time

import config
from database.models import User, Authority
from utils.logger import get_logger
from utils.mailer import send_email_per_recipient

logger = get_logger(__name__)

_last_sent_at = {}  # risk_level -> last-sent unix timestamp (process-local)


def _cooldown_elapsed(risk_level: str) -> bool:
    last = _last_sent_at.get(risk_level, 0)
    return (time.time() - last) >= config.ALERT_EMAIL_COOLDOWN_SECONDS


def get_alert_recipients(location_name: str = None) -> list:
    """
    Returns the full recipient list for a HIGH/CRITICAL alert at the
    given location (or None if the source has no location tag):
        [{"type": "admin"|"authority", "id": int, "name": str, "email": str}, ...]

    Zone matching: an Authority with a blank location_area is treated
    as "all zones" and always included. An Authority WITH a
    location_area is only included if it matches the video's
    location_name exactly (case-insensitive). If the video itself has
    no location_name, only "all zones" authorities are included —
    zone-specific responders are never paged for an unlocated source.
    """
    recipients = []

    admins = User.query.filter_by(role="admin").all()
    for admin in admins:
        if admin.email:
            recipients.append({"type": "admin", "id": admin.id, "name": admin.username, "email": admin.email})

    authorities = Authority.query.filter_by(is_active=True).all()
    normalized_location = (location_name or "").strip().lower()
    for authority in authorities:
        area = (authority.location_area or "").strip().lower()
        if not area or (normalized_location and area == normalized_location):
            recipients.append({
                "type": "authority", "id": authority.id,
                "name": authority.name, "email": authority.email,
            })

    return recipients


def send_alert_to_recipients(risk_level: str, alert_message: str, source_label: str,
                              location_name: str = None) -> list:
    """
    Sends a HIGH/CRITICAL alert email to every matching recipient
    (admins + location-matched active authorities), respecting the
    existing enable flag and cooldown window (UNCHANGED trigger logic
    — this function only runs once app.py/history.py already decided
    to fire an alert).

    Returns a list of per-recipient delivery results:
        [{"type", "id", "name", "email", "status", "error"}, ...]
    where status is "sent" or "failed". Returns [] if alerting is
    disabled or still within cooldown — callers should treat an empty
    list as "no delivery rows to create this time".
    """
    if risk_level not in config.ALERT_TRIGGER_LEVELS:
        return []

    if not config.ALERT_EMAIL_ENABLED:
        logger.info(f"Email alerts disabled; skipping {risk_level} notification.")
        return []

    if not _cooldown_elapsed(risk_level):
        logger.info(f"{risk_level} alert notification skipped — within cooldown window.")
        return []

    recipients = get_alert_recipients(location_name)
    if not recipients:
        logger.warning("No admin or matching authority recipients found — skipping alert notification.")
        return []

    subject, body = build_alert_email(risk_level, alert_message, source_label, location_name)

    emails = [r["email"] for r in recipients]
    send_results = send_email_per_recipient(subject, body, emails)

    delivery_results = []
    any_sent = False
    for r in recipients:
        outcome = send_results.get(r["email"], {"sent": False, "error": "No send attempt recorded."})
        status = "sent" if outcome["sent"] else "failed"
        any_sent = any_sent or outcome["sent"]
        delivery_results.append({
            "type": r["type"], "id": r["id"], "name": r["name"], "email": r["email"],
            "status": status, "error": outcome["error"],
        })

    if any_sent:
        _last_sent_at[risk_level] = time.time()

    sent_count = sum(1 for d in delivery_results if d["status"] == "sent")
    logger.warning(
        f"{risk_level} alert: {sent_count}/{len(delivery_results)} recipient notification(s) sent "
        f"({source_label})."
    )
    return delivery_results


def build_alert_email(risk_level: str, alert_message: str, source_label: str,
                      location_name: str = None) -> tuple:
    """Returns (subject, body) for a HIGH/CRITICAL alert email."""
    subject = f"\U0001F6A8 {risk_level} STAMPEDE RISK ALERT \u2014 {source_label}"
    body = "\n".join([
        f"Risk Level : {risk_level}",
        f"Source     : {source_label}",
        f"Location   : {location_name or 'Not specified'}",
        f"Details    : {alert_message}",
        "",
        "This is an automated alert from the AI-Based Real-Time Stampede",
        "Prediction and Alert System. Please review the live dashboard",
        "immediately and take appropriate crowd-safety action.",
    ])
    return subject, body


def retry_failed_deliveries(alert, db) -> dict:
    """
    Re-send an alert's email to every recipient whose delivery FAILED and
    update those AlertDelivery rows in place. Admin-triggered from the Alert
    History page. Returns {"retried": n, "sent": n, "failed": n}.
    """
    from datetime import datetime

    failed = [d for d in alert.deliveries if d.status == "failed"]
    if not failed:
        return {"retried": 0, "sent": 0, "failed": 0}

    video = alert.video
    source_label = (
        f"Live CCTV" if video.source_type == "cctv" else f"Uploaded Video: {video.original_filename}"
    )
    subject, body = build_alert_email(alert.risk_level, alert.message, source_label, video.location_name)
    outcomes = send_email_per_recipient(subject, body, [d.recipient_email for d in failed])

    sent = 0
    for d in failed:
        outcome = outcomes.get(d.recipient_email, {"sent": False, "error": "No send attempt recorded."})
        d.status = "sent" if outcome["sent"] else "failed"
        d.error_message = outcome["error"]
        d.sent_at = datetime.utcnow()
        sent += 1 if outcome["sent"] else 0
    alert.notified = any(d.status == "sent" for d in alert.deliveries)
    db.session.commit()
    return {"retried": len(failed), "sent": sent, "failed": len(failed) - sent}
