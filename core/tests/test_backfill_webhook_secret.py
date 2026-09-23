from io import StringIO

from django.core.management import CommandError, call_command
from django.test import TestCase, override_settings
from djstripe.models import WebhookEndpoint


def _endpoint(endpoint_id, secret=""):
    return WebhookEndpoint.objects.create(
        id=endpoint_id,
        url=f"https://example.com/stripe/webhook/{endpoint_id}/",
        secret=secret,
        livemode=True,
        status="enabled",
        enabled_events=["*"],
    )


@override_settings(DJSTRIPE_WEBHOOK_SECRET="whsec_live")
class BackfillWebhookSecretTests(TestCase):
    def _run(self, *args):
        out = StringIO()
        call_command("backfill_webhook_secret", *args, stdout=out)
        return out.getvalue()

    def test_sets_secret_on_endpoint_without_one(self):
        endpoint = _endpoint("we_live")
        _endpoint("we_dev_1234", secret="")

        output = self._run()

        endpoint.refresh_from_db()
        self.assertEqual(endpoint.secret, "whsec_live")
        self.assertNotIn("whsec_live", output)
        self.assertEqual(WebhookEndpoint.objects.get(id="we_dev_1234").secret, "")

    def test_leaves_existing_secret_alone(self):
        endpoint = _endpoint("we_live", secret="whsec_other")

        output = self._run()

        endpoint.refresh_from_db()
        self.assertEqual(endpoint.secret, "whsec_other")
        self.assertIn("No webhook endpoint needs a secret", output)

    def test_requires_endpoint_when_several_lack_secret(self):
        _endpoint("we_a")
        _endpoint("we_b")

        with self.assertRaisesMessage(CommandError, "--endpoint"):
            self._run()

        self._run("--endpoint", "we_b")
        self.assertEqual(WebhookEndpoint.objects.get(id="we_a").secret, "")
        self.assertEqual(WebhookEndpoint.objects.get(id="we_b").secret, "whsec_live")

    def test_dry_run_does_not_save(self):
        endpoint = _endpoint("we_live")

        output = self._run("--dry-run")

        endpoint.refresh_from_db()
        self.assertEqual(endpoint.secret, "")
        self.assertIn("Would set", output)

    @override_settings(DJSTRIPE_WEBHOOK_SECRET="")
    def test_refuses_without_configured_secret(self):
        _endpoint("we_live")

        with self.assertRaisesMessage(CommandError, "STRIPE_WEBHOOK_SECRET"):
            self._run()
