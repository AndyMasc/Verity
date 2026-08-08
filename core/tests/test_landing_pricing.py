"""Regression tests for the pricing section on the public landing page.

The landing page must render the pricing cards (they were silently blank when
the view stopped passing ``pricing_context``) and must not send anonymous
visitors into the login-required checkout flow.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse


class LandingPagePricingTests(TestCase):
    def test_landing_page_renders_pricing_section(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="pricing"')
        self.assertContains(response, "Plans & Pricing")
        # The free plan card renders even with no synced Stripe products.
        self.assertContains(response, "Free")

    def test_landing_page_anonymous_visitor_sees_signup_cta_not_checkout(self):
        response = self.client.get("/")
        self.assertContains(response, "Sign up to subscribe")
        self.assertNotContains(response, "Continue to checkout")

    def test_pricing_page_shows_checkout_for_authenticated_user(self):
        User = get_user_model()
        user = User.objects.create_user(
            username="pricing-tester",
            email="pricing-tester@example.com",
            password="testpass123",
        )
        self.client.force_login(user)
        response = self.client.get(reverse("pricing_page"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Continue to checkout")
        self.assertNotContains(response, "Sign up to subscribe")
