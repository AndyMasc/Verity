from unittest import mock

import stripe
from django.contrib import admin
from django.test import TestCase
from djstripe.models import WebhookEndpoint

from ..admin import WebhookEndpointAdmin, WebhookEndpointEditForm


def _stripe_update_response(**overrides):
    # Stripe's update response never includes the signing secret
    data = {
        "id": "we_live",
        "object": "webhook_endpoint",
        "url": "https://example.com/stripe/webhook/",
        "status": "enabled",
        "enabled_events": ["*"],
        "metadata": {},
        "livemode": True,
        "api_version": "",
        "application": None,
        "created": 1758412800,
        "description": None,
    }
    data.update(overrides)
    return stripe.StripeObject.construct_from(data, "sk_test_ci")


class WebhookEndpointEditFormTests(TestCase):
    def setUp(self):
        patcher = mock.patch(
            "djstripe.models.Account.get_or_retrieve_for_api_key", return_value=None
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _create_endpoint(self, secret):
        return WebhookEndpoint.objects.create(
            id="we_live",
            url="https://example.com/stripe/webhook/",
            secret=secret,
            livemode=True,
            status="enabled",
            enabled_events=["*"],
        )

    def _submit(self, endpoint, response):
        form = WebhookEndpointEditForm(
            data={
                "base_url": "",
                "enabled_events": ["*"],
                "metadata": "{}",
                "djstripe_tolerance": 300,
                "djstripe_validation_method": "verify_signature",
                "enabled": "on",
            },
            instance=endpoint,
        )
        with mock.patch.object(WebhookEndpoint, "_api_update", return_value=response):
            self.assertTrue(form.is_valid(), form.errors)
            saved = form.save()
        saved.save()
        return saved

    def test_saves_endpoint_synced_without_secret(self):
        endpoint = self._create_endpoint(secret="")

        saved = self._submit(endpoint, _stripe_update_response(status="disabled"))

        saved.refresh_from_db()
        self.assertEqual(saved.secret, "")
        self.assertEqual(saved.djstripe_tolerance, 300)

    def test_keeps_stored_secret(self):
        endpoint = self._create_endpoint(secret="whsec_stored")

        saved = self._submit(endpoint, _stripe_update_response())

        saved.refresh_from_db()
        self.assertEqual(saved.secret, "whsec_stored")

    def test_admin_uses_override_for_existing_endpoints(self):
        model_admin = admin.site._registry[WebhookEndpoint]
        self.assertIsInstance(model_admin, WebhookEndpointAdmin)
        endpoint = self._create_endpoint(secret="")
        self.assertIs(model_admin.get_form(None, obj=endpoint), WebhookEndpointEditForm)
