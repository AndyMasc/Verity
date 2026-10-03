"""Churn capture for cancelled subscriptions.

Cancellations are split by what the subscription covered -- a base plan or an
add-on -- and attributed to the moment the user decided to leave rather than the
moment the paid period ran out. Cancellations the app performs itself are not
churn and must never be reported as such.
"""

from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone
from djstripe.models import Customer, Price, Product, Subscription, SubscriptionItem

from .. import metadata
from ..webhooks import (
    clear_cancel_intent,
    mark_cancel_intent,
    read_cancel_intent,
    capture_subscription_cancelled,
    handle_subscription_changed,
    handle_subscription_deleted,
)


class CancellationCaptureTests(TestCase):
    def setUp(self):
        client = mock.patch("billing.webhooks.posthog_client", mock.Mock())
        client.start()
        self.addCleanup(client.stop)

        self.customer = Customer.objects.create(
            id="cus_churn", livemode=False, created=timezone.now()
        )
        self.user = get_user_model().objects.create_user(
            username="churn", email="churn@example.com", password="password"
        )
        self.customer.subscriber = self.user
        self.customer.save()

    def _add_item(self, sub_id: str, product_meta) -> None:
        """Attach a real price/product pair to a subscription."""
        sub, _ = Subscription.objects.get_or_create(
            id=sub_id,
            defaults=dict(
                livemode=False,
                created=timezone.now(),
                customer=self.customer,
                stripe_data={"status": "active"},
            ),
        )
        product, _ = Product.objects.get_or_create(
            id=product_meta.stripe_id,
            defaults=dict(livemode=False, active=True, name=product_meta.name),
        )
        price, _ = Price.objects.get_or_create(
            id=f"price_{product_meta.stripe_id}",
            defaults=dict(livemode=False, active=True, product=product, currency="usd"),
        )
        SubscriptionItem.objects.get_or_create(
            id=f"si_{sub_id}_{product_meta.stripe_id}",
            defaults=dict(
                livemode=False,
                created=timezone.now(),
                subscription=sub,
                price=price,
            ),
        )

    def _captured(self, sub_id: str, stripe_sub: dict | None = None):
        with mock.patch("billing.webhooks.capture") as capture:
            capture_subscription_cancelled(sub_id, stripe_sub)
        return {call.args[0]: call.kwargs["properties"] for call in capture.call_args_list}

    def test_base_plan_only_is_marked_base(self):
        self._add_item("sub_base", metadata.VERITY_PRO)
        events = self._captured("sub_base")
        self.assertEqual(set(events), {"subscription_cancelled"})
        self.assertEqual(events["subscription_cancelled"]["plan_types"], ["base_plan"])

    def test_addon_only_emits_addon_event(self):
        """Add-on cancellations used to be dropped by the base-plan gate."""
        addon = next(m for m in metadata.PRODUCTS.values() if m.category == "storage_plan")
        self._add_item("sub_addon", addon)
        events = self._captured("sub_addon")
        self.assertEqual(set(events), {"subscription_cancelled"})
        self.assertEqual(events["subscription_cancelled"]["plan_types"], ["storage_plan"])

    def test_combined_subscription_lists_both_plan_types(self):
        self._add_item("sub_both", metadata.VERITY_PRO)
        addon = next(m for m in metadata.PRODUCTS.values() if m.category == "storage_plan")
        self._add_item("sub_both", addon)
        events = self._captured("sub_both")
        self.assertEqual(
            events["subscription_cancelled"]["plan_types"],
            ["base_plan", "storage_plan"],
        )

    def test_system_cancellation_is_not_churn(self):
        self._add_item("sub_sys", metadata.VERITY_PRO)
        mark_cancel_intent("sub_sys", "system")
        self.addCleanup(clear_cancel_intent, "sub_sys")
        self.assertEqual(self._captured("sub_sys"), {})

    def test_cancel_type_uses_recorded_intent(self):
        """The deleted event can no longer say "period_end", so the intent is used."""
        self._add_item("sub_ct", metadata.VERITY_PRO)
        mark_cancel_intent("sub_ct", "period_end")
        self.addCleanup(clear_cancel_intent, "sub_ct")
        events = self._captured("sub_ct", {"id": "sub_ct", "cancel_at_period_end": False})
        self.assertEqual(events["subscription_cancelled"]["cancel_type"], "period_end")


class CancellationDedupeTests(TestCase):
    """A period-end cancel is recorded once, when it is scheduled."""

    def setUp(self):
        self.customer = Customer.objects.create(
            id="cus_dedupe", livemode=False, created=timezone.now()
        )
        product = Product.objects.create(
            id=metadata.VERITY_PRO.stripe_id,
            livemode=False,
            active=True,
            name="Verity Pro",
        )
        price = Price.objects.create(
            id="price_dedupe",
            livemode=False,
            active=True,
            product=product,
            currency="usd",
        )
        sub = Subscription.objects.create(
            id="sub_dedupe",
            livemode=False,
            created=timezone.now(),
            customer=self.customer,
            stripe_data={"status": "active"},
        )
        SubscriptionItem.objects.create(
            id="si_dedupe",
            livemode=False,
            created=timezone.now(),
            subscription=sub,
            price=price,
        )
        self.addCleanup(clear_cancel_intent, "sub_dedupe")

    def test_scheduled_cancellation_recorded_at_intent(self):
        with mock.patch("billing.webhooks.capture_subscription_cancelled") as capture:
            handle_subscription_changed(
                event=mock.Mock(
                    data={
                        "object": {
                            "id": "sub_dedupe",
                            "customer": "cus_dedupe",
                            "cancel_at_period_end": True,
                        }
                    }
                )
            )
        capture.assert_called_once()
        self.assertEqual(read_cancel_intent("sub_dedupe"), "period_end")

    def test_deletion_does_not_repeat_a_recorded_cancellation(self):
        mark_cancel_intent("sub_dedupe", "period_end")
        with mock.patch("billing.webhooks.capture_subscription_cancelled") as capture:
            handle_subscription_deleted(
                event=mock.Mock(
                    data={
                        "object": {
                            "id": "sub_dedupe",
                            "customer": "cus_dedupe",
                            "cancel_at_period_end": False,
                        }
                    }
                )
            )
        capture.assert_not_called()

    def test_immediate_cancellation_still_recorded(self):
        with mock.patch("billing.webhooks.capture_subscription_cancelled") as capture:
            handle_subscription_deleted(
                event=mock.Mock(
                    data={
                        "object": {
                            "id": "sub_dedupe",
                            "customer": "cus_dedupe",
                            "cancel_at_period_end": False,
                        }
                    }
                )
            )
        capture.assert_called_once()

    def test_marker_is_cleared_after_deletion(self):
        mark_cancel_intent("sub_dedupe", "period_end")
        # The cleanup is deferred with transaction.on_commit, which TestCase
        # rolls back rather than runs, so execute the queued callbacks here.
        with self.captureOnCommitCallbacks(execute=True):
            handle_subscription_deleted(
                event=mock.Mock(
                    data={
                        "object": {
                            "id": "sub_dedupe",
                            "customer": "cus_dedupe",
                            "cancel_at_period_end": False,
                        }
                    }
                )
            )
        self.assertIsNone(read_cancel_intent("sub_dedupe"))
        cache.delete("billing:cancel-intent:sub_dedupe")
