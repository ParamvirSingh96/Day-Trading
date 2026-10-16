from __future__ import annotations

import logging
import sys


def configure_logging(app_env: str = "development") -> None:
    """Configure structured logging for the application.

    - development: DEBUG level, human-readable format to stdout.
    - production:  INFO level, machine-parseable format suited for log aggregators.

    All tradehack loggers are under the 'tradehack' namespace so they can be
    filtered independently from noisy third-party library logs.
    """
    level = logging.DEBUG if app_env == "development" else logging.INFO

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.WARNING)

    app_logger = logging.getLogger("tradehack")
    app_logger.setLevel(level)
    app_logger.addHandler(handler)
    app_logger.propagate = False
