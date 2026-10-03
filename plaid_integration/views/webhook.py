"""Plaid webhook handler and JWT signature verification."""

import hashlib
import json
import logging

import jwt
import requests
from django.conf import settings
from django.core.cache import cache
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

WEBHOOK_MAX_BODY_SIZE = 1024 * 100
KEY_CACHE_TTL = 3600
_KEY_CACHE_PREFIX = "plaid:webhook_key:"


def _fetch_signing_key(kid: str) -> dict | None:
    """Fetch Plaid's signing key for "kid".

    Plaid serves the key from an authenticated POST that requires the key id,
    which is the "kid" carried in the webhook's JWT header.
    """
    host = settings.PLAID_ENV.lower()
    try:
        response = requests.post(
            f"https://{host}.plaid.com/webhook_verification_key/get",
            json={
                "client_id": settings.PLAID_CLIENT_ID,
                "secret": settings.PLAID_SECRET,
                "key_id": kid,
            },
            timeout=10,
        )
        response.raise_for_status()
        return response.json().get("key")
    except (requests.RequestException, OSError, ValueError, KeyError) as exc:
        # Any failure to obtain a trusted key means we cannot verify, so this
        # fails closed and is never cached.
        logger.error("Could not fetch Plaid signing key for kid=%s: %s", kid, exc)
        return None


def _signing_key(kid: str) -> dict | None:
    """Return Plaid's signing key for "kid", cached per key id.

    Caching per kid means a rotation costs one extra fetch rather than
    thrashing a single slot between the outgoing and incoming key. Failures
    are never cached, so a transient outage self-heals on the next webhook
    instead of blocking verification for the full TTL.
    """
    cache_key = f"{_KEY_CACHE_PREFIX}{kid}"
    cached = cache.get(cache_key)
    if cached:
        return cached

    jwk = _fetch_signing_key(kid)
    if jwk:
        cache.set(cache_key, jwk, KEY_CACHE_TTL)
    return jwk


def verify_plaid_webhook(body: bytes, plaid_verification: str | None) -> bool:
    """Verify a Plaid webhook's JWT signature and body hash.

    Plaid signs with ES256; the algorithm is read from the key rather than
    hardcoded so a token can never dictate its own verification algorithm.
    """
    if not plaid_verification:
        logger.warning("Missing Plaid-Verification header")
        return False

    try:
        kid = jwt.get_unverified_header(plaid_verification).get("kid", "")
        jwk = _signing_key(kid)
        if not jwk:
            logger.warning("No Plaid signing key available for kid=%s", kid)
            return False

        key = jwt.PyJWK.from_dict(jwk)
        claims = jwt.decode(
            plaid_verification,
            key.key,
            algorithms=[key.algorithm_name],
            options={"verify_iat": True, "verify_exp": True},
        )
    except (jwt.PyJWTError, ValueError, TypeError, KeyError) as exc:
        logger.warning("Plaid webhook JWT verification failed: %s", exc)
        return False

    if claims.get("request_body_sha256") != hashlib.sha256(body).hexdigest():
        logger.warning("Plaid webhook body hash mismatch")
        return False
    return True


@csrf_exempt
@require_POST
def plaid_webhook(request: HttpRequest) -> HttpResponse:
    """Handle incoming Plaid webhooks for transaction and credential events."""
    if len(request.body) > WEBHOOK_MAX_BODY_SIZE:
        logger.warning("Plaid webhook body too large: %d bytes", len(request.body))
        return HttpResponseBadRequest("Payload too large")

    try:
        payload = json.loads(request.body)
    except (ValueError, TypeError):
        return HttpResponseBadRequest("Invalid JSON")

    # Signature verification is mandatory everywhere except a local sandbox
    verification_required = not (settings.DEBUG and settings.PLAID_ENV == "sandbox")
    if verification_required and not verify_plaid_webhook(
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
