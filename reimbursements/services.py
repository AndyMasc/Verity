"""Service layer for Stripe reimbursements operations.

Keeps raw Stripe API calls out of models and views so they are mocked and
tested in one place, and always use djstripe's mode-aware secret key.
"""

import hashlib
import logging
from dataclasses import dataclass
from typing import Any

import stripe
from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from billing.services import _configure
from billing.services import (
    retrieve_checkout_session as _retrieve_billing_checkout_session,
)
from core.exchange_rates import get_rates
from records.models import Record

from .models import PackagePayment, ReimbursementPackage

logger = logging.getLogger(__name__)

# Make idempotency keys unique per checkout attempt.
_IDEMPOTENCY_PREFIX = "checkout"


@dataclass
class CheckoutOutcome:
    """Result of initiating a package payment checkout."""

    redirect_url: str | None = None
    error: str | None = None


def retrieve_checkout_session(session_id: str) -> stripe.checkout.Session:
    """Fetch a Stripe Checkout Session. Raises StripeError on failure."""
    return _retrieve_billing_checkout_session(str(session_id))


def create_checkout_session(**kwargs: Any) -> stripe.checkout.Session:
    """Create a Stripe Checkout Session with an idempotency key."""
    _configure()
    return stripe.checkout.Session.create(**kwargs)


def retrieve_stripe_account(account_id: str) -> stripe.Account:
    """Fetch a Stripe Connect account. Raises StripeError on failure."""
    _configure()
    return stripe.Account.retrieve(str(account_id))


def create_stripe_account(email: str, user_id: int) -> stripe.Account:
    """Create an Express Stripe Connect account for a user."""
    _configure()
    return stripe.Account.create(
        type="express",
        email=email,
        metadata={"user_id": str(user_id)},
    )


def create_account_link(account_id: str, refresh_url: str, return_url: str) -> stripe.AccountLink:
    """Create an account-onboarding AccountLink for the given Connect account."""
    _configure()
    return stripe.AccountLink.create(
        account=str(account_id),
        refresh_url=refresh_url,
        return_url=return_url,
        type="account_onboarding",
    )


def retrieve_charge(charge_id: str) -> stripe.Charge:
    """Fetch a Stripe Charge. Raises StripeError on failure."""
    _configure()
    return stripe.Charge.retrieve(str(charge_id))


def retrieve_payment_intent(payment_intent_id: str) -> stripe.PaymentIntent:
    """Fetch a Stripe PaymentIntent. Raises StripeError on failure."""
    _configure()
    return stripe.PaymentIntent.retrieve(str(payment_intent_id))


def get_payment_success_package(user, package_uuid: str) -> ReimbursementPackage | None:
    """Return the package referenced by a payment-success redirect, if visible to "user".

    Packages still open are re-checked against Stripe in the background so the
    page reflects the settled payment status.
    """
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
    """Create a Stripe Checkout Session for "package" and record the payment.

    The package row is locked for the duration of the attempt so concurrent
    checkouts cannot double-pay it. Returns the Stripe-hosted checkout URL on
    success, or a user-facing error message when the package is no longer
    payable or Stripe rejects the session.
    """
    ok, error = package.can_be_paid_by(payer)
    if not ok:
        return CheckoutOutcome(error=error)

    with transaction.atomic():
        locked = package.lock_for_payment()
        if locked is None:
            return CheckoutOutcome(error="This package is no longer available for payment.")
        existing_url = package.resumable_session_url()
        if existing_url:
            return CheckoutOutcome(redirect_url=existing_url)

    items = package.build_line_items(currency)
    if not items.line_items:
        return CheckoutOutcome(error="This package has no payable items.")

    checkout_args: dict[str, Any] = {
        "payment_method_types": ["card"],
        "line_items": items.line_items,
        "mode": "payment",
        "metadata": {"package_uuid": str(package.uuid)},
        # Also carried on the PaymentIntent so reversal events (charge.refunded,
        # charge.failed, charge.dispute.*) can be routed back to the payment
        # even if they arrive before checkout.session.completed is processed.
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

    try:
        idempotency_key = hashlib.sha256(
            (
                f"{_IDEMPOTENCY_PREFIX}:{package.uuid}:"
                f"{getattr(payer, 'id', 'external')}:{timezone.now().timestamp()}"
            ).encode()
        ).hexdigest()
        checkout_session = create_checkout_session(**checkout_args, idempotency_key=idempotency_key)
    except stripe.error.StripeError:
        logger.exception("Failed to create Stripe Checkout Session for package %s", package.uuid)
        return CheckoutOutcome(
            error="Unable to initiate payment session with Stripe. Please try again later."
        )

    # Record the payment before redirecting so the row exists before Stripe can
    # fire checkout.session.completed after the user finishes paying.
    with transaction.atomic():
        PackagePayment.objects.create(
            package=package,
            payer=payer,
            stripe_checkout_session_id=checkout_session.id,
            amount_paid=items.total_amount,
            payer_currency=currency,
        )

    return CheckoutOutcome(redirect_url=checkout_session.url)


def create_reimbursement_package(
    *,
    creator,
    recipient_email: str,
    record_ids: list[int],
    title: str,
    days_valid: int,
) -> tuple[ReimbursementPackage | None, str | None]:
    """Create a reimbursement package from selected records.

    The package can be sent to any email address. When the address matches a
    registered Verity user, that user is granted temporary,
    purpose-bound, view-only access to each packaged record ("RecordShare"
    with "purpose=reimbursement") and the package starts open. Otherwise the
    package starts queued, awaiting the external recipient: they pay through
    a public, unauthenticated page reached from the emailed link. Access is
    revoked automatically when the package is paid or deleted, and restored
    if it is refunded.

    Returns "(package, None)" on success, or "(None, user-facing error)"
    when the sender targets themselves, or no valid records were selected.
    """
    recipient = get_user_model().objects.filter(email__iexact=recipient_email).first()
    if recipient == creator:
        return None, "You cannot send a reimbursement package to yourself."

    records = Record.objects.filter(id__in=record_ids, user=creator, is_active=True)
    if not records.exists():
        return None, "No valid records found."

    package = ReimbursementPackage.objects.create_for(
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
    if recipient is not None:
        _grant_package_access(package)
    return package, None


def activate_queued_package(package: ReimbursementPackage) -> bool:
    """Open a queued package for payment once the external payer arrives.

    Returns True when the package transitioned from queued to open.
    """
    return package.activate()


def _grant_package_access(package: ReimbursementPackage) -> None:
    """Grant the recipient purpose-bound view access to each packaged record.

    scoped to "expires_at" (package expiry) so access expires with the
    package. Idempotent via the share service (active grants are left alone).
    No-op for external recipients without a registered account.
    """
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
    """Revoke (soft) the access granted when the package was created."""
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
