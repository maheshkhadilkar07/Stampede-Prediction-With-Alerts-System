"""
scripts/migrate_add_dispatch.py
=================================
Brings an existing stampede.db up to date for the officer dispatch feature.

SQLAlchemy's db.create_all() creates tables that do not exist yet, but it
will NOT add columns to a table that already does. The officer fields live
on the existing `users` table, so those need real ALTER TABLE statements -
which is this script's entire reason for existing.

Run once, from the project root, with the app stopped:

    python scripts/migrate_add_dispatch.py

Safe to run repeatedly: every step checks whether it has already been done.
The database is backed up first, because a migration you cannot undo is a
migration you should not run.

Adds to `users`:
    badge_number, phone_number, on_duty,
    last_latitude, last_longitude, last_location_at

Adds to `dispatches` (only if that table already exists from an earlier run):
    is_simulated

Creates (via the models, after the ALTERs):
    dispatches, dispatch_offers, push_subscriptions
"""

import os
import shutil
import sqlite3
import sys
from datetime import datetime

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import config  # noqa: E402  (needs the path set up first)

# (column name, SQL type + constraints). NOT NULL columns MUST carry a
# DEFAULT: SQLite cannot add a NOT NULL column to a table with existing rows
# without one, and every current user row needs a value for on_duty.
USER_COLUMNS = [
    ("badge_number", "VARCHAR(40)"),
    ("phone_number", "VARCHAR(30)"),
    ("on_duty", "BOOLEAN NOT NULL DEFAULT 0"),
    ("last_latitude", "FLOAT"),
    ("last_longitude", "FLOAT"),
    ("last_location_at", "DATETIME"),
]

# Added later, for the 3D simulation. Anyone who ran this script before that
# feature existed has a `dispatches` table without the column, and
# create_all() will not add it - so it needs the same ALTER treatment as the
# user columns. Existing rows are real incidents, hence DEFAULT 0.
DISPATCH_COLUMNS = [
    ("is_simulated", "BOOLEAN NOT NULL DEFAULT 0"),
]


def existing_columns(cursor, table):
    cursor.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in cursor.fetchall()}


def table_exists(cursor, table):
    cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,),
    )
    return cursor.fetchone() is not None


def back_up(db_path):
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = f"{db_path}.backup-{stamp}"
    shutil.copy2(db_path, backup_path)
    print(f"  backup written to {os.path.basename(backup_path)}")
    return backup_path


def add_columns(db_path, table, columns):
    """
    Add any of `columns` that `table` does not already have.

    Returns the number actually added, so the caller can report "0 added"
    on a second run rather than implying it did work it did not do.
    """
    connection = sqlite3.connect(db_path)
    cursor = connection.cursor()
    try:
        if not table_exists(cursor, table):
            print(f"  no '{table}' table yet - nothing to alter.")
            return 0

        present = existing_columns(cursor, table)
        added = 0
        for name, definition in columns:
            if name in present:
                print(f"  {table}.{name} already present - skipped")
                continue
            cursor.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
            print(f"  {table}.{name} added")
            added += 1

        # Any row that predates a NOT NULL column gets an explicit value
        # rather than being left NULL, so `WHERE on_duty = 0` behaves the way
        # the engine assumes it does.
        if table == "users":
            cursor.execute("UPDATE users SET on_duty = 0 WHERE on_duty IS NULL")
        if table == "dispatches":
            cursor.execute(
                "UPDATE dispatches SET is_simulated = 0 WHERE is_simulated IS NULL"
            )
        connection.commit()
        return added
    finally:
        connection.close()


def create_new_tables():
    """
    Done through the models rather than hand-written DDL so the schema has
    exactly one definition. Imported AFTER the ALTERs, because importing the
    app runs create_all() and we want the users table correct before any of
    the ORM touches it.
    """
    from app import app  # noqa: WPS433 - deliberate late import
    from database.database import db
    from database.models import Dispatch, DispatchOffer, PushSubscription  # noqa: F401

    with app.app_context():
        db.create_all()

    return ["dispatches", "dispatch_offers", "push_subscriptions"]


def main():
    db_path = config.SQLITE_DB_PATH
    uri = config.SQLALCHEMY_DATABASE_URI

    if not uri.startswith("sqlite"):
        print(
            "This migration only handles SQLite. Your STAMPEDE_DATABASE_URL "
            f"points at:\n    {uri}\n"
            "For another engine, apply the equivalent ALTER TABLE statements "
            "by hand - see USER_COLUMNS at the top of this file."
        )
        return 1

    print(f"\nDatabase: {db_path}")

    if os.path.exists(db_path):
        back_up(db_path)
        print("\nAltering 'users':")
        added = add_columns(db_path, "users", USER_COLUMNS)
        print(f"  {added} column(s) added.")
        print("\nAltering 'dispatches':")
        added = add_columns(db_path, "dispatches", DISPATCH_COLUMNS)
        print(f"  {added} column(s) added.")
    else:
        print("  database does not exist yet - it will be created from scratch.")

    print("\nCreating dispatch tables:")
    for name in create_new_tables():
        print(f"  {name} ready")

    print(
        "\nMigration complete.\n\n"
        "Next steps:\n"
        "  1. Promote an account to field officer:\n"
        "       Admin > Dispatch Board > Officers\n"
        "  2. Register the camera locations officers will be sent to:\n"
        "       Admin > Locations  (coordinates are required)\n"
        "  3. Optional, for alerts with the app closed:\n"
        "       pip install pywebpush\n"
        "       python scripts/generate_vapid_keys.py\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
