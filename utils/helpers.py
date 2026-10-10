"""
utils/helpers.py
=================
Small, reusable helper functions shared across the project:
file-extension checks, unique filename generation, and time formatting.

Save this file at: Stampede-Prediction-System/utils/helpers.py
"""

import os
import uuid
from datetime import datetime

import config


def allowed_video_file(filename: str) -> bool:
    """Return True if filename has an extension we accept for upload."""
    if "." not in filename:
        return False
    ext = filename.rsplit(".", 1)[1].lower()
    return ext in config.ALLOWED_VIDEO_EXTENSIONS


def generate_unique_filename(original_filename: str) -> str:
    """
    Build a collision-safe filename by prefixing a UUID4 + timestamp,
    while keeping the original extension.

    Example: 'crowd.mp4' -> '20260731_104501_9f1c2b7a.mp4'
    """
    ext = original_filename.rsplit(".", 1)[1].lower() if "." in original_filename else "mp4"
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    unique_id = uuid.uuid4().hex[:8]
    return f"{timestamp}_{unique_id}.{ext}"


def human_readable_size(num_bytes: int) -> str:
    """Convert a byte count into a human readable string (KB/MB/GB)."""
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024.0:
            return f"{size:.2f} {unit}"
        size /= 1024.0
    return f"{size:.2f} TB"


def seconds_to_timestamp(seconds: float) -> str:
    """Convert a float number of seconds into HH:MM:SS format."""
    seconds = int(seconds)
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def safe_join_upload_path(filename: str) -> str:
    """Return the absolute path of a file inside the uploads directory."""
    return os.path.join(config.VIDEO_UPLOAD_DIR, filename)


def safe_join_processed_path(filename: str) -> str:
    """Return the absolute path of a file inside the processed directory."""
    return os.path.join(config.VIDEO_PROCESSED_DIR, filename)
