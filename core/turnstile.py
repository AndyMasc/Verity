"""Cloudflare Turnstile verification utilities.

Provides server-side token verification for Turnstile CAPTCHA responses.
"""

import logging
import os
import sys
from typing import Any

import httpx
from django.conf import settings
from django.http import HttpRequest

logger = logging.getLogger(__name__)


def _test_env() -> bool:
    """True when running under pytest (module import or active test session)."""
    return "pytest" in sys.modules or bool(os.environ.get("PYTEST_CURRENT_TEST"))


def _stub_result(action: str) -> dict[str, Any]:
    """Successful stub returned when Turnstile is not configured during testing."""
    return {
        "success": True,
        "message": "Turnstile disabled in test environment",
        "raw_response": {"success": True, "action": action, "hostname": "testserver"},
    }


def _failure(message: str, raw_response: Any) -> dict[str, Any]:
    return {"success": False, "message": message, "raw_response": raw_response}


def turnstile_enabled() -> bool:
    """Whether server-side Turnstile verification should be enforced.

    Returns False for pytest runs and for deployments that have not configured
    a secret or hostname allowlist, so local and CI development are never
    blocked by an unconfigured widget.
    """
    if not getattr(settings, "TURNSTILE_ENABLED", True) or _test_env():
        return False
    return bool(settings.TURNSTILE_SECRET and settings.TURNSTILE_HOSTNAMES)


def verify_turnstile_token(
    token: str,
    action: str,
    request: HttpRequest | None = None,
) -> dict[str, Any]:
    """Verify a Turnstile token with Cloudflare's siteverify endpoint.

    ``request`` is optional and only used to attach ``remoteip`` (an optional
    siteverify parameter) when it can be derived reliably from the request.

    In tests and local developer setups without a real Turnstile secret, return a
    successful stub result instead of blocking form submission. The app still
    validates the token format and action checks where the secret is configured.
    """
    if not token or not isinstance(token, str) or len(token) > 2048:
        return _failure("Invalid token format", {})

    if not settings.TURNSTILE_SECRET:
        if _test_env() or getattr(settings, "RATELIMIT_ENABLE", True) is False:
            return _stub_result(action)
        logger.error("TURNSTILE_SECRET not configured")
        return _failure("Turnstile not configured", {})

    expected_hostnames = set(settings.TURNSTILE_HOSTNAMES)
    if not expected_hostnames:
        if _test_env():
            return _stub_result(action)
        logger.error("TURNSTILE_HOSTNAMES not configured")
        return _failure("Turnstile not configured", {})

    verify_data: dict[str, str] = {
        "secret": settings.TURNSTILE_SECRET,
        "response": token,
    }
    if client_ip := (get_client_ip(request) if request is not None else ""):
        verify_data["remoteip"] = client_ip

    try:
        response = httpx.post(
            "https://challenges.cloudflare.com/turnstile/v0/siteverify",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data=verify_data,
            timeout=10.0,
        )
        if response.status_code != 200:
            logger.error("Turnstile siteverify failed: %s", response.status_code)
            return _failure("Verification service error", {"status_code": response.status_code})
        result = response.json()
    except Exception as e:
        logger.error("Turnstile verification error: %s", e)
        return _failure("Verification service unavailable", {})

    if not result.get("success"):
        logger.warning("Turnstile verification failed: %s", result.get("error-codes", []))
        return _failure("Verification failed", result)

    if result.get("action") != action:
        logger.warning(
            "Turnstile action mismatch: expected %s, got %s",
            action,
            result.get("action"),
        )
        return _failure("Action mismatch", result)

    if result.get("hostname") not in expected_hostnames:
        logger.warning(
            "Turnstile hostname mismatch: %s not in %s",
            result.get("hostname"),
            expected_hostnames,
        )
        return _failure("Hostname verification failed", result)

    return {"success": True, "message": "Verification successful", "raw_response": result}


def get_client_ip(request: HttpRequest) -> str:
    """Extract client IP from request, handling proxies.

    Checks X-Forwarded-For header first (for proxied requests),
    then falls back to REMOTE_ADDR.
    """
    x_forwarded_for = request.headers.get("x-forwarded-for")
    if x_forwarded_for:
        ip = x_forwarded_for.split(",")[0].strip()
        return ip
    return request.META.get("REMOTE_ADDR", "")
