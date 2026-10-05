from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from djstripe.models import Customer, Price, Product

from .. import metadata

from .helpers import FakeSession


class PricingPageTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="pricing-user",
            email="pricing-user@example.com",
            password="password",
        )
        self.client.force_login(self.user)
        self.url = reverse("pricing_page")

    def test_plan_view_tracking_root_is_rendered(self):
        response = self.client.get(self.url)
        content = response.content.decode()
        self.assertIn("data-plan-view-tracking", content)
        self.assertIn("[data-plan-view-tracking]", content)

    def test_canceled_checkout_shows_message(self):
        response = self.client.get(self.url, {"checkout": "canceled"})
        messages = [str(m) for m in get_messages(response.wsgi_request)]
        self.assertEqual(len(messages), 1)
        self.assertIn("Checkout canceled", messages[0])

    def test_plain_visit_shows_no_message(self):
        response = self.client.get(self.url)
        self.assertEqual(list(get_messages(response.wsgi_request)), [])


class CheckoutCancelUrlTests(TestCase):
    def test_cancel_url_marks_checkout_as_canceled(self):
        user = get_user_model().objects.create_user(
            username="checkout-user",
            email="checkout-user@example.com",
            password="password",
        )
        customer = Customer.objects.create(
            id="cus_checkout", livemode=False, created=timezone.now()
        )
        self.client.force_login(user)

        # A real price, because checkout resolves the product to decide whether the
        # line item is metered.
        product = Product.objects.create(
            id=metadata.VERITY_PRO.stripe_id,
            livemode=False,
            active=True,
            name="Verity Pro",
        )
        Price.objects.create(
            id="price_pro",
            livemode=False,
            active=True,
            product=product,
            currency="usd",
        )

        with (
            mock.patch("billing.views.Customer.get_or_create", return_value=(customer, True)),
            mock.patch("billing.services.customer_missing_in_stripe", return_value=False),
            mock.patch(
                "billing.services.create_checkout_session",
                return_value=FakeSession(),
            ) as create_session,
        ):
            response = self.client.post(
                reverse("create_checkout_session"), {"price_ids": "price_pro"}
            )

        self.assertEqual(response.status_code, 302)
        cancel_url = create_session.call_args.kwargs["cancel_url"]
        self.assertTrue(cancel_url.endswith(reverse("pricing_page") + "?checkout=canceled"))
