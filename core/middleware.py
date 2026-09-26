"""Request-level middleware for logging correlation, timezone, and HTMX messages.

Provides four middleware classes:
- RequestIDMiddleware: propagates or generates a unique request ID for tracing.
- TimezoneMiddleware: activates the user's timezone from a cookie.
- PostHogSessionIdMiddleware: links server-side PostHog events to the session recording.
- HtmxMessageMiddleware: injects Django messages into HTMX responses via HX-Trigger.
"""

from __future__ import annotations

import contextvars
import json
import logging
import re
import uuid
from collections.abc import Callable
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.contrib.messages import get_messages
from django.http import HttpRequest, HttpResponse
from django.utils import timezone
from posthog import set_context_session

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_id", default=""
)


class RequestIDMiddleware:
    """Attaches a unique request ID to every request for distributed tracing.

    Uses an incoming "X-Request-ID" header when present, otherwise generates
    a UUID. The ID is set on the request object, stored in a context variable
    for log correlation, and echoed in the response header.
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        request.request_id = request_id  # type: ignore[attr-defined]
        token = request_id_var.set(request_id)
        try:
            response = self.get_response(request)
            response["X-Request-ID"] = request_id
            return response
        finally:
            request_id_var.reset(token)


class RequestIDLogFilter(logging.Filter):
    """Injects the current request ID into every log record.

    Attach this filter to log handlers so that log lines can be correlated
    with specific HTTP requests via the "request_id" attribute.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get("")
        return True


class TimezoneMiddleware:
    """Activates the user's timezone based on a "user_timezone" cookie.

    Sets "django.utils.timezone" to the correct zone so that all template
    date rendering and ORM queries use the user's local time. Falls back to
    the default timezone when the cookie is absent or contains an invalid name.
    """

    COOKIE_NAME = "user_timezone"

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        timezone_name = request.COOKIES.get(self.COOKIE_NAME)

        if timezone_name:
            try:
                timezone.activate(ZoneInfo(timezone_name))
            except ZoneInfoNotFoundError:
                timezone.deactivate()
        else:
            timezone.deactivate()

        return self.get_response(request)


class PostHogSessionIdMiddleware:
    """Attaches the browser's PostHog session ID to the current PostHog context.

    Only the browser and mobile SDKs mint a PostHog session ID, so events
    captured in Django (``user_logged_in``, view-level captures) arrive without
    ``$session_id`` and cannot be used to filter session recordings. The web SDK
    mirrors its session ID into a first-party cookie; this reads that cookie and
    sets it on the request's PostHog context, which the Python SDK merges onto
    every event captured during the request.

    Must be listed *after* ``posthog.integrations.django.PosthogContextMiddleware``
    in ``MIDDLEWARE``: that middleware opens a fresh context per request, so a
    session ID set before it would be discarded.
    """

    COOKIE_NAME = "verity_ph_session"
    SESSION_ID_PATTERN = re.compile(r"^[0-9A-Za-z_-]{1,64}$")

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        # The X-POSTHOG-SESSION-ID header (set by the SDK's `tracing_headers`
        # option) is handled by the PostHog context middleware and wins here.
        if "X-POSTHOG-SESSION-ID" not in request.headers:
            session_id = request.COOKIES.get(self.COOKIE_NAME, "")
            if self.SESSION_ID_PATTERN.match(session_id):
                set_context_session(session_id)

        return self.get_response(request)


class HtmxMessageMiddleware:
    """Bridges Django's message framework with HTMX responses.

    For HTMX requests that are not full-page redirects or refreshes, this
    middleware serializes pending messages into an "HX-Trigger" header so
    the client-side can display them without a full page reload.
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        response = self.get_response(request)

        if not self._should_attach_messages(request, response):
            return response

        messages_list = self._build_messages_list(request)
        if not messages_list:
            return response

        response["HX-Trigger"] = self._build_hx_trigger(
            response.get("HX-Trigger"), messages_list
        )
        return response

    @staticmethod
    def _should_attach_messages(request: HttpRequest, response: HttpResponse) -> bool:
        is_htmx_request = request.headers.get("HX-Request") == "true"
        is_full_page_response = "HX-Redirect" in response or "HX-Refresh" in response
        return is_htmx_request and not is_full_page_response

    @staticmethod
    def _build_messages_list(request: HttpRequest) -> list[dict[str, Any]]:
        storage = get_messages(request)
        return [
            {"message": str(message.message), "level": message.level}
            for message in storage
        ]

    @staticmethod
    def _build_hx_trigger(
        hx_trigger: str | None, messages_list: list[dict[str, Any]]
    ) -> str:
        payload: dict[str, Any] = {"djangoMessages": messages_list}

        if not hx_trigger:
            return json.dumps(payload)

        try:
            trigger_data = json.loads(hx_trigger)
        except ValueError:
            return json.dumps({hx_trigger: {}, "djangoMessages": messages_list})

        if isinstance(trigger_data, dict):
            trigger_data.update(payload)
            return json.dumps(trigger_data)

        return json.dumps(payload)
