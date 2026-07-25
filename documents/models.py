"""Database models and querysets for the documents module.

Manages the lifecycle of uploaded document files, from pending upload through
OCR processing to archival, including soft-delete for compliance retention.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone
from simple_history.models import HistoricalRecords

if TYPE_CHECKING:
    from django.contrib.auth.models import AbstractUser

User = settings.AUTH_USER_MODEL


class DocumentStatus(models.TextChoices):
    """Lifecycle states a document transitions through from upload to completion."""

    PENDING_UPLOAD = "pending_upload", "Pending Upload"
    UPLOADED = "uploaded", "Uploaded"
    PROCESSING = "processing", "Processing OCR"
    COMPLETED = "completed", "Completed"
    ERROR = "error", "Error"
    DELETING = "deleting", "Deleting"


class DocumentDataQuerySet(models.QuerySet):
    """Custom queryset providing filtered views across the document lifecycle."""

    def for_user(self, user: AbstractUser) -> DocumentDataQuerySet:
        """Return only active documents belonging to the given user."""
        return self.filter(user=user, is_active=True)

    def active(self) -> DocumentDataQuerySet:
        """Return non-trashed documents."""
        return self.filter(is_active=True)

    def trashed(self) -> DocumentDataQuerySet:
        """Return soft-deleted documents."""
        return self.filter(is_active=False)

    def orphaned(self) -> DocumentDataQuerySet:
        """Return active documents not linked to any record."""
        return self.filter(associated_record__isnull=True, is_active=True)

    def linked(self) -> DocumentDataQuerySet:
        """Return documents associated with at least one record."""
        return self.filter(associated_record__isnull=False)

    def by_status(self, status: str) -> DocumentDataQuerySet:
        """Filter documents to those matching the given lifecycle status."""
        return self.filter(status=status)

    def pending(self) -> DocumentDataQuerySet:
        """Return documents awaiting upload confirmation."""
        return self.by_status(DocumentStatus.PENDING_UPLOAD)

    def processing(self) -> DocumentDataQuerySet:
        """Return documents currently undergoing OCR processing."""
        return self.by_status(DocumentStatus.PROCESSING)

    def completed(self) -> DocumentDataQuerySet:
        """Return documents that have finished OCR successfully."""
        return self.by_status(DocumentStatus.COMPLETED)

    def errored(self) -> DocumentDataQuerySet:
        """Return documents that failed OCR processing."""
        return self.by_status(DocumentStatus.ERROR)

    def stale_pending(self, minutes: int = 30) -> DocumentDataQuerySet:
        """Return pending uploads older than the given threshold for cleanup."""
        from datetime import timedelta

        cutoff = timezone.now() - timedelta(minutes=minutes)
        return self.pending().filter(date_added__lt=cutoff)

    def stale_error(self, days: int = 2) -> DocumentDataQuerySet:
        """Return errored documents older than the given threshold for cleanup."""
        from datetime import timedelta

        cutoff = timezone.now() - timedelta(days=days)
        return self.errored().filter(date_added__lt=cutoff)

    def search(self, query: str) -> DocumentDataQuerySet:
        """Case-insensitive search across document title and notes."""
        if not (query := query.strip()):
            return self
        return self.filter(Q(title__icontains=query) | Q(notes__icontains=query))

    def with_record(self) -> DocumentDataQuerySet:
        """Eager-load the associated record to avoid N+1 queries."""
        return self.select_related("associated_record")


class DocumentDataManager(models.Manager.from_queryset(DocumentDataQuerySet)):
    """Manager that exposes DocumentDataQuerySet filters at the model level."""


class DocumentData(models.Model):
    """Represents an uploaded document file and its processing metadata.

    Documents track a file from initial upload through optional OCR extraction,
    linking to a Record once processed. OCR-processed documents are soft-deleted
    for 7-year compliance retention; unprocessed documents are hard-deleted.
    """

    id = models.BigAutoField(primary_key=True)
    title = models.CharField(max_length=200, default="Untitled")
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="documents",
    )
    filepath = models.CharField(max_length=500)
    date_added = models.DateTimeField(auto_now_add=True, db_index=True)
    associated_record = models.ForeignKey(
        "records.Record",
        on_delete=models.CASCADE,
        blank=True,
        null=True,
        related_name="documents",
    )
    did_ocr = models.BooleanField(default=False)
    ocr_retries = models.PositiveSmallIntegerField(default=0)
    ocr_error = models.TextField(blank=True, default="")
    ocr_metadata = models.JSONField(blank=True, null=True)
    notes = models.TextField(blank=True, default="")
    file_extension = models.CharField(max_length=10, blank=True, default="")
    file_size = models.BigIntegerField(null=True, blank=True)
    mime_type = models.CharField(max_length=100, blank=True, default="")
    file_hash = models.CharField(max_length=64, db_index=True)
    status = models.CharField(
        max_length=20,
        choices=DocumentStatus.choices,
        default=DocumentStatus.PENDING_UPLOAD,
        db_index=True,
    )
    is_active = models.BooleanField(default=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    deleted_at = models.DateTimeField(null=True, blank=True, db_index=True)

    objects = DocumentDataManager()
    history = HistoricalRecords(m2m_fields=[])

    class Meta:
        ordering = ["-date_added"]
        indexes = [
            models.Index(fields=["user", "associated_record"], name="idx_doc_user_record"),
            models.Index(fields=["user", "file_extension"], name="idx_doc_user_ext"),
            models.Index(fields=["date_added", "file_hash"], name="idx_doc_date_hash"),
            models.Index(fields=["user", "status"], name="idx_doc_user_status"),
            models.Index(fields=["user", "-date_added"], name="idx_doc_list_cover"),
            models.Index(fields=["user", "did_ocr", "-date_added"], name="idx_doc_main_cover"),
            models.Index(
                fields=["associated_record", "date_added"],
                name="idx_doc_orphaned_cleanup",
            ),
            models.Index(
                fields=["status", "date_added", "filepath"],
                name="idx_doc_reconcile_pending",
            ),
            models.Index(fields=["status", "date_added"], name="idx_doc_reconcile_error"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "file_hash"],
                name="unique_user_file_hash",
            )
        ]

    def save(self, *args, **kwargs):
        """Persist the document, auto-deriving file_extension from filepath if blank."""
        if self.filepath and not self.file_extension:
            _, ext = os.path.splitext(self.filepath)
            normalized = ext.replace(".", "").strip().lower()[:10]
            if normalized:
                self.file_extension = normalized
        super().save(*args, **kwargs)

    def delete(self, using=None, keep_parents=False):
        """Soft-delete OCR'd documents for compliance; hard-delete others."""
        if self.did_ocr:
            self.is_active = False
            self.deleted_at = timezone.now()
            self.associated_record = None
            self.save(update_fields=["is_active", "deleted_at", "associated_record"])
        else:
            super().delete(using=using, keep_parents=keep_parents)

    def hard_delete(self, using=None, keep_parents=False):
        """Permanently remove the database record regardless of OCR status."""
        super().delete(using=using, keep_parents=keep_parents)

    def undo_delete(self):
        """Restore a soft-deleted document to active status."""
        self.is_active = True
        self.deleted_at = None
        self.save(update_fields=["is_active", "deleted_at"])

    def __str__(self):
        return f"{self.filepath}"

    @property
    def is_processing(self) -> bool:
        """True when the document is still in the upload or OCR pipeline."""
        return self.status in (
            DocumentStatus.PENDING_UPLOAD,
            DocumentStatus.UPLOADED,
            DocumentStatus.PROCESSING,
        )

    @property
    def is_terminal(self) -> bool:
        """True when the document has reached a final state (completed or error)."""
        return self.status in (DocumentStatus.COMPLETED, DocumentStatus.ERROR)

    @property
    def presigned_view_url(self) -> str:
        """Generate a temporary S3 presigned URL for viewing the document."""
        from .storage import generate_read_presigned_url

        return generate_read_presigned_url(self.filepath)
