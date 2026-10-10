"""End-to-end checks for the three revenue-critical flows.

These assert business invariants rather than implementation details, so they
keep protecting the app across refactors:

1. 7-year record retention, with admin hard-delete available at any time.
2. Plaid transaction sync triggered by an incoming webhook.
3. Reimbursement money movement to the creator, less the platform fee.
"""

import base64
import datetime
import hashlib
import json
from datetime import UTC, timedelta
from unittest.mock import patch

import jwt as pyjwt
import plaid
import requests
from cryptography.hazmat.primitives.asymmetric import ec
from django.core.cache import cache
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from billing.models import CustomUser
from documents.models import DocumentData
from plaid_integration.models import PlaidItem
from records.models import Record
from reimbursements.models import PackagePayment, ReimbursementPackage

User = get_user_model()

SEVEN_YEARS = datetime.timedelta(days=365 * 7)

# PlatformFeeCalculator on $100.00 USD: Stripe ~3.2% + $0.30 = $3.50,
# plus the platform's 1% net = $4.50 fee. The creator is transferred the rest.
EXPECTED_FEE_CENTS = 450
EXPECTED_CREATOR_CENTS = 9_550


def make_user(username):
    return User.objects.create_user(username=username, email=f"{username}@e.com")


def make_record(user, **overrides):
    defaults = {
        "user": user,
        "title": "A receipt",
        "record_type": "expense_receipt",
        "transaction_date": datetime.date.today(),
        "balance": "10.00",
        "currency": "usd",
    }
    return Record.objects.create(**{**defaults, **overrides})


def aged_record(user, title, days_old, is_active=True):
    """Create a record back-dated past days_old (date_added is auto_now_add)."""
    record = make_record(user, title=title, is_active=is_active)
    Record.objects.filter(pk=record.pk).update(
        date_added=timezone.now().date() - datetime.timedelta(days=days_old)
    )
    record.refresh_from_db()
    return record


def _b64(n: int) -> str:
    """base64url-encode a 32-byte EC coordinate, as a Plaid JWK requires."""
    return base64.urlsafe_b64encode(n.to_bytes(32, "big")).rstrip(b"=").decode()


def plaid_error_response(body):
    return type(
        "Resp",
        (),
        {
            "status": 400,
            "reason": "Bad Request",
            "data": body,
            "getheaders": lambda _self: {},
        },
    )()


class RetentionAndHardDeleteTests(TestCase):
    """7-year retention gate, and the admin's unconditional override."""

    def setUp(self):
        self.user = make_user("retention")
        self.old = aged_record(self.user, "Ancient", 365 * 7 + 1)
        self.recent = aged_record(self.user, "Recent", 30)

    def test_hard_delete_is_blocked_before_seven_years(self):
        self.client.force_login(self.user)
        response = self.client.post(
            reverse("records:hard_delete_record", args=[self.recent.pk]),
            HTTP_HX_REQUEST="true",
        )

        self.assertEqual(response.status_code, 409)
        self.assertTrue(Record.objects.filter(pk=self.recent.pk).exists())

    def test_hard_delete_succeeds_at_seven_years(self):
        self.client.force_login(self.user)
        response = self.client.post(
            reverse("records:hard_delete_record", args=[self.old.pk]),
            HTTP_HX_REQUEST="true",
        )

        self.assertEqual(response.status_code, 204)
        self.assertFalse(Record.objects.filter(pk=self.old.pk).exists())

    def test_hard_delete_also_removes_associated_documents(self):
        document = DocumentData.objects.create(
            user=self.user,
            title="Scan",
            filepath="users/1/scan.pdf",
            status="completed",
            associated_record=self.old,
        )
        self.client.force_login(self.user)
        self.client.post(
            reverse("records:hard_delete_record", args=[self.old.pk]),
            HTTP_HX_REQUEST="true",
        )

        self.assertFalse(DocumentData.objects.filter(pk=document.pk).exists())

    def test_hard_delete_only_affects_the_owners_record(self):
        intruder = make_user("intruder")
        self.client.force_login(intruder)
        response = self.client.post(
            reverse("records:hard_delete_record", args=[self.old.pk]),
            HTTP_HX_REQUEST="true",
        )

        self.assertEqual(response.status_code, 404)
        self.assertTrue(Record.objects.filter(pk=self.old.pk).exists())

    def test_admin_can_hard_delete_a_recent_record(self):
        """Admins bypass the seven-year wait entirely."""
        superuser = User.objects.create_superuser(
            username="admin", email="admin@e.com", password="pw"
        )
        self.client.force_login(superuser)

        self.client.post(
            reverse("admin:records_record_changelist"),
            {"action": "hard_delete_records", "_selected_action": [self.recent.pk]},
        )

        self.assertFalse(Record.objects.filter(pk=self.recent.pk).exists())

    def test_non_superuser_admin_cannot_hard_delete(self):
        staff = User.objects.create_user(
            username="staff", email="staff@e.com", password="pw", is_staff=True
        )
        self.client.force_login(staff)

        self.client.post(
            reverse("admin:records_record_changelist"),
            {"action": "hard_delete_records", "_selected_action": [self.recent.pk]},
        )

        self.assertTrue(Record.objects.filter(pk=self.recent.pk).exists())

    def test_retention_purge_only_removes_archived_records_older_than_seven_years(
        self,
    ):
        from records.tasks import delete_7year_archived_records

        old_archived = aged_record(self.user, "Old archived", 365 * 7 + 1, is_active=False)

        with patch("documents.tasks.delete_document.send"):
            delete_7year_archived_records()

        # self.old is active, so retention keeps it despite its age; the
        # archived one past seven years is purged.
        self.assertTrue(Record.objects.filter(pk=self.old.pk).exists())
        self.assertFalse(Record.objects.filter(pk=old_archived.pk).exists())
        self.assertTrue(Record.objects.filter(pk=self.recent.pk).exists())

    def test_retention_purge_respects_the_user_opt_out_setting(self):
        from records.tasks import delete_7year_archived_records

        self.old.is_active = False
        self.old.save()
        self.user.settings.auto_delete_archived_records = False
        self.user.settings.save()

        with patch("documents.tasks.delete_document.send"):
            delete_7year_archived_records()

        self.assertTrue(Record.objects.filter(pk=self.old.pk).exists())


class PlaidWebhookSyncTests(TestCase):
    """A SYNC_UPDATES_AVAILABLE webhook results in a transaction sync.

    Signatures are verified for real, using Plaid's ES256 key shape, rather
    than stubbed out: a broken verifier silently turns every webhook into a
    403, which is exactly how this broke in production.
    """

    def setUp(self):
        self.user = CustomUser.objects.create_user(
            username="plaiduser", email="plaid@e.com", password="pw"
        )
        self.item = PlaidItem.objects.create(
            user=self.user, item_id="item-1", access_token="access-1"
        )
        self.private_key = ec.generate_private_key(ec.SECP256R1())
        numbers = self.private_key.public_key().public_numbers()
        self.key_response = {
            "key": {
                "alg": "ES256",
                "crv": "P-256",
                "kid": "kid-1",
                "kty": "EC",
                "use": "sig",
                "x": _b64(numbers.x),
                "y": _b64(numbers.y),
            }
        }
        cache.clear()
        # Every test must never reach Plaid for real; individual tests
        # reconfigure this to simulate outages or a different served key.
        key_post = patch("plaid_integration.views.webhook.requests.post").start()
        key_post.return_value.json.return_value = self.key_response
        self.addCleanup(patch.stopall)

    def sign(self, body: bytes) -> str:
        """Sign a body the way Plaid does, so verification actually runs."""
        now = datetime.datetime.now(UTC).timestamp()
        return pyjwt.encode(
            {
                "request_body_sha256": hashlib.sha256(body).hexdigest(),
                "iat": now,
                "exp": now + 86400,
            },
            self.private_key,
            algorithm="ES256",
            headers={"kid": "kid-1"},
        )

    def post_webhook(self, code="SYNC_UPDATES_AVAILABLE", item_id="item-1", verify=True):
        body = json.dumps(
            {
                "webhook_type": "TRANSACTIONS",
                "webhook_code": code,
                "item_id": item_id,
            }
        ).encode()
        return self.client.post(
            reverse("plaid:webhook"),
            data=body,
            content_type="application/json",
            HTTP_PLAID_VERIFICATION=self.sign(body) if verify else "bogus",
        )

    def test_a_genuinely_signed_webhook_is_accepted_and_triggers_a_sync(self):
        with patch("plaid_integration.services.dispatch_sync") as dispatch:
            response = self.post_webhook()

        self.assertEqual(response.status_code, 200)
        dispatch.assert_called_once()
        self.assertEqual(dispatch.call_args.args[0].item_id, "item-1")

    def test_forged_webhook_is_rejected(self):
        with patch("plaid_integration.services.dispatch_sync") as dispatch:
            response = self.post_webhook(verify=False)

        self.assertEqual(response.status_code, 403)
        dispatch.assert_not_called()

    def test_webhook_fails_closed_when_the_key_is_unavailable(self):
        """A Plaid outage must never open the door."""
        with patch(
            "plaid_integration.views.webhook.requests.post",
            side_effect=requests.exceptions.ConnectionError("plaid unreachable"),
        ):
            with patch("plaid_integration.services.dispatch_sync") as dispatch:
                response = self.post_webhook()

        self.assertEqual(response.status_code, 403)
        dispatch.assert_not_called()

    def test_sync_webhook_imports_transactions_end_to_end(self):
        """Webhook -> dispatch -> sync task -> Record rows."""
        page = {
            "added": [
                {
                    "transaction_id": "txn-1",
                    "name": "Coffee",
                    "amount": 4.5,
                    "date": "2024-06-15",
                    "account_id": "acc-1",
                    "category": ["Food and Drink"],
                }
            ],
            "modified": [],
            "removed": [],
            "next_cursor": "cursor-1",
            "has_more": False,
        }

        def run_sync_now(item):
            # Stand in for dramatiq's async enqueue so the test stays in-process.
            from plaid_integration.tasks import sync_and_convert_for_item_task

            return sync_and_convert_for_item_task.fn(item.id)

        with (
            patch("plaid_integration.tasks.client") as client,
            patch("plaid_integration.services.dispatch_sync", side_effect=run_sync_now),
            patch("plaid_integration.tasks.try_match_plaid_record"),
        ):
            client.transactions_sync.return_value = page
            self.post_webhook()

        record = Record.objects.filter(plaid_transaction_id="txn-1").first()
        self.assertIsNotNone(record)
        self.assertEqual(record.title, "Coffee")
        self.assertEqual(record.balance, 4.5)
        self.assertTrue(record.is_active)

        self.item.refresh_from_db()
        self.assertEqual(self.item.next_cursor, "cursor-1")

    def test_login_required_webhook_records_the_error_without_retrying(self):
        body = json.dumps({"error_code": "ITEM_LOGIN_REQUIRED", "error_message": "Login required."})

        def run_sync_now(item):
            from plaid_integration.tasks import sync_and_convert_for_item_task

            return sync_and_convert_for_item_task.fn(item.id)

        with (
            patch("plaid_integration.tasks.client") as client,
            patch("plaid_integration.services.dispatch_sync", side_effect=run_sync_now),
            patch("plaid_integration.tasks.try_match_plaid_record"),
        ):
            client.transactions_sync.side_effect = plaid.ApiException(
                status=400, reason="Bad Request", http_resp=plaid_error_response(body)
            )
            self.post_webhook("ITEM_LOGIN_REQUIRED")

        self.item.refresh_from_db()
        self.assertEqual(self.item.last_error_code, "ITEM_LOGIN_REQUIRED")
        self.assertEqual(Record.objects.filter(plaid_item=self.item).count(), 0)

    def test_unknown_item_webhook_is_acknowledged_not_500(self):
        response = self.post_webhook(item_id="does-not-exist")

        self.assertEqual(response.status_code, 200)


class ReimbursementMoneyFlowTests(TestCase):
    """Money moves payer -> creator, less the platform fee."""

    def setUp(self):
        self.creator = make_user("creator")
        self.payer = make_user("payer")

        # The StripeAccount row is auto-created with the user; complete
        # onboarding on it before the package is built, since payouts require
        # an active account.
        account = self.creator.stripe_account
        account.stripe_account_id = "acct_creator"
        account.stripe_details_submitted = True
        account.charges_enabled = True
        account.payouts_enabled = True
        account.save()

        self.package = ReimbursementPackage.objects.create(
            creator=self.creator,
            title="Trip expenses",
            currency="usd",
            status=ReimbursementPackage.Status.OPEN,
        )
        self.record = make_record(self.creator, title="Hotel", balance="100.00")
        self.package.records.add(self.record)

    def checkout(self):
        from reimbursements import services

        with patch("reimbursements.services.create_checkout_session") as create:
            create.return_value = type("Session", (), {"id": "cs_1", "url": "https://pay.test/1"})()
            outcome = services.create_package_checkout(
                package=self.package,
                payer=self.payer,
                currency="usd",
                success_url="https://verity.test/ok",
                cancel_url="https://verity.test/cancel",
            )
        return outcome, create.call_args.kwargs

    def test_platform_fee_is_positive_and_leaves_the_creator_a_share(self):
        from reimbursements.fees import PlatformFeeCalculator

        fee = PlatformFeeCalculator.compute(10_000, "usd", rates={})

        self.assertGreater(fee, 0)
        self.assertLess(fee, 10_000)
        self.assertEqual(fee, EXPECTED_FEE_CENTS)
        self.assertEqual(10_000 - fee, EXPECTED_CREATOR_CENTS)

    def test_platform_fee_never_exceeds_the_charge(self):
        from reimbursements.fees import PlatformFeeCalculator

        self.assertEqual(PlatformFeeCalculator.compute(1, "usd", rates={}), 1)
        self.assertEqual(PlatformFeeCalculator.compute(0, "usd", rates={}), 0)
        self.assertEqual(PlatformFeeCalculator.compute(-5, "usd", rates={}), 0)

    def test_checkout_creates_a_destination_charge_that_splits_the_money(self):
        outcome, kwargs = self.checkout()

        self.assertIsNone(outcome.error)
        intent = kwargs["payment_intent_data"]

        # The transfer destination is the creator's connected account...
        self.assertEqual(intent["transfer_data"]["destination"], "acct_creator")
        # ...and the platform takes its cut via the application fee.
        self.assertEqual(intent["application_fee_amount"], EXPECTED_FEE_CENTS)

        total = sum(
            item["price_data"]["unit_amount"] * item["quantity"] for item in kwargs["line_items"]
        )
        self.assertEqual(total, 10_000)
        self.assertEqual(total - intent["application_fee_amount"], EXPECTED_CREATOR_CENTS)

    def test_marking_paid_marks_records_reimbursed_and_credits_the_payer(self):
        self.package.mark_as_paid(self.payer)

        self.package.refresh_from_db()
        self.assertEqual(self.package.status, ReimbursementPackage.Status.PAID)

        self.record.refresh_from_db()
        self.assertTrue(self.record.reimbursed)

        payer_record = Record.objects.filter(user=self.payer).first()
        self.assertIsNotNone(payer_record)
        self.assertEqual(payer_record.balance, 100)
        self.assertEqual(payer_record.currency, "usd")

    def test_marking_paid_twice_does_not_double_pay(self):
        self.package.mark_as_paid(self.payer)
        self.package.mark_as_paid(self.payer)

        self.assertEqual(Record.objects.filter(user=self.payer).count(), 1)

    def test_checkout_records_the_claimed_total_for_reconciliation(self):
        outcome, _ = self.checkout()

        self.assertIsNone(outcome.error)
        payment = PackagePayment.objects.get(stripe_checkout_session_id="cs_1")
        self.assertEqual(payment.expected_amount_cents, 10_000)
        self.assertEqual(payment.amount_paid, 100)
        self.assertEqual(payment.payer_currency, "usd")
        self.assertFalse(payment.is_completed)
