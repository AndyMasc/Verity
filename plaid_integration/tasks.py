"""Async tasks for syncing Plaid transactions into Verity records.

Uses Plaid's Transactions Sync endpoint to incrementally fetch new,
modified, and removed transactions. Converts them into Record objects
and organizes them into user folders by category.
"""

import json
import logging
from datetime import date
from typing import Any

import dramatiq
import plaid
from django.conf import settings
from django.db import IntegrityError
from django.db import transaction as db_transaction
from django.db.models import Q
from django.utils import timezone
from periodiq import cron
from plaid.model.transactions_sync_request import TransactionsSyncRequest

from billing.models import CustomUser as User
from core.apps import posthog_client
from records.matching import try_match_plaid_record
from records.models import Folder, Record

from .models import PlaidItem
from .plaid_client import client
from .services import dispatch_sync

logger: logging.Logger = logging.getLogger(__name__)


def _plaid_error(error: plaid.ApiException) -> tuple[str, str]:
    """Return (error_code, error_message) parsed from a Plaid API exception."""
    body = getattr(error, "body", "")
    if isinstance(body, bytes):
        body = body.decode("utf-8", errors="replace")
    try:
        payload = json.loads(body)
        return payload.get("error_code", ""), payload.get("error_message", str(error))
    except (json.JSONDecodeError, TypeError, AttributeError):
        return "", str(error)


def choose_folder(
    user: User, category: str | None, folder_cache: dict[str, Folder] | None = None
) -> Folder | None:
    """Find or create a Folder matching a Plaid category name.

    folder_cache memoises lookups across a single sync batch.
    """
    category = (category or "").strip()
    if not category:
        return None
    if folder_cache is not None and category in folder_cache:
        return folder_cache[category]

    query = Q()
    for word in category.lower().split():
        query |= Q(name__icontains=word)

    folder = Folder.objects.filter(query, user=user).first()
    if not folder:
        try:
            folder, _ = Folder.objects.get_or_create(user=user, name=category)
        except IntegrityError:
            # A concurrent sync for the same user can claim the name first.
            folder = Folder.objects.filter(user=user, name=category).first()

    if folder_cache is not None and folder:
        folder_cache[category] = folder
    return folder


def _get_payment_method(plaid_item: PlaidItem, account_id: str) -> str:
    """Build a display string for the payment method from stored account data."""
    if not account_id:
        return ""

    # accounts_data is an EncryptedJSONField, so it deserializes to a list already.
    for account in plaid_item.accounts_data or []:
        if account.get("id") != account_id:
            continue
        name, mask = account.get("name", ""), account.get("mask", "")
        return f"{name} (••{mask})" if name and mask else name

    return ""


def _txn_to_record(
    txn: dict[str, Any],
    plaid_item: PlaidItem,
    folder_cache: dict[str, Folder] | None = None,
) -> dict[str, Any]:
    """Map a Plaid transaction onto Record model field values."""
    user = plaid_item.user
    category = (txn.get("category") or [""])[0]

    auto_create = getattr(user.settings, "auto_create_and_organize_folders", True)
    folder = choose_folder(user, category, folder_cache) if auto_create else None

    # Plaid sends ISO strings, but guard against an already-parsed date.
    raw_date = txn.get("authorized_date") or txn["date"]

    return {
        "user": user,
        "plaid_item": plaid_item,
        "title": txn["name"],
        "merchant": txn.get("merchant_name") or txn["name"],
        "balance": abs(txn["amount"]),
        "currency": (
            txn.get("iso_currency_code")
            or txn.get("unofficial_currency_code")
            or getattr(user.settings, "default_currency", "usd")
        ).lower(),
        "transaction_date": (
            date.fromisoformat(raw_date) if isinstance(raw_date, str) else raw_date
        ),
        "record_type": Record.RecordTypes.FINANCIAL_DOCUMENT,
        "notes": category,
        "folder": folder,
        "payment_method": _get_payment_method(plaid_item, txn.get("account_id", "")),
    }


def _save_records(
    batch: list[dict[str, Any]],
    existing_ids: set[str],
    plaid_item: PlaidItem,
    folder_cache: dict[str, Folder],
) -> None:
    """Create or update Records for one page of added/modified transactions."""
    now = timezone.now()
    to_create: list[Record] = []
    to_update: list[Record] = []

    for txn in batch:
        record = Record(
            plaid_transaction_id=txn["transaction_id"],
            last_edited=now,
            **_txn_to_record(txn, plaid_item, folder_cache),
        )
        (to_update if txn["transaction_id"] in existing_ids else to_create).append(record)

    if to_create:
        Record.objects.bulk_create(to_create)
    if to_update:
        # is_active is included so Plaid can resurrect a transaction it previously removed.
        Record.objects.bulk_update(
            to_update,
            fields=[
                "user",
                "plaid_item",
                "title",
                "merchant",
                "balance",
                "currency",
                "transaction_date",
                "record_type",
                "notes",
                "folder",
                "payment_method",
                "last_edited",
                "is_active",
            ],
        )


def _match_records_to_documents(plaid_item: PlaidItem) -> None:
    """Match synced records to existing uploaded documents."""
    try:
        records = Record.objects.filter(plaid_item=plaid_item, is_active=True).only("pk", "user_id")
        for record in records.iterator(chunk_size=500):
            try_match_plaid_record(record)
    except Exception:
        logger.exception("Error matching records to documents for item %s", plaid_item.id)


def _fetch_sync_page(plaid_item: PlaidItem, cursor: str) -> dict[str, Any] | None:
    """Fetch one Transactions Sync page; return None when re-authentication is needed.

    An "ITEM_LOGIN_REQUIRED" error is recorded on the item and surfaced as
    None so the caller stops without retrying. Any other failure is
    re-raised so the broker's retry policy still applies.
    """
    try:
        response = client.transactions_sync(
            TransactionsSyncRequest(access_token=plaid_item.access_token, cursor=cursor)
        )
    except plaid.ApiException as exc:
        error_code, error_message = _plaid_error(exc)
        if error_code != "ITEM_LOGIN_REQUIRED":
            raise
        PlaidItem.objects.filter(id=plaid_item.id).update(
            last_error_code=error_code,
            last_error_message=error_message,
            last_error_at=timezone.now(),
        )
        return None
    return response if isinstance(response, dict) else response.to_dict()


def _process_sync_page(
    data: dict[str, Any],
    plaid_item: PlaidItem,
    folder_cache: dict[str, Folder],
    stats: dict[str, int],
) -> tuple[str, bool]:
    """Apply one Transactions Sync page atomically; return (next_cursor, has_more)."""
    with db_transaction.atomic():
        for txn in data.get("removed", []):
            stats["removed"] += Record.objects.filter(
                plaid_transaction_id=txn["transaction_id"]
            ).update(is_active=False, last_edited=timezone.now())

        batch = data.get("added", []) + data.get("modified", [])
        if batch:
            existing_ids = set(
                Record.objects.filter(
                    plaid_transaction_id__in=[t["transaction_id"] for t in batch]
                ).values_list("plaid_transaction_id", flat=True)
            )
            _save_records(batch, existing_ids, plaid_item, folder_cache)
            stats["added"] += len(data.get("added", []))
            stats["modified"] += len(data.get("modified", []))

        cursor = data.get("next_cursor", plaid_item.next_cursor)
        plaid_item.next_cursor = cursor
        plaid_item.save(update_fields=["next_cursor"])
        return cursor, data.get("has_more", False)


@dramatiq.actor(max_retries=3)
def sync_and_convert_for_item_task(plaid_item_id: int | str):
    """Sync all pending transactions for a Plaid item and create/update Records.

    Paginates through the Plaid Transactions Sync endpoint using the stored
    cursor, processing added, modified, and removed transactions in atomic
    batches. After syncing, attempts to match new financial records against
    existing uploaded documents.
    """
    try:
        plaid_item: PlaidItem = PlaidItem.objects.select_related("user").get(id=plaid_item_id)
    except PlaidItem.DoesNotExist:
        return {"error": f"PlaidItem {plaid_item_id} not found"}

    cursor: str = plaid_item.next_cursor or ""
    has_more: bool = True
    stats: dict[str, int] = {"added": 0, "modified": 0, "removed": 0}
    folder_cache: dict[str, Folder] = {}

    while has_more:
        data = _fetch_sync_page(plaid_item, cursor)
        if data is None:
            return {"error": "ITEM_LOGIN_REQUIRED"}
        cursor, has_more = _process_sync_page(data, plaid_item, folder_cache, stats)

    _match_records_to_documents(plaid_item)

    # One event per sync rather than per transaction: a single sync can import hundreds of rows, and the hourly fallback task syncs items with no changes at all.
    if stats["added"] and posthog_client is not None:
        posthog_client.capture(
            "record_imported",
            distinct_id=str(plaid_item.user_id),
            properties={
                "import_source": "plaid",
                "record_count": stats["added"],
            },
        )
    if settings.DEBUG:
        return {"status": "synced", **stats}
    return None  # In production, returning nothing silences benign errors - the result is logged in the broker's result backend if configured.


@dramatiq.actor(max_retries=3, min_backoff=2, periodic=cron("0 * * * *"))
def periodic_plaid_sync_task() -> None:
    """Poll Transactions Sync for every linked item hourly as a webhook fallback."""
    for plaid_item in PlaidItem.objects.all().only("id", "item_id"):
        dispatch_sync(plaid_item)
