"""
utils/video_converter.py
==========================
Re-encodes the annotated video written by OpenCV (codec 'mp4v', which most
browsers cannot play) into H.264 + AAC-free MP4 with the moov atom at the
front ("faststart"), so it plays in the dashboard's <video> tag.

FFmpeg lookup order:
    1. config.FFMPEG_BINARY (explicit path from .env)
    2. an `ffmpeg` executable on PATH
    3. the binary bundled by the optional `imageio-ffmpeg` package

If none is found, or the conversion fails, the ORIGINAL file is kept and the
function returns False — the UI then offers a download instead. Conversion
never raises, so a missing FFmpeg can never break video processing.

Save this file at: Stampede-Prediction-System/utils/video_converter.py
"""

import os
import shutil
import subprocess

import config
from utils.logger import get_logger

logger = get_logger(__name__)


def find_ffmpeg():
    """Return the path of a usable ffmpeg executable, or None."""
    if config.FFMPEG_BINARY and os.path.isfile(config.FFMPEG_BINARY):
        return config.FFMPEG_BINARY
    on_path = shutil.which("ffmpeg")
    if on_path:
        return on_path
    try:
        import imageio_ffmpeg  # optional dependency
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001 - package missing or binary unavailable
        return None


def convert_to_browser_mp4(video_path: str) -> bool:
    """
    Convert `video_path` in place to browser-playable H.264 MP4.
    Returns True on success, False if FFmpeg is unavailable/disabled or the
    conversion failed (the original file is left untouched in that case).
    """
    if not config.FFMPEG_ENABLED:
        logger.info("FFmpeg conversion disabled (FFMPEG_ENABLED=false).")
        return False

    ffmpeg = find_ffmpeg()
    if ffmpeg is None:
        logger.warning(
            "FFmpeg not found - processed video keeps the OpenCV codec and may "
            "not play in the browser. Install FFmpeg or `pip install imageio-ffmpeg`."
        )
        return False

    tmp_path = video_path + ".h264.tmp.mp4"
    command = [
        ffmpeg, "-y", "-loglevel", "error", "-i", video_path,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-an", tmp_path,
    ]
    try:
        subprocess.run(command, check=True, capture_output=True, timeout=1800)
        os.replace(tmp_path, video_path)
        logger.info(f"Converted {os.path.basename(video_path)} to browser-playable H.264.")
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error(f"FFmpeg conversion failed for {video_path}: {exc}")
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        return False
