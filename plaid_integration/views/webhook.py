"""Plaid webhook handler and JWT signature verification."""

import datetime
import hashlib
import json
import logging
from typing import Any

import jwt
import requests
from django.conf import settings
from django.http import (
    HttpRequest,
    HttpResponse,
    HttpResponseBadRequest,
    HttpResponseForbidden,
)
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from ..models import PlaidItem
from ..services import route_webhook

logger: logging.Logger = logging.getLogger(__name__)

PLAID_JWKS_URL = "https://plaid.com/auth/v1/webhook_public_key"
_jwks_cache: dict[str, Any] = {}
_jwks_fetched_at: float | None = None

WEBHOOK_MAX_BODY_SIZE = 1024 * 100  # 100KB


def _get_plaid_jwk(kid: str, max_age: int = 3600) -> dict[str, Any] | None:
    """Fetch and cache a Plaid JSON Web Key by key ID."""
    global _jwks_cache, _jwks_fetched_at
    now = datetime.datetime.now(datetime.UTC).timestamp()
    if not _jwks_fetched_at or (now - _jwks_fetched_at) > max_age:
        try:
            resp = requests.get(PLAID_JWKS_URL, timeout=10)
            resp.raise_for_status()
            keys = resp.json().get("keys", [])
            _jwks_cache = {k["kid"]: k for k in keys}
            _jwks_fetched_at = now
        except Exception:
            logger.exception("Failed to fetch Plaid JWKS")
            return None
    return _jwks_cache.get(kid)


def verify_plaid_webhook(body: bytes, plaid_verification: str | None) -> bool:
    """Verify the JWT signature and body hash of an incoming Plaid webhook."""
    if not plaid_verification:
        logger.warning("Missing Plaid-Verification header")
        return False
    try:
        unverified = jwt.decode(plaid_verification, options={"verify_signature": False})
        kid = unverified.get("kid", "")
        jwk = _get_plaid_jwk(kid)
        if not jwk:
            logger.warning("No Plaid JWK found for kid=%s", kid)
            return False

        public_key = jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(jwk))
        claims = jwt.decode(
            plaid_verification,
            public_key,
            algorithms=["RS256"],
            options={"verify_iat": True, "verify_exp": True},
        )
        body_hash = hashlib.sha256(body).hexdigest()
        if claims.get("request_body_sha256") != body_hash:
            logger.warning("Plaid webhook body hash mismatch")
            return False
        return True
    except jwt.PyJWTError as e:
        logger.warning("Plaid webhook JWT verification failed: %s", e)
        return False


@csrf_exempt
@require_POST
def plaid_webhook(request: HttpRequest) -> HttpResponse:
    """Handle incoming Plaid webhooks for transaction and credential events."""
    if len(request.body) > WEBHOOK_MAX_BODY_SIZE:
        logger.warning("Plaid webhook body too large: %d bytes", len(request.body))
        return HttpResponseBadRequest("Payload too large")

    try:
        payload = json.loads(request.body)
    except ValueError, TypeError:
        return HttpResponseBadRequest("Invalid JSON")

    if settings.PLAID_ENV != "sandbox" and not verify_plaid_webhook(
        request.body, request.headers.get("Plaid-Verification")
    ):
        logger.warning("Plaid webhook verification failed for %s", payload.get("item_id"))
        return HttpResponseForbidden("Invalid webhook signature")

    webhook_type: str = payload.get("webhook_type", "")
    webhook_code: str = payload.get("webhook_code", "")
    item_id: str = payload.get("item_id", "")

    logger.info(
        "Plaid webhook received: %s / %s for item %s",
        webhook_type,
        webhook_code,
        item_id,
    )

    try:
        plaid_item = PlaidItem.objects.get(item_id=item_id)
    except PlaidItem.DoesNotExist:
        logger.warning("Webhook received for unknown item %s", item_id)
        return HttpResponse("OK")

    route_webhook(webhook_code, plaid_item, payload)

    return HttpResponse("OK")
