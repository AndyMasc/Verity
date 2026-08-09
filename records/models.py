"""Core domain models for the records module.

Defines the data layer for Papertrail's record-keeping system: expense records,
folders for organisation, merge tracking between Plaid and document records,
and an audit log that captures every significant mutation.
"""

from __future__ import annotations

import calendar
import datetime
import re
from decimal import Decimal, InvalidOperation
from functools import reduce
from operator import or_
from typing import TYPE_CHECKING

from django.conf import settings
from django.db import models
from django.db.models import Count, Q
from django.utils import timezone
from simple_history.models import HistoricalRecords

from core.currencies import CURRENCY_CHOICES, DEFAULT_CURRENCY

from .constants import RECORD_TYPE_COLOR_MAP

if TYPE_CHECKING:
    from django.contrib.auth.models import AbstractUser

_MONTH_MAP = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}

_RELATIVE_DAYS = frozenset({"today", "yesterday", "tomorrow"})
_YEAR_RE = re.compile(r"^\d{4}$")
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_TEXT_SEARCH_FIELDS = ("title", "merchant", "products", "notes")
_DATE_FIELDS = ("transaction_date", "expiry_date", "date_added")


def _month_range(year: int, month: int) -> tuple[datetime.date, datetime.date]:
    last_day = calendar.monthrange(year, month)[1]
    return datetime.date(year, month, 1), datetime.date(year, month, last_day)


class RecordQuerySet(models.QuerySet):
    """Custom QuerySet for Record that enforces safe deletion patterns.

    Bulk ``delete()`` is blocked by default to prevent accidental data loss.
    Use ``allow_bulk_delete()`` to opt in, or call ``record.delete()``
    (soft-delete) / ``record.hard_delete()`` (permanent) instead.
    """

    def delete(self) -> tuple[int, dict[str, int]]:
        if getattr(self, "_allow_bulk_delete", False):
            return super().delete()
        raise TypeError(
            "Use record.delete() for soft-delete or record.hard_delete() for permanent deletion. "
            "QuerySet.delete() is not allowed on Record to prevent accidental data loss."
        )

    def allow_bulk_delete(self) -> RecordQuerySet:
        """Return a clone that permits ``QuerySet.delete()`` for maintenance tasks."""
        qs = self.all()
        qs._allow_bulk_delete = True
        return qs

    def for_user(self, user: AbstractUser) -> RecordQuerySet:
        """Scope the queryset to records belonging to *user*."""
        return self.filter(user=user)

    def shared_with_me(self, user: AbstractUser) -> RecordQuerySet:
        """Records another user has explicitly shared with *user*."""
        return self.filter(shares__user=user, is_active=True)

    def visible_to(self, user: AbstractUser) -> RecordQuerySet:
        """Everything *user* may see: own records plus records shared with them.

        This is the single access boundary for the application. Authoritative
        checks (detail views, mutations) must go through this queryset, never
        ``for_user`` alone.
        """
        return self.filter(Q(user=user) | Q(shares__user=user)).distinct()

    def active(self) -> RecordQuerySet:
        """Return only records that have not been soft-deleted."""
        return self.filter(is_active=True)

    def archived(self) -> RecordQuerySet:
        """Return only records that have been soft-deleted."""
        return self.filter(is_active=False)

    def with_documents(self) -> RecordQuerySet:
        """Prefetch related ``DocumentData`` objects to avoid N+1 queries."""
        return self.prefetch_related("documents")

    def expiring_soon(self, days: int = 30) -> RecordQuerySet:
        """Return active records whose expiry falls within the next *days* days."""
        today = timezone.now().date()
        return self.active().filter(
            expiry_date__gte=today,
            expiry_date__lte=today + datetime.timedelta(days=days),
        )

    def expired(self) -> RecordQuerySet:
        """Return active records whose expiry date has already passed."""
        return self.active().filter(expiry_date__lt=timezone.now().date())

    def smart_search(self, search_query: str) -> RecordQuerySet:
        """Search across text, numeric, and date fields with natural-language heuristics.

        Accepts free-text queries that are matched against titles, merchants,
        products, notes, record types, balances, and dates (including relative
        terms like "today" or month names). Returns an empty queryset when the
        query is blank after stripping.
        """
        if not (search_query := search_query.strip()):
            return self

        lower = search_query.lower()
        conditions = reduce(
            or_,
            (Q(**{f"{field}__icontains": search_query}) for field in _TEXT_SEARCH_FIELDS),
        )

        matching_choices = [
            k
            for k, v in Record.RecordTypes.choices
            if search_query.lower() in k.lower() or search_query.lower() in v.lower()
        ]
        if matching_choices:
            conditions |= Q(record_type__in=matching_choices)

        clean_num = "".join(c for c in search_query if c.isdigit() or c == ".")
        if clean_num and clean_num.replace(".", "", 1).isdigit():
            try:
                val = Decimal(clean_num)
                conditions |= Q(balance__gte=val, balance__lt=val + 1)
            except InvalidOperation, ValueError, OverflowError:
                pass

        start: datetime.date | None = None
        end: datetime.date | None = None

        if lower in _RELATIVE_DAYS:
            delta = {"today": 0, "yesterday": -1, "tomorrow": 1}[lower]
            start = end = timezone.now().date() + datetime.timedelta(days=delta)

        elif _YEAR_RE.match(search_query):
            year = int(search_query)
            start, end = datetime.date(year, 1, 1), datetime.date(year, 12, 31)

        elif lower in _MONTH_MAP:
            start, end = _month_range(timezone.now().date().year, _MONTH_MAP[lower])

        elif _ISO_DATE_RE.match(search_query):
            try:
                start = end = datetime.date.fromisoformat(search_query)
            except ValueError:
                start = end = None

        if start is not None and end is not None:
            conditions |= reduce(
                or_,
                (Q(**{f"{f}__range": (start, end)}) for f in _DATE_FIELDS),
            )

        return self.filter(conditions)


class FolderQuerySet(models.QuerySet):
    """Custom queryset for Folder with annotation helpers."""

    def with_record_counts(self) -> FolderQuerySet:
        """Annotate each folder with its active record count."""
        return self.annotate(
            active_records_count=Count("records", filter=Q(records__is_active=True))
        )


class Folder(models.Model):
    """User-owned folder for organising records into logical groups."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="folders"
    )
    name = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = FolderQuerySet.as_manager()

    def __str__(self) -> str:
        return self.name


class RecordManager(models.Manager.from_queryset(RecordQuerySet)):
    pass


class Record(models.Model):
    """Central domain object representing an individual financial record.

    Stores metadata extracted from uploaded receipts or synced from Plaid bank
    transactions. Records are soft-deleted by flipping ``is_active`` rather
    than removing the row, preserving referential integrity for merge logs
    and audit history.
    """

    class RecordTypes(models.TextChoices):
        EXPENSE_RECEIPT = "expense_receipt", "Expense Receipt"
        VOUCHER = "voucher", "Voucher"
        FINANCIAL_DOCUMENT = "financial_document", "Financial Document"
        WARRANTY_CERTIFICATE = "warranty_certificate", "Warranty Certificate"
        VENDOR_INVOICE = "vendor_invoice", "Vendor Invoice"
        CUSTOMER_INVOICE = "customer_invoice", "Customer Invoice"
        LOAN_DOCUMENT = "loan_document", "Loan Document"
        CREDIT_CARD_STATEMENT = "credit_card_statement", "Credit Card Statement"
        BANK_STATEMENT = "bank_statement", "Bank Statement"
        PURCHASE_ORDER = "purchase_order", "Purchase Order"
        PAYSLIP = "payslip", "Payslip"
        TAX_DOCUMENT = "tax_document", "Tax Document"
        SERVICE_CONTRACT = "service_contract", "Service Contract"
        LEASE_AGREEMENT = "lease_agreement", "Lease Agreement"
        INSURANCE_POLICY = "insurance_policy", "Insurance Policy"
        OTHER = "other", "Other"

    id = models.BigAutoField(primary_key=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="records",
    )
    date_added = models.DateField(auto_now_add=True, db_index=True)
    last_edited = models.DateTimeField(auto_now=True, db_index=True)
    is_active = models.BooleanField(default=True, db_index=True)
    reimbursed = models.BooleanField(default=False, db_index=True)

    title = models.CharField(max_length=255)
    merchant = models.CharField(max_length=255, default="")
    balance = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        default=None,
        db_index=True,
    )
    currency = models.CharField(
        max_length=3,
        choices=CURRENCY_CHOICES,
        default=DEFAULT_CURRENCY,
    )
    products = models.TextField(blank=True, default="")
    transaction_date = models.DateField(null=True, blank=True, db_index=True)
    expiry_date = models.DateField(null=True, blank=True, db_index=True)
    notes = models.TextField(blank=True, default="")
    payment_method = models.CharField(max_length=255, blank=True, default="")
    nickname = models.CharField(max_length=255, blank=True, default="")
    record_type = models.CharField(
        max_length=30,
        choices=RecordTypes.choices,
        default=RecordTypes.EXPENSE_RECEIPT,
        db_index=True,
    )

    folder = models.ForeignKey(
        Folder,
        on_delete=models.SET_NULL,
        related_name="records",
        null=True,
        blank=True,
    )

    expiry_notification_sent = models.BooleanField(default=False, db_index=True)

    plaid_transaction_id = models.CharField(max_length=255, unique=True, null=True, blank=True)
    plaid_item = models.ForeignKey(
        "plaid_integration.PlaidItem",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="records",
    )

    objects = RecordManager()
    history = HistoricalRecords()

    class Meta:
        ordering = ["-last_edited"]
        indexes = [
            models.Index(fields=["user", "is_active"], name="idx_record_user_active"),
            models.Index(fields=["user", "-last_edited"], name="idx_record_user_edited"),
            models.Index(fields=["user", "record_type"], name="idx_record_user_type"),
            models.Index(
                fields=["user", "is_active", "-last_edited"],
                name="idx_record_list_cover",
            ),
            models.Index(
                fields=["user", "is_active", "record_type"],
                name="idx_record_type_filter",
            ),
            models.Index(fields=["expiry_date", "is_active"], name="idx_record_expiry_active"),
            models.Index(
                fields=["expiry_date", "is_active", "user"],
                name="idx_record_expiry_active_user",
            ),
            models.Index(
                fields=["expiry_date", "is_active", "user", "date_added"],
                name="idx_record_archive_filter",
            ),
            models.Index(fields=["transaction_date"], name="idx_record_trans_date"),
            models.Index(fields=["user", "balance"], name="idx_record_user_balance"),
        ]

    def delete(self, using=None, keep_parents=False):  # noqa: ARG002
        """Soft-delete the record by marking it inactive rather than removing it."""
        self.is_active = False
        self.last_edited = timezone.now()
        self.save(update_fields=["is_active", "last_edited"])

    def hard_delete(self, using=None, keep_parents=False):
        """Permanently remove this record from the database. Irreversible."""
        super().delete(using=using, keep_parents=keep_parents)

    def is_shared_with(self, user: AbstractUser) -> bool:
        """True if *user* can access this record through a share."""
        if user.pk == self.user_id:
            return True
        return self.shares.filter(user=user).exists()

    @property
    def shared_count(self) -> int:
        return self.shares.count()

    def __str__(self):
        return self.title

    @property
    def badge_classes(self) -> str:
        """Return Tailwind CSS classes for the record-type badge in the UI."""
        return RECORD_TYPE_COLOR_MAP.get(self.record_type, RECORD_TYPE_COLOR_MAP["other"])

    @property
    def is_plaid_record(self) -> bool:
        """True when this record originated from a Plaid bank transaction."""
        return bool(self.plaid_transaction_id)

    @property
    def is_expired(self) -> bool:
        """True when the expiry date is in the past."""
        if self.expiry_date:
            return self.expiry_date < timezone.now().date()
        return False

    def is_expiring_soon(self, days: int = 30) -> bool:
        """True when the expiry date falls within the next *days* days."""
        if self.expiry_date:
            return self.expiry_date <= (timezone.now().date() + datetime.timedelta(days=days))
        return False

    def save(self, *args, **kwargs):
        if self.pk and self.is_plaid_record:
            protected = {"plaid_transaction_id", "plaid_item"}
            if update_fields := kwargs.get("update_fields"):
                kwargs["update_fields"] = [f for f in update_fields if f not in protected]
        super().save(*args, **kwargs)


class MergeLog(models.Model):
    """Immutable record of a merge between a Plaid transaction and a document record.

    Captures snapshots of both records at merge time so the operation can be
    undone or the receipt replaced later without data loss. The
    ``idempotency_key`` column prevents duplicate merges for the same pair.
    """

    plaid_record = models.ForeignKey(
        Record, on_delete=models.SET_NULL, null=True, related_name="merge_logs_as_plaid"
    )
    document_record = models.ForeignKey(
        Record,
        on_delete=models.SET_NULL,
        null=True,
        related_name="merge_logs_as_document",
    )
    document = models.ForeignKey(
        "documents.DocumentData", on_delete=models.SET_NULL, null=True, blank=True
    )
    plaid_snapshot = models.JSONField()
    document_snapshot = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    undone_at = models.DateTimeField(null=True, blank=True, db_index=True)
    search_text = models.TextField(blank=True, db_index=False)
    idempotency_key = models.CharField(
        max_length=64, unique=True, null=True, blank=True, db_index=True
    )

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["search_text"], name="idx_mergelog_search"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["plaid_record", "document_record"],
                name="unique_active_merge",
                condition=models.Q(undone_at__isnull=True),
                violation_error_message="An active merge already exists for this pair.",
            ),
        ]

    def save(
        self,
        force_insert=False,
        force_update=False,
        using=None,
        update_fields=None,
    ):
        parts = []
        if self.plaid_record_id and self.plaid_record:
            parts.extend([self.plaid_record.title or "", self.plaid_record.merchant or ""])
        if self.document_record_id and self.document_record:
            parts.extend([self.document_record.title or "", self.document_record.merchant or ""])
        self.search_text = " ".join(p for p in parts if p)
        super().save(
            force_insert=force_insert,
            force_update=force_update,
            using=using,
            update_fields=update_fields,
        )

    def __str__(self) -> str:
        return f"Merge {self.pk}: plaid={self.plaid_record_id} <- doc={self.document_record_id}"


class AuditLog(models.Model):
    """Append-only log of every significant record mutation for accountability.

    Each entry captures who did what (action), to which record, at what time,
    and optionally why (details JSON). Used for compliance and debugging.
    """

    class Action(models.TextChoices):
        MERGE = "merge"
        UNDO_MERGE = "undo_merge"
        DETACH_RECEIPT = "detach_receipt"
        REPLACE_RECEIPT = "replace_receipt"
        CREATE_RECORD = "create_record"
        UPDATE_RECORD = "update_record"
        SOFT_DELETE = "soft_delete"
        HARD_DELETE = "hard_delete"
        ARCHIVE = "archive"
        UNARCHIVE = "unarchive"
        SHARE = "share"  # record shared with a user
        REVOKE_SHARE = "revoke_share"  # record share removed

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="audit_logs",
    )
    action = models.CharField(max_length=32, choices=Action.choices, db_index=True)
    record = models.ForeignKey(
        Record, on_delete=models.SET_NULL, null=True, related_name="audit_logs"
    )
    merge_log = models.ForeignKey(MergeLog, on_delete=models.SET_NULL, null=True, blank=True)
    details = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["user", "action"], name="idx_auditlog_user_action"),
            models.Index(fields=["record", "action"], name="idx_auditlog_record_action"),
        ]

    def __str__(self) -> str:
        return f"{self.action} by user={self.user_id} record={self.record_id} at {self.created_at}"


class RecordShare(models.Model):
    """An explicit grant of access to a record for another user.

    Sharing grants the full view + edit experience: the recipient sees the
    record in their list/search, can open and edit it, and their edits are
    attributed to them in the record's history (simple_history already
    records ``history_user`` per change).

    Model choices
    -------------
    * One row per (record, user) pair; the unique constraint makes grants
      idempotent at the DB layer and is indexed for the ``visible_to``
      queryset.
    """

    record = models.ForeignKey(
        Record,
        on_delete=models.CASCADE,
        related_name="shares",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="shared_records",
    )
    shared_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        help_text="User who granted the share",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["record", "user"],
                name="unique_record_share",
                violation_error_message="This record is already shared with that user.",
            )
        ]
        indexes = [
            models.Index(fields=["record"], name="idx_recordshare_record"),
            models.Index(fields=["user"], name="idx_recordshare_user"),
        ]

    def __str__(self) -> str:
        return f"share record={self.record_id} -> user={self.user_id}"
