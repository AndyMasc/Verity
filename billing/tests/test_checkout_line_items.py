"""The line items sent to Stripe Checkout.

Stripe enforces two opposite rules, so these tests assert the outbound payload
rather than the response:

* a licensed line item must carry "quantity"
* a metered line item must not, because usage arrives via the billing meter

Either mistake is a 400 from the API, and neither shows up in a response-only
test because the Stripe call is mocked.
"""

from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from djstripe.models import Customer, Price, Product

from .. import metadata
from .helpers import FakeSession, give_pro_subscription

PRO_PRICE = "price_pro_monthly"
FREE_PRICE = "price_free_monthly"
METERED_FREE = metadata.USAGE_BASED_STORAGE.price_id_for_plan(metadata.VERITY_FREE)
METERED_PAID = metadata.USAGE_BASED_STORAGE.price_id_for_plan(metadata.VERITY_PRO)


class CheckoutLineItemTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="checkout",
            email="checkout@example.com",
            password="password",
        )
        Customer.objects.create(id="cus_checkout", livemode=False, created=timezone.now())
        self.client.force_login(self.user)
        for meta, price_ids in (
            (metadata.VERITY_PRO, (PRO_PRICE,)),
            (metadata.VERITY_FREE, (FREE_PRICE,)),
        ):
            product = Product.objects.create(
                id=meta.stripe_id,
                livemode=False,
                active=True,
                name=meta.name,
                metadata={"category": metadata.BASE_PLAN_CATEGORY},
            )
            for price_id in price_ids:
                price = Price.objects.create(
                    id=price_id,
                    livemode=False,
                    active=True,
                    product=product,
                    currency="usd",
                )
                price.stripe_data = {"recurring": {"interval": "month"}}
                price.save()
        metered = Product.objects.create(
            id=metadata.USAGE_BASED_STORAGE.stripe_id,
            livemode=False,
            active=True,
            name=metadata.USAGE_BASED_STORAGE.name,
            metadata={"category": metadata.STORAGE_PLAN_CATEGORY},
        )
        for price_id in metadata.USAGE_BASED_STORAGE.price_ids.values():
            price = Price.objects.create(
                id=price_id,
                livemode=False,
                active=True,
                product=metered,
                currency="usd",
            )
            price.stripe_data = {"recurring": {"interval": "month"}}
            price.save()

    def _deactivate(self, price_id):
        Price.objects.filter(id=price_id).update(active=False)

    def _checkout(self, *price_ids):
        """Post the pricing form and return the line items handed to Stripe."""
        with (
            self.captureOnCommitCallbacks(execute=True),
            mock.patch(
                "billing.services.create_checkout_session",
                return_value=FakeSession(),
            ) as create_session,
        ):
            response = self.client.post(
                reverse("create_checkout_session"),
                {"price_ids": price_ids},
            )
        self.assertEqual(response.status_code, 302)
        return create_session.call_args.kwargs["line_items"]

    def test_a_plan_alone_carries_a_quantity(self):
        for price_id in (PRO_PRICE, FREE_PRICE):
            self.assertEqual(self._checkout(price_id), [{"price": price_id, "quantity": 1}])

    def test_metered_line_item_omits_quantity(self):
        (line_item,) = self._checkout(METERED_PAID)
        self.assertNotIn("quantity", line_item)

    def test_plan_and_usage_storage_together(self):
        self.assertEqual(
            self._checkout(PRO_PRICE, METERED_PAID),
            [
                {"price": PRO_PRICE, "quantity": 1},
                {"price": METERED_PAID},
            ],
        )

    def test_free_plan_buys_the_free_metered_rate(self):
        self.assertEqual(
            self._checkout(FREE_PRICE, METERED_FREE),
            [{"price": FREE_PRICE, "quantity": 1}, {"price": METERED_FREE}],
        )

    def test_downgrading_to_free_buys_the_free_metered_rate(self):
        """Selecting Free must not bill the metered rate of the plan being left."""
        give_pro_subscription(self.user)
        self.assertEqual(
            self._checkout(FREE_PRICE, METERED_PAID),
            [{"price": FREE_PRICE, "quantity": 1}, {"price": METERED_FREE}],
        )

    def test_metered_only_order_keeps_the_current_tier(self):
        give_pro_subscription(self.user)
        (metered,) = self._checkout(METERED_FREE)
        self.assertEqual(metered["price"], METERED_PAID)

    def test_unavailable_tier_price_is_rejected_not_substituted(self):
        """The tier price, not the selected one, is what Stripe charges."""
        self._deactivate(METERED_PAID)
        response = self.client.post(
            reverse("create_checkout_session"),
            {"price_ids": [FREE_PRICE, METERED_FREE]},
        )
        self.assertEqual(response.status_code, 400)

    def test_two_plans_are_rejected(self):
        response = self.client.post(
            reverse("create_checkout_session"),
            {"price_ids": [PRO_PRICE, FREE_PRICE]},
        )
        self.assertEqual(response.status_code, 400)
