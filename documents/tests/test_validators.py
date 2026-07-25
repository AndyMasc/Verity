import io
import zipfile

from django.test import TestCase

from documents.validators import (
    validate_file_upload,
    validate_file_bytes,
    _detect_mime_from_bytes,
)


class ValidatorsTest(TestCase):
    def test_validate_file_upload_ok(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        file = SimpleUploadedFile("test.pdf", b"%PDF-1.4 content", content_type="application/pdf")
        result = validate_file_upload(file)
        self.assertEqual(result.mime_type, "application/pdf")

    def test_validate_file_upload_invalid_mime(self):
        from django.core.exceptions import ValidationError
        from django.core.files.uploadedfile import SimpleUploadedFile

        file = SimpleUploadedFile(
            "test.exe", b"binary content", content_type="application/x-msdownload"
        )
        with self.assertRaises(ValidationError):
            validate_file_upload(file)

    def test_validate_file_upload_size_limit(self):
        from django.core.exceptions import ValidationError
        from django.core.files.uploadedfile import SimpleUploadedFile

        file = SimpleUploadedFile(
            "large.pdf", b"x" * (51 * 1024 * 1024), content_type="application/pdf"
        )
        with self.assertRaises(ValidationError):
            validate_file_upload(file)

    def test_detect_mime_from_bytes_pdf(self):
        mime = _detect_mime_from_bytes(b"%PDF-1.4 content")
        self.assertEqual(mime, "application/pdf")

    def test_detect_mime_from_bytes_zip(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("test.txt", "content")
        buf.seek(0)
        mime = _detect_mime_from_bytes(buf.read())
        self.assertEqual(mime, "application/zip")

    def test_detect_mime_from_bytes_jpeg(self):
        mime = _detect_mime_from_bytes(b"\xff\xd8\xff\xe0")
        self.assertEqual(mime, "image/jpeg")

    def test_detect_mime_from_bytes_png(self):
        mime = _detect_mime_from_bytes(b"\x89PNG\r\n\x1a\n")
        self.assertEqual(mime, "image/png")

    def test_detect_mime_from_bytes_gif(self):
        mime = _detect_mime_from_bytes(b"GIF89a")
        self.assertEqual(mime, "image/gif")

    def test_detect_mime_from_bytes_webp(self):
        mime = _detect_mime_from_bytes(b"RIFF\x00\x00\x00\x00WEBP")
        self.assertIsNotNone(mime)

    def test_detect_mime_from_bytes_tiff(self):
        mime = _detect_mime_from_bytes(b"II*\x00")
        self.assertEqual(mime, "image/tiff")

    def test_detect_mime_from_bytes_unknown(self):
        mime = _detect_mime_from_bytes(b"\x00\x01\x02\x03")
        self.assertIsNone(mime)

    def test_validate_file_bytes(self):
        result = validate_file_bytes(b"%PDF-1.4", 100)
        self.assertEqual(result.mime_type, "application/pdf")

    def test_validate_file_bytes_too_large(self):
        from django.core.exceptions import ValidationError

        with self.assertRaises(ValidationError):
            validate_file_bytes(b"test", 51 * 1024 * 1024)

    def test_validate_file_bytes_empty(self):
        from django.core.exceptions import ValidationError

        with self.assertRaises(ValidationError):
            validate_file_bytes(b"", 0)

    def test_validate_file_bytes_unknown_type(self):
        from django.core.exceptions import ValidationError

        with self.assertRaises(ValidationError):
            validate_file_bytes(b"\x00\x01\x02\x03", 100)
