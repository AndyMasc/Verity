from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from djstripe.models import Customer, Price, Product, Subscription, SubscriptionItem

from .. import entitlements, metadata
from ..tasks import sync_upload_overage_to_stripe


class SyncUploadOverageTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="metered",
            email="metered@example.com",
            password="password",
        )
        self.customer = Customer.objects.create(
            id="cus_metered",
            livemode=False,
            created=timezone.now(),
            subscriber=self.user,
        )
        self.user.customer = self.customer
        self.user.save(update_fields=["customer"])

    def _subscribe(self, product_id, status="active"):
        product = Product.objects.create(
            id=product_id, livemode=False, active=True, name=product_id
        )
        price = Price.objects.create(
            id=f"price_{product_id}",
            livemode=False,
            active=True,
            product=product,
            currency="usd",
        )
        subscription = Subscription.objects.create(
            id=f"sub_{product_id}_{status}",
            livemode=False,
            created=timezone.now(),
            customer=self.customer,
            stripe_data={"status": status},
        )
        SubscriptionItem.objects.create(
            id=f"si_{product_id}_{status}",
            livemode=False,
            created=timezone.now(),
            subscription=subscription,
            price=price,
        )
        return subscription

    def _over(self, count):
        for _ in range(count):
            entitlements.record_monthly_upload_usage(self.user, "upload")
        return self._run()

    def _run(self):
        with (
            mock.patch("billing.tasks._configure"),
            mock.patch("stripe.billing.MeterEvent.create") as create_event,
        ):
            sync_upload_overage_to_stripe.fn()
        return create_event

    def test_event_carries_customer_and_event_name(self):
        self._subscribe(metadata.USAGE_BASED_STORAGE.stripe_id)
        limit = entitlements.get_monthly_upload_limit(self.user, "upload")

        create_event = self._over(limit + 1)

        self.assertEqual(create_event.call_count, 1)
        self.assertEqual(create_event.call_args.kwargs["event_name"], "upload_overage")
        self.assertEqual(
            create_event.call_args.kwargs["payload"]["stripe_customer_id"], self.customer.id
        )

    def test_reported_value_is_the_overage(self):
        self._subscribe(metadata.USAGE_BASED_STORAGE.stripe_id)
        limit = entitlements.get_monthly_upload_limit(self.user, "upload")

        create_event = self._over(limit + 7)

        self.assertEqual(create_event.call_args.kwargs["payload"]["value"], "7")

    def test_within_allowance_sends_nothing(self):
        self._subscribe(metadata.USAGE_BASED_STORAGE.stripe_id)
        limit = entitlements.get_monthly_upload_limit(self.user, "upload")

        self.assertEqual(self._over(limit).call_count, 0)

    def test_no_metered_subscription_sends_nothing(self):
        self._subscribe(metadata.VERITY_PRO.stripe_id)
        self.assertEqual(self._over(100).call_count, 0)

    def test_cancelled_subscription_sends_nothing(self):
        self._subscribe(metadata.USAGE_BASED_STORAGE.stripe_id, status="canceled")
        self.assertEqual(self._over(100).call_count, 0)
