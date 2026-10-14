"""Deployment error logging without exception text or request payloads."""

import logging
import re
import sys
import traceback


class SafeExceptionFormatter(logging.Formatter):
    """Retain stack locations without exception messages or chained SQL errors."""

    def formatException(self, info) -> str:
        frames = traceback.extract_tb(info[2])
        locations = "\n".join(
            f"  {frame.filename}:{frame.lineno} in {frame.name}" for frame in frames
        )
        return f"failure_class={info[0].__name__}\n{locations}"


def configure_logging(level: str, *, stdio: bool = False) -> None:
    # ERROR must remain visible even if a deployment requests CRITICAL only.
    logging.basicConfig(
        level=min(getattr(logging, level.upper(), logging.INFO), logging.ERROR),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr if stdio else sys.stdout,
        force=True,
    )
    formatter = SafeExceptionFormatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    for handler in logging.getLogger().handlers:
        handler.setFormatter(formatter)
    # Uvicorn owns separate handlers and can log the original exception after
    # Starlette has returned our sanitized 500 response. Protect that path too.
    for name in ("uvicorn", "uvicorn.error"):
        for handler in logging.getLogger(name).handlers:
            handler.setFormatter(formatter)
            if isinstance(handler, logging.StreamHandler):
                handler.setStream(sys.stderr if stdio else sys.stdout)


def log_failure(
    logger: logging.Logger,
    operation: str,
    exc: Exception | None = None,
    *,
    code: str = "INTERNAL_ERROR",
    status: int | None = None,
) -> None:
    candidate = getattr(exc, "code", code)
    safe_code = (
        candidate
        if isinstance(candidate, str)
        and re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", candidate)
        else "INTERNAL_ERROR"
    )
    logger.error(
        "operation=%s code=%s status=%s failure_class=%s",
        operation,
        safe_code,
        status if status is not None else "-",
        type(exc).__name__ if exc is not None else "-",
    )
