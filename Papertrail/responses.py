"""Standardized API response helpers for the Papertrail project.

Provides convenience functions that return consistent JSON error/success
bodies. When the request comes from an HTMX client, responses include
an "HX-Trigger" header to display toast notifications in the UI.
"""

import json
from typing import Any

from django.http import HttpRequest, HttpResponse, JsonResponse


def api_error(
    request: HttpRequest,
    message: str,
    code: str = "error",
    status: int = 400,
    details: dict | None = None,
) -> HttpResponse:
    """Return a standardized JSON error response.

    For HTMX requests, includes an "HX-Trigger" header that shows
    an error toast in the frontend. The "details" dict is optional
    and provides additional context for validation errors.
    """
    body: dict[str, Any] = {"error": {"code": code, "message": message}}
    if details:
        body["error"]["details"] = details

    if request.headers.get("HX-Request") == "true":
        response = HttpResponse(json.dumps(body), status=status, content_type="application/json")
        response["HX-Trigger"] = json.dumps({"showToast": {"text": message, "tags": "error"}})
        return response

    return JsonResponse(body, status=status)
