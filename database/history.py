"""
database/history.py
=====================
Read-oriented query helpers used by the Flask routes (dashboard,
alerts, analytics). Keeping these here means app.py never writes raw
SQLAlchemy queries inline, which keeps routes short and testable.

Save this file at: Stampede-Prediction-System/database/history.py
"""

from datetime import datetime, timedelta

from database.database import db
from database.models import Video, Alert


def get_recent_videos(limit: int = 10):
    """Most recently uploaded videos, newest first."""
    return Video.query.order_by(Video.upload_time.desc()).limit(limit).all()


def get_dashboard_summary() -> dict:
    """
    Build the aggregate stats dict consumed by templates/dashboard.html
    (summary.current_person_count, summary.current_risk_level, etc.).
    """
    latest_completed = (
        Video.query.filter_by(processing_status="completed")
        .order_by(Video.upload_time.desc())
        .first()
    )

    total_videos = Video.query.count()
    peak_person_count = db.session.query(db.func.max(Video.peak_count)).scalar() or 0

    since = datetime.utcnow() - timedelta(hours=24)
    recent_alerts_count = Alert.query.filter(Alert.created_at >= since).count()

    if latest_completed:
        current_person_count = latest_completed.peak_count or 0
        # Reuse average_count as a simple "density score" proxy for the card.
        current_density_score = round(latest_completed.average_count or 0.0, 2)
        current_risk_level = latest_completed.final_risk_level or "SAFE"
    else:
        current_person_count = 0
        current_density_score = 0.0
        current_risk_level = "SAFE"

    return {
        "current_person_count": current_person_count,
        "current_density_score": current_density_score,
        "current_risk_level": current_risk_level,
        "recent_alerts_count": recent_alerts_count,
        "total_videos": total_videos,
        "peak_person_count": peak_person_count,
    }


def get_recent_alerts(limit: int = 50):
    """Most recent alerts across all videos, newest first."""
    return Alert.query.order_by(Alert.created_at.desc()).limit(limit).all()


def get_filtered_alerts(risk=None, date_from=None, date_to=None, location=None,
                         delivery_status=None, limit: int = 200):
    """
    Alert History page query helper. All filters are optional and
    combine with AND. delivery_status ('sent'|'failed'|'pending')
    filters to alerts that have AT LEAST ONE recipient row with that
    status — matches the intuitive "show me alerts with a failure"
    use case rather than requiring ALL recipients to match.
    """
    from database.models import AlertDelivery

    query = Alert.query.join(Video, Alert.video_id == Video.id)

    if risk:
        query = query.filter(Alert.risk_level == risk)
    if date_from:
        query = query.filter(Alert.created_at >= date_from)
    if date_to:
        query = query.filter(Alert.created_at <= date_to)
    if location:
        query = query.filter(Video.location_name == location)
    if delivery_status:
        query = query.join(AlertDelivery, AlertDelivery.alert_id == Alert.id).filter(
            AlertDelivery.status == delivery_status
        ).distinct()

    return query.order_by(Alert.created_at.desc()).limit(limit).all()


def get_alert_location_options():
    """Distinct, non-empty location names — populates the Alert History location filter dropdown."""
    rows = db.session.query(Video.location_name).filter(Video.location_name.isnot(None)).distinct().all()
    return sorted({r[0] for r in rows if r[0]})


def create_alerts_from_timeline(video: Video) -> None:
    """
    Scan a finished video's risk_timeline for HIGH/CRITICAL entries and
    persist one Alert row per escalation into that zone (not one row per
    second, otherwise a 30-second CRITICAL stretch would spam 30 alerts).
    Each new alert also notifies admins + location-matched active
    authorities (see utils/email_alert.py) and records one AlertDelivery
    row per recipient, exactly like the live-CCTV alert path.

    Any HIGH/CRITICAL alert also raises an officer dispatch, but only after
    the commit below - officers are never paged about an incident that
    failed to save.
    """
    import config
    from database.models import AlertDelivery
    from utils.email_alert import send_alert_to_recipients

    timeline = video.get_risk_timeline()
    previous_level = None
    dispatchable_alert_ids = []

    for entry in timeline:
        level = entry["level"]
        if level in config.ALERT_TRIGGER_LEVELS and level != previous_level:
            message = (
                f"{level.title()} risk detected at "
                f"{entry['second']}s into '{video.original_filename}'."
            )
            alert = Alert(
                video_id=video.id, risk_level=level, message=message,
                person_count=entry.get("count", video.peak_count),
                max_cell_density=entry.get("density"),
                growth_rate=entry.get("growth_rate"),
            )
            db.session.add(alert)
            db.session.flush()  # assign alert.id before the email/commit

            source_label = f"Uploaded Video: {video.original_filename}"
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

            if level in config.DISPATCH_TRIGGER_LEVELS:
                dispatchable_alert_ids.append(alert.id)

        previous_level = level

    db.session.commit()

    _dispatch_for_alerts(dispatchable_alert_ids)


def _dispatch_for_alerts(alert_ids) -> None:
    """
    Page field officers for alerts that have already been committed.

    The engine is imported here rather than at module scope because
    dispatch.engine imports models, which import this package - a top-level
    import would make the cycle bite on a fresh interpreter. Failures are
    swallowed and logged: the alerts are already saved and emailed, so a
    dispatch problem must not turn a successful analysis into an error.
    """
    if not alert_ids:
        return

    from dispatch.engine import dispatch_engine

    for alert_id in alert_ids:
        try:
            dispatch_id = dispatch_engine.create_dispatch_for_alert(alert_id)
            if dispatch_id:
                from utils.logger import get_logger
                get_logger(__name__).warning(
                    f"Dispatch #{dispatch_id} raised for alert {alert_id}."
                )
        except Exception as exc:  # noqa: BLE001
            db.session.rollback()
            from utils.logger import get_logger
            get_logger(__name__).error(
                f"Dispatch creation failed for alert {alert_id}: {exc}"
            )


def get_admin_overview(active_incidents: int) -> dict:
    """
    Real, database-driven numbers for the Admin Dashboard cards. Nothing
    here is hardcoded. `active_incidents` is computed by app.py (it needs
    the live-session state); everything else comes straight from the DB.
    "Today" means the current day in config.APP_TIMEZONE.
    """
    from database.models import Authority, AlertDelivery, ContactMessage
    from utils.timeutils import local_day_start_utc

    day_start = local_day_start_utc()
    return {
        "total_authorities": Authority.query.count(),
        "active_authorities": Authority.query.filter_by(is_active=True).count(),
        "active_incidents": active_incidents,
        "alerts_today": Alert.query.filter(Alert.created_at >= day_start).count(),
        "notifications_sent": AlertDelivery.query.filter_by(status="sent").count(),
        "notifications_failed": AlertDelivery.query.filter_by(status="failed").count(),
        "notifications_sent_today": AlertDelivery.query.filter(
            AlertDelivery.status == "sent", AlertDelivery.sent_at >= day_start).count(),
        "notifications_failed_today": AlertDelivery.query.filter(
            AlertDelivery.status == "failed", AlertDelivery.sent_at >= day_start).count(),
        "unread_messages": ContactMessage.query.filter_by(is_read=False).count(),
    }


def get_authority_alert_counts() -> dict:
    """{authority_id: number of alert notifications ever addressed to them}."""
    from database.models import AlertDelivery
    rows = (
        db.session.query(AlertDelivery.recipient_id, db.func.count(AlertDelivery.id))
        .filter(AlertDelivery.recipient_type == "authority")
        .group_by(AlertDelivery.recipient_id).all()
    )
    return {rid: n for rid, n in rows}


def get_alert_counts_by_level(days: int = 7) -> dict:
    """Alert totals per risk level over the last N days (dashboard chart)."""
    since = datetime.utcnow() - timedelta(days=days)
    rows = (
        db.session.query(Alert.risk_level, db.func.count(Alert.id))
        .filter(Alert.created_at >= since).group_by(Alert.risk_level).all()
    )
    return {level: n for level, n in rows}
