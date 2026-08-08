import subprocess
import uuid

from django.core.management.base import BaseCommand
from djstripe.models import WebhookEndpoint


class Command(BaseCommand):
    help = "Automates a single local Stripe listener handling both Account and Connect scopes."

    def handle(self, *args, **options):
        # Fetch the temporary session secret
        secret_proc = subprocess.run(
            ["stripe", "listen", "--print-secret"], capture_output=True, text=True
        )
        secret = secret_proc.stdout.strip()

        if not secret.startswith("whsec_"):
            self.stderr.write(
                "Failed to fetch Stripe CLI secret. Make sure you are logged into 'stripe login'."
            )
            return

        # Flush old dev endpoints to prevent cluttering
        WebhookEndpoint.objects.filter(url__contains="localhost:8000").delete()

        # Create the automated database configurations
        u_account, u_connect = uuid.uuid4(), uuid.uuid4()

        # Row 1: Standard Billing
        WebhookEndpoint.objects.create(
            id=f"we_dev_{u_account.hex[:8]}",
            url=f"http://localhost:8000/stripe/webhook/{u_account}/",
            djstripe_uuid=u_account,
            secret=secret,
            livemode=False,
            status="enabled",
            enabled_events=["*"],
        )

        # Row 2: Connect Marketplace
        WebhookEndpoint.objects.create(
            id=f"we_dev_{u_connect.hex[:8]}",
            url=f"http://localhost:8000/stripe/webhook/{u_connect}/",
            djstripe_uuid=u_connect,
            secret=secret,
            livemode=False,
            status="enabled",
            enabled_events=["*"],
        )

        self.stdout.write(
            self.style.SUCCESS(
                f"Endpoints injected! Listening to account ({u_account}) and connect ({u_connect})..."
            )
        )

        # Trigger the multi-forwarding listener process
        subprocess.run(
            [
                "stripe",
                "listen",
                "--forward-to",
                f"http://localhost:8000/stripe/webhook/{u_account}/",
                "--forward-connect-to",
                f"http://localhost:8000/stripe/webhook/{u_connect}/",
            ]
        )
