import json
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch, MagicMock

from django.contrib.auth import get_user_model
from django.test import TestCase, RequestFactory
from django.urls import reverse
from django.utils import timezone

from reimbursements.models import ReimbursementPackage, PackagePayment, StripeAccount
from reimbursements.views import validate_recipient_email
from records.models import Record

User = get_user_model()


def _user(email="test@example.com", **kwargs):
    username = kwargs.pop("username", email.split("@")[0])
    return User.objects.create_user(
        username=username, email=email, password="testpass123", **kwargs
    )


def _package(creator, recipient=None, status="open", **kwargs):
    return ReimbursementPackage.objects.create(
        creator=creator,
        recipient=recipient,
        title=kwargs.get("title", "Test Package"),
        status=status,
        **{k: v for k, v in kwargs.items() if k not in ("title", "status")},
    )


def _record(user, balance=Decimal("25.00")):
    return Record.objects.create(
        user=user,
        title="Test Expense",
        balance=balance,
        record_type="expense",
        is_active=True,
    )


def _stripe_account(user, active=True):
    StripeAccount.objects.filter(user=user).update(
        stripe_account_id="acct_test123" if active else None,
        stripe_details_submitted=active,
    )
    user.stripe_account.refresh_from_db()
    return user.stripe_account


class ReimbursementPackageModelTest(TestCase):
    def setUp(self):
        self.creator = _user("creator@test.com")
        self.recipient = _user("recipient@test.com")

    def test_str(self):
        pkg = _package(self.creator)
        self.assertIn(pkg.title, str(pkg))

    def test_total_amount(self):
        pkg = _package(self.creator)
        r1 = _record(self.creator, Decimal("10.00"))
        r2 = _record(self.creator, Decimal("20.50"))
        pkg.records.add(r1, r2)
        self.assertEqual(pkg.total_amount, Decimal("30.50"))

    def test_total_amount_excludes_inactive(self):
        pkg = _package(self.creator)
        r1 = _record(self.creator, Decimal("10.00"))
        r2 = _record(self.creator, Decimal("20.00"))
        r2.is_active = False
        r2.save()
        pkg.records.add(r1, r2)
        self.assertEqual(pkg.total_amount, Decimal("10.00"))

    def test_total_amount_cents(self):
        pkg = _package(self.creator)
        r = _record(self.creator, Decimal("15.75"))
        pkg.records.add(r)
        self.assertEqual(pkg.total_amount_cents, 1575)

    def test_mark_as_paid(self):
        pkg = _package(self.creator, self.recipient)
        pkg.mark_as_paid(self.recipient)
        pkg.refresh_from_db()
        self.assertEqual(pkg.status, ReimbursementPackage.Status.PAID)
        self.assertEqual(pkg.paid_by, self.recipient)
        self.assertIsNotNone(pkg.paid_at)

    def test_mark_as_paid_idempotent(self):
        pkg = _package(self.creator, self.recipient)
        pkg.mark_as_paid(self.recipient)
        first_paid_at = pkg.paid_at
        pkg.mark_as_paid(self.recipient)
        pkg.refresh_from_db()
        self.assertEqual(pkg.paid_at, first_paid_at)

    def test_mark_as_refunded(self):
        pkg = _package(self.creator, self.recipient)
        pkg.mark_as_paid(self.recipient)
        pkg.refresh_from_db()
        self.assertEqual(pkg.status, ReimbursementPackage.Status.PAID)
        pkg.mark_as_refunded()
        pkg.refresh_from_db()
        self.assertEqual(pkg.status, ReimbursementPackage.Status.OPEN)
        self.assertIsNone(pkg.paid_by)
        self.assertIsNone(pkg.paid_at)

    def test_mark_as_refunded_idempotent(self):
        pkg = _package(self.creator, self.recipient)
        pkg.mark_as_refunded()
        pkg.refresh_from_db()
        self.assertEqual(pkg.status, ReimbursementPackage.Status.OPEN)

    def test_is_expired_false(self):
        pkg = _package(self.creator, expires_at=timezone.now() + timedelta(days=7))
        self.assertFalse(pkg.is_expired)

    def test_is_expired_true(self):
        pkg = _package(self.creator, expires_at=timezone.now() - timedelta(days=1))
        self.assertTrue(pkg.is_expired)

    def test_is_expired_none(self):
        pkg = _package(self.creator, expires_at=None)
        self.assertFalse(pkg.is_expired)


class StripeAccountModelTest(TestCase):
    def test_is_active(self):
        user = _user("stripe@test.com")
        acct = _stripe_account(user, active=True)
        self.assertTrue(acct.is_active)

    def test_is_inactive(self):
        user = _user("stripe2@test.com")
        acct = _stripe_account(user, active=False)
        self.assertFalse(acct.is_active)


class ValidateRecipientEmailTest(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.user = _user()
        self.url = reverse("reimbursements:validate-email")

    def test_valid_email(self):
        _user("valid@test.com")
        request = self.factory.post(
            self.url,
            data=json.dumps({"email": "valid@test.com"}),
            content_type="application/json",
        )
        request.user = self.user
        response = validate_recipient_email(request)
        data = json.loads(response.content)
        self.assertTrue(data["valid"])

    def test_self_send(self):
        request = self.factory.post(
            self.url,
            data=json.dumps({"email": self.user.email}),
            content_type="application/json",
        )
        request.user = self.user
        response = validate_recipient_email(request)
        data = json.loads(response.content)
        self.assertFalse(data["valid"])
        self.assertIn("yourself", data["error"].lower())

    def test_nonexistent_email(self):
        request = self.factory.post(
            self.url,
            data=json.dumps({"email": "nobody@test.com"}),
            content_type="application/json",
        )
        request.user = self.user
        response = validate_recipient_email(request)
        data = json.loads(response.content)
        self.assertFalse(data["valid"])
        self.assertNotIn("nobody@test.com", data["error"])

    def test_empty_email(self):
        request = self.factory.post(
            self.url,
            data=json.dumps({"email": ""}),
            content_type="application/json",
        )
        request.user = self.user
        response = validate_recipient_email(request)
        self.assertEqual(response.status_code, 400)


class PackageListViewTest(TestCase):
    def setUp(self):
        self.user = _user()
        self.client.force_login(self.user)
        self.url = reverse("reimbursements:package-list")

    def test_empty_list(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)

    def test_shows_created_packages(self):
        _package(self.user, title="My Package")
        response = self.client.get(self.url)
        self.assertContains(response, "My Package")

    def test_shows_received_packages(self):
        other = _user("other@test.com")
        _package(other, self.user, title="Received Package")
        response = self.client.get(self.url)
        self.assertContains(response, "Received Package")


class PackageDetailViewTest(TestCase):
    def setUp(self):
        self.user = _user()
        self.other = _user("other@test.com")
        self.client.force_login(self.user)

    def test_creator_can_view(self):
        pkg = _package(self.user, self.other)
        response = self.client.get(
            reverse("reimbursements:package-detail", kwargs={"package_uuid": pkg.uuid})
        )
        self.assertEqual(response.status_code, 200)

    def test_recipient_can_view(self):
        pkg = _package(self.other, self.user)
        response = self.client.get(
            reverse("reimbursements:package-detail", kwargs={"package_uuid": pkg.uuid})
        )
        self.assertEqual(response.status_code, 200)

    def test_unrelated_user_cannot_view(self):
        stranger = _user("stranger@test.com")
        pkg = _package(self.user, self.other)
        self.client.force_login(stranger)
        response = self.client.get(
            reverse("reimbursements:package-detail", kwargs={"package_uuid": pkg.uuid})
        )
        self.assertEqual(response.status_code, 404)


@patch("django_ratelimit.decorators.is_ratelimited", return_value=False)
@patch("reimbursements.notifications.send_package_created_notification")
class CreatePackageFromRecordsViewTest(TestCase):
    def setUp(self):
        self.user = _user()
        _stripe_account(self.user)
        self.recipient = _user("recipient@test.com")
        self.client.force_login(self.user)
        self.url = reverse("reimbursements:create-package")

    def test_create_package(self, _mock_notify, _mock_rl):
        r = _record(self.user)
        response = self.client.post(
            self.url,
            data=json.dumps(
                {
                    "record_ids": [r.id],
                    "title": "Test Reimbursement",
                    "recipient_email": self.recipient.email,
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertIn("redirect_url", data)
        self.assertTrue(ReimbursementPackage.objects.filter(title="Test Reimbursement").exists())

    def test_self_send_rejected(self, _mock_notify, _mock_rl):
        r = _record(self.user)
        response = self.client.post(
            self.url,
            data=json.dumps(
                {
                    "record_ids": [r.id],
                    "title": "Self",
                    "recipient_email": self.user.email,
                }
            ),
            content_type="application/json",
        )
        data = json.loads(response.content)
        self.assertEqual(response.status_code, 400)
        self.assertIn("yourself", data["error"].lower())

    def test_nonexistent_recipient(self, _mock_notify, _mock_rl):
        r = _record(self.user)
        response = self.client.post(
            self.url,
            data=json.dumps(
                {
                    "record_ids": [r.id],
                    "title": "Fail",
                    "recipient_email": "ghost@test.com",
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)

    def test_no_records(self, _mock_notify, _mock_rl):
        response = self.client.post(
            self.url,
            data=json.dumps(
                {
                    "record_ids": [],
                    "title": "Empty",
                    "recipient_email": self.recipient.email,
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)

    def test_days_valid_clamped(self, _mock_notify, _mock_rl):
        r = _record(self.user)
        response = self.client.post(
            self.url,
            data=json.dumps(
                {
                    "record_ids": [r.id],
                    "title": "Clamped",
                    "recipient_email": self.recipient.email,
                    "days_valid": 9999,
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        pkg = ReimbursementPackage.objects.get(title="Clamped")
        max_expected = timezone.now() + timedelta(days=365)
        self.assertLess(pkg.expires_at, max_expected)

    def test_invalid_days_valid(self, _mock_notify, _mock_rl):
        r = _record(self.user)
        response = self.client.post(
            self.url,
            data=json.dumps(
                {
                    "record_ids": [r.id],
                    "title": "Bad Days",
                    "recipient_email": self.recipient.email,
                    "days_valid": "not_a_number",
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)


@patch("django_ratelimit.decorators.is_ratelimited", return_value=False)
class CreatePackageCheckoutViewTest(TestCase):
    def setUp(self):
        self.creator = _user("creator@test.com")
        _stripe_account(self.creator)
        self.payer = _user("payer@test.com")
        self.pkg = _package(self.creator, self.payer, title="Checkout Test")
        self.client.force_login(self.payer)
        self.url = reverse("reimbursements:create-checkout", kwargs={"package_uuid": self.pkg.uuid})

    def test_creator_cannot_pay(self, _mock_rl):
        self.client.force_login(self.creator)
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 302)

    def test_paid_package_redirects(self, _mock_rl):
        self.pkg.mark_as_paid(self.payer)
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 302)

    def test_expired_package_redirects(self, _mock_rl):
        self.pkg.expires_at = timezone.now() - timedelta(days=1)
        self.pkg.save(update_fields=["expires_at"])
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 302)

    def test_get_not_allowed(self, _mock_rl):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 405)


class PaymentSuccessViewTest(TestCase):
    def setUp(self):
        self.user = _user()
        self.other = _user("other@test.com")
        self.client.force_login(self.user)
        self.url = reverse("reimbursements:payment-success")

    def test_no_package_param(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)

    def test_valid_package(self):
        pkg = _package(self.user, self.other)
        response = self.client.get(f"{self.url}?package={pkg.uuid}")
        self.assertEqual(response.status_code, 200)

    def test_payer_can_view_package_before_webhook(self):
        pkg = _package(self.user, self.other)
        PackagePayment.objects.create(
            package=pkg,
            payer=self.other,
            stripe_checkout_session_id="cs_test_foo",
            amount_paid=pkg.total_amount,
        )
        self.client.force_login(self.other)
        response = self.client.get(f"{self.url}?package={pkg.uuid}")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, pkg.title)

    def test_other_users_package_not_visible(self):
        stranger = _user("stranger@test.com")
        pkg = _package(self.user, self.other)
        self.client.force_login(stranger)
        response = self.client.get(f"{self.url}?package={pkg.uuid}")
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, pkg.title)


class MarkAsPaidTransactionTest(TestCase):
    def setUp(self):
        self.creator = _user("creator@test.com")
        self.payer = _user("payer@test.com")

    def test_records_marked_reimbursed_atomically(self):
        pkg = _package(self.creator, self.payer)
        r1 = _record(self.creator, Decimal("10.00"))
        r2 = _record(self.creator, Decimal("20.00"))
        pkg.records.add(r1, r2)
        pkg.mark_as_paid(self.payer)
        r1.refresh_from_db()
        r2.refresh_from_db()
        self.assertTrue(r1.reimbursed)
        self.assertTrue(r2.reimbursed)


# ---------------------------------------------------------------------------
# Webhook tests
# ---------------------------------------------------------------------------
class StripeWebhookTest(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.url = reverse("reimbursements:stripe-webhook")

    @patch("reimbursements.webhooks._send_package_paid_notification")
    @patch("reimbursements.webhooks.stripe.Webhook.construct_event")
    def test_checkout_session_completed(self, mock_construct, _mock_notify):
        creator = _user("creator@test.com")
        payer = _user("payer@test.com")
        pkg = _package(creator, payer)
        payment = PackagePayment.objects.create(
            package=pkg,
            payer=payer,
            stripe_checkout_session_id="cs_test123",
            amount_paid=Decimal("50.00"),
        )

        mock_construct.return_value = {
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": "cs_test123",
                    "metadata": {"package_uuid": str(pkg.uuid)},
                }
            },
        }

        request = self.factory.post(
            self.url,
            data=b"{}",
            content_type="application/json",
            HTTP_STRIPE_SIGNATURE="sig_test",
        )
        with patch("reimbursements.webhooks.settings") as mock_settings:
            mock_settings.STRIPE_WEBHOOK_SECRET = "whsec_test_fake"
            from reimbursements.webhooks import stripe_webhook

            response = stripe_webhook(request)

        self.assertEqual(response.status_code, 200)
        payment.refresh_from_db()
        pkg.refresh_from_db()
        self.assertTrue(payment.is_completed)
        self.assertEqual(pkg.status, ReimbursementPackage.Status.PAID)

    @patch("reimbursements.webhooks.stripe.Webhook.construct_event")
    def test_checkout_session_missing_payment_returns_500(self, mock_construct):
        mock_construct.return_value = {
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": "cs_nonexistent",
                    "metadata": {"package_uuid": "00000000-0000-0000-0000-000000000000"},
                }
            },
        }

        request = self.factory.post(
            self.url,
            data=b"{}",
            content_type="application/json",
            HTTP_STRIPE_SIGNATURE="sig_test",
        )
        with patch("reimbursements.webhooks.settings") as mock_settings:
            mock_settings.STRIPE_WEBHOOK_SECRET = "whsec_test_fake"
            from reimbursements.webhooks import stripe_webhook

            response = stripe_webhook(request)

        self.assertEqual(response.status_code, 500)

    @patch("reimbursements.webhooks.stripe.Webhook.construct_event")
    def test_account_updated(self, mock_construct):
        user = _user("stripe@test.com")
        user.stripe_account.stripe_account_id = "acct_test"
        user.stripe_account.save(update_fields=["stripe_account_id"])

        mock_construct.return_value = {
            "type": "account.updated",
            "data": {"object": {"id": "acct_test", "details_submitted": True}},
        }

        request = self.factory.post(
            self.url,
            data=b"{}",
            content_type="application/json",
            HTTP_STRIPE_SIGNATURE="sig_test",
        )
        with patch("reimbursements.webhooks.settings") as mock_settings:
            mock_settings.STRIPE_WEBHOOK_SECRET = "whsec_test_fake"
            from reimbursements.webhooks import stripe_webhook

            response = stripe_webhook(request)

        self.assertEqual(response.status_code, 200)
        user.stripe_account.refresh_from_db()
        self.assertTrue(user.stripe_account.stripe_details_submitted)

    @patch("reimbursements.webhooks.stripe.Webhook.construct_event")
    def test_invalid_signature(self, mock_construct):
        mock_construct.side_effect = MagicMock(side_effect=ValueError("Invalid signature"))
        request = self.factory.post(
            self.url,
            data=b"{}",
            content_type="application/json",
            HTTP_STRIPE_SIGNATURE="sig_bad",
        )
        from reimbursements.webhooks import stripe_webhook

        response = stripe_webhook(request)
        self.assertEqual(response.status_code, 400)

    @patch("reimbursements.webhooks.stripe.Webhook.construct_event")
    def test_charge_refunded(self, mock_construct):
        creator = _user("creator@test.com")
        payer = _user("payer@test.com")
        pkg = _package(creator, payer)
        pkg.mark_as_paid(payer)
        pkg.refresh_from_db()
        self.assertEqual(pkg.status, ReimbursementPackage.Status.PAID)

        payment = PackagePayment.objects.create(
            package=pkg,
            payer=payer,
            stripe_checkout_session_id="cs_refund_test",
            stripe_payment_intent_id="pi_refund_test",
            amount_paid=Decimal("50.00"),
            is_completed=True,
        )

        mock_construct.return_value = {
            "type": "charge.refunded",
            "data": {
                "object": {
                    "payment_intent": "pi_refund_test",
                    "amount_refunded": 5000,
                }
            },
        }

        request = self.factory.post(
            self.url,
            data=b"{}",
            content_type="application/json",
            HTTP_STRIPE_SIGNATURE="sig_test",
        )
        with patch("reimbursements.webhooks.settings") as mock_settings:
            mock_settings.STRIPE_WEBHOOK_SECRET = "whsec_test_fake"
            from reimbursements.webhooks import stripe_webhook

            response = stripe_webhook(request)

        self.assertEqual(response.status_code, 200)
        pkg.refresh_from_db()
        self.assertEqual(pkg.status, ReimbursementPackage.Status.OPEN)
