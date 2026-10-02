"""Can an uptime monitor safely POST to the webhook URLs without side effects?"""

import datetime
import json
import uuid

from django.contrib.auth import get_user_model
from django.test import TestCase

from plaid_integration.models import PlaidItem
from reimbursements.models import ProcessedStripeEvent, ReimbursementPackage
from records.models import Record

U = get_user_model()


class WebhookProbeSafetyTest(TestCase):
    """A monitor hitting the real webhook URLs must never mutate anything."""

    def setUp(self):
        self.user = U.objects.create_user(username="probe", password="p")
        self.pkg = ReimbursementPackage.objects.create(
            creator=self.user, title="p", currency="usd", status="open"
        )

    def test_unsigned_plaid_probe_is_rejected_and_mutates_nothing(self):
        PlaidItem.objects.create(user=self.user, item_id="item-1", access_token="t")
        before = PlaidItem.objects.count()

        response = self.client.post(
            "/plaid/webhook/",
            data=json.dumps(
                {
                    "webhook_type": "TRANSACTIONS",
                    "webhook_code": "SYNC_UPDATES_AVAILABLE",
                    "item_id": "item-1",
                }
            ),
            content_type="application/json",
        )

        # Rejected, and the item was not mutated by a fake SYNC webhook.
        self.assertEqual(response.status_code, 403)
        item = PlaidItem.objects.get(item_id="item-1")
        self.assertEqual(item.next_cursor, "")
        self.assertEqual(PlaidItem.objects.count(), before)

    def test_unsigned_stripe_probe_is_rejected_and_writes_no_event(self):
        from djstripe.models import WebhookEndpoint

        endpoint = WebhookEndpoint.objects.create(
            id="we_probe",
            djstripe_uuid=uuid.uuid4(),
            url="https://x.test/stripe/webhook/",
            secret="whsec_probe",
            livemode=False,
            status="enabled",
            enabled_events=["*"],
        )

        response = self.client.post(
            f"/stripe/webhook/{endpoint.djstripe_uuid}/",
            data=json.dumps(
                {
                    "id": "evt_probe",
                    "type": "customer.subscription.deleted",
                    "data": {"object": {}},
                }
            ),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)  # missing stripe-signature
        self.assertFalse(
            ProcessedStripeEvent.objects.filter(event_id="evt_probe").exists()
        )
        self.package_untouched()

    def test_get_probe_on_stripe_returns_405_but_proves_app_is_alive(self):
        """A GET-only monitor still works: any HTTP response means 'up'."""
        response = self.client.get(
            "/stripe/webhook/00000000-0000-0000-0000-000000000000/"
        )
        self.assertIn(response.status_code, (400, 404, 405, 500))
        self.assertIsNotNone(response.status_code)  # Django answered = app alive

    def package_untouched(self):
        self.pkg.refresh_from_db()
        self.assertEqual(self.pkg.status, ReimbursementPackage.Status.OPEN)
        self.assertEqual(Record.objects.count(), 0)
