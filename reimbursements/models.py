from __future__ import annotations

import hashlib
import logging
import uuid
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
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
    to_stripe_amount,
)
from records.models import Record

from . import pricing
from .fees import PlatformFeeCalculator
from .pricing import CheckoutItems, PackageDetailItems

if TYPE_CHECKING:
    from django.contrib.auth.base_user import AbstractBaseUser as User

logger = logging.getLogger(__name__)


def one_week_from_now():
    return timezone.now() + timedelta(days=7)


@dataclass(frozen=True)
class PackageDraft:
    creator: User
    recipient: User | None
    title: str
    records: models.QuerySet
    currency: str | None = None
    days_valid: int = 7
    recipient_email: str | None = None
    status: str | None = None

    def resolve_currency(self) -> str:
        return self.currency or getattr(
            getattr(self.creator, "settings", None), "default_currency", "usd")


class ReimbursementPackageQuerySet(models.QuerySet):

    def with_annotated_total(self):
        return self.annotate(_annotated_total=Sum(
            "records__balance", filter=Q(records__is_active=True)))

    def with_prefetched_active_records(self):
        return self.prefetch_related(
            models.Prefetch("records",
                            queryset=Record.objects.filter(is_active=True)))

    def create_for(self, draft: PackageDraft):
        with transaction.atomic():
            package = self.create(
                creator=draft.creator,
                recipient=draft.recipient,
                recipient_email=draft.recipient_email or "",
                title=draft.title,
                currency=draft.resolve_currency(),
                status=draft.status or ReimbursementPackage.Status.OPEN,
                expires_at=timezone.now() + timedelta(days=draft.days_valid),
            )
            package.records.set(draft.records)
        return package


class StripeAccount(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="stripe_account",
    )
    stripe_account_id = models.CharField(max_length=255,
                                         blank=True,
                                         default="")
    stripe_details_submitted = models.BooleanField(default=False)
    charges_enabled = models.BooleanField(default=False)
    payouts_enabled = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"Stripe Account for {self.user.email}"

    @property
    def is_active(self) -> bool:
        return bool(self.stripe_account_id and self.stripe_details_submitted
                    and self.charges_enabled and self.payouts_enabled)

    def sync_from_stripe(self) -> bool:
        from . import services

        live_account = services.retrieve_stripe_account(self.stripe_account_id)
        self.stripe_details_submitted = live_account.details_submitted
        self.charges_enabled = live_account.charges_enabled
        self.payouts_enabled = live_account.payouts_enabled
        self.save(update_fields=[
            "stripe_details_submitted",
            "charges_enabled",
            "payouts_enabled",
        ])
        return self.is_active


def external_payer_distinct_id(email: str) -> str:
    """A stable id for a payer who has no account.
    The address is hashed rather than sent, so the analytics
    identifier is not a copy of a real one and cannot be reversed to it.
    """
    normalised = email.strip().lower()
    digest = hashlib.sha256(normalised.encode()).hexdigest()[:32]
    return f"external-payer-{digest}"


def capture_recipient_paid(locked, payer, shared) -> None:
    from core.apps import posthog_client

    if posthog_client is None:
        return

    properties = {
        **shared,
        "package_uuid": str(locked.uuid),
    }
    if payer is not None:
        posthog_client.capture(
            "recipient_paid_reimbursement",
            distinct_id=str(payer.pk),
            properties={
                **properties, "payer_type": "registered"
            },
        )
        return

    email = (getattr(getattr(locked, "email_verification", None), "email", "")
             or locked.recipient_email)
    if not email:
        # Nothing identifies this payer, so leave the event anonymous rather
        # than attributing it to the wrong person.
        posthog_client.capture("recipient_paid_reimbursement",
                               properties=properties)
        return
    posthog_client.capture(
        "recipient_paid_reimbursement",
        distinct_id=external_payer_distinct_id(email),
        properties={
            **properties, "payer_type": "external"
        },
    )


class ReimbursementPackage(models.Model):

    class Status(models.TextChoices):
        QUEUED = "queued", "Awaiting Payer"
        OPEN = "open", "Open for Payment"
        PAID = "paid", "Fully Paid"

    uuid = models.UUIDField(default=uuid.uuid4,
                            editable=False,
                            unique=True,
                            db_index=True)
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
    recipient_email = models.EmailField(max_length=254,
                                        blank=True,
                                        default="",
                                        db_index=True)
    title = models.CharField(max_length=255)
    currency = models.CharField(
        max_length=3,
        choices=CURRENCY_CHOICES,
        default=DEFAULT_CURRENCY,
    )
    records = models.ManyToManyField(Record, related_name="packages")
    status = models.CharField(max_length=20,
                              choices=Status.choices,
                              default=Status.OPEN,
                              db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    expires_at = models.DateTimeField(default=one_week_from_now)

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
        if self.recipient is not None:
            return self.recipient.email
        return self.recipient_email

    def activate(self) -> bool:
        with transaction.atomic():
            locked = (ReimbursementPackage.objects.select_for_update(
                skip_locked=True).filter(pk=self.pk,
                                         status=self.Status.QUEUED).first())
            if locked is None:
                return False
            locked.status = self.Status.OPEN
            locked.save(update_fields=["status"])
            self.status = locked.status
            return True

    def can_delete(self, user: User) -> bool:
        if self.deleted_at is not None:
            return False
        return user == self.creator or (self.status == self.Status.PAID
                                        and user == self.recipient)

    def delete_package(self, user: User) -> bool:
        if not self.can_delete(user):
            return False
        from .services import revoke_package_access

        try:
            revoke_package_access(self)
        except Exception:
            logger.exception(
                "Failed to revoke access for package %s; leaving it undeleted",
                self.uuid,
            )
            return False
        self.deleted_at = timezone.now()
        self.save(update_fields=["deleted_at"])
        return True

    def mark_as_paid(self,
                     payer: User | None,
                     payer_currency: str | None = None):
        """Marks paid, records payer/time, updates records. Idempotent; row-locked."""
        record_currency = payer_currency or self.currency
        converted = self.converted_total(record_currency)
        payer_record: Record | None = None

        with transaction.atomic():
            # QUEUED is the normal resting state for an external recipient now
            # that there is no pay page to activate it, so both are payable.
            locked = (ReimbursementPackage.objects.select_for_update(
                skip_locked=True).filter(
                    pk=self.pk,
                    status__in=(self.Status.OPEN, self.Status.QUEUED)).first())
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
                    f"Date: {locked.paid_at.strftime('%Y-%m-%d')}. "
                    f"(package {self.uuid})")
                payer_record = Record.objects.create(
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
        from core.apps import posthog_client

        if self.status == self.Status.PAID and posthog_client is not None:
            record_count = self.records.filter(is_active=True).count()
            shared = {
                "currency": record_currency,
                "total_amount": float(converted),
                "record_count": record_count,
                "payer_is_registered": payer is not None,
            }
            posthog_client.capture(
                "sender_reimbursement_settled",
                distinct_id=str(self.creator_id),
                properties=shared,
            )
            capture_recipient_paid(locked, payer, shared)

        if payer_record is not None and posthog_client is not None:
            # System-generated, not something the payer asked for, so it counts as imported rather than created.
            posthog_client.capture(
                "record_imported",
                distinct_id=str(payer_record.user_id),
                properties={
                    "import_source": "reimbursement",
                    "record_count": 1
                },
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
        with transaction.atomic():
            locked = (ReimbursementPackage.objects.select_for_update(
                skip_locked=True).filter(pk=self.pk,
                                         status=self.Status.PAID).first())
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
                # Match the payer's receipt by its package uuid (recorded in
                # notes since this annotation existed); fall back to the
                # title+merchant pair for legacy records. Title alone is too
                # generic — unrelated records can share it.
                Record.objects.filter(
                    user=previous_payer,
                    record_type=Record.RecordTypes.EXPENSE_RECEIPT,
                ).filter(
                    Q(notes__icontains=str(self.uuid))
                    | Q(
                        title=f"Reimbursement: {self.title}",
                        merchant=self.creator.email,
                    )).update(
                        notes=Concat("notes", models.Value(" [REFUNDED]")))
        from core.apps import posthog_client

        if self.status == self.Status.OPEN and posthog_client is not None:
            posthog_client.capture(
                "reimbursement_refunded",
                distinct_id=str(self.creator_id),
                properties={
                    "currency": self.currency,
                    "total_amount": float(self.total_amount),
                },
            )

        # Refunds reopen the workflow, so restore the recipient's access.
        try:
            from .services import grant_package_access

            grant_package_access(self)
        except Exception:
            logger.exception(
                "Failed to restore package access after refund (package=%s)",
                getattr(self, "uuid", self.pk),
            )

    @property
    def is_expired(self) -> bool:
        if not self.expires_at:
            return False
        return timezone.now() > self.expires_at

    @property
    def total_amount(self) -> Decimal:
        if hasattr(self,
                   "_annotated_total") and self._annotated_total is not None:
            return self._annotated_total
        return self.records.filter(is_active=True).exclude(
            balance__isnull=True).aggregate(
                total=Sum("balance"))["total"] or Decimal("0.00")

    @property
    def display_total(self) -> Decimal:
        if self._prefetched_converted_total is not None:
            return self._prefetched_converted_total
        return self.converted_total(self.currency)

    @property
    def total_amount_cents(self) -> int:
        return to_stripe_amount(self.total_amount, self.currency)

    def converted_total(self, to_currency: str | None = None) -> Decimal:
        return pricing.converted_total(self, to_currency)

    def converted_total_cents(self, to_currency: str | None = None) -> int:
        return pricing.converted_total_cents(self, to_currency)

    @property
    def payout_account_id(self) -> str | None:
        account = getattr(self.creator, "stripe_account", None)
        return account.stripe_account_id if account and account.is_active else None

    def can_be_paid_by(self, user: User) -> tuple[bool, str | None]:
        """Return "(ok, error_message)" describing whether "user" may pay."""
        if user == self.creator:
            return False, "You cannot pay for your own reimbursement package."
        if self.deleted_at is not None:
            return False, "This reimbursement package is no longer available."
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
        """Atomically claim a payable package for an invoice.

        QUEUED counts: an external recipient is queued until they open the pay
        page, and there is no pay page now, so the invoice itself is the handoff.
        """
        return (ReimbursementPackage.objects.select_for_update().filter(
            pk=self.pk,
            status__in=(self.Status.OPEN, self.Status.QUEUED)).first())

    def build_line_items(self, payer_currency: str) -> CheckoutItems:
        return pricing.build_line_items(self, payer_currency)

    def platform_fee_cents(self, total_cents: int, payer_currency: str,
                           rates) -> int:
        """Compute the application fee for a destination charge: estimated
        Stripe processing costs plus the guaranteed platform net margin,
        clamped to the payment total."""
        return PlatformFeeCalculator.compute(total_cents, payer_currency,
                                             rates)

    def detail_items(self, user_currency: str) -> PackageDetailItems:
        return pricing.detail_items(self, user_currency)

    @classmethod
    def prefetch_converted_totals(
            cls, packages: list[ReimbursementPackage],
            to_currency: str) -> list[ReimbursementPackage]:
        return pricing.prefetch_converted_totals(packages, to_currency)


class ProcessedStripeEvent(models.Model):
    event_id = models.CharField(max_length=255, primary_key=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return self.event_id


class PackagePayment(models.Model):
    package = models.ForeignKey(ReimbursementPackage,
                                on_delete=models.CASCADE,
                                related_name="payments")
    payer = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="payments_made",
    )
    stripe_checkout_session_id = models.CharField(max_length=255,
                                                  unique=True,
                                                  null=True,
                                                  blank=True)
    stripe_invoice_id = models.CharField(max_length=255,
                                         blank=True,
                                         db_index=True)
    stripe_payment_intent_id = models.CharField(max_length=255,
                                                blank=True,
                                                default="",
                                                db_index=True)
    amount_paid = models.DecimalField(max_digits=12, decimal_places=2)
    # Exact expected total in Stripe's smallest unit, captured when the
    # checkout attempt is claimed. Null for legacy rows created before this
    # field existed.
    expected_amount_cents = models.PositiveIntegerField(null=True, blank=True)
    payer_currency = models.CharField(max_length=3, default=DEFAULT_CURRENCY)
    payer_email = models.EmailField(max_length=254, blank=True)
    is_completed = models.BooleanField(default=False, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return f"Payment {self.amount_paid} {self.payer_currency} for {self.package_id} (Paid: {self.is_completed})"

    def amount_matches(self, settled, *, key: str = "amount_total") -> bool:
        """Settlement must equal expected_amount_cents exactly; a tolerance would admit systematic underpayment."""
        session_currency = (settled.get("currency")
                            or self.payer_currency).lower()
        amount_total = settled.get(key)
        if session_currency != self.payer_currency.lower():
            logger.error(
                "Package %s: session %s currency mismatch (%s vs %s) — refusing to mark as paid",
                self.package_id,
                settled.get("id"),
                session_currency,
                self.payer_currency,
            )
            return False
        if amount_total is None:
            logger.error(
                "Package %s: session %s has no amount_total — refusing to mark as paid",
                self.package_id,
                settled.get("id"),
            )
            return False
        if self.expected_amount_cents is not None:
            if int(amount_total) != int(self.expected_amount_cents):
                logger.error(
                    "Package %s: session %s settled %s cents but %s were expected — refusing to mark as paid",
                    self.package_id,
                    settled.get("id"),
                    amount_total,
                    self.expected_amount_cents,
                )
                return False
            return True
        # Legacy rows without a recorded expectation: compare against the
        # stored decimal with a one-cent allowance for rounding only.
        expected_cents = to_stripe_amount(self.amount_paid,
                                          self.payer_currency)
        if abs(int(amount_total) - expected_cents) > 1:
            logger.error(
                "Package %s: session %s settled amount %s %s does not match expected %s %s — refusing to mark as paid",
                self.package_id,
                settled.get("id"),
                settled,
                session_currency,
                self.amount_paid,
                self.payer_currency,
            )
            return False
        return True

    def complete_from_session(self, session) -> None:
        self.is_completed = True
        payment_intent_id = (session.get("payment_intent") if isinstance(
            session, dict) else getattr(session, "payment_intent", None))
        if payment_intent_id:
            self.stripe_payment_intent_id = payment_intent_id
        self.save(update_fields=["is_completed", "stripe_payment_intent_id"])

    def mark_failed(self) -> None:
        self.is_completed = False
        self.save(update_fields=["is_completed"])
