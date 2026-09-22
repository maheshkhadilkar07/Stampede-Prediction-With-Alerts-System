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


def create_alerts_from_timeline(video: Video) -> None:
    """
    Scan a finished video's risk_timeline for HIGH/CRITICAL entries and
    persist one Alert row per escalation into that zone (not one row per
    second, otherwise a 30-second CRITICAL stretch would spam 30 alerts).
    """
    import config

    timeline = video.get_risk_timeline()
    previous_level = None

    for entry in timeline:
        level = entry["level"]
        if level in config.ALERT_TRIGGER_LEVELS and level != previous_level:
            alert = Alert(
                video_id=video.id,
                risk_level=level,
                message=(
                    f"{level.title()} risk detected at "
                    f"{entry['second']}s into '{video.original_filename}'."
                ),
            )
            db.session.add(alert)
        previous_level = level

    db.session.commit()
