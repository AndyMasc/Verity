from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from djstripe.models import Customer, Price, Product, Subscription, SubscriptionItem

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
        price = Price.objects.create(
            id=price_id,
            livemode=False,
            active=active,
            product=self.product,
            currency="usd",
        )
        price.stripe_data = {"recurring": {"interval": interval} if interval else None}
        price.save(update_fields=["stripe_data"])
        return price

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
        )
        current.stripe_data = {"recurring": {"interval": "month"}}
        current.save(update_fields=["stripe_data"])

        archived = Price.objects.create(
            id="price_10_archived",
            livemode=False,
            active=False,
            product=product,
            currency="usd",
        )
        archived.stripe_data = {"recurring": {"interval": "month"}}
        archived.save(update_fields=["stripe_data"])

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
        keep = Price.objects.create(
            id="price_keep",
            livemode=False,
            active=True,
            product=Product.objects.get(id=metadata.VERITY_PRO.stripe_id),
            currency="usd",
        )
        keep.stripe_data = {"recurring": {"interval": "month"}}
        keep.save(update_fields=["stripe_data"])

        archived = Price.objects.create(
            id="price_archived",
            livemode=False,
            active=False,
            product=Product.objects.get(id=metadata.VERITY_PRO.stripe_id),
            currency="usd",
        )
        archived.stripe_data = {"recurring": {"interval": "month"}}
        archived.save(update_fields=["stripe_data"])

        context = services.pricing_context(self._user())
        pro_card = next(p for p in context["base_plans"] if p.id == metadata.VERITY_PRO.stripe_id)
        self.assertEqual(pro_card.checkout_price_id, "price_keep")


class AlreadyActiveTests(TestCase):
    """Plans the user already holds are flagged so the UI can disable them."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="owner",
            email="owner@example.com",
            password="password",
        )
        self.customer = Customer.objects.create(
            id="cus_owner", livemode=False, created=timezone.now()
        )
        self.user.customer = self.customer
        self.user.save(update_fields=["customer"])

    def _product(self, meta):
        product, _ = Product.objects.get_or_create(
            id=meta.stripe_id,
            livemode=False,
            defaults={
                "active": True,
                "name": meta.name,
                "metadata": {"category": meta.category},
            },
        )
        price, _ = Price.objects.get_or_create(
            id="price_" + meta.stripe_id.replace("prod_", ""),
            livemode=False,
            defaults={
                "active": True,
                "product": product,
                "currency": "usd",
            },
        )
        price.stripe_data = {"recurring": {"interval": "month"}}
        price.save(update_fields=["stripe_data"])
        return product

    def _subscribe(self, meta):
        self._product(meta)
        price = Price.objects.get(id="price_" + meta.stripe_id.replace("prod_", ""))
        subscription = Subscription.objects.create(
            id=f"sub_{meta.stripe_id}",
            livemode=False,
            created=timezone.now(),
            customer=self.customer,
            stripe_data={"status": "active"},
        )
        SubscriptionItem.objects.create(
            id=f"si_{meta.stripe_id}",
            livemode=False,
            created=timezone.now(),
            subscription=subscription,
            price=price,
        )

    def _card(self, context, meta):
        return next(
            p for p in context["base_plans"] + context["storage_plans"] if p.id == meta.stripe_id
        )

    def test_free_user_has_no_active_plan(self):
        self._product(metadata.VERITY_PRO)
        context = services.pricing_context(self.user)
        self.assertFalse(self._card(context, metadata.VERITY_PRO).already_active)

    def test_held_base_plan_is_marked_active(self):
        self._product(metadata.STORAGE_UPGRADE_10)
        self._subscribe(metadata.VERITY_PRO)
        context = services.pricing_context(self.user)
        self.assertTrue(self._card(context, metadata.VERITY_PRO).already_active)
        self.assertFalse(self._card(context, metadata.STORAGE_UPGRADE_10).already_active)

    def test_held_storage_plan_is_marked_active(self):
        self._product(metadata.VERITY_PRO)
        self._subscribe(metadata.VERITY_PRO)
        self._subscribe(metadata.STORAGE_UPGRADE_10)
        context = services.pricing_context(self.user)
        self.assertTrue(self._card(context, metadata.VERITY_PRO).already_active)
        self.assertTrue(self._card(context, metadata.STORAGE_UPGRADE_10).already_active)
