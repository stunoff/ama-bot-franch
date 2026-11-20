"""Redact credentials before HTTP log records and operator errors are emitted."""

import logging
import re
import traceback
from ama.config import settings


def redact(value, secrets=()):
    text = str(value)
    cfg = settings()
    values = [*secrets, cfg.telegram_token, cfg.max_token, cfg.telegram_webhook_secret, cfg.max_webhook_secret]
    for secret in sorted({s for s in values if s}, key=len, reverse=True):
        text = text.replace(secret, "[REDACTED]")
    return re.sub(r'(https?://api\.telegram\.org/bot)[^/\s?"<>]+', r"\1[REDACTED]", text)


class CredentialFilter(logging.Filter):
    def filter(self, record):
        record.msg = redact(record.getMessage())
        record.args = ()
        if record.exc_info:
            record.exc_text = redact("".join(traceback.format_exception(*record.exc_info)))
            record.exc_info = None
        return True


def configure():
    for handler in logging.getLogger().handlers:
        if not any(isinstance(f, CredentialFilter) for f in handler.filters):
            handler.addFilter(CredentialFilter())
    names = {"httpx", "httpcore"} | {
        name for name in logging.Logger.manager.loggerDict if name.startswith(("httpx.", "httpcore."))
    }
    for name in names:
        logger = logging.getLogger(name)
        if not any(isinstance(f, CredentialFilter) for f in logger.filters):
            logger.addFilter(CredentialFilter())


def response_error(response, secrets=()):
    description = ""
    try:
        data = response.json()
        if isinstance(data, dict):
            description = str(data.get("description") or data.get("message") or data.get("error") or "")
    except (ValueError, TypeError):
        pass
    # Keep diagnostics bounded and on one line; never dump request URLs or bodies.
    description = " ".join(redact(description, secrets).split())[:500]
    return f"HTTP {response.status_code}" + (f": {description}" if description else "")
