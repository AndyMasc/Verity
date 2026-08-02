import logging

import stripe
from django.conf import settings
from django.core.management.base import BaseCommand

from reimbursements.models import StripeAccount

stripe.api_key = settings.STRIPE_SECRET_KEY
logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Backfill charges_enabled/payouts_enabled on existing StripeAccount rows from Stripe API"

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Log what would be updated without modifying the database",
        )

    def handle(self, *args, **options):  # noqa: ARG002 - Django BaseCommand signature
        dry_run = options["dry_run"]
        accounts = StripeAccount.objects.filter(
            stripe_account_id__isnull=False,
        ).exclude(stripe_account_id="")

        if not accounts.exists():
            self.stdout.write(self.style.WARNING("No StripeAccount records to backfill"))
            return

        updated = 0
        failed = 0

        for acct in accounts:
            try:
                sa = stripe.Account.retrieve(acct.stripe_account_id)
            except stripe.error.StripeError as e:
                self.stderr.write(
                    self.style.ERROR(
                        f"Failed to retrieve account {acct.stripe_account_id} for user {acct.user_id}: {e}"
                    )
                )
                failed += 1
                continue

            charges_enabled = getattr(sa, "charges_enabled", False)
            payouts_enabled = getattr(sa, "payouts_enabled", False)

            if dry_run:
                self.stdout.write(
                    f"[DRY-RUN] Would set user={acct.user_id} "
                    f"charges_enabled={charges_enabled} "
                    f"payouts_enabled={payouts_enabled}"
                )
            else:
                StripeAccount.objects.filter(pk=acct.pk).update(
                    charges_enabled=charges_enabled,
                    payouts_enabled=payouts_enabled,
                )
                self.stdout.write(
                    f"Updated user={acct.user_id} "
                    f"charges_enabled={charges_enabled} "
                    f"payouts_enabled={payouts_enabled}"
                )
            updated += 1

        self.stdout.write(self.style.SUCCESS(f"Done. {updated} processed, {failed} failed."))
