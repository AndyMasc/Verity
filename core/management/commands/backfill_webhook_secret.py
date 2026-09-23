from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError, CommandParser
from djstripe.models import WebhookEndpoint


class Command(BaseCommand):
    help = (
        "Copies STRIPE_WEBHOOK_SECRET onto a synced Stripe webhook endpoint that has no "
        "stored signing secret. Stripe only returns the secret when an endpoint is "
        "created, so endpoints created in the Stripe dashboard sync in without one."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--endpoint",
            help="Stripe ID (we_...) of the endpoint to update. Required if several lack a secret.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report the endpoint that would be updated without saving.",
        )

    def handle(self, *_args: Any, **options: Any) -> None:
        secret = settings.DJSTRIPE_WEBHOOK_SECRET
        if not secret.startswith("whsec_"):
            raise CommandError("STRIPE_WEBHOOK_SECRET is not set to a whsec_ signing secret.")

        # stripe_listen creates we_dev_* rows with the Stripe CLI's own secret
        candidates = WebhookEndpoint.objects.filter(
            secret=""  # nosec B106 - matches rows with no secret, not a hardcoded credential
        ).exclude(id__startswith="we_dev_")
        if options["endpoint"]:
            candidates = candidates.filter(id=options["endpoint"])

        endpoints = list(candidates)
        if not endpoints:
            self.stdout.write("No webhook endpoint needs a secret.")
            return
        if len(endpoints) > 1:
            ids = ", ".join(endpoint.id for endpoint in endpoints)
            raise CommandError(
                f"Several endpoints have no secret ({ids}). Pass --endpoint to pick the one "
                "that STRIPE_WEBHOOK_SECRET belongs to."
            )

        endpoint = endpoints[0]
        if options["dry_run"]:
            self.stdout.write(f"Would set the signing secret on {endpoint.id} ({endpoint.url}).")
            return

        endpoint.secret = secret
        endpoint.save(update_fields=["secret"])
        self.stdout.write(
            self.style.SUCCESS(f"Set the signing secret on {endpoint.id} ({endpoint.url}).")
        )
