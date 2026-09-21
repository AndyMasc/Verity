from django.contrib.auth import get_user_model
from django.test import TestCase
from djstripe.models import Price, Product

from .. import metadata, services


class CheckoutPriceIdTests(TestCase):
    """_checkout_price_id must select the active monthly price the card shows."""

    def setUp(self):
        self.product = Product.objects.create(
            id=metadata.VERITY_PRO.stripe_id,
            livemode=False,
            active=True,
            name="Verity Pro",
        )

    def _price(self, price_id, *, active=True, interval="month"):
        return Price.objects.create(
            id=price_id,
            livemode=False,
            active=active,
            product=self.product,
            currency="usd",
            recurring={"interval": interval} if interval else None,
        )

    def test_picks_newest_active_monthly_price(self):
        self._price("price_old")
        newest = self._price("price_new")
        self.assertEqual(services._checkout_price_id(self.product), newest.id)

    def test_ignores_archived_annual_response_archived_and_one_time(self):
        self._price("price_archived", active=False)
        self._price("price_annual", interval="year")
        self._price("price_one_time", interval=None)
        monthly = self._price("price_monthly")
        self.assertEqual(services._checkout_price_id(self.product), monthly.id)

    def test_none_when_only_one_time_or_inactive_prices(self):
        self._price("price_one_time", interval=None)
        self.assertIsNone(services._checkout_price_id(self.product))
        self.product.prices.all().delete()
        self._price("price_archived", active=False)
        self.assertIsNone(services._checkout_price_id(self.product))

    def test_storage_pro_only_product_uses_active_monthly_price(self):
        product = Product.objects.create(
            id=metadata.STORAGE_UPGRADE_10.stripe_id,
            livemode=False,
            active=True,
            name="10 GB Storage Pack",
        )
        current = Price.objects.create(
            id="price_10",
            livemode=False,
            active=True,
            product=product,
            currency="usd",
            recurring={"interval": "month"},
        )
        Price.objects.create(
            id="price_10_archived",
            livemode=False,
            active=False,
            product=product,
            currency="usd",
            recurring={"interval": "month"},
        )
        self.assertEqual(services._checkout_price_id(product), current.id)


class PricingContextTests(TestCase):
    """pricing_context attaches a deterministic checkout price per product."""

    def _user(self):
        return get_user_model().objects.create_user(
            username="pricing_user",
            email="pricing@example.com",
            password="password",
        )

    def test_base_plan_card_gets_active_monthly_price(self):
        Product.objects.create(
            id=metadata.VERITY_PRO.stripe_id,
            livemode=False,
            active=True,
            name="Verity Pro",
            metadata={"category": "base_plan"},
        )
        Price.objects.create(
            id="price_keep",
            livemode=False,
            active=True,
            product=Product.objects.get(id=metadata.VERITY_PRO.stripe_id),
            currency="usd",
            recurring={"interval": "month"},
        )
        Price.objects.create(
            id="price_archived",
            livemode=False,
            active=False,
            product=Product.objects.get(id=metadata.VERITY_PRO.stripe_id),
            currency="usd",
            recurring={"interval": "month"},
        )

        context = services.pricing_context(self._user())
        pro_card = next(p for p in context["base_plans"] if p.id == metadata.VERITY_PRO.stripe_id)
        self.assertEqual(pro_card.checkout_price_id, "price_keep")
