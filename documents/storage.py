"""Cloudflare R2 (S3-compatible) storage operations for document files.

Handles presigned URL generation, object existence verification, gatekeeper
validation of uploaded files, and deletion of single or batch objects.
"""

import logging
import uuid
from functools import lru_cache
from io import BytesIO

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from django.conf import settings
from django.core.exceptions import ValidationError
from PIL import Image, ImageFile

from .validators import (
    MAX_FILE_SIZE,
    MAX_IMAGE_PIXELS,
    validate_file_bytes,
)

logger = logging.getLogger(__name__)

ImageFile.LOAD_TRUNCATED_IMAGES = True

TIMEOUT_SECONDS = 30
BUCKET = settings.R2_STORAGE_BUCKET_NAME


@lru_cache(maxsize=1)
def _get_s3_client():
    return boto3.client(
        service_name="s3",
        endpoint_url=settings.R2_S3_ENDPOINT_URL,
        aws_access_key_id=settings.R2_ACCESS_KEY_ID,
        aws_secret_access_key=settings.R2_SECRET_ACCESS_KEY,
        region_name="auto",
        config=Config(
            signature_version="s3v4",
            connect_timeout=TIMEOUT_SECONDS,
            read_timeout=TIMEOUT_SECONDS,
            max_pool_connections=50,
            retries={"max_attempts": 3, "mode": "standard"},
        ),
    )


def get_s3_client():
    """Return the cached S3-compatible client for R2 operations."""
    return _get_s3_client()


def generate_upload_key(user_id: int, extension: str) -> str:
    """Create a unique, namespaced S3 key for a user's uploaded file."""
    safe_ext = extension.lstrip(".").lower()
    return f"users/{user_id}/{uuid.uuid4()}.{safe_ext}"


def generate_presigned_post(user_id: int, key: str, content_type: str) -> str:  # noqa: ARG001
    """Generate a presigned PUT URL for uploading a file to R2 (15-minute expiry)."""
    s3 = get_s3_client()
    return s3.generate_presigned_url(
        "put_object",
        Params={
            "Bucket": BUCKET,
            "Key": key,
            "ContentType": content_type,
        },
        ExpiresIn=900,
    )


def generate_read_presigned_url(key: str) -> str:
    """Generate a presigned GET URL for viewing a file from R2 (15-minute expiry)."""
    s3 = get_s3_client()
    return s3.generate_presigned_url(
        "get_object",
        Params={
            "Bucket": BUCKET,
            "Key": key,
        },
        ExpiresIn=900,
    )


def verify_r2_object_exists(key: str) -> bool:
    """Check whether a file exists in R2 by performing a HEAD request."""
    s3 = get_s3_client()
    try:
        s3.head_object(Bucket=BUCKET, Key=key)
        return True
    except ClientError:
        return False


def get_r2_object_head(key: str) -> dict | None:
    """Retrieve R2 object metadata, returning None if the object doesn't exist."""
    s3 = get_s3_client()
    try:
        return s3.head_object(Bucket=BUCKET, Key=key)
    except ClientError:
        return None


def gatekeeper_validate_r2_object(key: str, head: dict | None = None) -> dict:
    """Validate an uploaded R2 object for size, emptiness, file type, and image dimensions.

    Rejects files that exceed size limits, are empty, have disallowed MIME types,
    or contain images with excessively large pixel counts. Deletes invalid objects.

    *head* may be a previously fetched HEAD response to avoid an extra R2
    round trip when the caller already has one.

    Returns:
        Dict with 'valid' key (bool) and optional 'error' message.
    """
    s3 = get_s3_client()
    if head is None:
        head = get_r2_object_head(key)
    if head is None:
        return {"valid": False, "error": "Object not found in R2."}

    content_length = head.get("ContentLength", 0)
    if content_length == 0 or content_length > MAX_FILE_SIZE:
        s3.delete_object(Bucket=BUCKET, Key=key)
        return {"valid": False, "error": _size_error(content_length)}

    try:
        resp = s3.get_object(
            Bucket=BUCKET,
            Key=key,
            Range="bytes=0-8191",
        )
        header_bytes = resp["Body"].read()

        error = _validate_header(header_bytes, content_length)
        if error:
            s3.delete_object(Bucket=BUCKET, Key=key)
            return {"valid": False, "error": error}
    except ClientError as e:
        logger.warning("Gatekeeper partial read failed for %s: %s", key, e)

    return {"valid": True}


def validate_uploaded_bytes(content: bytes) -> str | None:
    """Validate raw file bytes for size, emptiness, file type, and image dimensions.

    Applies the same gatekeeper rules as "gatekeeper_validate_r2_object" but on
    in-memory bytes, so the OCR worker can gate extraction without extra R2
    round trips. Returns an error message, or None when the bytes pass.
    """
    content_length = len(content)
    if content_length == 0 or content_length > MAX_FILE_SIZE:
        return _size_error(content_length)
    return _validate_header(content[:8192], content_length)


def _size_error(content_length: int) -> str:
    """Return the gatekeeper rejection message for an empty or oversized file."""
    if content_length == 0:
        return "Empty file rejected."
    return f"File exceeds {MAX_FILE_SIZE / 1024 / 1024}MB limit."


def _validate_header(header_bytes: bytes, content_length: int) -> str | None:
    """Run MIME and image-dimension checks on a file's header bytes.

    Returns an error message, or None when the header passes all checks.
    """
    try:
        validate_file_bytes(header_bytes, content_length)
    except ValidationError as e:
        return str(e)

    if header_bytes[:4] in (
        b"\xff\xd8\xff",
        b"\x89\x50\x4e\x47\x0d\x0a\x1a\x0a",
        b"\x52\x49\x46\x46",
        b"\x42\x4d",
    ) or header_bytes.startswith(b"%PDF"):
        try:
            img = Image.open(BytesIO(header_bytes))
            img.verify()
            img = Image.open(BytesIO(header_bytes))
            width, height = img.size
            total_pixels = width * height
            if total_pixels > MAX_IMAGE_PIXELS:
                return (
                    f"Image dimensions too large ({width}x{height}). "
                    f"Maximum {MAX_IMAGE_PIXELS:,} pixels."
                )
        except Exception as e:
            logger.warning("Image dimension check failed: %s", e)
    return None


def delete_r2_object(key: str) -> None:
    """Delete a single object from R2, logging any client errors."""
    s3 = get_s3_client()
    try:
        s3.delete_object(Bucket=BUCKET, Key=key)
    except ClientError as e:
        logger.error("Failed to delete R2 object %s: %s", key, e)


def delete_r2_objects_batch(keys: list[str]) -> None:
    """Delete multiple R2 objects in chunks of 1000 (R2 batch limit)."""
    if not keys:
        return
    s3 = get_s3_client()
    CHUNK = 1000
    for i in range(0, len(keys), CHUNK):
        chunk = keys[i : i + CHUNK]
        try:
            s3.delete_objects(
                Bucket=BUCKET,
                Delete={"Objects": [{"Key": k} for k in chunk]},
            )
        except ClientError as e:
            logger.error("Batch R2 deletion failed for chunk: %s", e)
