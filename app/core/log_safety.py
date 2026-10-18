"""Protect request URLs at application logging and error boundaries."""
from __future__ import annotations

import logging
import re
import urllib.parse

_SECRET_QUERY_KEYS = frozenset({"key", "api_key", "access_token", "token"})
_URL_PATTERN = re.compile(r"https?://[^\s<>\"'\[\](){}\uFF1B\uFF0C\u3002\uFF01\uFF1F]+", re.IGNORECASE)


def redact_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    query = [(key, "<redacted>" if key.casefold() in _SECRET_QUERY_KEYS else value) for key, value in query]
    netloc = parsed.netloc
    if "@" in netloc:
        netloc = "<redacted>@" + netloc.rsplit("@", 1)[1]
    return urllib.parse.urlunsplit(parsed._replace(netloc=netloc, query=urllib.parse.urlencode(query)))


def redact_log_text(text: str) -> str:
    return _URL_PATTERN.sub(lambda match: redact_url(match.group()), text)


class _RequestURLFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        safe_message = redact_log_text(message)
        if safe_message != message:
            record.msg, record.args = safe_message, ()
        return True


class _RedactingFormatter(logging.Formatter):
    def __init__(self, delegate: logging.Formatter | None):
        super().__init__()
        self.delegate = delegate or logging.Formatter()

    def format(self, record: logging.LogRecord) -> str:
        # Redact the final formatted exception too, while preserving the
        # existing formatter's layout, timestamps and optional color handling.
        return redact_log_text(self.delegate.format(record))


def install_log_redaction() -> None:
    """Install once after logging configuration, including direct uvicorn startup."""
    logger = logging.getLogger("httpx")
    if not any(isinstance(item, _RequestURLFilter) for item in logger.filters):
        logger.addFilter(_RequestURLFilter())
    handlers = set(logging.getLogger().handlers)
    for item in logging.Logger.manager.loggerDict.values():
        if isinstance(item, logging.Logger):
            handlers.update(item.handlers)
    for handler in handlers:
        if not isinstance(handler.formatter, _RedactingFormatter):
            handler.setFormatter(_RedactingFormatter(handler.formatter))
