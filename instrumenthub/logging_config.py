from __future__ import annotations

import json
import logging
from logging.handlers import RotatingFileHandler

from .config import Settings
from .security import utc_text


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": utc_text(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in ("request_id", "method", "path", "status", "duration_ms"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def configure_logging(settings: Settings) -> None:
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    target = str((settings.log_dir / "instrumenthub.jsonl").resolve())
    logger = logging.getLogger("instrumenthub")
    logger.setLevel(logging.INFO)
    if any(getattr(handler, "baseFilename", None) == target for handler in logger.handlers):
        return
    handler = RotatingFileHandler(target, maxBytes=5_000_000, backupCount=10, encoding="utf-8")
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
    logger.propagate = False

