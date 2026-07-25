"""Async tasks for syncing Plaid transactions into Papertrail records.

Uses Plaid's Transactions Sync endpoint to incrementally fetch new,
modified, and removed transactions. Converts them into Record objects
and organizes them into user folders by category.
"""

import json
import logging
from datetime import date
from typing import Any

from django.contrib.auth.models import User
from django.db import IntegrityError
from django.db import transaction as db_transaction
from django.db.models import Q
from django.utils import timezone
from django_qstash import shared_task
from plaid.model.transactions_sync_request import TransactionsSyncRequest

from records.models import Folder, Record

from .models import PlaidItem
from .plaid_client import client

logger: logging.Logger = logging.getLogger(__name__)


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

    key_words = [word.strip().lower() for word in category_clean.split() if len(word.strip()) > 0]
    if not key_words:
        return None

    query = Q()
    for word in key_words:
        query |= Q(name__icontains=word)

    folder = Folder.objects.filter(query, user=user).first()

    if not folder:
        try:
            folder, created = Folder.objects.get_or_create(user=user, name=category_clean)
        except IntegrityError:
            folder = Folder.objects.filter(user=user, name=category_clean).first()

    if folder_cache is not None and folder:
        folder_cache[category_clean] = folder

    return folder


def _get_payment_method(plaid_item: PlaidItem, account_id: str) -> str:
    """Build a display string for the payment method from stored account data."""
    accounts = plaid_item.accounts_data
    if not accounts or not account_id:
        return ""

    while isinstance(accounts, str):
        try:
            accounts = json.loads(accounts)
        except (json.JSONDecodeError, TypeError):
            return ""

    if not isinstance(accounts, list):
        return ""

    for acct in accounts:
        if isinstance(acct, dict) and acct.get("id") == account_id:
            name = acct.get("name", "")
            mask = acct.get("mask", "")
            if name and mask:
                return f"{name} (••{mask})"
            return name or ""

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

    auto_create_enabled = getattr(user.settings, "auto_create_and_organize_folders", True)

    matched_folder = None
    if auto_create_enabled:
        matched_folder = choose_folder(user, primary_category, folder_cache=folder_cache)

    raw_date = txn.get("authorized_date") or txn["date"]
    if isinstance(raw_date, str):
        raw_date = date.fromisoformat(raw_date)
    defaults = {
        "user": user,
        "plaid_item": plaid_item,
        "title": txn["name"],
        "merchant": txn.get("merchant_name") or txn["name"],
        "balance": abs(txn["amount"]),
        "transaction_date": raw_date,
        "record_type": Record.RecordTypes.FINANCIAL_DOCUMENT,
        "notes": primary_category,
        "folder": matched_folder,
    }
    defaults["payment_method"] = _get_payment_method(plaid_item, txn.get("account_id", ""))
    return defaults


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def sync_and_convert_for_item_task(self, plaid_item_id: int | str) -> dict[str, Any]:
    """Sync all pending transactions for a Plaid item and create/update Records.

    Paginates through the Plaid Transactions Sync endpoint using the stored
    cursor, processing added, modified, and removed transactions in atomic
    batches. After syncing, attempts to match new financial records against
    existing uploaded documents. Retries with exponential backoff on API errors.
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
        try:
            response: Any = client.transactions_sync(
                TransactionsSyncRequest(
                    access_token=plaid_item.access_token,
                    cursor=cursor,
                )
            )
        except Exception as e:
            logger.warning(
                "Plaid API error or rate limit hit for item %s. Retrying task.", plaid_item_id
            )
            countdown = self.default_retry_delay * (2**self.request.retries)
            raise self.retry(exc=e, countdown=countdown) from None

        data: dict[str, Any] = response if isinstance(response, dict) else response.to_dict()

        with db_transaction.atomic():
            txn: dict[str, Any]
            for txn in data.get("removed", []):
                archived = Record.objects.filter(plaid_transaction_id=txn["transaction_id"]).update(
                    is_active=False, last_edited=timezone.now()
                )
                stats["removed"] += archived

            for txn in data.get("added", []):
                Record.objects.update_or_create(
                    plaid_transaction_id=txn["transaction_id"],
                    defaults=_txn_to_record_defaults(txn, plaid_item, folder_cache),
                )
                stats["added"] += 1

            for txn in data.get("modified", []):
                Record.objects.update_or_create(
                    plaid_transaction_id=txn["transaction_id"],
                    defaults=_txn_to_record_defaults(txn, plaid_item, folder_cache),
                )
                stats["modified"] += 1

            cursor = data.get("next_cursor", cursor)
            has_more = data.get("has_more", False)

            plaid_item.next_cursor = cursor
            plaid_item.save(update_fields=["next_cursor"])

    try:
        from records.matching import try_match_plaid_record

        for plaid_record in (
            Record.objects.filter(plaid_item=plaid_item, is_active=True)
            .only("pk", "user_id")
            .iterator(chunk_size=500)
        ):
            try_match_plaid_record(plaid_record)
    except Exception:
        logger.exception("Error matching plaid records to documents for item %s", plaid_item_id)

    return {"status": "synced", **stats}
