"""
streaming/consumer.py
========================
Reads frame records back off a Fluvio topic (published by producer.py),
decodes them from base64 JPEG back into OpenCV BGR frames, and yields
them to the caller (streaming/stream_manager.py) for YOLO inference.

Save this file at: Stampede-Prediction-System/streaming/consumer.py
"""

import base64
import time

import cv2
import numpy as np

import config
from utils.logger import get_logger
from streaming.producer import FluvioUnavailableError, FLUVIO_PACKAGE_AVAILABLE

logger = get_logger(__name__)

if FLUVIO_PACKAGE_AVAILABLE:
    from fluvio import Fluvio, Offset
else:
    Fluvio = None
    Offset = None


class FrameConsumer:
    """
    Connects to a Fluvio topic/partition and exposes an iterator that
    yields decoded (frame, capture_timestamp) tuples as new records
    arrive on the stream.
    """

    def __init__(self, topic_name: str = None, partition: int = 0):
        self.topic_name = topic_name or config.FLUVIO_TOPIC_NAME
        self.partition = partition
        self._fluvio = None
        self._consumer = None
        self._stream = None

    def connect(self) -> None:
        """
        Connect to Fluvio and open a partition consumer reading from the
        end of the topic (i.e. only new, live frames — not historical
        backlog, which matters for a real-time CCTV feed).
        """
        if not FLUVIO_PACKAGE_AVAILABLE:
            raise FluvioUnavailableError(
                "The 'fluvio' Python package is not installed."
            )
        try:
            self._fluvio = Fluvio.connect()
            self._consumer = self._fluvio.partition_consumer(self.topic_name, self.partition)
            self._stream = self._consumer.stream(Offset.end())
            logger.info(f"Connected to Fluvio. Consuming topic '{self.topic_name}'.")
        except Exception as exc:  # noqa: BLE001
            raise FluvioUnavailableError(
                f"Could not connect a Fluvio consumer: {exc}"
            ) from exc

    def frames(self):
        """
        Generator that yields (frame, capture_timestamp) tuples decoded
        from incoming Fluvio records. Runs until the underlying stream
        is closed or the caller stops iterating.
        """
        if self._stream is None:
            raise FluvioUnavailableError("frames() called before connect().")

        for record in self._stream:
            try:
                raw = record.value_string()
                timestamp_str, payload_b64 = raw.split("|", 1)
                jpeg_bytes = base64.b64decode(payload_b64)
                np_arr = np.frombuffer(jpeg_bytes, dtype=np.uint8)
                frame = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
                if frame is None:
                    continue
                yield frame, float(timestamp_str)
            except Exception as exc:  # noqa: BLE001 - never let one bad record kill the stream
                logger.warning(f"Skipping malformed Fluvio record: {exc}")
                continue

    def close(self) -> None:
        """Release the consumer/connection."""
        self._stream = None
        self._consumer = None
        self._fluvio = None
        logger.info(f"Fluvio consumer for topic '{self.topic_name}' closed.")
