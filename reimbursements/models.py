import uuid
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import models, transaction
from django.utils import timezone

from core.currencies import CURRENCY_CHOICES, DEFAULT_CURRENCY, format_currency
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

    def mark_as_paid(self, payer: User):
        """Marks package as paid, records who/when, updates linked records, and
        creates a reimbursement record for the payer.

        Idempotent: if already paid, this is a no-op.
        Uses select_for_update to prevent race conditions from concurrent webhooks.
        """
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
            for record in self.records.all():
                record.reimbursed = True
                record.save(update_fields=["reimbursed"])
            Record.objects.create(
                user=payer,
                title=f"Reimbursement: {self.title}",
                transaction_date=locked.paid_at.strftime("%Y-%m-%d"),
                merchant=self.creator.email,
                balance=self.total_amount,
                currency=self.currency,
                record_type=Record.RecordTypes.EXPENSE_RECEIPT,
                payment_method="Papertrail reimbursment transfer",
                notes=(
                    f"Reimbursement package '{self.title}' paid to {self.creator.email}. "
                    f"Amount: {format_currency(self.total_amount, self.currency)}. "
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
            locked.status = self.Status.OPEN
            locked.paid_by = None
            locked.paid_at = None
            locked.save(update_fields=["status", "paid_by", "paid_at"])
            self.status = locked.status
            self.paid_by = locked.paid_by
            self.paid_at = locked.paid_at
            for record in self.records.all():
                record.reimbursed = False
                record.save(update_fields=["reimbursed"])
            Record.objects.filter(
                user=locked.paid_by,
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
            (r.balance for r in self.records.filter(is_active=True) if r.balance),
            Decimal("0.00"),
        )

    @property
    def total_amount_cents(self) -> int:
        return int(self.total_amount * 100)


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
    is_completed = models.BooleanField(default=False, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return f"Payment {self.amount_paid} for {self.package_id} (Paid: {self.is_completed})"
