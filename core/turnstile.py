"""Cloudflare Turnstile verification utilities.

Provides server-side token verification for Turnstile CAPTCHA responses.
"""

import logging
from typing import Any

import httpx
from django.conf import settings
from django.http import HttpRequest

logger = logging.getLogger(__name__)


def verify_turnstile_token(
    token: str,
    action: str,
    request: HttpRequest,
) -> dict[str, Any]:
    """Verify a Turnstile token with Cloudflare's siteverify endpoint.

    Args:
        token: The cf-turnstile-response token from the form submission
        action: The expected action (e.g., "signup", "login")
        request: The HTTP request object to extract client IP

    Returns:
        A dict with:
            - success (bool): Whether verification passed all checks
            - message (str): Human-readable result or error message
            - raw_response (dict): The full response from Cloudflare (for debugging)

    The verification requires:
        - Token is non-empty and reasonable length
        - Cloudflare returns success: true
        - Action matches the expected action
        - Hostname matches the approved list
        - Secret is configured in environment
    """
    if not token or not isinstance(token, str) or len(token) > 2048:
        return {
            "success": False,
            "message": "Invalid token format",
            "raw_response": {},
        }

    secret = settings.TURNSTILE_SECRET
    if not secret:
        logger.error("TURNSTILE_SECRET not configured")
        return {
            "success": False,
            "message": "Turnstile not configured",
            "raw_response": {},
        }

    expected_hostnames = set(settings.TURNSTILE_HOSTNAMES)
    if not expected_hostnames:
        logger.error("TURNSTILE_HOSTNAMES not configured")
        return {
            "success": False,
            "message": "Turnstile not configured",
            "raw_response": {},
        }

    client_ip = get_client_ip(request)

    try:
        response = httpx.post(
            "https://challenges.cloudflare.com/turnstile/v0/siteverify",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "secret": secret,
                "response": token,
                "remoteip": client_ip,
            },
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
