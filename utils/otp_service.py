"""
utils/otp_service.py
=======================
Generates, emails, and verifies one-time codes for two purposes:
    "login"    — email-based 2FA after a correct password (existing)
    "register" — confirms the person owns the email they registered with

Both share the same OtpCode table/logic; only the email subject/body
text and which session key holds the pending user_id differ, handled
by app.py's routes.

Save this file at: Stampede-Prediction-System/utils/otp_service.py
"""

import secrets
from datetime import datetime, timedelta

from werkzeug.security import generate_password_hash, check_password_hash

import config
from database.database import db
from database.models import OtpCode, User
from utils.logger import get_logger
from utils.mailer import send_email, smtp_configured

logger = get_logger(__name__)

_SUBJECTS = {
    "login": "Your StampedeGuard login code",
    "register": "Verify your StampedeGuard account",
}

_BODIES = {
    "login": (
        "Your one-time login code is: {code}\n\n"
        "This code expires in {minutes} minutes. "
        "If you did not attempt to log in, you can ignore this email."
    ),
    "register": (
        "Welcome to StampedeGuard! Your email verification code is: {code}\n\n"
        "Enter this code to confirm your account. It expires in {minutes} minutes. "
        "If you did not create this account, you can ignore this email."
    ),
}


def _generate_code() -> str:
    """Cryptographically random numeric code, e.g. '042819'. Leading zeros allowed."""
    return "".join(secrets.choice("0123456789") for _ in range(config.OTP_LENGTH))


def _invalidate_existing_codes(user_id: int, purpose: str) -> None:
    """Supersede any still-valid codes for this user/purpose (e.g. on resend)."""
    OtpCode.query.filter_by(user_id=user_id, purpose=purpose, consumed=False).update(
        {"consumed": True}
    )


def seconds_until_resend_allowed(user: User, purpose: str = "login") -> int:
    """
    Returns 0 if a new OTP can be sent right now for this purpose,
    otherwise the number of seconds the user must still wait.
    """
    latest = (
        OtpCode.query.filter_by(user_id=user.id, purpose=purpose)
        .order_by(OtpCode.created_at.desc())
        .first()
    )
    if latest is None:
        return 0
    elapsed = (datetime.utcnow() - latest.created_at).total_seconds()
    remaining = config.OTP_RESEND_COOLDOWN_SECONDS - elapsed
    return max(0, int(remaining))


def generate_and_send_otp(user: User, purpose: str = "login") -> dict:
    """
    Creates a new OTP for `user` under the given purpose, invalidating
    any previous unconsumed code for that same purpose, and sends it by
    email (or logs it, in the DEBUG-only dev fallback when SMTP isn't
    configured).

    Returns {"sent": bool, "dev_code": str|None, "wait_seconds": int}.
    """
    wait = seconds_until_resend_allowed(user, purpose)
    if wait > 0:
        return {"sent": False, "dev_code": None, "wait_seconds": wait}

    _invalidate_existing_codes(user.id, purpose)

    code = _generate_code()
    otp_row = OtpCode(
        user_id=user.id,
        code_hash=generate_password_hash(code),
        purpose=purpose,
        expires_at=datetime.utcnow() + timedelta(seconds=config.OTP_EXPIRY_SECONDS),
    )
    db.session.add(otp_row)
    db.session.commit()

    minutes = config.OTP_EXPIRY_SECONDS // 60
    subject = _SUBJECTS.get(purpose, "Your StampedeGuard verification code")
    body_template = _BODIES.get(purpose, "Your verification code is: {code}\n\nExpires in {minutes} minutes.")
    body = body_template.format(code=code, minutes=minutes)

    if smtp_configured():
        sent = send_email(subject, body, [user.email])
        if sent:
            logger.info(f"{purpose} OTP emailed to user_id={user.id}.")
            return {"sent": True, "dev_code": None, "wait_seconds": 0}
        logger.error(f"{purpose} OTP email FAILED to send for user_id={user.id}.")
        return {"sent": False, "dev_code": None, "wait_seconds": 0}

    if config.DEBUG:
        logger.warning(
            f"[DEV MODE — SMTP not configured] {purpose} OTP for user_id={user.id} "
            f"('{user.username}'): {code}"
        )
        return {"sent": True, "dev_code": code, "wait_seconds": 0}

    logger.error(
        f"Cannot send {purpose} OTP for user_id={user.id}: SMTP is not configured "
        "and DEBUG is False. Configure SMTP_HOST/SMTP_USERNAME/SMTP_PASSWORD in .env."
    )
    return {"sent": False, "dev_code": None, "wait_seconds": 0}


def verify_otp_code(user_id: int, submitted_code: str, purpose: str = "login") -> dict:
    """
    Checks `submitted_code` against the latest unconsumed OTP for this
    user AND purpose. Returns {"ok": bool, "reason": str} where reason
    is one of: "success", "no_code", "expired", "max_attempts", "incorrect".
    """
    otp_row = (
        OtpCode.query.filter_by(user_id=user_id, purpose=purpose, consumed=False)
        .order_by(OtpCode.created_at.desc())
        .first()
    )
    if otp_row is None:
        return {"ok": False, "reason": "no_code"}

    if datetime.utcnow() > otp_row.expires_at:
        otp_row.consumed = True
        db.session.commit()
        return {"ok": False, "reason": "expired"}

    if otp_row.attempts >= config.OTP_MAX_ATTEMPTS:
        otp_row.consumed = True
        db.session.commit()
        return {"ok": False, "reason": "max_attempts"}

    if check_password_hash(otp_row.code_hash, submitted_code.strip()):
        otp_row.consumed = True
        db.session.commit()
        return {"ok": True, "reason": "success"}

    otp_row.attempts += 1
    db.session.commit()
    return {"ok": False, "reason": "incorrect"}


# ---------------------------------------------------------------------------
# Backward-compatible wrappers (your existing app.py login routes already
# import these two exact names — keeping them means you do NOT need to
# touch your existing /login or /login/verify routes at all).
# ---------------------------------------------------------------------------
def generate_and_send_login_otp(user: User) -> dict:
    return generate_and_send_otp(user, purpose="login")


def verify_login_otp(user_id: int, submitted_code: str) -> dict:
    return verify_otp_code(user_id, submitted_code, purpose="login")