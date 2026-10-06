"""The hourly usage sync that feeds Stripe's billing meter.

Subscription.status is a property over stripe_data and is not queryable, so a
filter on it raises FieldError before any meter event is sent. That failure mode
is invisible until a customer is under-billed, so the task is exercised here
end-to-end with the Stripe call mocked.
"""

from unittest import mock

import stripe

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from djstripe.models import Customer, Price, Product, Subscription, SubscriptionItem

from .. import metadata
from ..models import CustomUser
from ..tasks import sync_storage_usage_to_stripe


class SyncStorageUsageTests(TestCase):
    def setUp(self):
        self.user = self._user("metered")
        self.customer = self.user.customer

    def _subscribe(self, meta, status="active", category=None, price_id=None, customer=None):
        customer = customer or self.customer
        product, _ = Product.objects.get_or_create(
            id=meta.stripe_id,
            defaults={
                "livemode": False,
                "active": True,
                "name": meta.name,
                "metadata": {"category": category or metadata.BASE_PLAN_CATEGORY},
            },
        )
        # The task resolves the metered price from the plan the user is on, so the
        # fixture has to carry that exact Stripe price id.
        price, _ = Price.objects.get_or_create(
            id=price_id or f"price_sync_{meta.stripe_id}",
            defaults={"livemode": False, "active": True, "product": product, "currency": "usd"},
        )
        subscription = Subscription.objects.create(
            id=f"sub_sync_{meta.stripe_id}_{status}_{customer.id}",
            livemode=False,
            created=timezone.now(),
            customer=customer,
            stripe_data={"status": status},
        )
        item = SubscriptionItem.objects.create(
            id=f"si_sync_{meta.stripe_id}_{status}_{customer.id}",
            livemode=False,
            created=timezone.now(),
            subscription=subscription,
            price=price,
        )
        return subscription, item

    def _customer(self, stripe_id, user=None):
        return Customer.objects.create(
            id=stripe_id,
            livemode=False,
            created=timezone.now(),
            subscriber=user or self.user,
        )

    def _user(self, username):
        user = get_user_model().objects.create_user(
            username=username, email=f"{username}@example.com", password="password"
        )
        user.customer = self._customer(f"cus_{username}", user=user)
        user.save(update_fields=["customer"])
        return user

    def _subscribe_metered(self, customer=None, status="active"):
        return self._subscribe(
            metadata.USAGE_BASED_STORAGE,
            status=status,
            category=metadata.STORAGE_PLAN_CATEGORY,
            price_id=self._free_metered_price(),
            customer=customer,
        )

    def _free_metered_price(self):
        return metadata.USAGE_BASED_STORAGE.price_ids[metadata.VERITY_FREE.stripe_id]

    def _run(self, create_event=None):
        create_event = create_event or mock.Mock()
        with (
            mock.patch("billing.services._configure"),
            mock.patch("billing.tasks._configure"),
            mock.patch("stripe.billing.MeterEvent.create", create_event),
        ):
            sync_storage_usage_to_stripe.fn()
        return create_event

    def test_task_runs_without_raising(self):
        # Regression: the ORM status filter made this raise FieldError, so no
        # meter event was ever sent.
        self._subscribe_metered()
        self._run()

    def test_metered_subscriber_gets_an_event(self):
        self._subscribe_metered()
        create_event = self._run()
        self.assertEqual(create_event.call_count, 1)
        payload = create_event.call_args.kwargs["payload"]
        self.assertEqual(payload["stripe_customer_id"], self.customer.id)
        self.assertEqual(create_event.call_args.kwargs["event_name"], "storage_usage")

    def test_reported_value_is_whole_megabytes(self):
        self._subscribe_metered()
        # The task reads the denormalised byte counter, so drive the real path.
        CustomUser.objects.filter(pk=self.user.pk).update(storage_used_bytes=10**9)
        create_event = self._run()
        payload = create_event.call_args.kwargs["payload"]
        # 10**9 bytes is one decimal GB, which is 1000 MB on the meter.
        self.assertEqual(payload["value"], "1000")

    def test_plan_subscriber_without_metered_storage_is_skipped(self):
        self._subscribe(metadata.VERITY_PRO)
        self.assertEqual(self._run().call_count, 0)

    def test_cancelled_metered_subscription_is_skipped(self):
        self._subscribe_metered(status="canceled")
        self.assertEqual(self._run().call_count, 0)

    def test_metered_subscription_on_a_linked_customer_is_billed_to_that_customer(self):
        # Regression: only one linked customer per subscriber was kept, so a metered
        # subscription on any other duplicate customer was skipped without an error.
        linked = [self._customer(f"cus_duplicate_{n}") for n in range(3)]
        self._subscribe_metered(linked[0])
        create_event = self._run()
        self.assertEqual(create_event.call_count, 1)
        self.assertEqual(
            create_event.call_args.kwargs["payload"]["stripe_customer_id"], linked[0].id
        )

    def test_subscriber_with_many_metered_items_gets_one_event(self):
        self._subscribe_metered()
        self._subscribe_metered(self._customer("cus_duplicate"))
        self.assertEqual(self._run().call_count, 1)

    def test_one_stripe_failure_does_not_stop_the_batch(self):
        self._subscribe_metered()
        self._subscribe_metered(self._user("second").customer)
        create_event = mock.Mock(side_effect=[stripe.error.APIConnectionError("down"), None])
        with self.assertRaises(RuntimeError):
            self._run(create_event)
        self.assertEqual(create_event.call_count, 2)

    def test_run_reports_completion(self):
        self._subscribe_metered()
        client = mock.MagicMock()
        with mock.patch("core.apps.posthog_client", client):
            self._run()
        client.capture.assert_called_once()
        (event,) = client.capture.call_args.args
        properties = client.capture.call_args.kwargs["properties"]
        self.assertEqual(event, "storage_usage_sync_completed")
        self.assertEqual(properties["candidates"], 1)
        self.assertEqual(properties["events_sent"], 1)
        self.assertEqual(properties["events_failed"], 0)
