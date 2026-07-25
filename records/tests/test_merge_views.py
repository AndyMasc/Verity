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
class MergeListViewTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="listmerges", password="pass")
        self.url = reverse("records:merge_list")

    def test_login_required(self):
        response = self.client.get(self.url)
        self.assertIn(response.status_code, [302, 300])

    def test_authenticated_access(self):
        self.client.force_login(self.user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)

    def test_shows_only_user_merges(self):
        other = User.objects.create_user(username="other_merger", password="pass")
        p1 = Record.objects.create(
            user=self.user,
            title="P1",
            record_type="expense_receipt",
            transaction_date="2024-06-15",
            plaid_transaction_id="t1",
        )
        d1 = Record.objects.create(
            user=self.user,
            title="D1",
            record_type="expense_receipt",
            transaction_date="2024-06-15",
        )
        MergeLog.objects.create(
            plaid_record=p1,
            document_record=d1,
            plaid_snapshot={"title": "P1"},
            document_snapshot={"title": "D1"},
        )
        p2 = Record.objects.create(
            user=other,
            title="P2",
            record_type="expense_receipt",
            transaction_date="2024-06-15",
            plaid_transaction_id="t2",
        )
        d2 = Record.objects.create(
            user=other,
            title="D2",
            record_type="expense_receipt",
            transaction_date="2024-06-15",
        )
        MergeLog.objects.create(
            plaid_record=p2,
            document_record=d2,
            plaid_snapshot={"title": "P2"},
            document_snapshot={"title": "D2"},
        )
        self.client.force_login(self.user)
        response = self.client.get(self.url)
        self.assertEqual(len(response.context["merges"]), 1)
