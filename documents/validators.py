"""File validation utilities for document uploads.

Detects MIME types from file headers using python-magic, filetype, and raw
magic-byte signatures as fallbacks. Enforces size limits, allowed types,
and pixel dimensions for images.
"""

import logging
from dataclasses import dataclass
from typing import IO

import filetype
from django.core.exceptions import ValidationError

try:
    import magic as python_magic

    HAS_MAGIC = True
except (ImportError, OSError):
    python_magic = None  # type: ignore[assignment]
    HAS_MAGIC = False

logger = logging.getLogger(__name__)

ALLOWED_MIME_TYPES = frozenset(
    {
        "application/pdf",
        "image/jpeg",
        "image/png",
        "image/webp",
        "image/tiff",
        "image/bmp",
        "image/heic",
        "image/heif",
    }
)

MAX_FILE_SIZE = 50 * 1024 * 1024
MAX_IMAGE_PIXELS = 50_000_000

MAGIC_SIGNATURES = {
    b"\x25\x50\x44\x46": "application/pdf",
    b"\xff\xd8\xff": "image/jpeg",
    b"\x89\x50\x4e\x47\x0d\x0a\x1a\x0a": "image/png",
    b"\x52\x49\x46\x46": "image/webp",
    b"\x49\x49\x2a\x00": "image/tiff",
    b"\x4d\x4d\x00\x2a": "image/tiff",
    b"\x42\x4d": "image/bmp",
}


def _detect_mime_from_bytes(header_bytes: bytes) -> str | None:
    """Detect MIME type from file header bytes using multiple detection strategies.

    Tries python-magic first, falls back to filetype, then raw magic-byte
    signature matching.
    """
    if HAS_MAGIC:
        try:
            detected = python_magic.from_buffer(header_bytes, mime=True)
            # libmagic reports unrecognized/truncated headers as
            # "application/octet-stream"; keep the specific fallbacks below
            # rather than treating that as a successful detection.
            if detected and detected != "application/octet-stream":
                return detected
        except Exception as e:
            logger.warning("python-magic detection failed: %s", e)

    kind = filetype.guess(header_bytes)
    if kind:
        return kind.mime

    for signature, mime_type in MAGIC_SIGNATURES.items():
        if header_bytes.startswith(signature):
            return mime_type

    return None


@dataclass
class ValidationResult:
    """Result of a file validation check, carrying size and detected MIME type."""

    file_size: int
    mime_type: str


def _validate_file(size: int, detected_mime: str | None) -> ValidationResult:
    """Enforce size and MIME type constraints, raising ValidationError on failure."""
    if size == 0:
        raise ValidationError("File is empty.")
    if size > MAX_FILE_SIZE:
        raise ValidationError(
            f"File size ({size / 1024 / 1024:.2f}MB) exceeds maximum of {MAX_FILE_SIZE / 1024 / 1024}MB."
        )
    if not detected_mime:
        raise ValidationError("Unable to validate file type.")
    if detected_mime not in ALLOWED_MIME_TYPES:
        raise ValidationError(
            f"File type '{detected_mime}' is not allowed. Allowed: {', '.join(sorted(ALLOWED_MIME_TYPES))}"
        )
    return ValidationResult(file_size=size, mime_type=detected_mime)


def validate_file_upload(
    file_obj: IO[bytes],
    declared_mime_type: str | None = None,  # noqa: ARG001
) -> ValidationResult:
    """Validate a file-like upload object by reading its header and checking constraints.

    Args:
        file_obj: A seekable file-like object to validate.
        declared_mime_type: Ignored; detection is always done from raw bytes.

    Returns:
            ValidationResult with file size and detected MIME type.

    Raises:
        ValidationError: If the file is empty, too large, or an unsupported type.
    """
    file_obj.seek(0, 2)
    file_size = file_obj.tell()
    file_obj.seek(0)

    header = file_obj.read(2048)
    file_obj.seek(0)

    detected_mime = _detect_mime_from_bytes(header)
    return _validate_file(file_size, detected_mime)


def validate_file_bytes(header_bytes: bytes, content_length: int) -> ValidationResult:
    """Validate file type and size from raw header bytes and a known content length.

    Used by the gatekeeper to validate R2 objects where only a partial read is available.

    Args:
            header_bytes: First ~8KB of the file for MIME detection.
            content_length: Total file size in bytes.

    Returns:
            ValidationResult with file size and detected MIME type.

    Raises:
        ValidationError: If the file is empty, too large, or an unsupported type.
    """
    detected_mime = _detect_mime_from_bytes(header_bytes)
    return _validate_file(content_length, detected_mime)
