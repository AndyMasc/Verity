"""Pairs Plaid bank transactions with uploaded receipts.

Scoring blends balance, date, merchant and title proximity. Above
MERGE_SCORE_THRESHOLD a pair is merged: the document record's editable fields
move onto the Plaid row, the document is deactivated, and a MergeLog snapshot
is written so the merge can be undone.
"""

import logging
from datetime import timedelta
from decimal import Decimal
from typing import Any

import rapidfuzz
from django.db import transaction as db_transaction
from django.db.models.query import QuerySet
from django.utils import timezone

from documents.models import DocumentData
from records.models import MergeLog, Record

logger = logging.getLogger(__name__)

BALANCE_TOLERANCE = Decimal("1.00")
DATE_TOLERANCE_DAYS = 3
MATCH_LOOKAHEAD_DAYS = 14
MERGE_SCORE_THRESHOLD = 55
MAX_MATCH_CANDIDATES = 2000

PLAID_RESTORE_FIELDS = [
    "products",
    "notes",
    "record_type",
    "folder_id",
    "payment_method",
]


def _similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    a = a.lower().strip()
    b = b.lower().strip()
    if len(a) < 3 or len(b) < 3:
        return float(a == b)
    max_len = max(len(a), len(b))
    if abs(len(a) - len(b)) / max_len > 0.6:
        return 0.0
    return rapidfuzz.fuzz.ratio(a, b) / 100.0


# (threshold, points), best band first.
_BALANCE_BANDS: tuple[tuple[Decimal, int], ...] = (
    (Decimal("0"), 40),
    (BALANCE_TOLERANCE, 30),
    (BALANCE_TOLERANCE * 3, 15),
)
_DATE_BANDS: tuple[tuple[int, int], ...] = (
    (0, 30),
    (1, 20),
    (DATE_TOLERANCE_DAYS, 10),
)
_MERCHANT_BANDS: tuple[tuple[float, int], ...] = (
    (0.9, 30),
    (0.7, 20),
    (0.5, 10),
    (0.3, 5),
)
_TITLE_BANDS: tuple[tuple[float, int], ...] = (
    (0.9, 20),
    (0.7, 15),
    (0.5, 8),
    (0.3, 3),
)


def _band_score(value, bands, *, at_least: bool) -> int:
    """Return the points for the first band that "value" reaches (or does not exceed)."""
    for threshold, points in bands:
        if (value >= threshold) if at_least else (value <= threshold):
            return points
    return 0


def calculate_match_score(record_a: Record, record_b: Record) -> int:
    """Composite match score; at or above MERGE_SCORE_THRESHOLD means probable."""
    score = 0

    a_balance, b_balance = record_a.balance, record_b.balance
    if a_balance is not None and b_balance is not None:
        score += _band_score(abs(a_balance - b_balance), _BALANCE_BANDS, at_least=False)

    a_date, b_date = record_a.transaction_date, record_b.transaction_date
    if a_date and b_date:
        score += _band_score(abs((a_date - b_date).days), _DATE_BANDS, at_least=False)

    score += _band_score(
        _similarity(record_a.merchant, record_b.merchant),
        _MERCHANT_BANDS,
        at_least=True,
    )
    score += _band_score(_similarity(record_a.title, record_b.title), _TITLE_BANDS, at_least=True)

    return score


def _candidates(source: Record, *, candidates_are_plaid: bool) -> QuerySet[Record]:
    """Same user's active records on the opposite side of a merge, in the date window.

    A "source" with no transaction_date has no window to search, so every
    active candidate of the same user is considered (still capped by
    MAX_MATCH_CANDIDATES).
    """
    qs = Record.objects.filter(
        user=source.user,
        is_active=True,
        plaid_transaction_id__isnull=not candidates_are_plaid,
    ).exclude(pk=source.pk)

    if source.transaction_date:
        qs = qs.filter(
            transaction_date__range=(
                source.transaction_date - timedelta(days=MATCH_LOOKAHEAD_DAYS),
                source.transaction_date + timedelta(days=MATCH_LOOKAHEAD_DAYS),
            )
        )

    return qs.select_related("folder", "user")[:MAX_MATCH_CANDIDATES]


def _scored_candidates(source: Record, *, candidates_are_plaid: bool):
    """Yield "(candidate, score)" for every plausible match partner of "source"."""
    for candidate in _candidates(source, candidates_are_plaid=candidates_are_plaid).iterator(
        chunk_size=500
    ):
        yield candidate, calculate_match_score(source, candidate)


def find_best_plaid_match(record: Record) -> Record | None:
    """Highest-scoring active Plaid record for "record", or None below threshold."""
    best: Record | None = None
    best_score = 0

    for candidate, score in _scored_candidates(record, candidates_are_plaid=True):
        if score > best_score:
            best, best_score = candidate, score

    if best is None or best_score < MERGE_SCORE_THRESHOLD:
        return None

    logger.info(
        "Found plaid match for record %s: record %s (score=%d)",
        record.pk,
        best.pk,
        best_score,
    )
    return best


def find_document_matches_for_plaid(plaid_record: Record) -> list[tuple[Record, int]]:
    """Every document record scoring at or above the threshold, best first.

    Used by both automatic matching and the manual merge search panel.
    """
    matches = [
        (candidate, score)
        for candidate, score in _scored_candidates(plaid_record, candidates_are_plaid=False)
        if score >= MERGE_SCORE_THRESHOLD
    ]
    matches.sort(key=lambda pair: -pair[1])
    return matches


def _record_snapshot(record: Record) -> dict[str, Any]:
    return {
        "products": record.products,
        "notes": record.notes,
        "record_type": record.record_type,
        "folder_id": record.folder_id,
        "is_active": record.is_active,
        "plaid_transaction_id": record.plaid_transaction_id,
        "title": record.title,
        "merchant": record.merchant,
        "balance": str(record.balance) if record.balance is not None else None,
        "transaction_date": (
            record.transaction_date.isoformat() if record.transaction_date else None
        ),
        "currency": record.currency,
        "payment_method": record.payment_method,
    }


def _restore_plaid_from_snapshot(locked_plaid: Record, snap: dict) -> None:
    """Restore a Plaid record to its pre-merge state from a MergeLog snapshot. Append same data that any other Plaid record would have."""
    locked_plaid._skip_auto_match = True
    locked_plaid.products = snap.get("products", "")
    locked_plaid.notes = snap.get("notes", "")
    locked_plaid.record_type = snap.get("record_type", Record.RecordTypes.FINANCIAL_DOCUMENT)
    locked_plaid.folder_id = snap.get("folder_id")
    locked_plaid.payment_method = snap.get("payment_method", "")
    locked_plaid.save(update_fields=PLAID_RESTORE_FIELDS)


def _restore_document_record(document_record: Record) -> None:
    """Restore a document record to its pre-merge state from a MergeLog snapshot."""
    locked_doc = Record.objects.select_for_update().get(pk=document_record.pk)
    locked_doc._skip_auto_match = True
    locked_doc.is_active = True
    locked_doc.plaid_transaction_id = None
    locked_doc.save(update_fields=["is_active", "plaid_transaction_id"])


def _apply_doc_fields_to_plaid(locked_plaid: Record, doc: Record) -> None:
    """Copy the document's canonical data onto the Plaid row."""
    if doc.products:
        locked_plaid.products = doc.products
    if doc.notes:
        locked_plaid.notes = doc.notes
    if doc.record_type != Record.RecordTypes.FINANCIAL_DOCUMENT:
        locked_plaid.record_type = doc.record_type
    if doc.folder_id:
        locked_plaid.folder_id = doc.folder_id
    locked_plaid.payment_method = locked_plaid.payment_method or doc.payment_method
    locked_plaid.save(update_fields=PLAID_RESTORE_FIELDS)


@db_transaction.atomic
def merge_document_into_plaid(
    plaid_record: Record,
    document_record: Record,
    document: DocumentData | None = None,
) -> Record | None:
    """Merge a document record into its Plaid transaction, atomically.

    Moves the document's details onto the Plaid row, re-points any attached
    DocumentData at the Plaid row, deactivates the document record, and writes
    a MergeLog snapshot so the merge can be undone. Returns the locked Plaid
    record, or None if either row vanished or the document is no longer
    mergeable (already inactive, or itself merged into a Plaid row).
    """
    try:
        locked_plaid = Record.objects.select_for_update().get(pk=plaid_record.pk)
        fresh_doc = Record.objects.select_for_update().get(pk=document_record.pk)
    except Record.DoesNotExist:
        return None

    if (
        not fresh_doc.is_active or fresh_doc.plaid_transaction_id is not None
    ):  # Skip if the document is already inactive or merged into a Plaid row
        return None

    plaid_snapshot = _record_snapshot(locked_plaid)
    document_snapshot = _record_snapshot(fresh_doc)

    # Suppress the post_save auto-match hook: this save is the merge.
    locked_plaid._skip_auto_match = True
    fresh_doc._skip_auto_match = True

    attached = list(DocumentData.objects.filter(associated_record=fresh_doc))
    DocumentData.objects.filter(pk__in=[d.pk for d in attached]).update(
        associated_record=locked_plaid
    )
    # Undo re-points these back at the document record.
    document_snapshot["document_ids"] = [d.pk for d in attached]

    _apply_doc_fields_to_plaid(locked_plaid, fresh_doc)

    fresh_doc.is_active = False
    fresh_doc.save(update_fields=["is_active"])

    MergeLog.objects.create(
        plaid_record=locked_plaid,
        document_record=fresh_doc,
        document=document or (attached[0] if attached else None),
        plaid_snapshot=plaid_snapshot,
        document_snapshot=document_snapshot,
    )

    return locked_plaid  # Return the locked Plaid record.


@db_transaction.atomic
def undo_merge(merge_log: MergeLog) -> Record | None:
    """Reverse a merge, restoring both records to their pre-merge state.

    Returns the restored document record, or None if the merge was already
    undone. The whole reversal runs under select_for_update so a concurrent
    merge cannot interleave.
    """
    merge_log = MergeLog.objects.select_for_update().get(pk=merge_log.pk)
    if merge_log.undone_at:
        return None

    plaid_record = merge_log.plaid_record
    document_record = merge_log.document_record

    if plaid_record and plaid_record.is_active:
        _restore_plaid_from_snapshot(
            Record.objects.select_for_update().get(pk=plaid_record.pk),
            merge_log.plaid_snapshot,
        )

    if document_record:
        _restore_document_record(document_record)

    doc_ids = merge_log.document_snapshot.get("document_ids")
    if doc_ids:
        DocumentData.objects.filter(pk__in=doc_ids).update(associated_record=document_record)
    elif merge_log.document and document_record:
        merge_log.document.associated_record = document_record
        merge_log.document.save(update_fields=["associated_record"])

    merge_log.undone_at = timezone.now()
    merge_log.save(update_fields=["undone_at"])

    logger.info("Undone merge %s", merge_log.pk)
    return document_record


def try_match_document_record(
    document_record: Record,
    document: DocumentData | None = None,
) -> Record | None:
    """Auto-merge a newly created document record into its best Plaid match."""
    plaid_match = find_best_plaid_match(document_record)
    if plaid_match is None:
        return None
    return merge_document_into_plaid(plaid_match, document_record, document)


def try_match_plaid_record(plaid_record: Record) -> list[Record]:
    """Auto-merge every matching document record into a Plaid transaction.

    Returns the document records that were successfully merged. Re-running is
    safe: a document record that was already merged is inactive and so is no
    longer a candidate.
    """
    merged = [
        doc_record
        for doc_record, _score in find_document_matches_for_plaid(plaid_record)
        if merge_document_into_plaid(plaid_record, doc_record) is not None
    ]
    return merged
