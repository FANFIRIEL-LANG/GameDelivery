from __future__ import annotations

import logging
import os
import traceback
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path
from threading import RLock


_LOGGER_NAME = "gamedelivery"
_CONFIG_LOCK = RLock()
_SECRET_PATTERNS = (
    re.compile(r"(?i)\b(IPCPassword|password|passwd|trade[_ -]?token|api[_ -]?key|authorization)\b(\s*[:=]\s*)([^\s,;]+)"),
    re.compile(r"(?i)([?&]token=)[^&#\s]+"),
    re.compile(r"(?i)(Bearer\s+)[A-Za-z0-9._~+/=-]+"),
)


def redact_sensitive_text(value: str) -> str:
    """Remove common credentials and Steam trade tokens from diagnostic text."""
    redacted = value
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub(lambda match: match.group(1) + match.group(2) + "[REDACTED]"
                               if match.lastindex and match.lastindex >= 3
                               else match.group(1) + "[REDACTED]", redacted)
    return redacted


class _RedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            rendered = record.getMessage()
        except Exception:
            rendered = "Log message could not be formatted"
        record.msg = redact_sensitive_text(rendered)
        record.args = ()
        if record.exc_text:
            record.exc_text = redact_sensitive_text(record.exc_text)
        return True


def get_log_directory() -> Path:
    """Return the per-user GameDelivery log directory without creating it."""
    root = os.environ.get("LOCALAPPDATA")
    if root:
        return Path(root) / "GameDelivery" / "logs"
    xdg_root = os.environ.get("XDG_STATE_HOME")
    if xdg_root:
        return Path(xdg_root) / "GameDelivery" / "logs"
    return Path.home() / ".local" / "state" / "GameDelivery" / "logs"


def configure_logging(*, log_directory: str | Path | None = None) -> Path:
    """Configure one rotating UTF-8 application log; safe to call more than once."""
    directory = Path(log_directory) if log_directory is not None else get_log_directory()
    with _CONFIG_LOCK:
        logger = logging.getLogger(_LOGGER_NAME)
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        directory.mkdir(parents=True, exist_ok=True)
        target = (directory / "gamedelivery.log").resolve()
        for handler in logger.handlers:
            if isinstance(handler, RotatingFileHandler) and Path(handler.baseFilename).resolve() == target:
                return directory

        handler = RotatingFileHandler(
            target,
            maxBytes=2 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
            delay=True,
        )
        handler.setLevel(logging.DEBUG)
        handler.setFormatter(logging.Formatter(
            "%(asctime)s.%(msecs)03d %(levelname)s [%(name)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))
        handler.addFilter(_RedactionFilter())
        logger.addHandler(handler)
    return directory


def get_logger(component: str) -> logging.Logger:
    """Return a component logger under the GameDelivery application namespace."""
    name = component.strip(". ")
    return logging.getLogger(_LOGGER_NAME + ("." + name if name else ""))


def log_exception(logger: logging.Logger, event: str, exception: BaseException) -> None:
    """Log exception type and a sanitized traceback without echoing arbitrary secrets."""
    logger.error("%s failed; exception_type=%s", event, type(exception).__name__)
    # Render the traceback into a message so the redaction filter processes it
    # before it reaches the file handler.
    rendered = "".join(traceback.format_exception(type(exception), exception, exception.__traceback__))
    logger.debug("Sanitized traceback for event=%s:\\n%s", event, rendered)
