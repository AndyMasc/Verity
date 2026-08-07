"""Service layer for Plaid API operations.

Encapsulates token exchanges, bank-item metadata fetches, sync triggers,
and webhook routing so views stay thin and business rules stay reusable.
"""

import datetime
import logging
from typing import Any

import plaid
from django.conf import settings
from django.db import models, transaction
from django.utils import timezone as tz
from plaid.model.accounts_get_request import AccountsGetRequest
from plaid.model.country_code import CountryCode
from plaid.model.institutions_get_by_id_request import InstitutionsGetByIdRequest
from plaid.model.institutions_get_by_id_request_options import (
    InstitutionsGetByIdRequestOptions,
)
from plaid.model.item_get_request import ItemGetRequest
from plaid.model.item_public_token_exchange_request import (
    ItemPublicTokenExchangeRequest,
)
from plaid.model.sandbox_item_fire_webhook_request import SandboxItemFireWebhookRequest

from records.models import Record

from .models import PlaidItem
from .plaid_client import client as plaid_client

logger = logging.getLogger(__name__)


def public_token_exchange(public_token: str) -> tuple[str, str]:
    """Exchange a Plaid public token for a long-lived access token and item ID.

    This is the final step of the bank linking flow. The returned access
    token is used for all subsequent Plaid API calls for this bank item.
    """
    try:
        request = ItemPublicTokenExchangeRequest(public_token=public_token)
        response = plaid_client.item_public_token_exchange(request)

        access_token = response["access_token"]
        item_id = response["item_id"]

        return access_token, item_id

    except plaid.ApiException as e:
        logger.error("Plaid API error during exchange: %s", e)
        raise
    except Exception:
        logger.exception("Unexpected error in public_token_exchange")
        raise


def fetch_institution_name(access_token: str, item_id: str) -> str:
    """Fetch the institution name from Plaid, returning a default on failure."""
    try:
        item_resp = plaid_client.item_get(ItemGetRequest(access_token=access_token))
        item_dict = item_resp.to_dict() if hasattr(item_resp, "to_dict") else item_resp
        inst_id = item_dict.get("item", {}).get("institution_id", "")
        if inst_id:
            inst_req = InstitutionsGetByIdRequest(
                institution_id=inst_id,
                country_codes=[CountryCode("US")],
                options=InstitutionsGetByIdRequestOptions(include_optional_metadata=False),
            )
            inst_resp = plaid_client.institutions_get_by_id(inst_req)
            inst_dict = inst_resp.to_dict() if hasattr(inst_resp, "to_dict") else inst_resp
            return inst_dict.get("institution", {}).get("name", "Bank Account")
    except Exception:
        logger.warning("Failed to fetch institution name for item %s", item_id)
    return "Bank Account"


def fetch_accounts(access_token: str, item_id: str) -> list[dict[str, str]]:
    """Fetch account metadata from Plaid, returning an empty list on failure."""
    try:
        acct_resp = plaid_client.accounts_get(AccountsGetRequest(access_token=access_token))
        acct_dict = acct_resp.to_dict() if hasattr(acct_resp, "to_dict") else acct_resp
        return [
            {
                "id": a["account_id"],
                "name": a["name"],
                "mask": a.get("mask", ""),
                "type": a.get("type", ""),
                "subtype": a.get("subtype", ""),
            }
            for a in acct_dict.get("accounts", [])
        ]
    except Exception:
        logger.warning("Failed to fetch accounts for item %s", item_id)
    return []


def trigger_initial_sync(plaid_item: PlaidItem) -> None:
    """Trigger initial sync via sandbox webhook in Sandbox, or directly via Celery in Prod."""
    if getattr(settings, "PLAID_ENV", "") == "sandbox":
        try:
            plaid_client.sandbox_item_fire_webhook(
                SandboxItemFireWebhookRequest(
                    access_token=plaid_item.access_token,
                    webhook_code="DEFAULT_UPDATE",
                )
            )
        except plaid.ApiException:
            logger.warning("Failed to fire initial sandbox webhook for item %s", plaid_item.item_id)
    else:
        from .tasks import sync_and_convert_for_item_task

        sync_and_convert_for_item_task.delay(plaid_item.id)


def dispatch_sync(plaid_item: PlaidItem) -> bool:
    """Enqueue a transaction sync for an item, debounced atomically by a per-item cooldown.

    Returns ``True`` when the sync was dispatched, ``False`` when it was
    skipped because the last sync fell within the cooldown window.
    """
    now = tz.now()
    cooldown_threshold = now - datetime.timedelta(seconds=settings.PLAID_SYNC_COOLDOWN_SECONDS)

    updated_count = (
        PlaidItem.objects.filter(id=plaid_item.id)
        .filter(
            models.Q(last_synced_at__isnull=True) | models.Q(last_synced_at__lt=cooldown_threshold)
        )
        .update(last_synced_at=now)
    )

    if updated_count == 0:
        logger.info(
            "Skipping sync for item %s: last sync within cooldown window.",
            plaid_item.item_id,
        )
        return False

    from .tasks import sync_and_convert_for_item_task

    sync_and_convert_for_item_task.delay(plaid_item.id)
    return True


def route_webhook(webhook_code: str, plaid_item: PlaidItem, payload: dict[str, Any]) -> None:
    """Dispatch a webhook to the appropriate handler based on the code."""
    if webhook_code in ("SYNC_UPDATES_AVAILABLE", "HISTORICAL_UPDATE"):
        dispatch_sync(plaid_item)

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
