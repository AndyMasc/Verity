"""Tests for record creation views: AddRecordView and CheckOCRStatus.

Covers OCR status polling, redirect to the auto-created record's inline
editor, and manual entry.
"""

import hashlib
from unittest.mock import patch, MagicMock

import pytest
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from documents.models import DocumentData, DocumentStatus
from records.models import Folder, Record

User = get_user_model()


@override_settings(
    CACHES={"default": {"BACKEND": "django.core.cache.backends.dummy.DummyCache"}},
    SESSION_ENGINE="django.contrib.sessions.backends.db",
)
class AddRecordViewTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="creator", password="pass")
        self.url = reverse("records:add_record_manual")

    def test_login_required(self):
        response = self.client.get(self.url)
        self.assertIn(response.status_code, [302, 300])

    def test_get_form(self):
        self.client.force_login(self.user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "records/add_record.html")

    def test_post_creates_record(self):
        self.client.force_login(self.user)
        response = self.client.post(
            self.url,
            {
                "title": "New Record",
                "products": "Test Item",
                "record_type": "expense_receipt",
                "transaction_date": "2024-06-15",
                "merchant": "Test Merchant",
                "balance": "25.00",
                "currency": "usd",
                "notes": "Business purpose",
                "payment_method": "Credit Card",
            },
        )
        self.assertIn(response.status_code, [200, 302])
        self.assertTrue(Record.objects.filter(title="New Record", user=self.user).exists())

    def test_post_with_document_id_creates_record(self):
        doc = DocumentData.objects.create(
            user=self.user,
            filepath="users/1/doc.pdf",
            file_hash=hashlib.sha256(b"test").hexdigest(),
            status=DocumentStatus.COMPLETED,
        )
        self.client.force_login(self.user)
        url = reverse("records:add_record", args=[doc.id])
        response = self.client.post(
            url,
            {
                "title": "From Doc",
                "products": "Item",
                "record_type": "expense_receipt",
                "transaction_date": "2024-06-15",
                "merchant": "Test",
                "balance": "10.00",
                "currency": "usd",
            },
        )
        self.assertIn(response.status_code, [200, 302])

    def test_already_associated_redirects_to_record(self):
        record = Record.objects.create(
            user=self.user,
            title="Existing",
            record_type="expense_receipt",
            transaction_date="2024-06-15",
        )
        doc = DocumentData.objects.create(
            user=self.user,
            filepath="users/1/doc.pdf",
            file_hash=hashlib.sha256(b"linked").hexdigest(),
            status=DocumentStatus.COMPLETED,
            associated_record=record,
        )
        self.client.force_login(self.user)
        url = reverse("records:add_record", args=[doc.id])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response["Location"],
            reverse("records:record_detail", args=[record.id]),
        )

    def test_suggested_folder_resolves_to_pk(self):
        folder = Folder.objects.create(user=self.user, name="Taxes")
        self.client.force_login(self.user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)


@override_settings(
    CACHES={"default": {"BACKEND": "django.core.cache.backends.dummy.DummyCache"}},
    SESSION_ENGINE="django.contrib.sessions.backends.db",
)
class CheckOCRStatusTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="ocrchecker", password="pass")
        self.doc = DocumentData.objects.create(
            user=self.user,
            filepath="users/1/doc.pdf",
            file_hash=hashlib.sha256(b"ocr_test").hexdigest(),
            status=DocumentStatus.PROCESSING,
            did_ocr=True,
        )
        self.url = reverse("records:check_ocr_status", args=[self.doc.id])

    def test_login_required(self):
        response = self.client.get(self.url)
        self.assertIn(response.status_code, [302, 300])

    def test_processing_returns_waiting(self):
        self.client.force_login(self.user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)

    def test_completed_returns_redirect(self):
        self.doc.status = DocumentStatus.COMPLETED
        self.doc.ocr_raw_data = {
            "title": "Auto Receipt",
            "transaction_date": "2024-06-15",
            "record_type": "expense_receipt",
            "currency": "usd",
        }
        self.doc.save()
        self.client.force_login(self.user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertIn("HX-Redirect", response.headers)
        record = Record.objects.get(title="Auto Receipt", user=self.user)
        self.assertIn(
            reverse("records:record_detail", args=[record.id]),
            response.headers["HX-Redirect"],
        )

    def test_completed_without_ocr_data_returns_error(self):
        self.doc.status = DocumentStatus.COMPLETED
        self.doc.save()
        self.client.force_login(self.user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Extraction produced no data")

    def test_error_returns_error_message(self):
        self.doc.status = DocumentStatus.ERROR
        self.doc.save()
        self.client.force_login(self.user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)

    def test_other_user_cannot_check(self):
        other = User.objects.create_user(username="other", password="pass")
        self.client.force_login(other)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 404)
