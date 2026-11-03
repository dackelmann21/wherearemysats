"""Application entry point for the Bitcoin Lineage Tracker."""

from __future__ import annotations

import logging
import os
import sys

import uvicorn

from backend.config import get_settings
from backend.logging_config import configure_logging

def main() -> None:
    # Newly created SQLite/WAL/config files are private to the service UID.
    os.umask(0o077)
    settings = get_settings()
    configure_logging()
    logger = logging.getLogger("lineage")
    logger.info(
        "Bitcoin Lineage Tracker starting (host=%s port=%s)",
        settings.app_host,
        settings.app_port,
    )
    uvicorn.run(
        "backend.api.server:app",
        host=settings.app_host,
        port=settings.app_port,
        log_level="info",
        access_log=False,
    )


if __name__ == "__main__":
    main()
