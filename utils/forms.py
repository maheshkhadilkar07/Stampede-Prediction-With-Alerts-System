"""
utils/forms.py
================
Flask-WTF form definitions for authentication and video upload. Field
names here MUST match what templates/login.html, register.html, and
upload.html already reference (form.username, form.video_file, etc.).

Save this file at: Stampede-Prediction-System/utils/forms.py
"""

from flask_wtf import FlaskForm
from flask_wtf.file import FileField, FileRequired, FileAllowed
from wtforms import StringField, PasswordField, SelectField, SubmitField, FloatField, BooleanField, TextAreaField
from wtforms.validators import DataRequired, Email, Length, EqualTo, Optional, NumberRange

import config


class LoginForm(FlaskForm):
    username = StringField("Username", validators=[DataRequired()])
    password = PasswordField("Password", validators=[DataRequired()])
    submit = SubmitField("Sign In")


class OtpForm(FlaskForm):
    code = StringField(
        "Verification Code",
        validators=[DataRequired(), Length(min=4, max=8, message="Enter the code exactly as emailed.")],
    )
    submit = SubmitField("Verify")


class RegisterForm(FlaskForm):
    username = StringField("Username", validators=[DataRequired(), Length(min=3, max=64)])
    email = StringField("Email", validators=[DataRequired(), Email()])
    password = PasswordField("Password", validators=[DataRequired(), Length(min=6)])
    confirm_password = PasswordField(
        "Confirm Password",
        validators=[DataRequired(), EqualTo("password", message="Passwords must match.")],
    )
    role = SelectField(
        "Role",
        choices=[
            ("user", "Operator"),
            ("officer", "Field Officer"),
            ("admin", "Admin"),
        ],
        default="user",
    )
    # Only meaningful for field officers, but cheap enough to always ask for:
    # a badge number is how a dispatcher identifies who accepted an incident
    # over the radio, and the phone number is the fallback when push
    # notifications do not reach a device.
    badge_number = StringField(
        "Badge Number (field officers)", validators=[Optional(), Length(max=40)],
    )
    phone_number = StringField(
        "Mobile Number (field officers)", validators=[Optional(), Length(max=30)],
    )
    submit = SubmitField("Create Account")


class UploadVideoForm(FlaskForm):
    video_file = FileField(
        "Video File",
        validators=[
            FileRequired(message="Please choose a video file."),
            FileAllowed(
                list(config.ALLOWED_VIDEO_EXTENSIONS),
                message=f"Only {', '.join(config.ALLOWED_VIDEO_EXTENSIONS)} files are allowed.",
            ),
        ],
    )
    location_id = SelectField("Registered Location", coerce=int, choices=[(0, "— Not specified —")], default=0)
    location_name = StringField("Location Name (optional)", validators=[Optional(), Length(max=150)])
    latitude = FloatField("Latitude (optional)", validators=[Optional(), NumberRange(min=-90, max=90)])
    longitude = FloatField("Longitude (optional)", validators=[Optional(), NumberRange(min=-180, max=180)])
    submit = SubmitField("Upload & Analyze")


class AuthorityForm(FlaskForm):
    """Add/edit form for a registered authority/responder contact (admin-only)."""
    name = StringField("Full Name", validators=[DataRequired(), Length(max=120)])
    role = SelectField(
        "Role",
        choices=[
            ("Police", "Police"),
            ("Traffic Police", "Traffic Police"),
            ("Security Officer", "Security Officer"),
            ("Emergency Response Team", "Emergency Response Team"),
            ("Event Security", "Event Security"),
            ("Other", "Other"),
        ],
        validators=[DataRequired()],
    )
    department = StringField("Department", validators=[Optional(), Length(max=150)])
    email = StringField("Email", validators=[DataRequired(), Email(), Length(max=150)])
    phone = StringField("Phone (optional)", validators=[Optional(), Length(max=30)])
    location_area = StringField("Location / Area Covered", validators=[Optional(), Length(max=150)])
    is_active = BooleanField("Active", default=True)
    submit = SubmitField("Save Authority")


class ContactForm(FlaskForm):
    """Public website contact form (no login required)."""
    name = StringField("Name", validators=[DataRequired(), Length(max=120)])
    email = StringField("Email", validators=[DataRequired(), Email(), Length(max=150)])
    message = TextAreaField("Message", validators=[DataRequired(), Length(min=10, max=2000)])
    submit = SubmitField("Send Message")


class LocationForm(FlaskForm):
    """Admin form to register a real-world monitored location."""
    name = StringField("Location Name", validators=[DataRequired(), Length(max=150)])
    description = StringField("Description (optional)", validators=[Optional(), Length(max=255)])
    latitude = FloatField("Latitude", validators=[DataRequired(), NumberRange(min=-90, max=90)])
    longitude = FloatField("Longitude", validators=[DataRequired(), NumberRange(min=-180, max=180)])
    is_active = BooleanField("Active", default=True)
    submit = SubmitField("Save Location")
