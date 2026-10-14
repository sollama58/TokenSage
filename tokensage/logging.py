"""structlog setup: JSON lines on Render, pretty in a terminal."""

from __future__ import annotations

import logging
import sys

import structlog


def configure_logging(level: str = "info") -> None:
    lvl = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=lvl)
    # httpx logs every request's full URL at INFO, and the RPC and callback URLs carry secrets
    # (the Helius key is in SOLANA_RPC_URL), so keep the HTTP client loggers to warnings.
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(max(lvl, logging.WARNING))
    renderer: structlog.types.Processor
    if sys.stdout.isatty():
        renderer = structlog.dev.ConsoleRenderer()
    else:
        renderer = structlog.processors.JSONRenderer()
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(lvl),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
