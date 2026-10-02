"""Adversarial probe: can one user reach or mutate another user's money objects?"""

import datetime
import json

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from reimbursements.models import ReimbursementPackage, StripeAccount
from records.models import Record, RecordShare

U = get_user_model()


class IDORProbe(TestCase):
    def setUp(self):
        self.victim = U.objects.create_user(username="victim", password="p")
        self.attacker = U.objects.create_user(username="attacker", password="p")
        self.rec = Record.objects.create(
            user=self.victim,
            title="Victim receipt",
            record_type="expense_receipt",
            transaction_date=datetime.date(2024, 6, 15),
            balance="500.00",
        )
        self.pkg = ReimbursementPackage.objects.create(
            creator=self.victim, title="Victim package", currency="usd", status="open"
        )
        self.pkg.records.add(self.rec)
        StripeAccount.objects.filter(user=self.victim).update(
            stripe_account_id="acct_victim",
            stripe_details_submitted=True,
            charges_enabled=True,
            payouts_enabled=True,
        )
        self.client.force_login(self.attacker)

    def test_cannot_hard_delete_another_users_old_record(self):
        Record.objects.filter(pk=self.rec.pk).update(
            date_added=timezone.now().date() - datetime.timedelta(days=365 * 8)
        )
        self.client.post(
            reverse("records:bulk_hard_delete"),
            data=json.dumps({"record_ids": [self.rec.pk]}),
            content_type="application/json",
        )
        self.assertTrue(
            Record.objects.filter(pk=self.rec.pk).exists(),
            "IDOR: deleted another user's record",
        )

    def test_cannot_view_another_users_record_detail(self):
        self.assertEqual(
            self.client.get(
                reverse("records:record_detail", args=[self.rec.pk])
            ).status_code,
            404,
        )

    def test_cannot_bulk_share_another_users_records(self):
        self.client.post(
            reverse("records:bulk_share"),
            data=json.dumps(
                {"record_ids": [self.rec.pk], "emails": self.attacker.email}
            ),
            content_type="application/json",
        )
        self.assertFalse(
            RecordShare.objects.filter(record=self.rec).exists(),
            "IDOR: shared another's record",
        )

    def test_cannot_bulk_archive_another_users_records(self):
        self.client.post(
            reverse("records:bulk_archive"),
            data=json.dumps({"record_ids": [self.rec.pk]}),
            content_type="application/json",
        )
        self.rec.refresh_from_db()
        self.assertTrue(self.rec.is_active, "IDOR: archived another user's record")

    def test_cannot_revoke_another_users_share(self):
        share = RecordShare.objects.create(record=self.rec, user=self.attacker)
        self.client.post(
            reverse("records:record_share_revoke", args=[self.rec.pk, share.pk])
        )
        share.refresh_from_db()
        self.assertIsNone(share.revoked_at, "IDOR: revoked another user's share")

    def test_money_endpoints_require_login(self):
        self.client.logout()
        for name, args in [
            ("records:bulk_archive", ()),
            ("records:bulk_hard_delete", ()),
            ("records:bulk_share", ()),
            ("records:bulk_unarchive", ()),
        ]:
            r = self.client.post(
                reverse(name, args=args), data="{}", content_type="application/json"
            )
            self.assertIn(
                r.status_code, (302, 401, 403), f"{name} reachable anonymously"
            )
