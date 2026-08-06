"""Image preprocessing and OCR result mapping for Gemini-based document extraction.

Handles HEIC decoding, deskewing, resizing, and WebP encoding to prepare
images for optimal Gemini OCR performance, plus mapping extracted JSON
to record form fields.
"""

import json
import logging

import cv2
import numpy as np
from deskew import determine_skew
from pillow_heif import is_supported, read_heif

logger = logging.getLogger(__name__)

MAX_DIMENSION = 1200
WEBP_QUALITY = 85
SKEW_MAX_DIM = 500
SKEW_THRESHOLD = 0.5


def ocr_data_to_form_initial(data: dict | None) -> dict:
    """Convert raw Gemini OCR output into a dict suitable for pre-populating a record form.

    Normalizes the products field from a list of dicts/strings into newline-joined text.
    """
    if not isinstance(data, dict):
        return {}

    products_data = data.get("products") or []

    if isinstance(products_data, list):
        processed_products = [
            json.dumps(p) if isinstance(p, (dict, list)) else str(p).strip() for p in products_data
        ]
        products_value = "\n".join(processed_products).strip()
    else:
        products_value = products_data

    return {
        "title": data.get("title"),
        "products": products_value,
        "merchant": data.get("merchant"),
        "balance": data.get("balance"),
        "currency": data.get("currency"),
        "transaction_date": data.get("transaction_date"),
        "expiry_date": data.get("expiry_date"),
        "record_type": data.get("record_type"),
        "suggested_folder": data.get("suggested_folder"),
    }


def _decode_image(image_bytes: bytes) -> np.ndarray | None:
    """Decode image bytes into an OpenCV BGR array, supporting HEIC via pillow-heif."""
    if is_supported(image_bytes):
        try:
            heif_file = read_heif(image_bytes)
            img_rgb = np.frombuffer(heif_file.data, dtype=np.uint8).reshape(
                heif_file.size[1], heif_file.size[0], 3
            )
            return cv2.cvtColor(img_rgb, cv2.COLOR_RGB2BGR)
        except Exception as e:
            logger.error("Failed to decode HEIC bytes: %s", e)
            return None

    nparr = np.frombuffer(image_bytes, np.uint8)
    return cv2.imdecode(nparr, cv2.IMREAD_COLOR)


def _deskew_image(img: np.ndarray) -> np.ndarray:
    """Rotate image to correct skew detected via the deskew library.

    Downsamples to SKEW_MAX_DIM for angle detection performance, then
    applies the rotation at full resolution if the angle exceeds the threshold.
    """
    h, w = img.shape[:2]
    scale = SKEW_MAX_DIM / max(h, w)
    if scale < 1.0:
        small_gray = cv2.resize(
            cv2.cvtColor(img, cv2.COLOR_BGR2GRAY),
            (int(w * scale), int(h * scale)),
            interpolation=cv2.INTER_AREA,
        )
    else:
        small_gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    angle = determine_skew(small_gray)

    if angle and abs(angle) > SKEW_THRESHOLD:
        center = (w // 2, h // 2)
        matrix = cv2.getRotationMatrix2D(center, angle, 1.0)
        img = cv2.warpAffine(
            img,
            matrix,
            (w, h),
            flags=cv2.INTER_CUBIC,
            borderMode=cv2.BORDER_REPLICATE,
        )
    return img


def _resize_image(img: np.ndarray) -> np.ndarray:
    """Scale image so the longest dimension does not exceed MAX_DIMENSION."""
    h, w = img.shape[:2]
    if max(h, w) > MAX_DIMENSION:
        scale = MAX_DIMENSION / max(h, w)
        img = cv2.resize(img, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
    return img


def _encode_webp(img: np.ndarray) -> bytes:
    """Encode an OpenCV image to WebP bytes at the configured quality level."""
    encode_param = [int(cv2.IMWRITE_WEBP_QUALITY), WEBP_QUALITY]
    success, encoded_img = cv2.imencode(".webp", img, encode_param)
    if not success:
        raise ValueError("Failed to encode image to WebP")
    return encoded_img.tobytes()


def prepare_image_for_gemini(image_bytes: bytes) -> bytes:
    """Full preprocessing pipeline: decode, deskew, resize, and encode to WebP.

    Falls back to the original bytes if any step fails, ensuring OCR can
    still attempt extraction on unprocessed images.
    """
    try:
        img = _decode_image(image_bytes)
        if img is None:
            raise ValueError("Could not decode image bytes")

        img = _deskew_image(img)
        img = _resize_image(img)
        return _encode_webp(img)

    except Exception as e:
        logger.error("Failed to optimize image: %s", e)
        return image_bytes
