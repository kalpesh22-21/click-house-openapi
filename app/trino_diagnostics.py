"""Restricted, bounded SQL diagnostics; never ordinary logs or tool payloads."""

from contextvars import ContextVar
from datetime import datetime, timezone
from functools import wraps
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import threading
import time
from uuid import uuid4

import sqlglot
from sqlglot import exp
from sqlglot.errors import ErrorLevel

_current = ContextVar("trino_diagnostic", default=None)
_handlers = {}
_lock = threading.Lock()
logger = logging.getLogger(__name__)


def redacted_sql(sql, dialect):
    try:
        statements = sqlglot.parse(sql, read=dialect, error_level=ErrorLevel.RAISE)
        if len(statements) != 1 or statements[0] is None:
            return "[unavailable: unparseable SQL]"
        tree = statements[0]
        for node in tree.walk():
            node.comments = None
            # Native ClickHouse SQL is itself a Trino string literal. Do not
            # preserve that embedded string: it contains injected tenant settings.
            if isinstance(node, exp.Literal):
                node.set("this", "[redacted]" if node.is_string else "0")
        return tree.sql(dialect=dialect)
    except Exception:
        return "[unavailable: unparseable SQL]"


def record_physical(sql):
    record = _current.get()
    if record is not None:
        record["trino_sql"] = redacted_sql(sql, "trino")


def record_query_id(value):
    record = _current.get()
    if record is not None and isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        record["trino_query_id"] = value


class _PrivateHandler(RotatingFileHandler):
    def _open(self):
        fd = os.open(self.baseFilename, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        os.fchmod(fd, 0o600)
        return os.fdopen(fd, "a", encoding="utf-8")


def _write(directory, record):
    # Per-worker files avoid cross-process rollover races. Each worker retains
    # at most 4 x 10 MiB. Mount the directory on protected persistent storage.
    with _lock:
        path = Path(directory).resolve()
        key = (str(path), os.getpid())
        handler = _handlers.get(key)
        if handler is None:
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
            path.chmod(0o700)
            handler = _PrivateHandler(path / f"queries-{os.getpid()}.jsonl",
                                      maxBytes=10 * 1024 * 1024, backupCount=3)
            handler.setFormatter(logging.Formatter("%(message)s"))
            _handlers[key] = handler
        message = json.dumps(record, ensure_ascii=False)
        handler.emit(logging.LogRecord("trino.sql", logging.INFO, "", 0, message, (), None))


def trace_execution(function):
    @wraps(function)
    def wrapped(sql, settings=None, parameters=None):
        from app.config import get_settings

        settings = settings or get_settings()
        if not settings.trino_enabled:
            return function(sql, settings, parameters)
        record = {
            "correlation_id": uuid4().hex,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "logical_sql": redacted_sql(sql, "clickhouse"),
            "status": "ok",
        }
        token = _current.set(record)
        started = time.monotonic()
        try:
            return function(sql, settings, parameters)
        except Exception as exc:
            record["status"] = "error"
            code = getattr(exc, "code", "INTERNAL_ERROR")
            record["error_code"] = code if isinstance(code, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", code) else "INTERNAL_ERROR"
            raise
        finally:
            _current.reset(token)
            record["duration_ms"] = round((time.monotonic() - started) * 1000, 3)
            try:
                _write(settings.trino_diagnostics_dir, record)
            except Exception:
                logger.warning("Trino diagnostic storage unavailable: correlation_id=%s", record["correlation_id"])
            logger.info("trino execution correlation_id=%s status=%s duration_ms=%s query_id=%s",
                        record["correlation_id"], record["status"], record["duration_ms"], record.get("trino_query_id", "-"))
    return wrapped
