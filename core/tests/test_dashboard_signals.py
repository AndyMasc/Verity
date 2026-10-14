"""Tests for signal-driven dashboard cache invalidation."""

from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase

from core.models import Notification
from documents.models import DocumentData, DocumentStatus
from records.models import MergeLog, Record
from reimbursements.models import PackagePayment, ReimbursementPackage

User = get_user_model()


def _prime_cache(user_id: int) -> None:
    cache.set(f"dashboard:{user_id}", {"records": []}, 60)


def _make_record(user, title: str) -> Record:
    return Record.objects.create(
        user=user,
        title=title,
        merchant=title,
        balance=Decimal("100.00"),
        transaction_date=date(2024, 6, 15),
        record_type=Record.RecordTypes.EXPENSE_RECEIPT,
    )


class DashboardCacheInvalidationTest(TestCase):
    def setUp(self):
        cache.clear()

    def test_record_save_invalidates_dashboard_cache(self):
        user = User.objects.create_user(username="signalrecord", password="pass")
        _prime_cache(user.id)
        _make_record(user, "Signal record")
        self.assertIsNone(cache.get(f"dashboard:{user.id}"))

    def test_record_delete_invalidates_dashboard_cache(self):
        user = User.objects.create_user(username="signaldelete", password="pass")
        record = _make_record(user, "Signal delete")
        _prime_cache(user.id)
        record.delete()
        self.assertIsNone(cache.get(f"dashboard:{user.id}"))

    def test_document_save_invalidates_dashboard_cache(self):
        user = User.objects.create_user(username="signaldoc", password="pass")
        _prime_cache(user.id)
        DocumentData.objects.create(
            user=user,
            title="Receipt",
            filepath=f"users/{user.id}/doc-1.pdf",
            status=DocumentStatus.PENDING_UPLOAD,
        )
        self.assertIsNone(cache.get(f"dashboard:{user.id}"))

    def test_notification_save_invalidates_dashboard_cache(self):
        user = User.objects.create_user(username="signalnotif", password="pass")
        _prime_cache(user.id)
        Notification.objects.create(recipient=user, message="Expiring soon")
        self.assertIsNone(cache.get(f"dashboard:{user.id}"))

    def test_user_settings_save_invalidates_dashboard_cache(self):
        user = User.objects.create_user(username="signalsettings", password="pass")
        _prime_cache(user.id)
        user.settings.save()
        self.assertIsNone(cache.get(f"dashboard:{user.id}"))

    def test_merge_log_create_invalidates_dashboard_cache(self):
        user = User.objects.create_user(username="signalmerge", password="pass")
        plaid = _make_record(user, "Bank transaction")
        doc_record = _make_record(user, "Receipt")
        _prime_cache(user.id)
        MergeLog.objects.create(
            plaid_record=plaid,
            document_record=doc_record,
            plaid_snapshot={"title": plaid.title},
            document_snapshot={"title": doc_record.title},
        )
        self.assertIsNone(cache.get(f"dashboard:{user.id}"))

    def test_reimbursement_package_invalidates_creator_and_recipient(self):
        creator = User.objects.create_user(username="signalcreator", password="pass")
        recipient = User.objects.create_user(username="signalrecipient", password="pass")
        _prime_cache(creator.id)
        _prime_cache(recipient.id)
        ReimbursementPackage.objects.create(
            creator=creator,
            recipient=recipient,
            title="Trip",
        )
        self.assertIsNone(cache.get(f"dashboard:{creator.id}"))
        self.assertIsNone(cache.get(f"dashboard:{recipient.id}"))

    def test_package_payment_invalidates_package_users(self):
        creator = User.objects.create_user(username="signalpaycreator", password="pass")
        recipient = User.objects.create_user(username="signalpayrecipient", password="pass")
        package = ReimbursementPackage.objects.create(
            creator=creator,
            recipient=recipient,
            title="Trip",
        )
        _prime_cache(creator.id)
        _prime_cache(recipient.id)
        PackagePayment.objects.create(
            package=package,
            stripe_checkout_session_id="cs_test_signal",
            amount_paid=Decimal("10.00"),
            payer_currency="USD",
            is_completed=True,
        )
        self.assertIsNone(cache.get(f"dashboard:{creator.id}"))
        self.assertIsNone(cache.get(f"dashboard:{recipient.id}"))

    def test_package_payment_cascade_delete_does_not_error(self):
        creator = User.objects.create_user(username="signalcascade", password="pass")
        package = ReimbursementPackage.objects.create(creator=creator, title="Trip")
        payment = PackagePayment.objects.create(
            package=package,
            stripe_checkout_session_id="cs_test_cascade",
            amount_paid=Decimal("10.00"),
            payer_currency="USD",
        )
        _prime_cache(creator.id)
        package.delete()
        self.assertIsNone(cache.get(f"dashboard:{creator.id}"))
        self.assertFalse(PackagePayment.objects.filter(pk=payment.pk).exists())
