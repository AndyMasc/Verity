from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import ROUND_DOWN, Decimal
from typing import TYPE_CHECKING, ClassVar

from django.conf import settings
from django.db import models, transaction
from django.db.models import Q, Sum
from django.db.models.functions import Concat
from django.utils import timezone

from core.currencies import (
    CURRENCY_CHOICES,
    DEFAULT_CURRENCY,
    format_currency,
    from_stripe_amount,
    to_stripe_amount,
)
from core.exchange_rates import convert as convert_currency
from core.exchange_rates import get_rates
from records.models import Record

if TYPE_CHECKING:
    from django.contrib.auth.base_user import AbstractBaseUser as User

logger = logging.getLogger(__name__)

STRIPE_MINIMUM_FEE_CENTS = 50
PLATFORM_FEE_PERCENT = Decimal("0.03")


@dataclass
class CheckoutItems:
    """Line items and totals for a Stripe Checkout Session."""

    line_items: list[dict] = field(default_factory=list)
    total_cents: int = 0
    total_amount: Decimal = Decimal("0.00")


@dataclass
class PackageDetailItems:
    """Per-record display values for the package detail page."""

    record_items: list[dict] = field(default_factory=list)
    converted_total: Decimal = Decimal("0.00")
    original_total: Decimal = Decimal("0.00")


class ReimbursementPackageQuerySet(models.QuerySet):
    def with_annotated_total(self):
        return self.annotate(
            _annotated_total=Sum("records__balance", filter=Q(records__is_active=True))
        )

    def with_prefetched_active_records(self):
        return self.prefetch_related(
            models.Prefetch("records", queryset=Record.objects.filter(is_active=True))
        )

    def create_for(
        self,
        *,
        creator,
        recipient,
        title,
        records,
        currency: str | None = None,
        days_valid: int = 7,
        recipient_email: str | None = None,
        status: str | None = None,
    ):
        """Create a package and attach the given (validated, owned, active) records.

        Returns the created package. The caller is responsible for input
        validation; this only builds the row and its record links atomically.
        "recipient_email" records the address the package was sent to, even
        when it resolves to a registered user.
        """
        package_currency = currency or getattr(
            getattr(creator, "settings", None), "default_currency", "usd"
        )
        with transaction.atomic():
            package = self.create(
                creator=creator,
                recipient=recipient,
                recipient_email=recipient_email or "",
                title=title,
                currency=package_currency,
                status=status or ReimbursementPackage.Status.OPEN,
                expires_at=timezone.now() + timedelta(days=days_valid),
            )
            package.records.set(records)
        return package


class StripeAccount(models.Model):
    """Holds Stripe Connect payment and onboarding information for a user."""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="stripe_account",
    )
    stripe_account_id = models.CharField(max_length=255, blank=True, default="")
    stripe_details_submitted = models.BooleanField(default=False)
    charges_enabled = models.BooleanField(default=False)
    payouts_enabled = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"Stripe Account for {self.user.email}"

    @property
    def is_active(self) -> bool:
        """Returns True if the user has completed Stripe onboarding and can receive payouts."""
        return bool(
            self.stripe_account_id
            and self.stripe_details_submitted
            and self.charges_enabled
            and self.payouts_enabled
        )

    def sync_from_stripe(self) -> bool:
        """Refresh onboarding flags from Stripe.

        Raises "stripe.error.StripeError" on API failure (the caller decides
        whether to surface or swallow it). Returns True once fully active.
        """
        from . import services

        live_account = services.retrieve_stripe_account(self.stripe_account_id)
        self.stripe_details_submitted = live_account.details_submitted
        self.charges_enabled = live_account.charges_enabled
        self.payouts_enabled = live_account.payouts_enabled
        self.save(
            update_fields=[
                "stripe_details_submitted",
                "charges_enabled",
                "payouts_enabled",
            ]
        )
        return self.is_active


class ReimbursementPackage(models.Model):
    class Status(models.TextChoices):
        QUEUED = "queued", "Awaiting Payer"
        OPEN = "open", "Open for Payment"
        PAID = "paid", "Fully Paid"

    uuid = models.UUIDField(default=uuid.uuid4, editable=False, unique=True, db_index=True)
    creator = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="reimbursement_packages",
    )
    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reimbursements_received",
    )
    recipient_email = models.EmailField(max_length=254, blank=True, default="", db_index=True)
    title = models.CharField(max_length=255)
    currency = models.CharField(
        max_length=3,
        choices=CURRENCY_CHOICES,
        default=DEFAULT_CURRENCY,
    )
    records = models.ManyToManyField(Record, related_name="packages")
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.OPEN, db_index=True
    )
    created_at = models.DateTimeField(auto_now_add=True)

    expires_at = models.DateTimeField(null=True, blank=True)

    paid_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reimbursements_paid",
    )
    paid_at = models.DateTimeField(null=True, blank=True)

    deleted_at = models.DateTimeField(null=True, blank=True, db_index=True)

    objects = ReimbursementPackageQuerySet.as_manager()

    _prefetched_converted_total: Decimal | None = None

    class Meta:
        ordering: ClassVar[list[str]] = ["-created_at"]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(fields=["status", "expires_at"]),
            models.Index(fields=["creator", "deleted_at"]),
            models.Index(fields=["recipient", "deleted_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.title} ({self.uuid})"

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None

    @property
    def recipient_address(self) -> str | None:
        """Email the package was sent to (registered user or external address)."""
        if self.recipient is not None:
            return self.recipient.email
        return self.recipient_email

    def activate(self) -> bool:
        """Transition a queued (external-recipient) package to open for payment.

        Returns True when the package was queued and is now open; False
        otherwise (already open, paid, or expired).
        """
        with transaction.atomic():
            locked = (
                ReimbursementPackage.objects.select_for_update(skip_locked=True)
                .filter(pk=self.pk, status=self.Status.QUEUED)
                .first()
            )
            if locked is None:
                return False
            locked.status = self.Status.OPEN
            locked.save(update_fields=["status"])
            self.status = locked.status
            return True

    def can_delete(self, user: User) -> bool:
        """Returns True if the given user is allowed to delete this package."""
        if self.deleted_at is not None:
            return False
        return user == self.creator or (self.status == self.Status.PAID and user == self.recipient)

    def delete_package(self, user: User) -> bool:
        """Soft-deletes the package. Returns True if successful, False if unauthorized."""
        if not self.can_delete(user):
            return False
        self.deleted_at = timezone.now()
        self.save(update_fields=["deleted_at"])
        try:
            from .services import revoke_package_access

            revoke_package_access(self)
        except Exception:
            logger.exception("Failed to revoke access for deleted package %s", self.uuid)
        return True

    def mark_as_paid(self, payer: User | None, payer_currency: str | None = None):
        """Marks package as paid, records who/when, updates linked records, and
        creates a reimbursement record for the payer (when the payer is a
        registered user; external payers have no account to attach it to).

        If "payer_currency" is given, the Record is created in that currency
        using converted amounts.  Falls back to the package currency.

        Idempotent: if already paid, this is a no-op.
        Uses select_for_update to prevent race conditions from concurrent webhooks.
        """
        record_currency = payer_currency or self.currency
        converted = self.converted_total(record_currency)

        with transaction.atomic():
            locked = (
                ReimbursementPackage.objects.select_for_update(skip_locked=True)
                .filter(pk=self.pk, status=self.Status.OPEN)
                .first()
            )
            if not locked:
                return
            locked.status = self.Status.PAID
            locked.paid_by = payer
            locked.paid_at = timezone.now()
            locked.save(update_fields=["status", "paid_by", "paid_at"])
            self.status = locked.status
            self.paid_by = locked.paid_by
            self.paid_at = locked.paid_at
            self.records.filter(is_active=True).update(reimbursed=True)
            if payer is not None:
                notes = (
                    f"Reimbursement package '{self.title}' paid to {self.creator.email}. "
                    f"Amount: {format_currency(converted, record_currency)}. "
                    f"Date: {locked.paid_at.strftime('%Y-%m-%d')}."
                )
                Record.objects.create(
                    user=payer,
                    title=f"Reimbursement: {self.title}",
                    transaction_date=locked.paid_at.strftime("%Y-%m-%d"),
                    merchant=self.creator.email,
                    balance=converted,
                    currency=record_currency,
                    record_type=Record.RecordTypes.EXPENSE_RECEIPT,
                    payment_method="Verity reimbursement transfer",
                    notes=notes,
                )
        # Access expires when the workflow ends: the recipient no longer needs
        # to review the records once the package is paid.
        try:
            from .services import revoke_package_access

            revoke_package_access(self)
        except Exception:
            logger.exception(
                "Failed to revoke package access after payment (package=%s)",
                getattr(self, "uuid", self.pk),
            )

    def mark_as_refunded(self):
        """Reverts a paid package back to open and un-marks linked records.

        Idempotent: if already open, this is a no-op.
        """
        with transaction.atomic():
            locked = (
                ReimbursementPackage.objects.select_for_update(skip_locked=True)
                .filter(pk=self.pk, status=self.Status.PAID)
                .first()
            )
            if not locked:
                return
            previous_payer = locked.paid_by
            locked.status = self.Status.OPEN
            locked.paid_by = None
            locked.paid_at = None
            locked.save(update_fields=["status", "paid_by", "paid_at"])
            self.status = locked.status
            self.paid_by = locked.paid_by
            self.paid_at = locked.paid_at
            self.records.filter(is_active=True).update(reimbursed=False)
            if previous_payer:
                Record.objects.filter(
                    user=previous_payer,
                    title=f"Reimbursement: {self.title}",
                    record_type=Record.RecordTypes.EXPENSE_RECEIPT,
                ).update(notes=Concat("notes", models.Value(" [REFUNDED]")))
        # Refunds reopen the workflow, so restore the recipient's access.
        try:
            from .services import _grant_package_access

            _grant_package_access(self)
        except Exception:
            logger.exception(
                "Failed to restore package access after refund (package=%s)",
                getattr(self, "uuid", self.pk),
            )

    @property
    def is_expired(self) -> bool:
        """Checks if the package has passed its expiration date."""
        if not self.expires_at:
            return False
        return timezone.now() > self.expires_at

    @property
    def total_amount(self) -> Decimal:
        if hasattr(self, "_annotated_total") and self._annotated_total is not None:
            return self._annotated_total
        return self.records.filter(is_active=True).exclude(balance__isnull=True).aggregate(
            total=Sum("balance")
        )["total"] or Decimal("0.00")

    @property
    def display_total(self) -> Decimal:
        if self._prefetched_converted_total is not None:
            return self._prefetched_converted_total
        return self.converted_total(self.currency)

    @property
    def total_amount_cents(self) -> int:
        return to_stripe_amount(self.total_amount, self.currency)

    def converted_total(self, to_currency: str | None = None) -> Decimal:
        target = to_currency or self.currency
        from core.exchange_rates import convert_batch

        cache = getattr(self, "_prefetched_objects_cache", {})
        if "records" in cache:
            items = [(r.balance, r.currency) for r in cache["records"] if r.is_active and r.balance]
        else:
            items = list(
                self.records.filter(is_active=True)
                .exclude(balance__isnull=True)
                .values_list("balance", "currency")
            )
        if not items:
            return Decimal("0.00")
        return convert_batch(items, target)

    def converted_total_cents(self, to_currency: str | None = None) -> int:
        target = to_currency or self.currency
        return to_stripe_amount(self.converted_total(target), target)

    def _active_records(self):
        cache = getattr(self, "_prefetched_objects_cache", {})
        if "records" in cache:
            return [r for r in cache["records"] if r.is_active]
        return list(self.records.filter(is_active=True))

    @property
    def payout_account_id(self) -> str | None:
        """Stripe Connect account that should receive the transfer, or None.

        The package creator (who is being reimbursed) is the transfer
        destination. Requires an active, onboarded Stripe account.
        """
        account = getattr(self.creator, "stripe_account", None)
        return account.stripe_account_id if account and account.is_active else None

    def can_be_paid_by(self, user: User) -> tuple[bool, str | None]:
        """Return "(ok, error_message)" describing whether "user" may pay.

        Covers the eligibility checks that don't need a row lock: the payer
        cannot be the package creator, the package must not be expired or
        already paid, and the creator must have an active payout account.
        """
        if user == self.creator:
            return False, "You cannot pay for your own reimbursement package."
        if self.is_expired:
            return False, "This reimbursement package has expired."
        if self.status == self.Status.PAID:
            return False, "This package has already been paid."
        if not self.payout_account_id:
            return (
                False,
                "This package's creator has not set up payouts yet. "
                "Please ask them to complete Stripe onboarding first.",
            )
        return True, None

    def lock_for_payment(self) -> ReimbursementPackage | None:
        """Atomically claim an open package for a new checkout.

        Returns the locked row, or None when a concurrent checkout already
        settled or claimed the package in the meantime.
        """
        return (
            ReimbursementPackage.objects.select_for_update()
            .filter(pk=self.pk, status=self.Status.OPEN)
            .first()
        )

    def resumable_session_url(self) -> str | None:
        """Return the URL of an existing in-progress checkout session, if still open.

        Returns None (after logging) when the previous session cannot be
        retrieved or has already completed, so the caller creates a new one.
        """
        from . import services

        payment = self.payments.filter(is_completed=False).order_by("-created_at").first()
        if not payment:
            return None
        try:
            session = services.retrieve_checkout_session(payment.stripe_checkout_session_id)
        except Exception:  # any Stripe API failure falls back to a new session
            logger.warning(
                "Failed to retrieve existing session %s, creating new one",
                payment.stripe_checkout_session_id,
            )
            return None
        if session.status == "open" and session.url:
            return session.url
        return None

    def build_line_items(self, payer_currency: str) -> CheckoutItems:
        """Build Stripe line items and totals for the payer's currency.

        Converts each active record balance into the payer's currency. Falls
        back to a single line item for the whole package when no individual
        record converts to a positive Stripe amount.
        """
        rates = get_rates("USD")
        line_items: list[dict] = []
        actual_total_cents = 0
        actual_total_amount = Decimal("0")

        for record in self.records.filter(is_active=True):
            if record.balance and record.balance > 0:
                converted = convert_currency(
                    record.balance, record.currency, payer_currency, rates=rates
                )
                converted_stripe = to_stripe_amount(converted, payer_currency)
                if converted_stripe <= 0:
                    continue

                product_data: dict = {"name": record.title or "Expense Item"}
                if getattr(record, "merchant", None):
                    product_data["description"] = f"Merchant: {record.merchant}"

                line_items.append(
                    {
                        "price_data": {
                            "currency": payer_currency,
                            "product_data": product_data,
                            "unit_amount": converted_stripe,
                        },
                        "quantity": 1,
                    }
                )
                actual_total_cents += converted_stripe
                actual_total_amount += converted

        if not line_items:
            fallback_cents = self.converted_total_cents(payer_currency)
            if fallback_cents <= 0:
                return CheckoutItems()
            line_items.append(
                {
                    "price_data": {
                        "currency": payer_currency,
                        "product_data": {"name": self.title},
                        "unit_amount": fallback_cents,
                    },
                    "quantity": 1,
                }
            )
            actual_total_cents = fallback_cents
            actual_total_amount = self.converted_total(payer_currency)

        return CheckoutItems(
            line_items=line_items,
            total_cents=actual_total_cents,
            total_amount=actual_total_amount,
        )

    def platform_fee_cents(self, total_cents: int, payer_currency: str, rates) -> int:
        """Compute the platform fee for a Connect transfer, clamped to the
        converted Stripe minimum and the payment total."""
        platform_fee_cents = int(
            (Decimal(str(total_cents)) * PLATFORM_FEE_PERCENT).quantize(
                Decimal("1"), rounding=ROUND_DOWN
            )
        )
        min_fee_converted = convert_currency(
            Decimal(STRIPE_MINIMUM_FEE_CENTS) / Decimal("100"),
            "usd",
            payer_currency,
            rates=rates,
        )
        min_fee_units = to_stripe_amount(min_fee_converted, payer_currency)

        if platform_fee_cents < min_fee_units:
            platform_fee_cents = min_fee_units
        if platform_fee_cents > total_cents:
            platform_fee_cents = total_cents
        return platform_fee_cents

    def detail_items(self, user_currency: str) -> PackageDetailItems:
        """Compute per-record display values for the package detail page.

        Compares each record's originally requested amount (from its first
        history entry) against the current balance, both converted to the
        viewer's currency.
        """
        all_records = list(self.records.all())
        user_rates = get_rates("USD")
        record_items: list[dict] = []
        converted_total = Decimal("0")
        original_total = Decimal("0")

        if all_records:
            HistoricalRecord = Record.history.model
            record_ids = [r.id for r in all_records]
            first_histories: dict[int, object] = {}
            for h in HistoricalRecord.objects.filter(id__in=record_ids).order_by("history_date"):
                if h.id not in first_histories:
                    first_histories[h.id] = h

            for rec in all_records:
                first = first_histories.get(rec.id)
                orig_bal = first.balance if first else rec.balance
                orig_cc = first.currency if first else rec.currency

                orig_converted = convert_currency(
                    orig_bal, orig_cc, user_currency, rates=user_rates
                )
                current_converted = (
                    convert_currency(rec.balance, rec.currency, user_currency, rates=user_rates)
                    if rec.balance
                    else orig_converted
                )

                converted_total += current_converted
                original_total += convert_currency(
                    orig_bal, orig_cc, self.currency, rates=user_rates
                )

                record_items.append(
                    {
                        "record": rec,
                        "original_converted": orig_converted,
                        "requested_converted": current_converted,
                        "converted_currency": user_currency,
                        "is_inactive": not rec.is_active,
                    }
                )

        return PackageDetailItems(
            record_items=record_items,
            converted_total=converted_total,
            original_total=original_total,
        )

    @classmethod
    def prefetch_converted_totals(
        cls, packages: list[ReimbursementPackage], to_currency: str
    ) -> list[ReimbursementPackage]:
        """Precompute each package's converted display total in one pass.

        Mutates "_prefetched_converted_total" on the given instances so
        "display_total" avoids per-record conversion queries on the list page.
        """
        if not packages:
            return packages
        rates = get_rates("USD")
        for pkg in packages:
            active = [r for r in pkg.records.all() if r.is_active and r.balance]
            if not active:
                pkg._prefetched_converted_total = Decimal("0.00")
                continue
            total = Decimal("0.00")
            for r in active:
                total += convert_currency(r.balance, r.currency, to_currency, rates=rates)
            pkg._prefetched_converted_total = total
        return packages


class ProcessedStripeEvent(models.Model):
    """Records Stripe webhook events already applied to the reimbursements flow.

    Stripe redelivers webhook events and Dramatiq retries on transient failure, so
    event handling must be idempotent. The event id is the stable key across
    every delivery/retry of the same event.
    """

    event_id = models.CharField(max_length=255, primary_key=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return self.event_id


class PackagePayment(models.Model):
    package = models.ForeignKey(
        ReimbursementPackage, on_delete=models.CASCADE, related_name="payments"
    )
    payer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="payments_made",
    )
    stripe_checkout_session_id = models.CharField(max_length=255, unique=True)
    stripe_payment_intent_id = models.CharField(
        max_length=255, blank=True, default="", db_index=True
    )
    amount_paid = models.DecimalField(max_digits=12, decimal_places=2)
    payer_currency = models.CharField(max_length=3, default=DEFAULT_CURRENCY)
    is_completed = models.BooleanField(default=False, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return f"Payment {self.amount_paid} {self.payer_currency} for {self.package_id} (Paid: {self.is_completed})"

    def amount_matches(self, session) -> bool:
        """Cross-check the settled checkout amount against the expected total.

        Prevents a session for a different/edited amount from being treated as
        a completed payment. A small tolerance absorbs the rounding difference
        between Stripe's per-line-item cent rounding and the stored converted
        total.
        """
        session_currency = (session.get("currency") or self.payer_currency).lower()
        amount_total = session.get("amount_total")
        if session_currency != self.payer_currency.lower():
            logger.error(
                "Package %s: session %s currency mismatch (%s vs %s) — refusing to mark as paid",
                self.package_id,
                session.get("id"),
                session_currency,
                self.payer_currency,
            )
            return False
        if amount_total is None:
            logger.error(
                "Package %s: session %s has no amount_total — refusing to mark as paid",
                self.package_id,
                session.get("id"),
            )
            return False
        settled = from_stripe_amount(amount_total, session_currency)
        expected = self.amount_paid
        tolerance = max(Decimal("0.02"), expected * Decimal("0.01"))
        if abs(settled - expected) > tolerance:
            logger.error(
                "Package %s: session %s settled amount %s %s does not match expected %s %s — refusing to mark as paid",
                self.package_id,
                session.get("id"),
                settled,
                session_currency,
                expected,
                self.payer_currency,
            )
            return False
        return True

    def complete_from_session(self, session) -> None:
        """Mark this payment completed, storing the payment intent from the session.

        Accepts either a dict (normalized webhook payload) or a
        "stripe.CheckoutSession" object (direct API retrieval).
        """
        self.is_completed = True
        payment_intent_id = (
            session.get("payment_intent")
            if isinstance(session, dict)
            else getattr(session, "payment_intent", None)
        )
        if payment_intent_id:
            self.stripe_payment_intent_id = payment_intent_id
        self.save(update_fields=["is_completed", "stripe_payment_intent_id"])

    def mark_failed(self) -> None:
        """Mark this payment as not completed (failed/refunded)."""
        self.is_completed = False
        self.save(update_fields=["is_completed"])


class PackageEmailVerification(models.Model):
    """One-time email verification for external (unauthenticated) payers.

    External recipients prove they are the intended recipient by entering the
    code emailed to the package's recipient address before they can view the
    package or pay. Only the latest issued code is stored (hashed), with an
    attempt budget and expiry to resist brute force.
    """

    package = models.OneToOneField(
        ReimbursementPackage,
        on_delete=models.CASCADE,
        related_name="email_verification",
    )
    email = models.EmailField(max_length=254)
    code_hash = models.CharField(max_length=64)
    salt = models.UUIDField(default=uuid.uuid4, editable=False)
    attempts = models.PositiveIntegerField(default=0)
    expires_at = models.DateTimeField()
    verified_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"Verification for package {self.package_id} ({self.email})"

    @property
    def is_expired(self) -> bool:
        return timezone.now() > self.expires_at
