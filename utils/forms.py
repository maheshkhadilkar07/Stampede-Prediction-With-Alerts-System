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
from wtforms import StringField, PasswordField, SelectField, SubmitField
from wtforms.validators import DataRequired, Email, Length, EqualTo

import config


class LoginForm(FlaskForm):
    username = StringField("Username", validators=[DataRequired()])
    password = PasswordField("Password", validators=[DataRequired()])
    submit = SubmitField("Sign In")


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
        choices=[("user", "Operator"), ("admin", "Admin")],
        default="user",
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
    submit = SubmitField("Upload & Analyze")

class OtpForm(FlaskForm):
    code = StringField(
        "Verification Code",
        validators=[DataRequired(), Length(min=4, max=8, message="Enter the code exactly as emailed.")],
    )
    submit = SubmitField("Verify")