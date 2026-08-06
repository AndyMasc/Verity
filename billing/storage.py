"""Storage accounting: O(1) per-user byte usage via a denormalized counter.

Usage checks used to run a ``SUM(file_size)`` over every active document on
each request, which grows linearly with the user's document count. Instead the
documents layer maintains ``CustomUser.storage_used_bytes`` transactionally at
every mutation point (upload confirm, soft/hard delete, restore, bulk cleanup),
and the checks here read that single row.

The counter is kept in sync by signals on ``DocumentData`` (``pre_save`` /
``post_save`` / ``post_delete``) so every lifecycle transition — including bulk
``QuerySet.delete()`` and admin deletions — stays accurate. As a safety net,
``reconcile_storage_usage`` recomputes counters from the source of truth (the
``DocumentData`` table) and is exposed via a management command for drift
correction.
"""

import logging

from django.db.models import F, Sum
from django.db.models.functions import Greatest

from documents.models import DocumentData

from .models import CustomUser

logger = logging.getLogger(__name__)


def get_storage_usage_bytes(user) -> int:
    """Return the user's total stored bytes (denormalized, O(1))."""
    if user is None or not getattr(user, "pk", None):
        return 0
    value = (
        CustomUser.objects.filter(pk=user.pk)
        .values_list("storage_used_bytes", flat=True)
        .first()
    )
    return value or 0


def adjust_storage_usage(user_id: int, delta: int) -> None:
    """Apply a signed byte delta to a user's storage counter (never negative).

    Uses a single ``UPDATE ... F()`` so concurrent uploads/cleanups cannot
    lose updates. Callers must invoke this inside the same transaction that
    mutates the documents so a rollback keeps the counter consistent.
    """
    if not delta:
        return
    CustomUser.objects.filter(pk=user_id).update(
        storage_used_bytes=Greatest(F("storage_used_bytes") + delta, 0)
    )


def reconcile_storage_usage(user_id: int | None = None) -> int:
    """Recompute storage counters from active documents for users.

    Also zeroes out counters that have drifted above zero but reference no
    active documents (e.g. the last document was hard-deleted outside a
    signal path).

    Args:
        user_id: Restrict reconciliation to a single user, or None for all users.

    Returns:
        Number of users whose counter was corrected.
    """
    docs = DocumentData.objects.filter(is_active=True)
    if user_id is not None:
        docs = docs.filter(user_id=user_id)

    totals = {
        row["user_id"]: row["total"] or 0
        for row in docs.values("user_id").annotate(total=Sum("file_size"))
    }

    corrected = 0
    for uid, total in totals.items():
        updated = CustomUser.objects.filter(pk=uid).update(storage_used_bytes=total)
        corrected += updated

    stale = CustomUser.objects.filter(storage_used_bytes__gt=0)
    if user_id is not None:
        stale = stale.filter(pk=user_id)
    if totals:
        stale = stale.exclude(pk__in=list(totals))
    corrected += stale.update(storage_used_bytes=0)
    return corrected
