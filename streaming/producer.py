"""
streaming/producer.py
========================
Publishes JPEG-encoded camera frames onto a Fluvio topic so they can be
consumed (potentially by a different process/machine) via consumer.py.

The `fluvio` package is a compiled Rust extension that is only reliably
installable on Linux/macOS (InfinyOn recommends WSL2 on Windows). This
module is written so the REST of the application never crashes if:
    (a) the `fluvio` package is not installed at all, or
    (b) it is installed but no Fluvio cluster is reachable.
In both cases FluvioUnavailableError is raised by connect(), and
streaming/stream_manager.py catches it to fall back to direct
(non-Fluvio) streaming automatically.

Save this file at: Stampede-Prediction-System/streaming/producer.py
"""

import base64
import time

import cv2

import config
from utils.logger import get_logger

logger = get_logger(__name__)

# The fluvio package may not be installed on this machine (e.g. plain
# Windows without WSL2). Import defensively so the whole app still boots.
try:
    from fluvio import Fluvio
    FLUVIO_PACKAGE_AVAILABLE = True
except ImportError:
    Fluvio = None
    FLUVIO_PACKAGE_AVAILABLE = False


class FluvioUnavailableError(Exception):
    """Raised when a Fluvio cluster cannot be reached or the client library is missing."""


class FrameProducer:
    """
    Encodes BGR frames as JPEG, base64-encodes them for safe text
    transport, and publishes them as string records to a Fluvio topic.
    """

    def __init__(self, topic_name: str = None):
        self.topic_name = topic_name or config.FLUVIO_TOPIC_NAME
        self._fluvio = None
        self._producer = None

    def connect(self) -> None:
        """
        Connect to the local/configured Fluvio cluster and open a
        producer for the configured topic.

        Raises FluvioUnavailableError if the fluvio package isn't
        installed or no cluster profile is reachable. Callers (namely
        LiveStreamManager) MUST catch this and fall back gracefully.
        """
        if not FLUVIO_PACKAGE_AVAILABLE:
            raise FluvioUnavailableError(
                "The 'fluvio' Python package is not installed. "
                "Install it inside a Linux/WSL2 environment with: pip install fluvio"
            )
        try:
            self._fluvio = Fluvio.connect()
            self._producer = self._fluvio.topic_producer(self.topic_name)
            logger.info(f"Connected to Fluvio. Producing to topic '{self.topic_name}'.")
        except Exception as exc:  # noqa: BLE001 - any connection failure -> fallback
            raise FluvioUnavailableError(
                f"Could not connect to a Fluvio cluster: {exc}. "
                "Is 'fluvio cluster start' running?"
            ) from exc

    def send_frame(self, frame, jpeg_quality: int = None) -> None:
        """
        JPEG-encode a BGR numpy frame and publish it as a base64 string
        record, tagged with a capture timestamp so the consumer can
        measure end-to-end latency if needed.
        """
        if self._producer is None:
            raise FluvioUnavailableError("send_frame() called before connect().")

        quality = jpeg_quality or config.STREAM_JPEG_QUALITY
        ok, buffer = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
        if not ok:
            logger.warning("Failed to JPEG-encode frame for Fluvio publish; skipping.")
            return

        payload = base64.b64encode(buffer).decode("ascii")
        record = f"{time.time()}|{payload}"
        self._producer.send_string(record)

    def close(self) -> None:
        """Flush any buffered records and release the producer/connection."""
        try:
            if self._producer is not None:
                self._producer.flush()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"Error flushing Fluvio producer: {exc}")
        self._producer = None
        self._fluvio = None
        logger.info(f"Fluvio producer for topic '{self.topic_name}' closed.")
