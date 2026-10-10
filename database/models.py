"""
database/models.py
====================
SQLAlchemy ORM models for the Stampede Prediction System.

Tables:
    users              - login accounts (admin / user / officer roles)
    videos             - every uploaded/processed video and its aggregate AI results
    alerts             - HIGH/CRITICAL risk events raised while processing a video
    otp_codes          - one-time login/registration codes (hashed)
    authorities        - responder contacts that alerts are emailed to
    alert_deliveries   - per-recipient send outcome for one alert
    contact_messages   - public website contact form submissions
    camera_locations   - admin-verified monitored sites with real coordinates
    dispatches         - an incident an officer is being sent to
    dispatch_offers    - one ask of one officer for one incident
    push_subscriptions - a device registered for Web Push alerts

Save this file at: Stampede-Prediction-System/database/models.py
"""

import json
from datetime import datetime

from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash

from database.database import db


class User(db.Model, UserMixin):
    """A dashboard login account. role is 'admin', 'user' or 'officer'."""

    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), unique=True, nullable=False, index=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False, default="user")  # 'admin' | 'user' | 'officer'
    is_verified = db.Column(db.Boolean, default=False)  # True once registration email OTP is confirmed
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    # --- Field-officer fields (Phase 3: officer dispatch) -----------------
    # Only meaningful when role == 'officer'. Kept on User rather than in a
    # separate table because an officer IS a login account: they sign in with
    # the same credentials and the same OTP flow as everyone else.
    badge_number = db.Column(db.String(40), nullable=True)
    phone_number = db.Column(db.String(30), nullable=True)

    # on_duty is set by the officer from their phone. An off-duty officer is
    # never offered an incident, which is the whole point: this is consent to
    # be located and paged, not a permanent property of the account.
    on_duty = db.Column(db.Boolean, default=False, nullable=False)
    last_latitude = db.Column(db.Float, nullable=True)
    last_longitude = db.Column(db.Float, nullable=True)
    last_location_at = db.Column(db.DateTime, nullable=True)

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

    @property
    def is_officer(self) -> bool:
        return self.role == "officer"

    def has_fresh_location(self, max_age_seconds: int) -> bool:
        """
        True only if this officer reported a position recently enough to be
        trusted. A position from an hour ago is not a location, it is a guess.
        """
        if self.last_latitude is None or self.last_longitude is None:
            return False
        if self.last_location_at is None:
            return False
        return (datetime.utcnow() - self.last_location_at).total_seconds() <= max_age_seconds

    def is_dispatchable(self, max_age_seconds: int) -> bool:
        """Can this account be offered an incident right now?"""
        return (
            self.role == "officer"
            and bool(self.on_duty)
            and bool(self.is_verified)
            and self.has_fresh_location(max_age_seconds)
        )

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

    # Physical location this footage/camera represents, for the risk map.
    # Optional: a video with no coordinates simply won't appear on the map.
    location_name = db.Column(db.String(150), nullable=True)   # e.g. "Main Gate, Sector 4"
    latitude = db.Column(db.Float, nullable=True)
    longitude = db.Column(db.Float, nullable=True)

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
    final_density = db.Column(db.Float, nullable=True)       # peak grid-cell density at the last analyzed frame
    final_growth_rate = db.Column(db.Float, nullable=True)   # crowd growth rate at the last analyzed frame
    browser_playable = db.Column(db.Boolean, default=False)  # True once FFmpeg re-encoded the output to H.264

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
    notified = db.Column(db.Boolean, default=False)  # True if at least one recipient was notified successfully
    person_count = db.Column(db.Integer, nullable=True)  # crowd count at the moment this alert fired
    max_cell_density = db.Column(db.Float, nullable=True)  # peak grid-cell density at alert time
    growth_rate = db.Column(db.Float, nullable=True)  # crowd growth rate at alert time

    def __repr__(self):
        return f"<Alert {self.risk_level} on video {self.video_id}>"


class OtpCode(db.Model):
    """
    A one-time login verification code emailed to a user. Only the hash
    is stored, never the plaintext code. One row is created per login
    attempt; 'consumed' is set True the moment a code is used
    successfully OR invalidated (superseded by a resend, expired, or
    max attempts exceeded), so at most one row per user is ever valid
    at a time.
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


class Authority(db.Model):
    """
    A registered authority/responder contact (police, traffic police,
    security officer, emergency response team, etc.) that HIGH/CRITICAL
    alerts can be routed to, in addition to admin user accounts.

    location_area is a free-text zone label (e.g. "Main Gate", "Sector 4")
    matched against Video.location_name for zone-based alert routing in
    a later phase. Left blank, an authority is treated as "all zones".
    """

    __tablename__ = "authorities"

    ROLE_CHOICES = [
        "Police",
        "Traffic Police",
        "Security Officer",
        "Emergency Response Team",
        "Event Security",
        "Other",
    ]

    id = db.Column(db.Integer, primary_key=True)

    name = db.Column(db.String(120), nullable=False)
    role = db.Column(db.String(50), nullable=False, default="Other")
    department = db.Column(db.String(150), nullable=True)
    email = db.Column(db.String(150), nullable=False)
    phone = db.Column(db.String(30), nullable=True)
    location_area = db.Column(db.String(150), nullable=True)  # zone/area this authority covers

    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_by = db.relationship("User")

    def __repr__(self):
        return f"<Authority {self.name} ({self.role}) active={self.is_active}>"


class AlertDelivery(db.Model):
    """
    One notification attempt to one recipient (an admin User or an
    Authority) for one Alert. This is what powers the Alert History
    page's per-recipient Sent/Failed/Pending breakdown.

    Honesty note: "sent" here means the SMTP server accepted the
    message for that individual recipient's send attempt — it is NOT a
    true delivery/read receipt (that would require a transactional
    email provider with webhook support, which this project does not
    use). "failed" means the send attempt itself raised an error
    (bad address, SMTP rejection, connection failure). "pending" is
    used only transiently, before a send attempt has been made.
    """

    __tablename__ = "alert_deliveries"

    id = db.Column(db.Integer, primary_key=True)
    alert_id = db.Column(db.Integer, db.ForeignKey("alerts.id"), nullable=False)

    recipient_type = db.Column(db.String(20), nullable=False)  # 'admin' | 'authority'
    recipient_id = db.Column(db.Integer, nullable=True)  # User.id or Authority.id, informational only
    recipient_name = db.Column(db.String(150), nullable=False)
    recipient_email = db.Column(db.String(150), nullable=False)

    status = db.Column(db.String(20), nullable=False, default="pending")  # 'sent' | 'failed' | 'pending'
    sent_at = db.Column(db.DateTime, nullable=True)
    error_message = db.Column(db.String(255), nullable=True)

    alert = db.relationship("Alert", backref=db.backref("deliveries", lazy=True, cascade="all, delete-orphan"))

    def __repr__(self):
        return f"<AlertDelivery alert={self.alert_id} to={self.recipient_email} status={self.status}>"


class ContactMessage(db.Model):
    """A message submitted through the public website's Contact page."""

    __tablename__ = "contact_messages"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(150), nullable=False)
    message = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    is_read = db.Column(db.Boolean, default=False)

    def __repr__(self):
        return f"<ContactMessage from={self.email} at={self.created_at}>"


class CameraLocation(db.Model):
    """
    A real-world monitored site (gate, platform, junction ...) that an
    admin registers once, with its true coordinates. Uploads and live
    CCTV sessions pick one of these so the map and the zone-based
    authority routing always use consistent, admin-verified locations
    instead of free-typed coordinates.

    Authority.location_area is matched (case-insensitively) against
    CameraLocation.name via Video.location_name.
    """

    __tablename__ = "camera_locations"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False, unique=True)
    description = db.Column(db.String(255), nullable=True)
    latitude = db.Column(db.Float, nullable=False)
    longitude = db.Column(db.Float, nullable=False)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<CameraLocation {self.name} ({self.latitude}, {self.longitude})>"


# ===========================================================================
# OFFICER DISPATCH (Phase 3)
# ===========================================================================
class Dispatch(db.Model):
    """
    One real-world incident that needs an officer on site.

    Created from a HIGH/CRITICAL Alert whose camera has coordinates, then
    offered to on-duty officers one at a time, nearest first. The incident's
    coordinates are SNAPSHOT here rather than read through the Alert at
    display time, for two reasons: the destination an officer accepted must
    never silently change underneath them, and the record has to survive the
    video row being deleted.

    Status flow:
        PENDING    - created, nobody asked yet (lives for milliseconds)
        OFFERED    - sitting on one officer's phone, countdown running
        ACCEPTED   - an officer took it
        EN_ROUTE   - officer says they are moving
        ARRIVED    - officer is on scene
        RESOLVED   - done (also acknowledges the parent Alert)
        UNASSIGNED - nobody available or everyone passed; admins escalated
        CANCELLED  - closed by an admin
    """

    __tablename__ = "dispatches"

    # Ordered for the stepper on the officer's phone and the admin table.
    PROGRESS_SEQUENCE = ["ACCEPTED", "EN_ROUTE", "ARRIVED", "RESOLVED"]

    id = db.Column(db.Integer, primary_key=True)

    # Nullable because an admin can raise a dispatch by hand for an incident
    # the cameras never saw.
    alert_id = db.Column(db.Integer, db.ForeignKey("alerts.id"), nullable=True)
    video_id = db.Column(db.Integer, db.ForeignKey("videos.id"), nullable=True)
    location_id = db.Column(db.Integer, db.ForeignKey("camera_locations.id"), nullable=True)

    # Snapshot of where the officer is being sent.
    location_name = db.Column(db.String(150), nullable=True)
    latitude = db.Column(db.Float, nullable=False)
    longitude = db.Column(db.Float, nullable=False)

    risk_level = db.Column(db.String(20), nullable=False)
    message = db.Column(db.String(255), nullable=False)

    status = db.Column(db.String(20), nullable=False, default="PENDING", index=True)
    offers_made = db.Column(db.Integer, nullable=False, default=0)
    close_reason = db.Column(db.String(255), nullable=True)

    accepted_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)

    # Raised by the 3D simulation rather than by a camera or a real admin.
    #
    # This exists because the simulation drives the REAL engine - real
    # ranking, real push notifications, a real officer tapping Accept - and
    # so it leaves real rows behind. Without a flag those rows would be
    # indistinguishable from genuine incidents in the one place that must
    # never lie: the response-time statistics an examiner or an operator
    # reads off the dispatch board. Marked rows are excluded from those
    # averages and can be purged in one action.
    is_simulated = db.Column(db.Boolean, nullable=False, default=False, index=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    accepted_at = db.Column(db.DateTime, nullable=True)
    en_route_at = db.Column(db.DateTime, nullable=True)
    arrived_at = db.Column(db.DateTime, nullable=True)
    resolved_at = db.Column(db.DateTime, nullable=True)

    alert = db.relationship("Alert", backref=db.backref("dispatches", lazy=True))
    video = db.relationship("Video")
    location = db.relationship("CameraLocation")
    accepted_by = db.relationship(
        "User", backref=db.backref("dispatches_accepted", lazy=True),
    )
    offers = db.relationship(
        "DispatchOffer", backref="dispatch", lazy=True,
        cascade="all, delete-orphan", order_by="DispatchOffer.sequence",
    )

    @property
    def is_open(self) -> bool:
        """Still needs an officer (nobody has accepted and it isn't closed)."""
        return self.status in ("PENDING", "OFFERED")

    @property
    def is_closed(self) -> bool:
        return self.status in ("RESOLVED", "CANCELLED", "UNASSIGNED")

    @property
    def is_claimed(self) -> bool:
        return self.status in ("ACCEPTED", "EN_ROUTE", "ARRIVED")

    @property
    def response_seconds(self):
        """Seconds from incident raised to an officer accepting it."""
        if self.accepted_at is None or self.created_at is None:
            return None
        return int((self.accepted_at - self.created_at).total_seconds())

    @property
    def resolution_seconds(self):
        if self.resolved_at is None or self.created_at is None:
            return None
        return int((self.resolved_at - self.created_at).total_seconds())

    def timestamp_for(self, status: str):
        return {
            "ACCEPTED": self.accepted_at,
            "EN_ROUTE": self.en_route_at,
            "ARRIVED": self.arrived_at,
            "RESOLVED": self.resolved_at,
        }.get(status)

    def to_dict(self) -> dict:
        """
        Serialised for the dispatch board's polling endpoint.

        created_at stays in ISO form for machine use; created_at_label is
        pre-rendered in the deployment's timezone so the browser does not
        have to guess what 'Z' means to a viewer in another region.
        """
        from utils.timeutils import fmt_local

        return {
            "id": self.id,
            "status": self.status,
            "risk_level": self.risk_level,
            "message": self.message,
            "location_name": self.location_name,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "offers_made": self.offers_made,
            "accepted_by": self.accepted_by.username if self.accepted_by else None,
            # The 3D view needs the id, not just the name, to draw the route
            # line from the right officer marker to the incident.
            "accepted_by_id": self.accepted_by_id,
            "is_simulated": bool(self.is_simulated),
            "close_reason": self.close_reason,
            "created_at": self.created_at.isoformat() + "Z" if self.created_at else None,
            "created_at_label": fmt_local(self.created_at),
            "response_seconds": self.response_seconds,
            "offers": [
                {
                    "sequence": offer.sequence,
                    "status": offer.status,
                    "officer": offer.officer.username if offer.officer else "unknown",
                    "officer_id": offer.officer_id,
                    "distance_km": offer.distance_km,
                    "seconds_remaining": offer.seconds_remaining,
                }
                for offer in self.offers
            ],
        }

    def __repr__(self):
        return f"<Dispatch {self.id} {self.risk_level} {self.status}>"


class DispatchOffer(db.Model):
    """
    One ask of one officer for one incident.

    The engine keeps at most ONE offer with status 'PENDING' per dispatch at
    any moment. That invariant is what makes two officers accepting the same
    incident impossible by construction, instead of depending on database row
    locking that SQLite would not honour anyway.

    sequence is 1 for the nearest officer, 2 for the next-nearest, and so on,
    so the admin view can show exactly how far down the roster an incident
    had to travel before someone answered.
    """

    __tablename__ = "dispatch_offers"

    id = db.Column(db.Integer, primary_key=True)
    dispatch_id = db.Column(db.Integer, db.ForeignKey("dispatches.id"), nullable=False)
    officer_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)

    # PENDING | ACCEPTED | DECLINED | TIMEOUT | SUPERSEDED
    status = db.Column(db.String(20), nullable=False, default="PENDING", index=True)
    sequence = db.Column(db.Integer, nullable=False, default=1)

    # Straight-line distance at the moment of the offer, kept for the audit
    # trail: it explains why this officer was chosen.
    distance_km = db.Column(db.Float, nullable=True)

    offered_at = db.Column(db.DateTime, default=datetime.utcnow)
    expires_at = db.Column(db.DateTime, nullable=False)
    responded_at = db.Column(db.DateTime, nullable=True)

    # Backrefs so an admin view can ask an officer "how many times were you
    # asked, and how often did you take it?" - the two numbers that show
    # whether the roster is sized and placed correctly.
    officer = db.relationship(
        "User", backref=db.backref("offers_received", lazy=True,
                                   cascade="all, delete-orphan"),
    )

    @property
    def seconds_remaining(self) -> int:
        if self.expires_at is None:
            return 0
        return max(0, int((self.expires_at - datetime.utcnow()).total_seconds()))

    @property
    def is_live(self) -> bool:
        """Still awaiting an answer and not yet lapsed."""
        return self.status == "PENDING" and self.seconds_remaining > 0

    def __repr__(self):
        return f"<DispatchOffer d={self.dispatch_id} officer={self.officer_id} {self.status}>"


class PushSubscription(db.Model):
    """
    One browser/device registered to receive Web Push for one officer.

    An officer may have several (phone, tablet, desktop) and all of them are
    pushed, because we cannot know which device is in their hand. The
    endpoint URL is the unique device identity assigned by the browser's push
    service; p256dh and auth are the client's public encryption keys, so the
    push service relays a payload it cannot itself read.
    """

    __tablename__ = "push_subscriptions"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)

    endpoint = db.Column(db.String(500), nullable=False, unique=True)
    p256dh = db.Column(db.String(255), nullable=False)
    auth = db.Column(db.String(255), nullable=False)
    user_agent = db.Column(db.String(255), nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_used_at = db.Column(db.DateTime, nullable=True)

    user = db.relationship("User", backref=db.backref("push_subscriptions", lazy=True,
                                                      cascade="all, delete-orphan"))

    def to_web_push_dict(self) -> dict:
        """The shape pywebpush expects for subscription_info."""
        return {
            "endpoint": self.endpoint,
            "keys": {"p256dh": self.p256dh, "auth": self.auth},
        }

    def __repr__(self):
        return f"<PushSubscription user={self.user_id} {self.endpoint[:40]}...>"
