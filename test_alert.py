"""
test_alert.py
=============
Standalone script to test the admin alert notifier.
Run from the project root: python test_alert.py
"""

import time
from app import app
from database.models import User
from utils.notifier import notify_admins_async

with app.app_context():
    admins = User.query.filter_by(role="admin").all()
    print(f"Found {len(admins)} admin(s): {[a.email for a in admins]}")

    if not admins:
        print("STOP: No admins found — the role='admin' filter still isn't matching anyone.")
    else:
        print("Sending test alert now...")
        notify_admins_async(1, "CRITICAL", "Test alert - crowd simulation")
        print("Waiting 8 seconds for the background thread to finish...")
        time.sleep(8)
        print("Done. Check your inbox (and spam folder), and check logs/stampede_system.log for details.")