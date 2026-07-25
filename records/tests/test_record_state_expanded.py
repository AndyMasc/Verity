"""Expanded tests for record_state views.

Covers BulkUnarchiveView, DeleteRecordView, and additional edge cases.
"""

import json
from datetime import date

import pytest
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from records.models import AuditLog, Record

User = get_user_model()


@override_settings(
    CACHES={"default": {"BACKEND": "django.core.cache.backends.dummy.DummyCache"}},
    SESSION_ENGINE="django.contrib.sessions.backends.db",
)
class BulkUnarchiveViewTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="bulkunarch", password="pass")
        self.url = reverse("records:bulk_unarchive")
        self.records = [
            Record.objects.create(
                user=self.user,
                title=f"Unarch {i}",
                record_type="expense_receipt",
                is_active=False,
                transaction_date=date(2024, 6, 15),
            )
            for i in range(3)
        ]

    def test_login_required(self):
        response = self.client.post(
            self.url,
            data='{"record_ids": []}',
            content_type="application/json",
        )
        self.assertIn(response.status_code, [302, 300])

    def test_bulk_unarchive_restores(self):
        self.client.force_login(self.user)
        ids = [r.pk for r in self.records[:2]]
        response = self.client.post(
            self.url,
            data=json.dumps({"record_ids": ids}),
            content_type="application/json",
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(response.status_code, 200)
        for r in Record.objects.filter(pk__in=ids):
            self.assertTrue(r.is_active)

    def test_bulk_unarchive_creates_audit_logs(self):
        self.client.force_login(self.user)
        ids = [r.pk for r in self.records[:2]]
        self.client.post(
            self.url,
            data=json.dumps({"record_ids": ids}),
            content_type="application/json",
        )
        audit_count = AuditLog.objects.filter(
            user=self.user,
            action=AuditLog.Action.UNARCHIVE,
        ).count()
        self.assertEqual(audit_count, 2)


@override_settings(
    CACHES={"default": {"BACKEND": "django.core.cache.backends.dummy.DummyCache"}},
    SESSION_ENGINE="django.contrib.sessions.backends.db",
)
class DeleteRecordViewHTTPTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="delrec", password="pass")
        self.record = Record.objects.create(
            user=self.user,
            title="To Delete",
            record_type="expense_receipt",
            transaction_date=date(2024, 6, 15),
        )
        self.url = reverse("records:delete_record", args=[self.record.id])

    def test_login_required(self):
        response = self.client.post(self.url)
        self.assertIn(response.status_code, [302, 300])

    def test_owner_can_delete(self):
        self.client.force_login(self.user)
        response = self.client.post(self.url)
        self.assertIn(response.status_code, [200, 302])
        self.record.refresh_from_db()
        self.assertFalse(self.record.is_active)

    def test_creates_soft_delete_audit_log(self):
        self.client.force_login(self.user)
        self.client.post(self.url)
        audit = AuditLog.objects.filter(
            user=self.user,
            action=AuditLog.Action.SOFT_DELETE,
        ).first()
        self.assertIsNotNone(audit)
        self.assertEqual(audit.record_id, self.record.pk)

    def test_other_user_cannot_delete(self):
        other = User.objects.create_user(username="otherdelrec", password="pass")
        self.client.force_login(other)
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 404)
        self.assertTrue(Record.objects.filter(id=self.record.id).exists())

    def test_get_not_allowed(self):
        self.client.force_login(self.user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 405)
