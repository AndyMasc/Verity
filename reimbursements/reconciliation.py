"""Daily reconciliation of local money state against Stripe.

The 15-minute "reconcile_pending_payments_task" catches payments that settled
without a webhook. This module is the wider safety net: it verifies that
locally recorded settlements still match Stripe's view of the world, and that
package/payment state transitions never drifted apart. Any mismatch is logged
at CRITICAL (picked up by Sentry) and written to the audit trail so drift
pages a human instead of waiting for a user to complain.
"""

import logging
from dataclasses import dataclass

import stripe
from django.utils import timezone

from core.currencies import to_stripe_amount
from records.models import AuditLog

from . import services
from .models import PackagePayment, ReimbursementPackage

logger = logging.getLogger(__name__)

DRIFT_EVENT = "reconciliation_drift"
DEDUP_WINDOW_HOURS = 24


@dataclass(frozen=True)
class Drift:
    """One inconsistency between local state and Stripe."""

    kind: str
    package_uuid: str
    detail: dict

    def as_audit_details(self) -> dict:
        return {
            "event": DRIFT_EVENT,
            "drift_kind": self.kind,
            "package_uuid": self.package_uuid,
            **self.detail,
        }


def run_daily_reconciliation() -> list[Drift]:
    """Run all reconciliation checks. Returns the list of detected drifts.

    Raises "stripe.error.StripeError" on transient API failure so the caller
    (Dramatiq) retries the whole run; findings are deduplicated so retries do
        not create duplicate audit entries.
    """
    drifts: list[Drift] = []
    drifts.extend(_check_completed_payments())
    drifts.extend(_check_paid_packages_have_completed_payment())
    drifts.extend(_check_completed_payments_have_paid_package())

    for drift in drifts:
        _flag(drift)

    if drifts:
        logger.critical(
            "Daily reconciliation found %d money-state drift(s) — manual review required",
            len(drifts),
        )
    else:
        logger.info("Daily reconciliation clean: local state matches Stripe")
    return drifts


def _check_completed_payments() -> list[Drift]:
    """Verify every completed payment against its PaymentIntent at Stripe."""
    drifts: list[Drift] = []
    payments = (
        PackagePayment.objects.filter(is_completed=True)
        .exclude(stripe_payment_intent_id="")
        .select_related("package", "payer")
        .iterator(chunk_size=200)
    )
    for payment in payments:
        expected_cents = (
            payment.expected_amount_cents
            if payment.expected_amount_cents is not None
            else to_stripe_amount(payment.amount_paid, payment.payer_currency)
        )
        try:
            intent = services.retrieve_payment_intent(payment.stripe_payment_intent_id)
        except stripe.error.InvalidRequestError:
            drifts.append(
                Drift(
                    kind="payment_intent_missing",
                    package_uuid=str(payment.package.uuid),
                    detail={
                        "payment_intent": payment.stripe_payment_intent_id,
                        "expected_cents": expected_cents,
                    },
                )
            )
            continue

        amount = intent.get("amount_received") or intent.get("amount") or 0
        currency = (intent.get("currency") or "").lower()
        status = intent.get("status")
        if status != "succeeded":
            drifts.append(
                Drift(
                    kind="payment_intent_not_succeeded",
                    package_uuid=str(payment.package.uuid),
                    detail={
                        "payment_intent": intent.get("id"),
                        "stripe_status": status,
                    },
                )
            )
        if int(amount) != int(expected_cents):
            drifts.append(
                Drift(
                    kind="settled_amount_mismatch",
                    package_uuid=str(payment.package.uuid),
                    detail={
                        "payment_intent": intent.get("id"),
                        "stripe_amount_cents": int(amount),
                        "expected_cents": int(expected_cents),
                    },
                )
            )
        if currency and payment.payer_currency and currency != payment.payer_currency.lower():
            drifts.append(
                Drift(
                    kind="settled_currency_mismatch",
                    package_uuid=str(payment.package.uuid),
                    detail={
                        "payment_intent": intent.get("id"),
                        "stripe_currency": currency,
                        "expected_currency": payment.payer_currency,
                    },
                )
            )
    return drifts


def _check_paid_packages_have_completed_payment() -> list[Drift]:
    """Every PAID package must have a completed payment backing it."""
    paid_without_payment = ReimbursementPackage.objects.filter(
        status=ReimbursementPackage.Status.PAID, deleted_at__isnull=True
    ).exclude(payments__is_completed=True)
    return [
        Drift(
            kind="paid_package_without_completed_payment",
            package_uuid=str(package.uuid),
            detail={},
        )
        for package in paid_without_payment.only("uuid")
    ]


def _check_completed_payments_have_paid_package() -> list[Drift]:
    """Every completed payment must belong to a PAID package."""
    unsettled = PackagePayment.objects.filter(is_completed=True).exclude(
        package__status=ReimbursementPackage.Status.PAID
    )
    return [
        Drift(
            kind="completed_payment_on_unpaid_package",
            package_uuid=str(payment.package.uuid),
            detail={
                "stripe_session_id": payment.stripe_checkout_session_id,
                "package_status": payment.package.status,
            },
        )
        for payment in unsettled.select_related("package").only(
            "pk", "stripe_checkout_session_id", "package__uuid"
        )
    ]


def _flag(drift: Drift) -> None:
    """Log CRITICAL and persist an audit entry once per unique drift per day."""
    details = drift.as_audit_details()
    window_start = timezone.now() - timezone.timedelta(hours=DEDUP_WINDOW_HOURS)
    already_flagged = AuditLog.objects.filter(
        action=AuditLog.Action.UPDATE_RECORD,
        created_at__gte=window_start,
        details__event=DRIFT_EVENT,
        details__drift_kind=drift.kind,
        details__package_uuid=drift.package_uuid,
    ).exists()
    if already_flagged:
        return

    logger.critical("Money-state drift detected: %s", details)
    first_package = ReimbursementPackage.objects.filter(uuid=drift.package_uuid).first()
    AuditLog.objects.create(
        user=getattr(first_package, "creator", None),
        action=AuditLog.Action.UPDATE_RECORD,
        details=details,
    )
