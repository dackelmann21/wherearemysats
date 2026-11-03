"""Structured logging configuration.

Logs MUST NOT contain RPC passwords (REQ 43).
"""

from __future__ import annotations

import logging
import sys
import time


def configure_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    fmt = logging.Formatter(
        "%(asctime)s Z %(levelname)s %(name)s %(message)s"
    )
    # The "Z" claims UTC: format asctime in UTC, not local time (B5-17).
    fmt.converter = time.gmtime
    handler.setFormatter(fmt)
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.INFO)
    for noisy in ("httpx", "httpcore", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)