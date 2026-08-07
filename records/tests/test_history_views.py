"""Tests for the unified history timeline view.

Covers RecordHistoryView queryset building, merge entries,
pagination, and access control.
"""

import pytest
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from records.models import MergeLog, Record

User = get_user_model()


@override_settings(
    CACHES={"default": {"BACKEND": "django.core.cache.backends.dummy.DummyCache"}},
    SESSION_ENGINE="django.contrib.sessions.backends.db",
)
class RecordHistoryViewTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="historyuser", password="pass")
        self.record = Record.objects.create(
            user=self.user,
            title="Test Record",
            record_type="expense_receipt",
            transaction_date="2024-06-15",
        )
        self.url = reverse("records:record_history", args=[self.record.id])

    def test_login_required(self):
        response = self.client.get(self.url)
        self.assertIn(response.status_code, [302, 300])

    def test_owner_can_view(self):
        self.client.force_login(self.user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "records/record_history.html")

    def test_other_user_cannot_view(self):
        other = User.objects.create_user(username="other_hist", password="pass")
        self.client.force_login(other)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 404)

    def test_includes_merge_entries(self):
        other_record = Record.objects.create(
            user=self.user,
            title="Other Record",
            record_type="expense_receipt",
            transaction_date="2024-06-15",
        )
        MergeLog.objects.create(
            plaid_record=self.record,
            document_record=other_record,
            plaid_snapshot={"title": "Test"},
            document_snapshot={"title": "Other"},
        )
        self.client.force_login(self.user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)

    def test_merge_snapshot_without_currency_renders_amount(self):
        other_record = Record.objects.create(
            user=self.user,
            title="Other Record",
            record_type="expense_receipt",
            transaction_date="2024-06-15",
        )
        MergeLog.objects.create(
            plaid_record=self.record,
            document_record=other_record,
            plaid_snapshot={
                "title": "Test",
                "merchant": "Bank",
                "balance": "42.00",
                "payment_method": "Card",
            },
            document_snapshot={"title": "Other", "balance": "7.00"},
        )
        self.client.force_login(self.user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "$")

    def test_merge_snapshot_without_currency_renders_amount(self):
        other_record = Record.objects.create(
            user=self.user,
            title="Other Record",
            record_type="expense_receipt",
            transaction_date="2024-06-15",
        )
        MergeLog.objects.create(
            plaid_record=self.record,
            document_record=other_record,
            plaid_snapshot={
                "title": "Test",
                "merchant": "Bank",
                "balance": "42.00",
                "payment_method": "Card",
            },
            document_snapshot={"title": "Other", "balance": "7.00"},
        )
        self.client.force_login(self.user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "$")

    def test_context_has_tracked_fields(self):
        self.client.force_login(self.user)
        response = self.client.get(self.url)
        self.assertIn("tracked_fields", response.context)
        self.assertIn("doc_tracked_fields", response.context)

    def test_nonexistent_record_returns_404(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("records:record_history", args=[99999]))
        self.assertEqual(response.status_code, 404)
