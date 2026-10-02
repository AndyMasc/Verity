import subprocess  # nosec B404
import uuid

from django.core.management.base import BaseCommand
from djstripe.models import WebhookEndpoint


class Command(BaseCommand):
    help = "Automates a single local Stripe listener handling both Account and Connect scopes."

    def handle(self, *_args, **_options):
        # Fetch the temporary session secret
        secret_proc = subprocess.run(  # nosec B603, B607 - local dev helper wrapping the Stripe CLI
            ["stripe", "listen", "--print-secret"],  # noqa: S607
            capture_output=True,
            text=True,
        )
        secret = secret_proc.stdout.strip()

        if not secret.startswith("whsec_"):
            self.stderr.write(
                "Failed to fetch Stripe CLI secret. Make sure you are logged into 'stripe login'."
            )
            return

        WebhookEndpoint.objects.filter(url__contains="localhost:8000").delete()

        # One endpoint each for the platform account and the Connect account.
        u_account, u_connect = uuid.uuid4(), uuid.uuid4()
        for endpoint_uuid in (u_account, u_connect):
            WebhookEndpoint.objects.create(
                id=f"we_dev_{endpoint_uuid.hex[:8]}",
                url=f"http://localhost:8000/stripe/webhook/{endpoint_uuid}/",
                djstripe_uuid=endpoint_uuid,
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

        subprocess.run(  # noqa: S603  # nosec B603, B607 - local dev helper wrapping the Stripe CLI
            [  # noqa: S607
                "stripe",
                "listen",
                "--forward-to",
                f"http://localhost:8000/stripe/webhook/{u_account}/",
                "--forward-connect-to",
                f"http://localhost:8000/stripe/webhook/{u_connect}/",
            ]
        )
