"""Report Dramatiq worker failures to error tracking.

Sentry's DjangoIntegration only sees exceptions that propagate out of an HTTP
request. A Dramatiq actor is not a request, so nothing it raised was ever
reported. LoggingIntegration partly covered the gap by forwarding log records,
which made every "log the failure then re-raise" in a worker load bearing, and
worker failures with no log line at all were invisible.

This closes the gap so the logs can go back to being ordinary log lines.
"""

from __future__ import annotations

from typing import Any

import sentry_sdk
from dramatiq.middleware import Middleware

_MAX_ARG = 200


class ErrorTracking(Middleware):
    """Capture actor exceptions, tagged with which actor and arguments failed.

    The actor name and its arguments are attached as context because a
    traceback does not carry them. That is what ties a failing task back to the
    specific record, payment or package it was working on.
    """

    @property
    def actor_options(self) -> set[str]:
        return set()

    def before_process_message(self, broker, message) -> None:  # noqa: ARG002
        scope = sentry_sdk.get_current_scope()
        scope.set_tag("dramatiq.actor", message.actor_name)
        scope.set_tag("dramatiq.queue", message.queue_name)
        scope.set_context("dramatiq.arguments", _safe_args(message))

    def after_process_message(
        self,
        broker,  # noqa: ARG002
        message,  # noqa: ARG002
        *,
        result: Any = None,  # noqa: ARG002
        exception: BaseException | None = None,
    ) -> None:
        if exception is not None:
            sentry_sdk.capture_exception(exception)

    def after_skip_message(self, broker, message) -> None:  # noqa: ARG002
        """Record a message discarded before it ran.

        AgeLimit drops work silently, so without this a task that never
        executed leaves no trace at all.
        """
        sentry_sdk.capture_message(
            f"Dramatiq message skipped without running: {message.actor_name}"
        )


def _safe_args(message) -> dict[str, str]:
    """Actor arguments, rendered as short strings.

    These are the only record of which record or payment a task was working on,
    so they are worth keeping even when a value cannot be encoded.
    """
    args = {}
    for name, value in (getattr(message, "args", None) or {}).items():
        try:
            args[str(name)] = repr(value)[:_MAX_ARG]
        except Exception:  # pragma: no cover - repr should not raise
            args[str(name)] = f"<unrepresentable {type(value).__name__}>"
    return args
