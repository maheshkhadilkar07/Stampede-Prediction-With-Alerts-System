"""
utils/timeutils.py
====================
All timestamps are stored in UTC (datetime.utcnow). These helpers convert
them to config.APP_TIMEZONE for display and compute "today" boundaries in
that timezone. If the timezone database is unavailable (e.g. Windows without
the `tzdata` package) everything falls back to UTC and says so.

Save this file at: Stampede-Prediction-System/utils/timeutils.py
"""

from datetime import datetime, timezone

import config

try:
    from zoneinfo import ZoneInfo
    _TZ = ZoneInfo(config.APP_TIMEZONE)
    TZ_LABEL = config.APP_TIMEZONE
except Exception:  # noqa: BLE001 - missing tzdata / bad tz name
    _TZ = timezone.utc
    TZ_LABEL = "UTC"


def to_local(dt):
    """Naive-UTC datetime -> aware datetime in the display timezone (None-safe)."""
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc).astimezone(_TZ)


def fmt_local(dt, fmt="%d %b %Y, %H:%M:%S"):
    local = to_local(dt)
    return local.strftime(fmt) if local else "—"


def iso_utc(dt):
    """ISO-8601 string with an explicit Z so browsers parse it as UTC."""
    return dt.isoformat() + "Z" if dt else None


def local_day_start_utc():
    """Start of the current local day, expressed as a naive-UTC datetime."""
    now_local = datetime.now(_TZ)
    start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    return start_local.astimezone(timezone.utc).replace(tzinfo=None)
