"""Request-level middleware for logging correlation, timezone, and HTMX messages.

Provides three middleware classes:
- RequestIDMiddleware: propagates or generates a unique request ID for tracing.
- TimezoneMiddleware: activates the user's timezone from a cookie.
- HtmxMessageMiddleware: injects Django messages into HTMX responses via HX-Trigger.
"""

from __future__ import annotations

import contextvars
import json
import logging
import uuid
from collections.abc import Callable
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.contrib.messages import get_messages
from django.http import HttpRequest, HttpResponse
from django.utils import timezone

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="")


class RequestIDMiddleware:
    """Attaches a unique request ID to every request for distributed tracing.

    Uses an incoming ``X-Request-ID`` header when present, otherwise generates
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
    with specific HTTP requests via the ``request_id`` attribute.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get("")
        return True


class TimezoneMiddleware:
    """Activates the user's timezone based on a ``user_timezone`` cookie.

    Sets ``django.utils.timezone`` to the correct zone so that all template
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


class HtmxMessageMiddleware:
    """Bridges Django's message framework with HTMX responses.

    For HTMX requests that are not full-page redirects or refreshes, this
    middleware serializes pending messages into an ``HX-Trigger`` header so
    the client-side can display them without a full page reload.
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        response = self.get_response(request)

        if (
            request.headers.get("HX-Request") == "true"
            and "HX-Redirect" not in response
            and "HX-Refresh" not in response
        ):
            storage = get_messages(request)
            messages_list: list[dict[str, Any]] = []

            for message in storage:
                messages_list.append({"message": str(message.message), "level": message.level})

            if messages_list:
                hx_trigger = response.get("HX-Trigger")

                payload: dict[str, Any] = {"djangoMessages": messages_list}

                if hx_trigger:
                    try:
                        trigger_data = json.loads(hx_trigger)
                        if isinstance(trigger_data, dict):
                            trigger_data.update(payload)
                            response["HX-Trigger"] = json.dumps(trigger_data)
                    except ValueError:
                        response["HX-Trigger"] = json.dumps(
                            {hx_trigger: {}, "djangoMessages": messages_list}
                        )
                else:
                    response["HX-Trigger"] = json.dumps(payload)

        return response
