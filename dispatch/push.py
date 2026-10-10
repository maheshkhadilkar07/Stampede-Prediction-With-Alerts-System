"""
dispatch/push.py
==================
Web Push delivery to a field officer's devices.

Why Web Push and not Firebase: the officer app is a PWA served by this same
Flask process, and Web Push with VAPID needs no third-party account, no SDK
and no client secret - just a key pair generated once (see
scripts/generate_vapid_keys.py). The browser's own push service does the
relaying, and because the payload is encrypted against the device's public
key, that service cannot read the incident details passing through it.

Everything here degrades instead of failing. If pywebpush is not installed,
or the VAPID keys were never generated, push is quietly disabled and the
officer page falls back to polling while it is open. That matters because a
dispatch system that refuses to start because of a missing optional
dependency is worse than one that pages a little less conveniently - and it
mirrors how this project already treats the optional Fluvio client.

Save this file at: Stampede-Prediction-System/dispatch/push.py
"""

import json
import threading
from datetime import datetime

import config
from utils.logger import get_logger

logger = get_logger(__name__)

# pywebpush is optional. Importing it defensively means `pip install -r
# requirements.txt` having failed on this one package cannot take the whole
# app down - the import of this module must never raise.
try:
    from pywebpush import webpush, WebPushException
    _PYWEBPUSH_AVAILABLE = True
except ImportError:  # pragma: no cover - depends on the local environment
    webpush = None
    WebPushException = Exception
    _PYWEBPUSH_AVAILABLE = False


def push_is_configured() -> bool:
    """True only if we can actually send a push right now."""
    return bool(
        _PYWEBPUSH_AVAILABLE
        and config.VAPID_PUBLIC_KEY
        and config.VAPID_PRIVATE_KEY
    )


def push_unavailable_reason() -> str:
    """A specific, actionable explanation for the admin screens."""
    if not _PYWEBPUSH_AVAILABLE:
        return (
            "The pywebpush package is not installed. Run "
            "'pip install pywebpush' to enable background alerts."
        )
    if not config.VAPID_PUBLIC_KEY or not config.VAPID_PRIVATE_KEY:
        return (
            "VAPID keys are not set. Run 'python scripts/generate_vapid_keys.py' "
            "and copy both values into your .env file."
        )
    return ""


# ---------------------------------------------------------------------------
# SENDING
# ---------------------------------------------------------------------------
def notify_officer_async(officer_id: int, payload: dict) -> None:
    """
    Fire a push to every device belonging to one officer, off-thread.

    Dispatch decisions are made inside a lock held by the engine, and a push
    service round-trip can take seconds. Blocking there would stall every
    other dispatch in the system behind one slow network call, so delivery is
    handed to a daemon thread and the engine moves on. The countdown the
    officer sees is driven by expires_at in the database, not by when the
    push happens to land, so a slow push shortens their window rather than
    corrupting the state machine.
    """
    if not push_is_configured():
        logger.debug("Push not configured; skipping push for officer %s.", officer_id)
        return

    thread = threading.Thread(
        target=_send_to_officer, args=(officer_id, payload), daemon=True,
    )
    thread.start()


def _send_to_officer(officer_id: int, payload: dict) -> None:
    """Thread body: needs its own app context to touch the database."""
    # Imported here rather than at module scope because app.py imports this
    # module, so a top-level `from app import app` would be a circular import.
    # This is the same pattern utils/notifier.py already uses.
    from app import app

    with app.app_context():
        from database.database import db
        from database.models import PushSubscription

        subscriptions = PushSubscription.query.filter_by(user_id=officer_id).all()
        if not subscriptions:
            logger.info("Officer %s has no registered push devices.", officer_id)
            return

        dead = []
        delivered = 0
        for subscription in subscriptions:
            ok, gone = _deliver(subscription, payload)
            if ok:
                delivered += 1
                subscription.last_used_at = datetime.utcnow()
            if gone:
                dead.append(subscription)

        # A browser that has been uninstalled or had its permission revoked
        # returns 404/410 forever. Pruning those keeps the table honest and
        # stops us retrying endpoints that will never work again.
        for subscription in dead:
            logger.info("Removing expired push endpoint for officer %s.", officer_id)
            db.session.delete(subscription)

        db.session.commit()
        logger.info(
            "Push to officer %s: %s/%s devices reached.",
            officer_id, delivered, len(subscriptions),
        )


def _deliver(subscription, payload: dict):
    """
    Send to one device.

    Returns (delivered, subscription_is_gone). The second flag is what lets
    the caller distinguish "this device is dead, forget it" from "the network
    hiccupped, keep the registration".
    """
    try:
        webpush(
            subscription_info=subscription.to_web_push_dict(),
            data=json.dumps(payload),
            vapid_private_key=config.VAPID_PRIVATE_KEY,
            vapid_claims={"sub": config.VAPID_SUBJECT},
            ttl=config.PUSH_TTL_SECONDS,
            # urgency=high asks the push service not to batch or delay this
            # for battery saving. It is exactly what the header is for.
            headers={"Urgency": "high"},
        )
        return True, False

    except WebPushException as exc:  # pragma: no cover - network dependent
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status in (404, 410):
            return False, True
        logger.warning(
            "Push failed for officer %s (status=%s): %s",
            subscription.user_id, status, exc,
        )
        return False, False

    except Exception as exc:  # noqa: BLE001 - a push must never crash a thread
        logger.warning("Unexpected push error for officer %s: %s", subscription.user_id, exc)
        return False, False


# ---------------------------------------------------------------------------
# PAYLOADS
# ---------------------------------------------------------------------------
def build_offer_payload(dispatch, offer, distance_text, eta_minutes) -> dict:
    """
    The notification an officer sees when an incident is offered to them.

    The body leads with distance because that is the one fact that decides
    whether they can take it. `tag` is keyed to the dispatch so a re-push for
    the same incident REPLACES the previous notification instead of stacking
    a second copy on the lock screen, and `renotify` makes that replacement
    still buzz. `requireInteraction` stops the OS auto-dismissing it after a
    few seconds, since this is a decision, not an FYI.
    """
    where = dispatch.location_name or "an unnamed camera location"
    eta_text = f" - about {eta_minutes} min out" if eta_minutes else ""

    return {
        "type": "dispatch_offer",
        "title": f"{dispatch.risk_level} crowd risk - {where}",
        "body": f"{distance_text} away{eta_text}. Tap to accept or pass.",
        "tag": f"dispatch-{dispatch.id}",
        "renotify": True,
        "requireInteraction": True,
        "dispatch_id": dispatch.id,
        "offer_id": offer.id,
        "risk_level": dispatch.risk_level,
        "seconds_remaining": offer.seconds_remaining,
        "url": "/officer/",
    }


def build_cancel_payload(dispatch_id: int, reason: str) -> dict:
    """
    Sent when an offer is no longer actionable - someone else took it, or an
    admin closed the incident. Without this, a stale notification sits on the
    lock screen inviting an officer to accept something that no longer exists.
    """
    return {
        "type": "dispatch_cancelled",
        "title": "Assignment closed",
        "body": reason,
        "tag": f"dispatch-{dispatch_id}",
        "dispatch_id": dispatch_id,
        "url": "/officer/",
    }
