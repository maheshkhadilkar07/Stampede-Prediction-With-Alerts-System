"""
database/database.py
======================
Creates the single, shared SQLAlchemy `db` instance used by every model
and by app.py. Keeping it in its own tiny module (instead of inside
app.py) avoids circular imports between app.py <-> models.py.

Currently configured for SQLite (see config.SQLALCHEMY_DATABASE_URI).
To move to MySQL later, only config.py needs to change: install
`pymysql`, then set STAMPEDE_DATABASE_URL to something like
"mysql+pymysql://user:password@localhost/stampede_db" - no code here
needs to change because SQLAlchemy abstracts the driver.

Save this file at: Stampede-Prediction-System/database/database.py
"""

from flask_sqlalchemy import SQLAlchemy

# Single SQLAlchemy instance, imported by models.py and app.py.
db = SQLAlchemy()


def init_db(app):
    """
    Bind the SQLAlchemy instance to the Flask app and create all tables
    if they do not already exist. Called once from app.py's factory.
    """
    db.init_app(app)
    with app.app_context():
        # Import models here (not at module top) so they register with
        # `db` before create_all() runs, without causing circular imports.
        from database import models  # noqa: F401
        db.create_all()
