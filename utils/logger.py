"""
utils/logger.py
================
Provides a single, project-wide logger factory so every module logs to the
same rotating file (logs/stampede_system.log) and to the console, with a
consistent format.

Save this file at: Stampede-Prediction-System/utils/logger.py
"""

import logging
from logging.handlers import RotatingFileHandler

import config


def get_logger(name: str) -> logging.Logger:
    """
    Return a configured logger for the given module name.

    Usage:
        from utils.logger import get_logger
        logger = get_logger(__name__)
        logger.info("message")
    """
    logger = logging.getLogger(name)

    # Avoid attaching duplicate handlers if get_logger() is called
    # multiple times for the same module (e.g. on Flask debug reloads).
    if logger.handlers:
        return logger

    logger.setLevel(getattr(logging, config.LOG_LEVEL, logging.INFO))

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Rotating file handler: keeps log files from growing unbounded.
    file_handler = RotatingFileHandler(
        config.LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)

    # Console handler so logs are also visible in the terminal during dev.
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    logger.propagate = False

    return logger
