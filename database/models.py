"""
database/models.py
====================
SQLAlchemy ORM models for the Stampede Prediction System.

Tables:
    users  - login accounts (admin / user roles)
    videos - every uploaded/processed video and its aggregate AI results
    alerts - HIGH/CRITICAL risk events raised while processing a video

Save this file at: Stampede-Prediction-System/database/models.py
"""

import json
from datetime import datetime

from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash

from database.database import db


class User(db.Model, UserMixin):
    """A dashboard login account. role is either 'admin' or 'user'."""

    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), unique=True, nullable=False, index=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    phone_number = db.Column(db.String(15), nullable=True)  # e.g. "9198XXXXXXXX", no + or spaces
    role = db.Column(db.String(20), nullable=False, default="user")  # 'admin' | 'user'
    is_verified = db.Column(db.Boolean, default=False)  # True once registration email OTP is confirmed
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    videos = db.relationship("Video", backref="uploaded_by", lazy=True)

    def set_password(self, raw_password: str) -> None:
        """Hash and store a plaintext password (never store it raw)."""
        self.password_hash = generate_password_hash(raw_password)

    def check_password(self, raw_password: str) -> bool:
        """Verify a plaintext password against the stored hash."""
        return check_password_hash(self.password_hash, raw_password)

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    def __repr__(self):
        return f"<User {self.username} ({self.role})>"


class Video(db.Model):
    """
    One uploaded/processed video and the aggregate results produced by
    detector.video_processor.VideoProcessor.process().
    """

    __tablename__ = "videos"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)

    original_filename = db.Column(db.String(255), nullable=False)
    stored_filename = db.Column(db.String(255), nullable=False)       # on-disk input name
    processed_filename = db.Column(db.String(255), nullable=True)     # on-disk output name
    source_type = db.Column(db.String(20), nullable=False, default="upload")  # 'upload' | 'cctv'

    upload_time = db.Column(db.DateTime, default=datetime.utcnow)
    processing_status = db.Column(db.String(20), nullable=False, default="pending")
    # 'pending' | 'processing' | 'completed' | 'failed'
    error_message = db.Column(db.Text, nullable=True)

    # Aggregate stats filled in once VideoProcessor.process() finishes.
    frame_width = db.Column(db.Integer, nullable=True)
    frame_height = db.Column(db.Integer, nullable=True)
    total_frames = db.Column(db.Integer, nullable=True)
    source_fps = db.Column(db.Float, nullable=True)
    processing_time_seconds = db.Column(db.Float, nullable=True)
    peak_count = db.Column(db.Integer, nullable=True)
    average_count = db.Column(db.Float, nullable=True)
    final_risk_level = db.Column(db.String(20), nullable=True)

    # Stored as JSON text: [{"second": 0, "level": "SAFE"}, ...]
    risk_timeline_json = db.Column(db.Text, nullable=True)

    alerts = db.relationship("Alert", backref="video", lazy=True, cascade="all, delete-orphan")

    def set_risk_timeline(self, timeline: list) -> None:
        self.risk_timeline_json = json.dumps(timeline)

    def get_risk_timeline(self) -> list:
        if not self.risk_timeline_json:
            return []
        return json.loads(self.risk_timeline_json)

    def __repr__(self):
        return f"<Video {self.original_filename} [{self.processing_status}]>"


class Alert(db.Model):
    """A HIGH/CRITICAL risk event raised while processing a specific video."""

    __tablename__ = "alerts"

    id = db.Column(db.Integer, primary_key=True)
    video_id = db.Column(db.Integer, db.ForeignKey("videos.id"), nullable=False)

    risk_level = db.Column(db.String(20), nullable=False)
    message = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    acknowledged = db.Column(db.Boolean, default=False)

    def __repr__(self):
        return f"<Alert {self.risk_level} on video {self.video_id}>"

class OtpCode(db.Model):
    """
    A one-time login verification code emailed to a user. Only the hash
    is stored, never the plaintext code.
    """
    __tablename__ = "otp_codes"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    code_hash = db.Column(db.String(255), nullable=False)
    purpose = db.Column(db.String(20), nullable=False, default="login")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    expires_at = db.Column(db.DateTime, nullable=False)
    attempts = db.Column(db.Integer, default=0)
    consumed = db.Column(db.Boolean, default=False)

    user = db.relationship("User")

    def __repr__(self):
        return f"<OtpCode user={self.user_id} purpose={self.purpose} consumed={self.consumed}>"