"""Service layer for Stripe reimbursements operations.
Keeps raw Stripe API calls out of models and views so they are mocked and
tested in one place, and always use djstripe's mode-aware secret key.
"""

import hashlib
import logging
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import stripe
from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from billing.services import _configure
from core.exchange_rates import ExchangeRateUnavailableError, get_rates
from records.models import Record

from .models import PackageDraft, PackagePayment, ReimbursementPackage

logger = logging.getLogger(__name__)

# Placeholder session-id prefix for a payment row claimed before its Stripe
# Checkout Session exists. Lets concurrent checkouts detect an in-flight
# attempt instead of racing to create duplicate sessions.
PENDING_SESSION_PREFIX = "pending:"
PENDING_SESSION_STALENESS = timedelta(minutes=15)


@dataclass
class CheckoutOutcome:
    redirect_url: str | None = None
    error: str | None = None


def retrieve_checkout_session(session_id: str) -> stripe.checkout.Session:
    _configure()
    return stripe.checkout.Session.retrieve(str(session_id))


def create_checkout_session(**kwargs: Any) -> stripe.checkout.Session:
    _configure()
    return stripe.checkout.Session.create(**kwargs)


def retrieve_stripe_account(account_id: str) -> stripe.Account:
    _configure()
    return stripe.Account.retrieve(str(account_id))


def create_stripe_account(email: str, user_id: int) -> stripe.Account:
    _configure()
    return stripe.Account.create(
        type="express",
        business_type="individual",
        email=email,
        metadata={"user_id": str(user_id)},
    )


def create_account_link(account_id: str, refresh_url: str, return_url: str) -> stripe.AccountLink:
    _configure()
    return stripe.AccountLink.create(
        account=str(account_id),
        refresh_url=refresh_url,
        return_url=return_url,
        type="account_onboarding",
    )


def retrieve_charge(charge_id: str) -> stripe.Charge:
    _configure()
    return stripe.Charge.retrieve(str(charge_id))


def retrieve_payment_intent(payment_intent_id: str) -> stripe.PaymentIntent:
    _configure()
    return stripe.PaymentIntent.retrieve(str(payment_intent_id))


def create_refund(
    payment_intent_id: str,
    *,
    reason: str,
    refund_application_fee: bool = True,
) -> stripe.Refund:
    """Refund a captured PaymentIntent with a deterministic idempotency key."""
    _configure()
    return stripe.Refund.create(
        payment_intent=str(payment_intent_id),
        idempotency_key=f"refund:{reason}:{payment_intent_id}",
        refund_application_fee=refund_application_fee,
    )


def get_payment_success_package(user, package_uuid: str) -> ReimbursementPackage | None:
    package = (
        ReimbursementPackage.objects.select_related("creator")
        .filter(
            Q(creator=user) | Q(paid_by=user) | Q(payments__payer=user),
            uuid=package_uuid,
            deleted_at__isnull=True,
        )
        .distinct()
        .first()
    )
    if package is None:
        return None

    if package.status == ReimbursementPackage.Status.OPEN:
        payment = package.payments.filter(is_completed=False).order_by("-created_at").first()
        if payment:
            from .tasks import sync_payment_status

            sync_payment_status.send(str(package.uuid), payment.pk)
        package.refresh_from_db()
    return package


def create_package_checkout(
    *,
    package: ReimbursementPackage,
    payer,
    currency: str,
    success_url: str,
    cancel_url: str,
) -> CheckoutOutcome:
    ok, error = package.can_be_paid_by(payer)
    if not ok:
        return CheckoutOutcome(error=error)

    try:
        with transaction.atomic():
            locked = package.lock_for_payment()
            if locked is None:
                return CheckoutOutcome(error="This package is no longer available for payment.")

            latest_incomplete = (
                locked.payments.filter(is_completed=False).order_by("-created_at").first()
            )
            if latest_incomplete is not None:
                if latest_incomplete.stripe_checkout_session_id.startswith(PENDING_SESSION_PREFIX):
                    if timezone.now() - latest_incomplete.created_at < PENDING_SESSION_STALENESS:
                        return CheckoutOutcome(
                            error=(
                                "A checkout is already being prepared for this package. "
                                "Please wait a few seconds and try again."
                            )
                        )
                    # Crashed attempt from a previous deploy: reclaim it.
                    latest_incomplete.delete()
                else:
                    existing_url = locked.resumable_session_url()
                    if existing_url:
                        return CheckoutOutcome(redirect_url=existing_url)

            items = locked.build_line_items(currency)
            if not items.line_items:
                return CheckoutOutcome(error="This package has no payable items.")

            # Recorded before any Stripe call so exactly one attempt exists per
            # claim; converted cents are stored for exact settlement matching.
            payment = PackagePayment.objects.create(
                package=locked,
                payer=payer,
                stripe_checkout_session_id=f"{PENDING_SESSION_PREFIX}{uuid.uuid4()}",
                amount_paid=items.total_amount,
                expected_amount_cents=items.total_cents,
                payer_currency=currency,
            )
    except ExchangeRateUnavailableError as e:
        logger.error("Checkout blocked for package %s: %s", package.uuid, e)
        return CheckoutOutcome(
            error=(
                "Currency exchange rates are temporarily unavailable. "
                "No charge was made — please try again shortly."
            )
        )

    checkout_args: dict[str, Any] = {
        "payment_method_types": ["card"],
        "line_items": items.line_items,
        "mode": "payment",
        "metadata": {"package_uuid": str(package.uuid)},
        "payment_intent_data": {"metadata": {"package_uuid": str(package.uuid)}},
        "success_url": success_url,
        "cancel_url": cancel_url,
    }

    if locked.payout_account_id:
        rates = get_rates("USD")
        checkout_args["payment_intent_data"].update(
            {
                "application_fee_amount": package.platform_fee_cents(
                    items.total_cents, currency, rates
                ),
                "transfer_data": {
                    "destination": locked.payout_account_id,
                },
            }
        )

    idempotency_key = hashlib.sha256(f"checkout:{payment.pk}".encode()).hexdigest()

    try:
        checkout_session = create_checkout_session(**checkout_args, idempotency_key=idempotency_key)
    except stripe.error.StripeError:
        logger.exception("Failed to create Stripe Checkout Session for package %s", package.uuid)
        payment.delete()
        return CheckoutOutcome(
            error="Unable to initiate payment session with Stripe. Please try again later."
        )

    payment.stripe_checkout_session_id = checkout_session.id
    payment.save(update_fields=["stripe_checkout_session_id"])

    return CheckoutOutcome(redirect_url=checkout_session.url)


def create_reimbursement_package(
    *,
    creator,
    recipient_email: str,
    record_ids: list[int],
    title: str,
    days_valid: int,
) -> tuple[ReimbursementPackage | None, str | None]:
    recipient = get_user_model().objects.filter(email__iexact=recipient_email).first()
    if recipient == creator:
        return None, "You cannot send a reimbursement package to yourself."

    records = Record.objects.filter(id__in=record_ids, user=creator, is_active=True)
    if not records.exists():
        return None, "No valid records found."

    package = ReimbursementPackage.objects.create_for(
        PackageDraft(
            creator=creator,
            recipient=recipient,
            recipient_email=recipient_email,
            title=title,
            records=records,
            days_valid=days_valid,
            status=(
                ReimbursementPackage.Status.OPEN
                if recipient is not None
                else ReimbursementPackage.Status.QUEUED
            ),
        )
    )
    if recipient is not None:
        _grant_package_access(package)
    return package, None


def _grant_package_access(package: ReimbursementPackage) -> None:
    if package.recipient is None:
        return
    from records.models import RecordShare
    from records.shares import ShareConfig, grant_access

    config = ShareConfig(
        permission=RecordShare.Permission.VIEW,
        purpose=RecordShare.Purpose.REIMBURSEMENT,
        include_documents=True,
        expires_at=package.expires_at,
    )
    for record in package.records.filter(is_active=True):
        grant_access(
            record=record,
            user=package.recipient,
            requester=package.creator,
            config=config,
        )


def revoke_package_access(package: ReimbursementPackage) -> None:
    if package.recipient is None:
        return
    from records.models import RecordShare
    from records.shares import revoke_share

    shares = RecordShare.active_for(package.recipient).filter(
        record__in=package.records.all(),
        purpose=RecordShare.Purpose.REIMBURSEMENT,
    )
    for share in shares.select_related("record"):
        revoke_share(record=share.record, actor=package.creator, share=share)
