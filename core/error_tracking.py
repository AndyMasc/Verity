"""Error tracking for the two paths Django's integration does not cover.

A Dramatiq actor is not a request, so nothing hooks into worker failures.
"ErrorTracking" reports those. An error the app catches and continues past never
becomes an exception either, so "LogCapture" reports those from the log stream.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

from dramatiq.middleware import Middleware

_MAX_TEXT = 2000
_MAX_ARG = 200
# "posthog" is skipped on purpose: a failure to report would otherwise recurse
# into another failure to report, and core.posthog_logs owns that namespace for
# PostHog's logs product.
_INTERNAL = ("posthog", "core.error_tracking")


def _client():
    from core.apps import posthog_client

    return posthog_client


def _text(value: Any, limit: int = _MAX_TEXT) -> str:
    try:
        return str(value)[:limit]
    except Exception:
        return f"<unrepresentable {type(value).__name__}>"


class ErrorTracking(Middleware):
    """Report actor exceptions, carrying which actor and arguments failed.

    The traceback does not say which record or payment a task was working on.
    """

    def after_process_message(
        self,
        broker,  # noqa: ARG002
        message,
        *,
        result: Any = None,  # noqa: ARG002
        exception: BaseException | None = None,
    ) -> None:
        if exception is None:
            return
        client = _client()
        if client is None:
            return
        client.capture_exception(
            exception,
            properties={
                "actor": message.actor_name,
                "queue": message.queue_name,
                "arguments": {
                    str(k): _text(v, _MAX_ARG) for k, v in (message.args or {}).items()
                },
            },
        )

    def after_skip_message(self, broker, message) -> None:  # noqa: ARG002
        """Work discarded before it ran, by AgeLimit or similar."""
        client = _client()
        if client is not None:
            client.capture(
                "dramatiq_message_skipped",
                properties={"actor": message.actor_name},
            )


class LogCapture(logging.Handler):
    """Forward warning and error log records to error tracking. The level
    is checked here as well so that attaching this handler without
    a level cannot turn every debug line into an error.
    """

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if record.levelno < logging.WARNING:
                return
            if record.name.split(".")[0] in _INTERNAL:
                return
            client = _client()
            if client is None:
                return

            # Logging from inside an except block leaves the original exception
            # live in sys.exc_info() even when the call passed no exc_info, so a
            # caught failure still reports with its traceback.
            exc = record.exc_info[1] if record.exc_info else sys.exc_info()[1]
            if exc is None:
                exc = RuntimeError(record.getMessage())

            client.capture_exception(
                exc,
                properties={
                    "level": record.levelname,
                    "logger": record.name,
                    "module": record.module,
                    "line": record.lineno,
                },
            )
        except Exception:
            self.handleError(record)

    def handleError(self, record: logging.LogRecord) -> None:
        # Never raise into whatever logged.
        pass
