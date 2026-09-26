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
from django.db import IntegrityError
from django.db import transaction as db_transaction
from django.db.models import Q
from django.utils import timezone
from periodiq import cron
from plaid.model.transactions_sync_request import TransactionsSyncRequest

from billing.models import CustomUser as User
from core.apps import posthog_client
from records.models import Folder, Record

from .models import PlaidItem
from .plaid_client import client
from .services import dispatch_sync

logger: logging.Logger = logging.getLogger(__name__)


def _extract_plaid_error(error: plaid.ApiException) -> tuple[str, str]:
    """Return ``(error_code, error_message)`` parsed from a Plaid API exception."""
    body = getattr(error, "body", "")
    if isinstance(body, bytes):
        body = body.decode("utf-8", errors="replace")
    if not isinstance(body, str):
        return "", str(error)

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return "", str(error)
    if not isinstance(payload, dict):
        return "", str(error)
    return payload.get("error_code", ""), payload.get("error_message", str(error))


def choose_folder(
    user: User, category: str | None, folder_cache: dict[str, Folder] | None = None
) -> Folder | None:
    """Find or create a Folder matching the given transaction category.

    Searches existing user folders using keyword matching on the category
    name. If no match is found, auto-creates a new folder unless the user
    has disabled auto-organization. Results are optionally cached to avoid
    repeated DB lookups within a single sync batch.
    """
    if not category:
        return None

    category_clean: str = category.strip()

    if folder_cache is not None and category_clean in folder_cache:
        return folder_cache[category_clean]

    key_words = [word.strip().lower() for word in category_clean.split()]
    if not key_words:
        return None

    query = Q()
    for word in key_words:
        query |= Q(name__icontains=word)

    folder = Folder.objects.filter(query, user=user).first()

    if not folder:
        try:
            folder = Folder.objects.get_or_create(user=user, name=category_clean)[0]
        except IntegrityError:
            folder = Folder.objects.filter(user=user, name=category_clean).first()

    if folder_cache is not None and folder:
        folder_cache[category_clean] = folder

    return folder


def _parse_accounts_data(data: Any) -> list[dict[str, Any]]:
    """Normalize stored Plaid account payloads into a list of account dicts."""
    if not data:
        return []

    while isinstance(data, str):
        try:
            data = json.loads(data)
        except (json.JSONDecodeError, TypeError):
            return []

    return data if isinstance(data, list) else []


def _format_account_name(account: dict[str, Any]) -> str:
    """Return a human-readable name for a Plaid account."""
    name = account.get("name", "")
    mask = account.get("mask", "")

    if name and mask:
        return f"{name} (••{mask})"
    return name or ""


def _get_payment_method(plaid_item: PlaidItem, account_id: str) -> str:
    """Build a display string for the payment method from stored account data."""
    if not account_id:
        return ""

    for account in _parse_accounts_data(plaid_item.accounts_data):
        if isinstance(account, dict) and account.get("id") == account_id:
            return _format_account_name(account)

    return ""


def _txn_to_record_defaults(
    txn: dict[str, Any],
    plaid_item: PlaidItem,
    folder_cache: dict[str, Folder] | None = None,
) -> dict[str, Any]:
    """Convert a Plaid transaction dict into Record model defaults.

    Extracts merchant name, amount, date, and category from the Plaid
    transaction and maps them to the corresponding Record fields. Also
    resolves the payment method display string from stored account data.
    """
    categories = txn.get("category") or []
    primary_category = categories[0] if categories else ""
    user = plaid_item.user

    auto_create_enabled = getattr(
        user.settings, "auto_create_and_organize_folders", True
    )

    matched_folder = None
    if auto_create_enabled:
        matched_folder = choose_folder(
            user, primary_category, folder_cache=folder_cache
        )

    raw_date = txn.get("authorized_date") or txn["date"]
    if isinstance(raw_date, str):
        raw_date = date.fromisoformat(raw_date)
    defaults = {
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
        "transaction_date": raw_date,
        "record_type": Record.RecordTypes.FINANCIAL_DOCUMENT,
        "notes": primary_category,
        "folder": matched_folder,
    }
    defaults["payment_method"] = _get_payment_method(
        plaid_item, txn.get("account_id", "")
    )
    return defaults


def _process_removed_transactions(data: dict[str, Any], stats: dict[str, int]) -> None:
    """Process removed transactions and update stats."""
    for txn in data.get("removed", []):
        archived = Record.objects.filter(
            plaid_transaction_id=txn["transaction_id"]
        ).update(is_active=False, last_edited=timezone.now())
        stats["removed"] += archived


def _process_added_modified_transactions(
    batch: list[dict[str, Any]],
    existing_ids: set[str],
    plaid_item: PlaidItem,
    folder_cache: dict[str, Folder],
) -> tuple[list[Record], list[Record]]:
    """Separate batch into records to create and update."""
    now = timezone.now()
    to_create: list[Record] = []
    to_update: list[Record] = []

    for txn in batch:
        record = Record(
            plaid_transaction_id=txn["transaction_id"],
            last_edited=now,
            **_txn_to_record_defaults(txn, plaid_item, folder_cache),
        )
        if txn["transaction_id"] in existing_ids:
            to_update.append(record)
        else:
            to_create.append(record)

    return to_create, to_update


def _bulk_create_update_records(
    to_create: list[Record], to_update: list[Record]
) -> None:
    """Bulk create and update records."""
    if to_create:
        Record.objects.bulk_create(to_create)
    if to_update:
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
                # Plaid corrections can ressurect a transaction that was previously removed; without this the record stays inactive forever with no recovery path.
                "is_active",
            ],
        )


def _match_records_to_documents(
    plaid_item: PlaidItem, plaid_item_id: int | str
) -> None:
    """Match synced records to existing uploaded documents."""
    try:
        from records.matching import try_match_plaid_record

        for plaid_record in (
            Record.objects.filter(plaid_item=plaid_item, is_active=True)
            .only("pk", "user_id")
            .iterator(chunk_size=500)
        ):
            try_match_plaid_record(plaid_record)
    except Exception:
        logger.exception(
            "Error matching plaid records to documents for item %s", plaid_item_id
        )


def _fetch_sync_page(plaid_item: PlaidItem, cursor: str) -> dict[str, Any] | None:
    """Fetch one Transactions Sync page; return ``None`` when the item needs re-authentication.

    A "ITEM_LOGIN_REQUIRED" error is recorded on the item and surfaced as
    ``None`` so the caller stops without retrying. Any other failure is
    re-raised unchanged so the broker's retry policy still applies.
    """
    try:
        response = client.transactions_sync(
            TransactionsSyncRequest(access_token=plaid_item.access_token, cursor=cursor)
        )
    except plaid.ApiException as e:
        error_code, error_message = _extract_plaid_error(e)
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
    """Apply one Transactions Sync page atomically; return ``(next_cursor, has_more)``."""
    with db_transaction.atomic():
        _process_removed_transactions(data, stats)

        batch = data.get("added", []) + data.get("modified", [])
        if batch:
            txn_ids = [t["transaction_id"] for t in batch]
            existing_ids = set(
                Record.objects.filter(plaid_transaction_id__in=txn_ids).values_list(
                    "plaid_transaction_id", flat=True
                )
            )
            to_create, to_update = _process_added_modified_transactions(
                batch, existing_ids, plaid_item, folder_cache
            )
            _bulk_create_update_records(to_create, to_update)
            stats["added"] += len(data["added"])
            stats["modified"] += len(data["modified"])

        cursor = data.get("next_cursor", plaid_item.next_cursor)
        has_more = data.get("has_more", False)
        plaid_item.next_cursor = cursor
        plaid_item.save(update_fields=["next_cursor"])
        return cursor, has_more


@dramatiq.actor(max_retries=3)
def sync_and_convert_for_item_task(plaid_item_id: int | str) -> dict[str, Any]:
    """Sync all pending transactions for a Plaid item and create/update Records.

    Paginates through the Plaid Transactions Sync endpoint using the stored
    cursor, processing added, modified, and removed transactions in atomic
    batches. After syncing, attempts to match new financial records against
    existing uploaded documents. Retries with exponential backoff on API errors.
    """
    try:
        plaid_item: PlaidItem = PlaidItem.objects.select_related("user").get(
            id=plaid_item_id
        )
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

    _match_records_to_documents(plaid_item, plaid_item_id)

    # One event per sync rather than per transaction: a single sync can import
    # hundreds of rows, and the hourly fallback task syncs items with no
    # changes at all.
    if stats["added"] and posthog_client is not None:
        posthog_client.capture(
            "record_imported",
            distinct_id=str(plaid_item.user_id),
            properties={
                "import_source": "plaid_sync",
                "record_count": stats["added"],
            },
        )

    return {"status": "synced", **stats}


@dramatiq.actor(max_retries=3, min_backoff=2, periodic=cron("0 * * * *"))
def periodic_plaid_sync_task() -> None:
    """Poll Transactions Sync for every linked item hourly as a webhook fallback."""
    for plaid_item in PlaidItem.objects.all().only("id", "item_id"):
        dispatch_sync(plaid_item)
