import uuid
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import models, transaction
from django.utils import timezone

from core.currencies import CURRENCY_CHOICES, DEFAULT_CURRENCY, format_currency, to_stripe_amount
from records.models import Record

User = get_user_model()

STRIPE_MINIMUM_FEE_CENTS = 50


class StripeAccount(models.Model):
    """Holds Stripe Connect payment and onboarding information for a user."""

    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name="stripe_account",
    )
    stripe_account_id = models.CharField(max_length=255, blank=True, null=True)
    stripe_details_submitted = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    @property
    def is_active(self) -> bool:
        """Returns True if the user has completed Stripe onboarding."""
        return bool(self.stripe_account_id and self.stripe_details_submitted)

    def __str__(self) -> str:
        return f"Stripe Account for {self.user.email}"


class ReimbursementPackage(models.Model):
    class Status(models.TextChoices):
        OPEN = "open", "Open for Payment"
        PAID = "paid", "Fully Paid"

    uuid = models.UUIDField(default=uuid.uuid4, editable=False, unique=True, db_index=True)
    creator = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="reimbursement_packages"
    )
    recipient = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reimbursements_received",
    )
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
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reimbursements_paid",
    )
    paid_at = models.DateTimeField(null=True, blank=True)

    deleted_at = models.DateTimeField(null=True, blank=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.title} ({self.uuid})"

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None

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
        return True

    def mark_as_paid(self, payer: User, payer_currency: str | None = None):
        """Marks package as paid, records who/when, updates linked records, and
        creates a reimbursement record for the payer.

        If *payer_currency* is given, the Record is created in that currency
        using converted amounts.  Falls back to the package currency.

        Idempotent: if already paid, this is a no-op.
        Uses select_for_update to prevent race conditions from concurrent webhooks.
        """
        record_currency = payer_currency or self.currency
        converted = self.converted_total(record_currency)

        with transaction.atomic():
            locked = (
                ReimbursementPackage.objects.select_for_update(nowait=True)
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
            records_to_update = list(self.records.all())
            for record in records_to_update:
                record.reimbursed = True
            Record.objects.bulk_update(records_to_update, ["reimbursed"])
            Record.objects.create(
                user=payer,
                title=f"Reimbursement: {self.title}",
                transaction_date=locked.paid_at.strftime("%Y-%m-%d"),
                merchant=self.creator.email,
                balance=converted,
                currency=record_currency,
                record_type=Record.RecordTypes.EXPENSE_RECEIPT,
                payment_method="Papertrail reimbursement transfer",
                notes=(
                    f"Reimbursement package '{self.title}' paid to {self.creator.email}. "
                    f"Amount: {format_currency(converted, record_currency)}. "
                    f"Date: {locked.paid_at.strftime('%Y-%m-%d')}."
                ),
            )

    def mark_as_refunded(self):
        """Reverts a paid package back to open and un-marks linked records.

        Idempotent: if already open, this is a no-op.
        """
        with transaction.atomic():
            locked = (
                ReimbursementPackage.objects.select_for_update(nowait=True)
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
            records_to_update = list(self.records.all())
            for record in records_to_update:
                record.reimbursed = False
            Record.objects.bulk_update(records_to_update, ["reimbursed"])
            if previous_payer:
                Record.objects.filter(
                    user=previous_payer,
                    title=f"Reimbursement: {self.title}",
                    record_type=Record.RecordTypes.EXPENSE_RECEIPT,
                ).update(notes=models.F("notes") + " [REFUNDED]")

    @property
    def is_expired(self) -> bool:
        """Checks if the package has passed its expiration date."""
        if not self.expires_at:
            return False
        return timezone.now() > self.expires_at

    @property
    def total_amount(self) -> Decimal:
        return sum(
            (r.balance for r in self.records.all() if r.is_active and r.balance),
            Decimal("0.00"),
        )

    @property
    def display_total(self) -> Decimal:
        return self.converted_total(self.currency)

    @property
    def total_amount_cents(self) -> int:
        return to_stripe_amount(self.total_amount, self.currency)

    def converted_total(self, to_currency: str | None = None) -> Decimal:
        target = to_currency or self.currency
        from core.exchange_rates import convert_batch

        items = [(r.balance, r.currency) for r in self.records.all() if r.is_active and r.balance]
        if not items:
            return Decimal("0.00")
        return convert_batch(items, target)

    def converted_total_cents(self, to_currency: str | None = None) -> int:
        target = to_currency or self.currency
        return to_stripe_amount(self.converted_total(target), target)

    def _active_records(self):
        return [r for r in self.records.all() if r.is_active]


class PackagePayment(models.Model):
    package = models.ForeignKey(
        ReimbursementPackage, on_delete=models.CASCADE, related_name="payments"
    )
    payer = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name="payments_made"
    )
    stripe_checkout_session_id = models.CharField(max_length=255, unique=True)
    stripe_payment_intent_id = models.CharField(
        max_length=255, blank=True, null=True, db_index=True
    )
    amount_paid = models.DecimalField(max_digits=12, decimal_places=2)
    payer_currency = models.CharField(max_length=3, default=DEFAULT_CURRENCY)
    is_completed = models.BooleanField(default=False, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return f"Payment {self.amount_paid} {self.payer_currency} for {self.package_id} (Paid: {self.is_completed})"
