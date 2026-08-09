"""Tests for merge views: ManualMergeView, UndoMergeView, MergeListView.

Covers merge initiation, undo, access control, and HTMX responses.
"""

import json
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from records.models import AuditLog, Folder, MergeLog, Record

User = get_user_model()


@override_settings(
    CACHES={"default": {"BACKEND": "django.core.cache.backends.dummy.DummyCache"}},
    SESSION_ENGINE="django.contrib.sessions.backends.db",
)
class ManualMergeViewTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="merger", password="pass")
        self.other = User.objects.create_user(username="other", password="pass")
        self.plaid = Record.objects.create(
            user=self.user,
            title="Plaid Record",
            record_type="expense_receipt",
            merchant="Amazon",
            balance=Decimal("50.00"),
            transaction_date="2024-06-15",
            plaid_transaction_id="txn_123",
        )
        self.doc = Record.objects.create(
            user=self.user,
            title="Doc Record",
            record_type="expense_receipt",
            merchant="Amazon",
            balance=Decimal("50.00"),
            transaction_date="2024-06-15",
        )
        self.url = reverse("records:manual_merge")

    def test_login_required(self):
        response = self.client.get(self.url)
        self.assertIn(response.status_code, [302, 300])

    def test_get_returns_form(self):
        self.client.force_login(self.user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "records/manual_merge.html")

    def test_post_merges_records(self):
        self.client.force_login(self.user)
        response = self.client.post(
            self.url,
            {
                "plaid_record_id": self.plaid.id,
                "document_record_id": self.doc.id,
            },
        )
        self.assertIn(response.status_code, [200, 302])
        self.plaid.refresh_from_db()
        self.doc.refresh_from_db()
        self.assertFalse(self.doc.is_active)


@override_settings(
    CACHES={"default": {"BACKEND": "django.core.cache.backends.dummy.DummyCache"}},
    SESSION_ENGINE="django.contrib.sessions.backends.db",
)
class UndoMergeViewTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="undoer", password="pass")
        self.other = User.objects.create_user(username="other_undo", password="pass")
        self.plaid = Record.objects.create(
            user=self.user,
            title="Plaid",
            record_type="expense_receipt",
            transaction_date="2024-06-15",
            plaid_transaction_id="txn_123",
        )
        self.doc = Record.objects.create(
            user=self.user,
            title="Doc",
            record_type="expense_receipt",
            transaction_date="2024-06-15",
        )
        self.merge_log = MergeLog.objects.create(
            plaid_record=self.plaid,
            document_record=self.doc,
            plaid_snapshot={"title": "Plaid"},
            document_snapshot={"title": "Doc"},
        )
        self.url = reverse("records:undo_merge", args=[self.merge_log.id])

    def test_owner_can_undo(self):
        self.client.force_login(self.user)
        response = self.client.post(self.url)
        self.assertIn(response.status_code, [200, 302])
        self.merge_log.refresh_from_db()
        self.assertIsNotNone(self.merge_log.undone_at)

    def test_other_user_cannot_undo(self):
        self.client.force_login(self.other)
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 404)
        self.merge_log.refresh_from_db()
        self.assertIsNone(self.merge_log.undone_at)


@override_settings(
    CACHES={"default": {"BACKEND": "django.core.cache.backends.dummy.DummyCache"}},
    SESSION_ENGINE="django.contrib.sessions.backends.db",
)
class MergedRecordsFilterTest(TestCase):
    """The merged view is a filter on the main records list, not a separate page."""

    def setUp(self):
        self.user = User.objects.create_user(username="listmerges", password="pass")
        self.url = reverse("records:view_all_records")

    def test_merged_filter_shows_only_merged_plaid_records(self):
        self.client.force_login(self.user)
        plaid = Record.objects.create(
            user=self.user,
            title="Merged Plaid",
            record_type="expense_receipt",
            transaction_date="2024-06-15",
            plaid_transaction_id="t1",
        )
        doc = Record.objects.create(
            user=self.user,
            title="Receipt",
            record_type="expense_receipt",
            transaction_date="2024-06-15",
        )
        MergeLog.objects.create(
            plaid_record=plaid,
            document_record=doc,
            plaid_snapshot={"title": "Merged Plaid"},
            document_snapshot={"title": "Receipt"},
        )
        response = self.client.get(self.url, {"merged": "True"})
        self.assertEqual(len(response.context["records"]), 1)
        self.assertEqual(response.context["records"][0].pk, plaid.pk)

    def test_merged_filter_excludes_unmerged_and_undone(self):
        self.client.force_login(self.user)
        plaid = Record.objects.create(
            user=self.user,
            title="Merged Plaid",
            record_type="expense_receipt",
            transaction_date="2024-06-15",
            plaid_transaction_id="t2",
        )
        doc = Record.objects.create(
            user=self.user,
            title="Receipt",
            record_type="expense_receipt",
            transaction_date="2024-06-15",
        )
        merge_log = MergeLog.objects.create(
            plaid_record=plaid,
            document_record=doc,
            plaid_snapshot={"title": "Merged Plaid"},
            document_snapshot={"title": "Receipt"},
        )
        merge_log.undone_at = "2024-06-16T00:00:00Z"
        merge_log.save()
        Record.objects.create(
            user=self.user,
            title="Unmerged",
            record_type="expense_receipt",
            transaction_date="2024-06-15",
        )
        response = self.client.get(self.url, {"merged": "True"})
        self.assertEqual(len(response.context["records"]), 0)

    def test_merged_metric_count(self):
        self.client.force_login(self.user)
        plaid = Record.objects.create(
            user=self.user,
            title="Merged Plaid",
            record_type="expense_receipt",
            transaction_date="2024-06-15",
            plaid_transaction_id="t3",
        )
        doc = Record.objects.create(
            user=self.user,
            title="Receipt",
            record_type="expense_receipt",
            transaction_date="2024-06-15",
        )
        MergeLog.objects.create(
            plaid_record=plaid,
            document_record=doc,
            plaid_snapshot={"title": "Merged Plaid"},
            document_snapshot={"title": "Receipt"},
        )
        response = self.client.get(self.url)
        metrics = response.context["metrics"]
        merged_metric = next(m for m in metrics if m["label"] == "Merged")
        self.assertEqual(merged_metric["value"], 1)
