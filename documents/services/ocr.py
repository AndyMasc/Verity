"""OCR pipeline service for Gemini-based document extraction.

Encapsulates the full OCR pipeline: Gemini client/schema configuration,
R2 file fetching, image preprocessing, API calls with retry logic, and
result caching. The tasks layer delegates here for all extraction work.
"""

import logging
import re
from typing import Any

from django.conf import settings
from django.core.cache import cache
from django.db.models import F

from documents.models import DocumentData, DocumentStatus
from documents.ocr_helpers import prepare_image_for_gemini
from documents.storage import BUCKET, get_s3_client

from .cleanup import normalize_s3_key

logger = logging.getLogger(__name__)

OCR_CACHE_TTL = 604800
MAX_OCR_RETRIES = 3

try:
    from google import genai
    from google.genai import types
    from pydantic import BaseModel, Field

    from records.models import Record

    client = genai.Client(api_key=settings.GEMINI_API_KEY)

    class OCRResult(BaseModel):
        title: str = Field(
            description="2-5 word title: 'Merchant + Item'. Shorten product names; preserve identity. Default to shortest identifier for warranties/loans. No invoice numbers/dates. If no clear title exists, use a generic description like 'Financial Document', 'Receipt', or 'Untitled'. Absolute Max length 255 chars."
        )
        merchant: str | None = Field(
            default=None,
            description="The business name. Infer from logos if clear. Default to null if ambiguous. Absolute max length 255 chars.",
        )
        balance: float | None = Field(
            default=None,
            description="Total monetary sum as a raw number. No symbols, commas or currency symbols.",
        )
        currency: str | None = Field(
            default=None,
            description="ISO 4217 lowercase currency code (e.g. usd, eur, gbp). Infer from currency symbols on the document. Default to null if unclear.",
        )
        products: list[str] = Field(
            default_factory=list,
            description="List of items. Standardize typos, expand abbreviations, use Title Case. If no products are listed, use an empty list.",
        )
        transaction_date: str | None = Field(default=None, description="Date in YYYY-MM-DD format.")
        expiry_date: str | None = Field(default=None, description="Date in YYYY-MM-DD format.")
        record_type: Record.RecordTypes = Field(
            description="Strictly classify the document type. If no clear type can be found, default to EXPENSE_RECEIPT"
        )
        suggested_folder: str | None = Field(
            default=None,
            description="Choose from the user's available folders provided in the prompt. If the document clearly belongs in one, return that folder name exactly. If none fit, return null.",
        )

    CONFIG = types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=OCRResult,
        system_instruction="Extract data exactly as it appears from the image. Never hallucinate or invent information. No preamble.",
        temperature=0.0,
        max_output_tokens=700,
    )
except ImportError:
    client = None
    OCRResult = None  # type: ignore[misc]
    CONFIG = None


class GeminiOCRError(Exception):
    """Raised when Gemini OCR fails after exhausting all retry attempts."""


def get_cache_key(document_id: int) -> str:
    """Return the cache key used to store OCR results for a document."""
    return f"ocr_status_{document_id}"


def set_document_status(document_id: int, status: str, **kwargs: Any) -> None:
    """Update a document's status and any additional fields atomically."""
    DocumentData.objects.filter(id=document_id).update(status=status, **kwargs)


def increment_ocr_retries(document_id: int) -> int:
    """Atomically increment and return the current OCR retry count."""
    DocumentData.objects.filter(id=document_id).update(ocr_retries=F("ocr_retries") + 1)
    doc = DocumentData.objects.filter(id=document_id).values("ocr_retries").first()
    return doc["ocr_retries"] if doc else 0


def fetch_from_r2(filepath: str) -> bytes:
    """Download the full file content from R2 for the given key."""
    s3 = get_s3_client()
    key = normalize_s3_key(filepath)
    response = s3.get_object(Bucket=BUCKET, Key=key)
    body = response["Body"]
    if hasattr(body, "read"):
        return body.read()
    return b"".join(chunk for chunk in body.iter_chunks(chunk_size=1024 * 1024))


def process_image(image_bytes: bytes, filepath: str) -> types.Part:
    """Convert raw file bytes into a Gemini-compatible Part, preprocessing images."""
    if filepath.lower().endswith(".pdf"):
        return types.Part.from_bytes(data=image_bytes, mime_type="application/pdf")

    image_bytes = prepare_image_for_gemini(image_bytes)
    return types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg")


def call_gemini(image_part: types.Part, folder_names: list[str]) -> dict[str, Any]:
    """Send the image to Gemini with folder context and return parsed OCR results."""
    contents = []

    folder_context = (
        f"User's available folders: {', '.join(folder_names) if folder_names else 'None'}. "
        f"If the document context logically fits one of these folders, populate 'suggested_folder' with the exact name string. Otherwise null."
    )

    contents.append(folder_context)
    contents.append(image_part)

    result = client.models.generate_content(
        model="gemini-3.5-flash-lite",
        contents=contents,
        config=CONFIG,
    )

    if result.parsed is not None:
        return result.parsed.model_dump(mode="json")

    raw_text = result.text.strip()
    clean_json = re.sub(r"^```json\s*|```$", "", raw_text, flags=re.MULTILINE).strip()
    return OCRResult.model_validate_json(clean_json).model_dump(mode="json")


def extract(document_id: int) -> dict[str, Any]:
    """Run the full OCR pipeline on a document, returning structured financial data.

    Checks for cached results and already-processed documents before fetching
    from R2. On failure, increments retry count and raises GeminiOCRError
    after exhausting MAX_OCR_RETRIES.
    """
    cache_key = get_cache_key(document_id)

    doc_lookup = DocumentData.objects.filter(id=document_id).values("status", "did_ocr").first()
    if not doc_lookup:
        logger.error("Document %s does not exist.", document_id)
        return {"error": "Document not found."}

    if doc_lookup["did_ocr"]:
        cached = cache.get(cache_key)
        if isinstance(cached, dict) and "error" not in cached:
            return cached
        return {"error": "Already processed"}

    if doc_lookup["status"] == DocumentStatus.COMPLETED:
        cached = cache.get(cache_key)
        if isinstance(cached, dict) and "error" not in cached:
            return cached

    try:
        set_document_status(document_id, DocumentStatus.PROCESSING)

        document = (
            DocumentData.objects.select_related("user")
            .prefetch_related("user__folders")
            .get(id=document_id)
        )

        folder_names = list(document.user.folders.values_list("name", flat=True))

        image_content = fetch_from_r2(document.filepath)
        part = process_image(image_content, document.filepath)

        final_data = call_gemini(part, folder_names)

        cache.set(cache_key, final_data, timeout=OCR_CACHE_TTL)
        set_document_status(
            document_id,
            DocumentStatus.COMPLETED,
            ocr_error="",
            did_ocr=True,
            ocr_raw_data=final_data,
        )
        return final_data

    except Exception as exc:
        logger.warning("OCR attempt failed for doc %s: %s", document_id, exc, exc_info=True)
        retries = increment_ocr_retries(document_id)

        if retries >= MAX_OCR_RETRIES:
            error_payload = {"error": "Failed to automatically extract document details."}
            cache.set(cache_key, error_payload, timeout=OCR_CACHE_TTL)
            set_document_status(
                document_id,
                DocumentStatus.ERROR,
                ocr_error=str(exc),
                ocr_raw_data=error_payload,
            )
            raise GeminiOCRError(f"OCR execution hard-failed for document {document_id}") from exc

        raise
