"""Upload service for handling document presign requests and duplicate detection.

Encapsulates the presigned-URL upload workflow: validates input, detects
duplicates by file hash, generates R2 upload keys, and creates DocumentData
records in a transactional block.
"""

import hashlib
import json
import os
import uuid
from dataclasses import dataclass
from typing import Any

from django.db import transaction
from django.http import HttpRequest

from billing.entitlements import can_add_storage, get_storage_limit, is_storage_limit_exceeded
from documents.forms import R2UploadForm
from documents.models import DocumentData, DocumentStatus
from documents.storage import generate_presigned_post, generate_upload_key


@dataclass
class PresignResult:
    """Result of a presign request, containing either an upload URL or duplicate info."""

    status: str = "error"
    upload_url: str | None = None
    key: str | None = None
    document_id: int | None = None
    existing_document_id: int | None = None
    existing_record_id: int | None = None
    existing_record_label: str | None = None
    existing_record_url: str | None = None
    error: str | None = None
    error_details: dict[str, Any] | None = None


def _parse_request_data(request: HttpRequest) -> dict[str, Any]:
    """Extract upload metadata from JSON body or form POST data."""
    if request.content_type == "application/json":
        try:
            return json.loads(request.body)
        except json.JSONDecodeError:
            return {}
    return request.POST


class UploadService:
    """Orchestrates the document upload flow from validation through presigned URL generation."""

    def __init__(self, request: HttpRequest, record_id: int | None = None):
        self.request = request
        self.record_id = record_id
        self.user = request.user

    def handle(self) -> PresignResult:
        """Process an upload request: validate, check duplicates, and return a presigned URL.

        Returns a PresignResult with status indicating either 'upload_url',
        'duplicate_confirmed', or 'error'.
        """
        data = _parse_request_data(self.request)

        file_hash = data.get("file_hash", "").strip()
        filename = data.get("filename", "").strip()
        content_type = data.get("content_type", "").strip().split(";")[0].strip()
        notes = data.get("notes", "").strip()
        force_upload = data.get("force_upload") == "true"

        if not file_hash or not filename:
            return PresignResult(status="error", error="Missing file_hash or filename.")

        form = R2UploadForm(
            {
                "filename": filename,
                "content_type": content_type,
                "notes": notes,
                "file_size": data.get("file_size", ""),
            }
        )
        if not form.is_valid():
            return PresignResult(
                status="error",
                error="Invalid file parameters.",
                error_details=form.errors,
            )

        if not force_upload:
            existing = self._find_duplicate(file_hash)
            if existing:
                return self._duplicate_result(existing)

        if not self._within_storage_limit(form.cleaned_data.get("file_size")):
            limit_gb = get_storage_limit(self.user)
            return PresignResult(
                status="error",
                error=(
                    f"Storage limit reached ({limit_gb} GB). Upgrade to upload more files, or contact us to hard delete documents for you."
                ),
            )

        effective_hash = self._resolve_hash(file_hash, force_upload)
        key, safe_title = self._prepare_key_and_title(filename)

        with transaction.atomic():
            document = DocumentData.objects.create(
                user=self.user,
                filepath=key,
                associated_record_id=self.record_id,
                did_ocr=(self.record_id is None),
                title=safe_title,
                notes=notes,
                file_hash=effective_hash,
                status=DocumentStatus.PENDING_UPLOAD,
            )

        upload_url = generate_presigned_post(self.user.id, key, content_type)

        return PresignResult(
            status="upload_url",
            upload_url=upload_url,
            key=key,
            document_id=document.id,
        )

    def _within_storage_limit(self, file_size: int | None) -> bool:
        """Return whether the current upload stays within the user's storage quota.

        Falls back to blocking only when the quota is already exceeded if the
        prospective file size is unknown (e.g. legacy clients).
        """
        if file_size is None:
            return not is_storage_limit_exceeded(self.user)
        return can_add_storage(self.user, file_size)

    def _find_duplicate(self, file_hash: str) -> DocumentData | None:
        """Search for an existing active document with the same file hash for this user."""
        return (
            DocumentData.objects.filter(user=self.user, file_hash=file_hash)
            .exclude(status=DocumentStatus.DELETING)
            .with_record()
            .first()
        )

    def _duplicate_result(self, existing: DocumentData) -> PresignResult:
        """Build a PresignResult reflecting a detected duplicate upload."""
        record_id = None
        record_label = "Unassociated Document"
        record_url = "#"
        if existing.associated_record:
            record_id = existing.associated_record.id
            record_label = getattr(
                existing.associated_record,
                "title",
                f"Record #{record_id}",
            )
            record_url = f"/records/record_detail/{record_id}"

        return PresignResult(
            status="duplicate_confirmed",
            existing_document_id=existing.id,
            existing_record_id=record_id,
            existing_record_label=record_label,
            existing_record_url=record_url,
        )

    @staticmethod
    def _resolve_hash(file_hash: str, force_upload: bool) -> str:
        """Return the original hash or a salted variant to bypass duplicate detection."""
        if not force_upload:
            return file_hash
        salt = f"-forced-{uuid.uuid4().hex}"
        return hashlib.sha256((file_hash + salt).encode("utf-8")).hexdigest()

    def _prepare_key_and_title(self, filename: str) -> tuple[str, str]:
        """Derive the R2 storage key and a human-readable title from the filename."""
        ext = os.path.splitext(filename)[1].lower() or ".bin"
        safe_title = os.path.splitext(filename)[0]
        safe_title = safe_title.replace("_", " ").replace("-", " ").title()
        key = generate_upload_key(self.user.id, ext)
        return key, safe_title
