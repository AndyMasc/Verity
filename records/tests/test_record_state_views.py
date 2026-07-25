import json
from datetime import date, timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from records.models import Record, AuditLog


class ArchiveRecordViewTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="archuser", password="pass")
        self.record = Record.objects.create(
            user=self.user,
            title="To Archive",
            record_type="expense_receipt",
            transaction_date=date(2024, 6, 15),
        )
        self.url = reverse("records:archive_record", args=[self.record.id])

    def test_login_required(self):
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 302)

    def test_owner_can_archive(self):
        self.client.force_login(self.user)
        response = self.client.post(self.url)
        self.assertIn(response.status_code, [200, 302])
        self.record.refresh_from_db()
        self.assertFalse(self.record.is_active)

    def test_other_user_cannot_archive(self):
        user2 = User.objects.create_user(username="otherarch", password="pass")
        self.client.force_login(user2)
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 404)

    def test_get_not_allowed(self):
        self.client.force_login(self.user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 405)


class UnarchiveRecordViewTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="unarchuser", password="pass")
        self.record = Record.objects.create(
            user=self.user,
            title="To Unarchive",
            record_type="expense_receipt",
            is_active=False,
            transaction_date=date(2024, 6, 15),
        )
        self.url = reverse("records:unarchive_record", args=[self.record.id])

    def test_owner_can_unarchive(self):
        self.client.force_login(self.user)
        response = self.client.post(self.url)
        self.assertIn(response.status_code, [200, 302])
        self.record.refresh_from_db()
        self.assertTrue(self.record.is_active)

    def test_other_user_cannot_unarchive(self):
        user2 = User.objects.create_user(username="otherunarch", password="pass")
        self.client.force_login(user2)
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 404)
        self.record.refresh_from_db()
        self.assertFalse(self.record.is_active)

    def test_get_not_allowed(self):
        self.client.force_login(self.user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 405)


class DeleteRecordViewTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="deluser", password="pass")
        self.record = Record.objects.create(
            user=self.user,
            title="To Delete",
            record_type="expense_receipt",
            transaction_date=date(2024, 6, 15),
        )
        self.url = reverse("records:delete_record", args=[self.record.id])

    def test_owner_can_delete(self):
        self.client.force_login(self.user)
        response = self.client.post(self.url)
        self.assertIn(response.status_code, [200, 302])
        self.record.refresh_from_db()
        self.assertFalse(self.record.is_active)

    def test_other_user_cannot_delete(self):
        user2 = User.objects.create_user(username="otherdel", password="pass")
        self.client.force_login(user2)
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 404)
        self.assertTrue(Record.objects.filter(id=self.record.id).exists())


class HardDeleteViewHTTPTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="harddel_http", password="pass")
        self.url_name = "records:hard_delete_record"

    def _make_old_record(self):
        old_date = timezone.now().date() - timedelta(days=365 * 8)
        record = Record.objects.create(
            user=self.user,
            title="Old Record",
            record_type="expense_receipt",
            transaction_date=date(2015, 6, 15),
        )
        Record.objects.filter(pk=record.pk).update(date_added=old_date)
        record.refresh_from_db()
        return record

    def _make_young_record(self):
        return Record.objects.create(
            user=self.user,
            title="Young Record",
            record_type="expense_receipt",
            transaction_date=date(2024, 6, 15),
        )

    def test_too_young_htmx_returns_409(self):
        record = self._make_young_record()
        self.client.force_login(self.user)
        url = reverse(self.url_name, args=[record.pk])
        response = self.client.post(url, HTTP_HX_REQUEST="true")
        self.assertEqual(response.status_code, 409)
        import json

        trigger = json.loads(response["HX-Trigger"])
        self.assertEqual(trigger["showToast"]["tags"], "error")

    def test_too_young_non_htmx_redirects(self):
        record = self._make_young_record()
        self.client.force_login(self.user)
        url = reverse(self.url_name, args=[record.pk])
        response = self.client.post(url)
        self.assertEqual(response.status_code, 302)
        self.assertIn("record_detail", response.url)

    def test_old_record_htmx_returns_204(self):
        record = self._make_old_record()
        self.client.force_login(self.user)
        url = reverse(self.url_name, args=[record.pk])
        response = self.client.post(url, HTTP_HX_REQUEST="true")
        self.assertEqual(response.status_code, 204)
        self.assertFalse(Record.objects.filter(pk=record.pk).exists())

    def test_old_record_creates_audit_log(self):
        record = self._make_old_record()
        self.client.force_login(self.user)
        url = reverse(self.url_name, args=[record.pk])
        self.client.post(url)
        audit = AuditLog.objects.filter(
            user=self.user,
            action=AuditLog.Action.HARD_DELETE,
        ).first()
        self.assertIsNotNone(audit)
        self.assertEqual(audit.details.get("title"), "Old Record")

    def test_other_user_cannot_delete(self):
        record = self._make_old_record()
        other = User.objects.create_user(username="other_hd", password="pass")
        self.client.force_login(other)
        url = reverse(self.url_name, args=[record.pk])
        response = self.client.post(url)
        self.assertEqual(response.status_code, 404)
        self.assertTrue(Record.objects.filter(pk=record.pk).exists())


class ArchiveViewHTTPTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="archive_http", password="pass")

    def test_archive_deactivates_record(self):
        record = Record.objects.create(
            user=self.user,
            title="To Archive",
            record_type="expense_receipt",
            transaction_date=date(2024, 6, 15),
        )
        self.client.force_login(self.user)
        url = reverse("records:archive_record", args=[record.pk])
        response = self.client.post(url)
        self.assertEqual(response.status_code, 302)
        record.refresh_from_db()
        self.assertFalse(record.is_active)

    def test_archive_creates_audit_log(self):
        record = Record.objects.create(
            user=self.user,
            title="Audit Archive",
            record_type="expense_receipt",
            transaction_date=date(2024, 6, 15),
        )
        self.client.force_login(self.user)
        url = reverse("records:archive_record", args=[record.pk])
        self.client.post(url)
        audit = AuditLog.objects.filter(
            user=self.user,
            action=AuditLog.Action.ARCHIVE,
        ).first()
        self.assertIsNotNone(audit)
        self.assertEqual(audit.record_id, record.pk)

    def test_unarchive_reactivates_record(self):
        record = Record.objects.create(
            user=self.user,
            title="To Unarchive",
            record_type="expense_receipt",
            transaction_date=date(2024, 6, 15),
            is_active=False,
        )
        self.client.force_login(self.user)
        url = reverse("records:unarchive_record", args=[record.pk])
        response = self.client.post(url)
        self.assertEqual(response.status_code, 302)
        record.refresh_from_db()
        self.assertTrue(record.is_active)

    def test_unarchive_creates_audit_log(self):
        record = Record.objects.create(
            user=self.user,
            title="Audit Unarchive",
            record_type="expense_receipt",
            transaction_date=date(2024, 6, 15),
            is_active=False,
        )
        self.client.force_login(self.user)
        url = reverse("records:unarchive_record", args=[record.pk])
        self.client.post(url)
        audit = AuditLog.objects.filter(
            user=self.user,
            action=AuditLog.Action.UNARCHIVE,
        ).first()
        self.assertIsNotNone(audit)
        self.assertEqual(audit.record_id, record.pk)


class BulkArchiveViewTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="bulkarch", password="pass")
        self.url = reverse("records:bulk_archive")
        self.records = [
            Record.objects.create(
                user=self.user,
                title=f"Bulk {i}",
                record_type="expense_receipt",
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
        self.assertEqual(response.status_code, 302)

    def test_post_not_allowed_without_json(self):
        self.client.force_login(self.user)
        response = self.client.post(self.url, data="not json", content_type="text/plain")
        self.assertEqual(response.status_code, 400)

    def test_bulk_archive_archives_records(self):
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
            self.assertFalse(r.is_active)
        self.assertTrue(Record.objects.filter(pk=self.records[2].pk, is_active=True).exists())

    def test_bulk_archive_creates_audit_logs(self):
        self.client.force_login(self.user)
        ids = [r.pk for r in self.records[:2]]
        self.client.post(
            self.url,
            data=json.dumps({"record_ids": ids}),
            content_type="application/json",
        )
        audit_count = AuditLog.objects.filter(
            user=self.user,
            action=AuditLog.Action.ARCHIVE,
        ).count()
        self.assertEqual(audit_count, 2)

    def test_bulk_archive_skips_already_archived(self):
        self.records[0].is_active = False
        self.records[0].save(update_fields=["is_active"])
        self.client.force_login(self.user)
        ids = [r.pk for r in self.records]
        response = self.client.post(
            self.url,
            data=json.dumps({"record_ids": ids}),
            content_type="application/json",
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(response.status_code, 200)
        audit_count = AuditLog.objects.filter(
            user=self.user,
            action=AuditLog.Action.ARCHIVE,
        ).count()
        self.assertEqual(audit_count, 2)

    def test_bulk_archive_ignores_other_users_records(self):
        other = User.objects.create_user(username="bulkother", password="pass")
        other_record = Record.objects.create(
            user=other,
            title="Not mine",
            record_type="expense_receipt",
            transaction_date=date(2024, 6, 15),
        )
        self.client.force_login(self.user)
        response = self.client.post(
            self.url,
            data=json.dumps({"record_ids": [other_record.pk]}),
            content_type="application/json",
        )
        other_record.refresh_from_db()
        self.assertTrue(other_record.is_active)

    def test_bulk_archive_htmx_returns_trigger_header(self):
        self.client.force_login(self.user)
        response = self.client.post(
            self.url,
            data=json.dumps({"record_ids": [self.records[0].pk]}),
            content_type="application/json",
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(response.status_code, 200)
        trigger = json.loads(response["HX-Trigger"])
        self.assertIn("recordChanged", trigger)
        self.assertIn("showToast", trigger)

    def test_bulk_archive_invalid_json(self):
        self.client.force_login(self.user)
        response = self.client.post(
            self.url,
            data="not json",
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)

    def test_bulk_archive_empty_list(self):
        self.client.force_login(self.user)
        response = self.client.post(
            self.url,
            data=json.dumps({"record_ids": []}),
            content_type="application/json",
            HTTP_HX_REQUEST="true",
        )
        self.assertEqual(response.status_code, 200)
        trigger = json.loads(response["HX-Trigger"])
        self.assertEqual(trigger["showToast"]["message"], "0 records archived.")

    def test_get_not_allowed(self):
        self.client.force_login(self.user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 405)
