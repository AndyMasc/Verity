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


def turnstile_enabled() -> bool:
    """Whether server-side Turnstile verification should be enforced.

    Returns False for pytest runs and for deployments that have not configured
    a secret or hostname allowlist, so local and CI development are never
    blocked by an unconfigured widget.
    """
    if not getattr(settings, "TURNSTILE_ENABLED", True):
        return False
    if "pytest" in sys.modules or os.environ.get("PYTEST_CURRENT_TEST"):
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
        return {
            "success": False,
            "message": "Invalid token format",
            "raw_response": {},
        }

    secret = settings.TURNSTILE_SECRET
    if not secret:
        if "pytest" in sys.modules or getattr(settings, "RATELIMIT_ENABLE", True) is False:
            return {
                "success": True,
                "message": "Turnstile disabled in test environment",
                "raw_response": {"success": True, "action": action, "hostname": "testserver"},
            }
        logger.error("TURNSTILE_SECRET not configured")
        return {
            "success": False,
            "message": "Turnstile not configured",
            "raw_response": {},
        }

    expected_hostnames = set(settings.TURNSTILE_HOSTNAMES)
    if not expected_hostnames:
        if "pytest" in sys.modules:
            return {
                "success": True,
                "message": "Turnstile disabled in test environment",
                "raw_response": {"success": True, "action": action, "hostname": "testserver"},
            }
        logger.error("TURNSTILE_HOSTNAMES not configured")
        return {
            "success": False,
            "message": "Turnstile not configured",
            "raw_response": {},
        }

    client_ip = get_client_ip(request) if request is not None else ""

    verify_data: dict[str, str] = {
        "secret": secret,
        "response": token,
    }
    if client_ip:
        verify_data["remoteip"] = client_ip

    try:
        response = httpx.post(
            "https://challenges.cloudflare.com/turnstile/v0/siteverify",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data=verify_data,
            timeout=10.0,
        )

        if response.status_code != 200:
            logger.error(f"Turnstile siteverify failed: {response.status_code}")
            return {
                "success": False,
                "message": "Verification service error",
                "raw_response": {"status_code": response.status_code},
            }

        result = response.json()

    except Exception as e:
        logger.error(f"Turnstile verification error: {e}")
        return {
            "success": False,
            "message": "Verification service unavailable",
            "raw_response": {},
        }

    if not result.get("success"):
        logger.warning(
            f"Turnstile verification failed: {result.get('error-codes', [])}",
        )
        return {
            "success": False,
            "message": "Verification failed",
            "raw_response": result,
        }

    returned_action = result.get("action")
    if returned_action != action:
        logger.warning(
            f"Turnstile action mismatch: expected {action}, got {returned_action}",
        )
        return {
            "success": False,
            "message": "Action mismatch",
            "raw_response": result,
        }

    returned_hostname = result.get("hostname")
    if returned_hostname not in expected_hostnames:
        logger.warning(
            f"Turnstile hostname mismatch: {returned_hostname} not in {expected_hostnames}",
        )
        return {
            "success": False,
            "message": "Hostname verification failed",
            "raw_response": result,
        }

    return {
        "success": True,
        "message": "Verification successful",
        "raw_response": result,
    }


def get_client_ip(request: HttpRequest) -> str:
    """Extract client IP from request, handling proxies.

    Checks X-Forwarded-For header first (for proxied requests),
    then falls back to REMOTE_ADDR.
    """
    x_forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR")
    if x_forwarded_for:
        ip = x_forwarded_for.split(",")[0].strip()
        return ip
    return request.META.get("REMOTE_ADDR", "")
