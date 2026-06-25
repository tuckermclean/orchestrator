"""Application logging configuration.

Installs a stdout StreamHandler on the ``src`` package logger so that all
application-level ``_log = logging.getLogger(__name__)`` calls in ``src.*``
modules reach ``kubectl logs`` / stdout.

Without explicit configuration, the container starts via
``uvicorn src.api.main:app``, which configures only uvicorn's own loggers.
Application loggers (``src.*``) have no handler and their records are
silently swallowed.

Usage
-----
Call :func:`configure_logging` once at application startup, before any other
``src.*`` module emits a log record.  Multiple calls are idempotent: the
handler is added only once.

The log level is controlled by the ``LOG_LEVEL`` environment variable
(default ``"INFO"``).  Valid values are the standard Python level names:
``DEBUG``, ``INFO``, ``WARNING``, ``ERROR``, ``CRITICAL``.

Design constraints
------------------
- Only the ``src`` logger and the root logger are touched; uvicorn's own
  loggers (``uvicorn``, ``uvicorn.access``, ``uvicorn.error``) are left
  untouched so access-log and startup lines are not duplicated.
- The handler is added with a sentinel attribute (``_orchestrator_installed``)
  so repeated calls (e.g. in a test that re-imports) are no-ops.
"""

from __future__ import annotations

import logging
import os
import sys

_SENTINEL_ATTR = "_orchestrator_installed"
_LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s  %(message)s"
_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S"


def configure_logging() -> None:
    """Install a stdout StreamHandler on the ``src`` package logger.

    Idempotent: safe to call multiple times (e.g. during test setUp).
    Does not touch ``uvicorn*`` loggers.
    """
    raw_level = os.environ.get("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, raw_level, logging.INFO)

    src_logger = logging.getLogger("src")

    # Idempotency guard: skip if our handler is already installed.
    if any(getattr(h, _SENTINEL_ATTR, False) for h in src_logger.handlers):
        return

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(fmt=_LOG_FORMAT, datefmt=_DATE_FORMAT))
    setattr(handler, _SENTINEL_ATTR, True)

    src_logger.setLevel(level)
    src_logger.addHandler(handler)
    # Propagate=True (the default) means records also reach the root logger.
    # We set it explicitly to False here so that if the root logger has its
    # own handler (e.g. uvicorn's fallback handler), records from src.* do
    # not get double-emitted.
    src_logger.propagate = False
