"""Plaid webhook handler and JWT signature verification."""

import hashlib
import json
import logging

import jwt
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
WEBHOOK_MAX_BODY_SIZE = 1024 * 100

# Caches Plaid's public keys for an hour, well inside their rotation window.
jwks_client = jwt.PyJWKClient(PLAID_JWKS_URL, lifespan=3600)


def verify_plaid_webhook(body: bytes, plaid_verification: str | None) -> bool:
    """Verify a Plaid webhook's JWT signature and body hash."""
    if not plaid_verification:
        logger.warning("Missing Plaid-Verification header")
        return False

    try:
        signing_key = jwks_client.get_signing_key_from_jwt(plaid_verification)
        claims = jwt.decode(
            plaid_verification,
            signing_key.key,
            algorithms=["RS256"],
            options={"verify_iat": True, "verify_exp": True},
        )
    except jwt.PyJWTError as exc:
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
        logger.warning(
            "Plaid webhook verification failed for %s", payload.get("item_id")
        )
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
