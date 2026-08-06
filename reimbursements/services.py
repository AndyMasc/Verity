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
    _configure()
    return stripe.checkout.Session.retrieve(str(session_id))


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


def get_payment_success_package(user, package_uuid: str) -> ReimbursementPackage | None:
    """Return the package referenced by a payment-success redirect, if visible to *user*.

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

            sync_payment_status.delay(str(package.uuid), payment.pk)
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
    """Create a Stripe Checkout Session for *package* and record the payment.

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
        "success_url": success_url,
        "cancel_url": cancel_url,
    }

    if locked.payout_account_id:
        rates = get_rates("USD")
        checkout_args["payment_intent_data"] = {
            "application_fee_amount": package.platform_fee_cents(
                items.total_cents, currency, rates
            ),
            "transfer_data": {
                "destination": locked.payout_account_id,
            },
        }

    try:
        idempotency_key = hashlib.sha256(
            f"{_IDEMPOTENCY_PREFIX}:{package.uuid}:{payer.id}:{timezone.now().timestamp()}".encode()
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

    Returns ``(package, None)`` on success, or ``(None, user-facing error)``
    when the recipient does not exist, the sender targets themselves, or no
    valid records were selected.
    """
    recipient = get_user_model().objects.filter(email__iexact=recipient_email).first()
    if recipient is None:
        return None, "No Papertrail user found with that email address."
    if recipient == creator:
        return None, "You cannot send a reimbursement package to yourself."

    records = Record.objects.filter(id__in=record_ids, user=creator, is_active=True)
    if not records.exists():
        return None, "No valid records found."

    package = ReimbursementPackage.objects.create_for(
        creator=creator,
        recipient=recipient,
        title=title,
        records=records,
        days_valid=days_valid,
    )
    return package, None
