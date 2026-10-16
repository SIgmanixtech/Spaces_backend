"""Logging with per-request correlation IDs.

"json" writes one structured line per record, for log collectors. "text" writes
one readable line per record, and per API request:

    12:46:54  GET    /api/v1/properties  -> 200  (34.2 ms)
"""

import json
import logging
import sys
from contextvars import ContextVar
from datetime import UTC, datetime

request_id_ctx: ContextVar[str | None] = ContextVar("request_id", default=None)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        request_id = request_id_ctx.get()
        if request_id:
            payload["request_id"] = request_id
        context = getattr(record, "context", None)
        if isinstance(context, dict):
            payload.update(context)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class TextFormatter(logging.Formatter):
    _COLOURS = {2: "\033[32m", 3: "\033[36m", 4: "\033[33m", 5: "\033[31m"}

    def __init__(self, colour: bool) -> None:
        super().__init__()
        self.colour = colour

    def format(self, record: logging.LogRecord) -> str:
        when = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
        context = getattr(record, "context", None)
        if record.getMessage() == "request" and isinstance(context, dict):
            status = int(context.get("status", 0))
            shown = str(status)
            if self.colour and status // 100 in self._COLOURS:
                shown = f"{self._COLOURS[status // 100]}{status}\033[0m"
            line = (
                f"{when}  {context.get('method', ''):<6} {context.get('path', '')}  -> "
                f"{shown}  ({context.get('duration_ms', '?')} ms)"
            )
        else:
            line = f"{when}  {record.levelname:<7} {record.name}: {record.getMessage()}"
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


def configure_logging(level: str, fmt: str = "json") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        TextFormatter(colour=sys.stdout.isatty()) if fmt == "text" else JsonFormatter()
    )
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
    # SQL statements can carry customer data; keep them out of the logs.
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
