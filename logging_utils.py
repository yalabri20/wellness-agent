"""Structured logging helpers with correlation IDs."""

from __future__ import annotations

import logging
import uuid
from contextvars import ContextVar

correlation_id_var: ContextVar[str] = ContextVar("correlation_id", default="")


def new_correlation_id() -> str:
    """Generate and store a correlation id for the current request."""
    cid = str(uuid.uuid4())
    correlation_id_var.set(cid)
    return cid


def get_correlation_id() -> str:
    """Return current correlation id or empty string."""
    return correlation_id_var.get()


class CorrelationFilter(logging.Filter):
    """Inject correlation_id into every log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.correlation_id = get_correlation_id() or "-"
        return True


def setup_logging(level: int = logging.INFO) -> None:
    """Configure root logger once for the process."""
    root = logging.getLogger()
    if any(isinstance(f, CorrelationFilter) for h in root.handlers for f in h.filters):
        root.setLevel(level)
        return
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)s [%(correlation_id)s] %(name)s: %(message)s"
        )
    )
    handler.addFilter(CorrelationFilter())
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
