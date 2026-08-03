"""Plaid webhook handler and JWT signature verification."""

import hashlib
import json
import logging
from datetime import UTC, datetime
from typing import Any

import jwt
import requests
from django.conf import settings
from django.db import transaction
from django.http import (
    HttpRequest,
    HttpResponse,
    HttpResponseBadRequest,
    HttpResponseForbidden,
)
from django.utils import timezone as tz
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from records.models import Record

from ..models import PlaidItem
from ..tasks import sync_and_convert_for_item_task

logger: logging.Logger = logging.getLogger(__name__)

PLAID_JWKS_URL = "https://plaid.com/auth/v1/webhook_public_key"
_jwks_cache: dict[str, Any] = {}
_jwks_fetched_at: float | None = None

WEBHOOK_MAX_BODY_SIZE = 1024 * 100  # 100KB


def _get_plaid_jwk(kid: str, max_age: int = 3600) -> dict[str, Any] | None:
    """Fetch and cache a Plaid JSON Web Key by key ID.

    Maintains an in-memory cache that refreshes after ``max_age`` seconds
    to avoid hammering Plaid's JWKS endpoint on every webhook.
    """
    global _jwks_cache, _jwks_fetched_at
    now = datetime.now(UTC).timestamp()
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
    """Verify the JWT signature and body hash of an incoming Plaid webhook.

    Uses Plaid's public JWKS endpoint to validate the RS256 signature,
    then confirms the request body hasn't been tampered with.
    """
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
    """Handle incoming Plaid webhooks for transaction and credential events.

    Routes different webhook types to the appropriate handler: transaction
    syncs are dispatched as async tasks, credential errors are persisted
    on the PlaidItem for UI display, and removed transactions are marked
    inactive. Verification is skipped in sandbox for easier testing.
    """
    if len(request.body) > WEBHOOK_MAX_BODY_SIZE:
        logger.warning("Plaid webhook body too large: %d bytes", len(request.body))
        return HttpResponseBadRequest("Payload too large")

    try:
        payload = json.loads(request.body)
    except ValueError, TypeError:
        return HttpResponseBadRequest("Invalid JSON")

    # Only skip signature verification when we are running against Plaid's
    # sandbox. An attacker-supplied "environment": "sandbox" in the payload
    # must never disable verification in a live environment.
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

    _route_webhook(webhook_code, plaid_item, payload)

    return HttpResponse("OK")


def _dispatch_sync(plaid_item: PlaidItem) -> bool:
    """Enqueue a transaction sync for an item, debounced by a per-item cooldown.

    Plaid may deliver several SYNC_UPDATES_AVAILABLE webhooks for the same
    item within seconds. Only the first one within
    ``PLAID_SYNC_COOLDOWN_SECONDS`` triggers a task; the rest are dropped to
    avoid redundant (and potentially expensive) sync runs.
    """
    now = tz.now()
    last_synced_at = plaid_item.last_synced_at
    if last_synced_at is not None:
        elapsed = (now - last_synced_at).total_seconds()
        if elapsed < settings.PLAID_SYNC_COOLDOWN_SECONDS:
            logger.info(
                "Skipping sync for item %s: last sync %.1fs ago (cooldown %ss)",
                plaid_item.item_id,
                elapsed,
                settings.PLAID_SYNC_COOLDOWN_SECONDS,
            )
            return False

    PlaidItem.objects.filter(id=plaid_item.id).update(last_synced_at=now)
    sync_and_convert_for_item_task.delay(plaid_item.id)
    return True


def _route_webhook(webhook_code: str, plaid_item: PlaidItem, payload: dict[str, Any]) -> None:
    """Dispatch a webhook to the appropriate handler based on the code."""
    if webhook_code in ("SYNC_UPDATES_AVAILABLE", "HISTORICAL_UPDATE"):
        _dispatch_sync(plaid_item)

    elif webhook_code in (
        "ITEM_LOGIN_REQUIRED",
        "ITEM_REQUIRES_UPDATE",
        "PENDING_EXPIRATION",
    ):
        logger.warning(
            "Item %s requires manual user intervention: %s",
            plaid_item.item_id,
            webhook_code,
        )
        PlaidItem.objects.filter(id=plaid_item.id).update(
            last_error_code=webhook_code,
            last_error_message=payload.get("error", {}).get(
                "error_message", "User action required"
            ),
            last_error_at=tz.now(),
        )

    elif webhook_code == "ERROR":
        error: dict[str, Any] = payload.get("error", {})
        logger.error(
            "Plaid error for item %s: %s",
            plaid_item.item_id,
            error.get("error_message"),
        )
        PlaidItem.objects.filter(id=plaid_item.id).update(
            last_error_code=error.get("error_code", webhook_code),
            last_error_message=error.get("error_message", "Unknown error"),
            last_error_at=tz.now(),
        )

    elif webhook_code == "TRANSACTIONS_REMOVED":
        txns: list[str] = payload.get("removed_transactions", [])
        with transaction.atomic():
            Record.objects.filter(plaid_transaction_id__in=txns).update(
                is_active=False, last_edited=tz.now()
            )
