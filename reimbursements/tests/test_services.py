"""Tests for reimbursements services: package creation and record access."""

from django.test import TestCase

from records.models import Record, RecordShare

from reimbursements import services

from ._helpers import _record, _user


class ReimbursementRecordAccessTest(TestCase):
    """Purpose-bound, temporary record access granted to package recipients."""

    def setUp(self):
        self.creator = _user("andy@test.com")
        self.recipient = _user("sarah@test.com")
        self.other_record = _record(self.creator)

    def _package_with_records(self):
        r1 = _record(self.creator)
        r2 = _record(self.creator)
        pkg, _ = services.create_reimbursement_package(
            creator=self.creator,
            recipient_email=self.recipient.email,
            record_ids=[r1.pk, r2.pk],
            title="Lunch receipts",
            days_valid=7,
        )
        return pkg, r1, r2

    def test_create_grants_temporary_view_access(self):
        pkg, r1, r2 = self._package_with_records()
        for r in (r1, r2):
            share = RecordShare.objects.get(record=r, user=self.recipient)
            self.assertEqual(share.permission, RecordShare.Permission.VIEW)
            self.assertEqual(share.purpose, RecordShare.Purpose.REIMBURSEMENT)
            self.assertTrue(share.include_documents)
            self.assertEqual(share.expires_at, pkg.expires_at)
            self.assertIsNone(share.revoked_at)

    def test_create_only_grants_packaged_records(self):
        _, r1, r2 = self._package_with_records()
        visible = set(Record.objects.visible_to(self.recipient).values_list("pk", flat=True))
        self.assertIn(r1.pk, visible)
        self.assertIn(r2.pk, visible)
        self.assertNotIn(self.other_record.pk, visible)
        self.assertFalse(
            RecordShare.objects.filter(record=self.other_record, user=self.recipient).exists()
        )

    def test_mark_as_paid_revokes_access(self):
        pkg, r1, _ = self._package_with_records()
        pkg.mark_as_paid(self.recipient)
        share = RecordShare.objects.get(record=r1, user=self.recipient)
        self.assertIsNotNone(share.revoked_at)
        self.assertFalse(share.is_active)
        self.assertNotIn(
            r1.pk, Record.objects.visible_to(self.recipient).values_list("pk", flat=True)
        )

    def test_refund_restores_access(self):
        pkg, r1, _ = self._package_with_records()
        pkg.mark_as_paid(self.recipient)
        pkg.mark_as_refunded()
        share = RecordShare.objects.get(record=r1, user=self.recipient)
        self.assertIsNone(share.revoked_at)
        self.assertTrue(share.is_active)
        self.assertIn(r1.pk, Record.objects.visible_to(self.recipient).values_list("pk", flat=True))

    def test_deleted_package_revokes_access(self):
        pkg, r1, _ = self._package_with_records()
        pkg.delete_package(self.creator)
        share = RecordShare.objects.get(record=r1, user=self.recipient)
        self.assertIsNotNone(share.revoked_at)
