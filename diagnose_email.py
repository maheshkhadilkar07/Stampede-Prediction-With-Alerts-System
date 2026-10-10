"""
diagnose_email.py
=================
One-command email diagnosis. Run from the project root, with your venv active:

    python diagnose_email.py                 # sends a test to SMTP_USERNAME
    python diagnose_email.py you@example.com # sends a test to that address

It prints your SMTP settings (password masked), tries a REAL send the same way
the app does, shows the EXACT reason if it fails, and lists who is configured to
receive alerts. No web server needed.

Nothing here is a substitute for valid credentials: if the send fails with
"AUTH FAILED", the fix is a NEW Gmail App Password in .env — not a code change.
"""

import os
import sys
import sqlite3
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import config  # loads .env via load_dotenv(override=True)

HERE = os.path.dirname(os.path.abspath(__file__))


def mask(secret):
    if not secret:
        return "(EMPTY)"
    s = str(secret)
    return (s[:2] + "…" + s[-2:] + f"  (length {len(s)})") if len(s) > 4 else "(set)"


def main():
    print("=" * 68)
    print(" STAMPEDEGUARD — EMAIL DIAGNOSIS")
    print("=" * 68)
    print(f"ALERT_EMAIL_ENABLED : {config.ALERT_EMAIL_ENABLED}")
    print(f"TWO_FACTOR_ENABLED  : {config.TWO_FACTOR_ENABLED}")
    print(f"SMTP_HOST           : {config.SMTP_HOST or '(EMPTY)'}")
    print(f"SMTP_PORT           : {config.SMTP_PORT}")
    print(f"SMTP_USE_TLS        : {config.SMTP_USE_TLS}")
    print(f"SMTP_USERNAME       : {config.SMTP_USERNAME or '(EMPTY)'}")
    print(f"SMTP_PASSWORD       : {mask(config.SMTP_PASSWORD)}")
    print(f"ALERT_EMAIL_FROM    : {config.ALERT_EMAIL_FROM or '(EMPTY)'}")

    problems = []
    if not config.SMTP_HOST:
        problems.append("SMTP_HOST is empty")
    if not config.SMTP_USERNAME:
        problems.append("SMTP_USERNAME is empty")
    if not config.SMTP_PASSWORD:
        problems.append("SMTP_PASSWORD is empty")
    if not config.ALERT_EMAIL_ENABLED:
        problems.append("ALERT_EMAIL_ENABLED is false — set ALERT_EMAIL_ENABLED=true in .env")
    if problems:
        print("\nCONFIG ISSUES:")
        for p in problems:
            print("   -", p)

    if not (config.SMTP_HOST and config.SMTP_USERNAME and config.SMTP_PASSWORD):
        print("\n>> SMTP is NOT fully configured. Fill in the SMTP_* values in .env and re-run.")
        _print_recipients()
        return 1

    target = sys.argv[1] if len(sys.argv) > 1 else config.SMTP_USERNAME
    print(f"\nTrying a REAL send to: {target} ...")
    try:
        msg = MIMEMultipart()
        msg["Subject"] = "StampedeGuard test email"
        msg["From"] = config.ALERT_EMAIL_FROM
        msg["To"] = target
        msg.attach(MIMEText(
            "If you can read this, your SMTP settings work and alerts can be sent.", "plain"))
        with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=20) as server:
            server.ehlo()
            if config.SMTP_USE_TLS:
                server.starttls()
                server.ehlo()
            server.login(config.SMTP_USERNAME, config.SMTP_PASSWORD)
            server.sendmail(config.ALERT_EMAIL_FROM, [target], msg.as_string())
        print("\n  RESULT: ✅ SUCCESS — email accepted by the server.")
        print(f"  Check the inbox AND the spam folder of {target}.")
    except smtplib.SMTPAuthenticationError as exc:
        print(f"\n  RESULT: ❌ AUTHENTICATION FAILED  ({exc.smtp_code})")
        try:
            print("  Server said:", exc.smtp_error.decode("utf-8", "replace"))
        except Exception:
            print("  Server said:", exc.smtp_error)
        print("\n  >> This is almost always a bad/expired Gmail App Password.")
        print("     1. The Google account (SMTP_USERNAME) must have 2-Step Verification ON.")
        print("     2. Create a NEW 16-char App Password: https://myaccount.google.com/apppasswords")
        print("     3. Put it in .env as SMTP_PASSWORD (no spaces), save, and restart the app.")
        print("     If you ever revoked a previous password, the old one in .env is now DEAD —")
        print("     generate a fresh one.")
    except (smtplib.SMTPConnectError, OSError) as exc:
        print(f"\n  RESULT: ❌ COULD NOT CONNECT to {config.SMTP_HOST}:{config.SMTP_PORT}")
        print("  Reason:", exc)
        print("  >> Check internet access and that port 587 isn't blocked (firewall / campus Wi-Fi).")
    except Exception as exc:  # noqa: BLE001
        print(f"\n  RESULT: ❌ FAILED — {type(exc).__name__}: {exc}")

    _print_recipients()
    return 0


def _print_recipients():
    """Who would actually receive an alert, read straight from the SQLite DB."""
    db_path = os.path.join(HERE, "database", "stampede.db")
    print(f"\nWho is set up to receive alerts (from {db_path}):")
    if not os.path.exists(db_path):
        print("   (database not created yet — run the app once, then register an admin)")
        return
    try:
        con = sqlite3.connect(db_path)
        cur = con.cursor()
        cur.execute("SELECT COUNT(*) FROM users WHERE role='admin' AND email IS NOT NULL AND email<>''")
        admins = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM authorities WHERE is_active=1 AND email IS NOT NULL AND email<>''")
        auths = cur.fetchone()[0]
        con.close()
        print(f"   admins with an email        : {admins}")
        print(f"   active authorities w/ email : {auths}")
        if admins == 0 and auths == 0:
            print("   >> Nobody is configured to receive alerts. Register an admin account")
            print("      (or add an active Authority with an email) so there is a recipient.")
    except Exception as exc:  # noqa: BLE001
        print("   (couldn't read the database:", exc, ")")


if __name__ == "__main__":
    sys.exit(main())
