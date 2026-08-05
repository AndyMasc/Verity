from unittest import mock

import stripe
from django.test import TestCase
from django.utils import timezone
from djstripe.models import Customer, Subscription

from . import services


class ReconcileSubscriptionStatusesTests(TestCase):
    def setUp(self):
        self.customer = Customer.objects.create(
            id="cus_reconcile", livemode=False, created=timezone.now()
        )

    def _make_subscription(self, sub_id, status):
        return Subscription.objects.create(
            id=sub_id,
            livemode=False,
            created=timezone.now(),
            customer=self.customer,
            stripe_data={"status": status},
        )

    def _fake_remote(self, status):
        return mock.Mock(status=status)

    def test_active_local_corrected_to_canceled(self):
        local = self._make_subscription("sub_recon_drifted", "active")
        with mock.patch(
            "billing.services.stripe.Subscription.retrieve",
            return_value=self._fake_remote("canceled"),
        ):
            corrected = services.reconcile_subscription_statuses()
        self.assertEqual(corrected, 1)
        local.refresh_from_db()
        self.assertEqual(local.stripe_data["status"], "canceled")

    def test_matching_status_is_not_touched(self):
        local = self._make_subscription("sub_recon_ok", "active")
        with mock.patch(
            "billing.services.stripe.Subscription.retrieve",
            return_value=self._fake_remote("active"),
        ):
            corrected = services.reconcile_subscription_statuses()
        self.assertEqual(corrected, 0)
        local.refresh_from_db()
        self.assertEqual(local.stripe_data["status"], "active")

    def test_gone_in_stripe_corrected_to_canceled(self):
        local = self._make_subscription("sub_recon_gone", "active")
        with mock.patch(
            "billing.services.stripe.Subscription.retrieve",
            side_effect=stripe.error.InvalidRequestError(
                message="No such subscription", param="id"
            ),
        ):
            corrected = services.reconcile_subscription_statuses()
        self.assertEqual(corrected, 1)
        local.refresh_from_db()
        self.assertEqual(local.stripe_data["status"], "canceled")

    def test_transient_stripe_error_skips_row(self):
        local = self._make_subscription("sub_recon_retry", "active")
        with mock.patch(
            "billing.services.stripe.Subscription.retrieve",
            side_effect=stripe.error.APIConnectionError("boom"),
        ):
            corrected = services.reconcile_subscription_statuses()
        self.assertEqual(corrected, 0)
        local.refresh_from_db()
        self.assertEqual(local.stripe_data["status"], "active")

    def test_filtered_to_given_subscription_ids(self):
        local = self._make_subscription("sub_recon_filtered", "active")
        self._make_subscription("sub_recon_other", "active")
        with mock.patch(
            "billing.services.stripe.Subscription.retrieve",
            side_effect=stripe.error.InvalidRequestError(
                message="No such subscription", param="id"
            ),
        ):
            corrected = services.reconcile_subscription_statuses(
                ["sub_recon_filtered"]
            )
        self.assertEqual(corrected, 1)
        local.refresh_from_db()
        self.assertEqual(local.stripe_data["status"], "canceled")


class ReconcileSubscriptionStatusesTaskTests(TestCase):
    def test_task_returns_corrected_count(self):
        customer = Customer.objects.create(
            id="cus_recon_task", livemode=False, created=timezone.now()
        )
        Subscription.objects.create(
            id="sub_recon_task",
            livemode=False,
            created=timezone.now(),
            customer=customer,
            stripe_data={"status": "active"},
        )
        with mock.patch(
            "billing.services.stripe.Subscription.retrieve",
            side_effect=stripe.error.InvalidRequestError(
                message="No such subscription", param="id"
            ),
        ):
            from .tasks import reconcile_subscription_statuses_task

            self.assertEqual(reconcile_subscription_statuses_task(), 1)
