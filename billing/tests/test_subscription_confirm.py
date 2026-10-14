from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

from ..context_processors import _SUBSCRIPTION_STATUS_KEY
from .helpers import FakeSession


class SubscriptionConfirmTests(TestCase):
    """subscription_confirm validates the Checkout Session and redirects.

    It is deliberately not the path that writes state: the customer/subscription
    link is maintained by dj-stripe and the subscription sync by webhooks, so
    landing here before the webhook arrives is safe.
    """

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="andy",
            email="andy@example.com",
            password="password",
        )
        self.other_user = get_user_model().objects.create_user(
            username="wendy",
            email="wendy@example.com",
            password="password",
        )
        self.url = reverse("subscription_confirm")

    def _get(self, session, user=None):
        patcher = mock.patch(
            "stripe.checkout.Session.retrieve",
            return_value=session,
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        client = self.client
        client.force_login(user or self.user)
        return client.get(self.url, {"session_id": "cs_test"})

    def test_accepts_own_session(self):
        response = self._get(FakeSession(client_reference_id=str(self.user.pk)))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse("core:dashboard"))

    def test_accepts_zero_due_trial(self):
        # Stripe reports no_payment_required when nothing is charged up front.
        response = self._get(
            FakeSession(
                payment_status="no_payment_required",
                client_reference_id=str(self.user.pk),
            )
        )
        self.assertEqual(response.status_code, 302)

    def test_accepts_completed_metered_session_reported_unpaid(self):
        # A usage-based line has no fixed amount up front, so Stripe can leave
        # payment_status on "unpaid" even though the customer completed Checkout.
        response = self._get(
            FakeSession(
                payment_status="unpaid",
                client_reference_id=str(self.user.pk),
            )
        )
        self.assertEqual(response.status_code, 302)

    def test_rejects_another_users_session(self):
        response = self._get(FakeSession(client_reference_id=str(self.other_user.pk)))
        self.assertEqual(response.status_code, 400)

    def test_rejects_session_without_client_reference(self):
        response = self._get(FakeSession(client_reference_id=None))
        self.assertEqual(response.status_code, 400)

    def test_rejects_incomplete_session(self):
        response = self._get(
            FakeSession(
                status="open",
                payment_status="unpaid",
                client_reference_id=str(self.user.pk),
            )
        )
        self.assertEqual(response.status_code, 400)

    def test_rejects_non_subscription_checkout(self):
        response = self._get(
            FakeSession(
                subscription=None,
                client_reference_id=str(self.user.pk),
            )
        )
        self.assertEqual(response.status_code, 400)

    def test_confirm_invalidates_the_plan_cache(self):
        # The webhook that syncs the subscription lands after this redirect. If the
        # cache is not dropped here, the dashboard renders the pre-purchase plan and
        # holds it for the full 60s TTL.
        key = _SUBSCRIPTION_STATUS_KEY.format(user_id=self.user.pk)
        cache.set(key, {"plan_name": "Free"}, 60)

        response = self._get(FakeSession(client_reference_id=str(self.user.pk)))

        self.assertEqual(response.status_code, 302)
        self.assertIsNone(cache.get(key))

    def test_rejects_missing_session_id(self):
        patcher = mock.patch("stripe.checkout.Session.retrieve")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client.force_login(self.user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 400)
