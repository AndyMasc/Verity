"""Opening the Stripe billing portal when the stored customer is stale.

The view passed the stored customer ID straight to Stripe, so a customer deleted in
Stripe raised InvalidRequestError and the request failed with HTTP 500.
"""

from types import SimpleNamespace
from unittest import mock

import stripe
from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from djstripe.models import Customer

PORTAL_URL = "https://billing.stripe.com/p/session/test"


class CreatePortalSessionTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = get_user_model().objects.create_user(
            username="portal", email="portal@example.com", password="password"
        )
        self.customer = Customer.objects.create(
            id="cus_portal",
            livemode=False,
            created=timezone.now(),
            subscriber=self.user,
        )
        self.user.customer = self.customer
        self.user.save(update_fields=["customer"])
        self.client.force_login(self.user)
        self.url = reverse("portal_session")

    def _post(self, *, lookup=None, portal=None):
        lookup = lookup or mock.Mock(return_value=False)
        portal = portal or mock.Mock(return_value=SimpleNamespace(url=PORTAL_URL))
        with (
            mock.patch("billing.services.customer_missing_in_stripe", lookup),
            mock.patch("billing.services.create_billing_portal_session", portal),
        ):
            response = self.client.post(self.url)
        return response, portal

    def _messages(self, response):
        return [str(message) for message in get_messages(response.wsgi_request)]

    def test_valid_customer_redirects_to_portal(self):
        response, portal = self._post()
        self.assertRedirects(response, PORTAL_URL, fetch_redirect_response=False)
        portal.assert_called_once()
        self.assertEqual(portal.call_args.kwargs["customer"], "cus_portal")

    def test_customer_deleted_in_stripe_redirects_with_message(self):
        response, portal = self._post(lookup=mock.Mock(return_value=True))
        self.assertRedirects(response, reverse("pricing_page"), fetch_redirect_response=False)
        portal.assert_not_called()
        self.assertTrue(any("billing account" in message for message in self._messages(response)))
        # A new customer would have no subscriptions to manage.
        self.assertEqual(Customer.objects.count(), 1)

    def test_portal_invalid_request_error_redirects_with_message(self):
        portal = mock.Mock(
            side_effect=stripe.error.InvalidRequestError("No such customer", "customer")
        )
        response, _ = self._post(portal=portal)
        self.assertRedirects(response, reverse("core:profile_page"), fetch_redirect_response=False)
        self.assertTrue(any("billing portal" in message for message in self._messages(response)))

    def test_stripe_error_during_lookup_redirects_with_message(self):
        response, portal = self._post(
            lookup=mock.Mock(side_effect=stripe.error.APIConnectionError("timeout"))
        )
        self.assertRedirects(response, reverse("core:profile_page"), fetch_redirect_response=False)
        portal.assert_not_called()
