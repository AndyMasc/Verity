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
from billing.services import (
    retrieve_checkout_session as _retrieve_billing_checkout_session,
)
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


def create_account_link(
    account_id: str, refresh_url: str, return_url: str
) -> stripe.AccountLink:
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


def create_refund(
    payment_intent_id: str,
    *,
    reason: str,
    refund_application_fee: bool = True,
) -> stripe.Refund:
    """Refund a captured PaymentIntent with a deterministic idempotency key.

    The key is stable per (reason, payment intent) so webhook/task retries can
    never double-refund: Stripe replays the first refund's result instead.
    Raises "stripe.error.StripeError" on failure.

    Destination charges with a collected application fee must refund that fee
    back to the payer, otherwise the platform retains it and the payer is not
    made whole. "refund_application_fee=True" (default) returns the fee
    proportionally; it is a no-op for charges without an application fee.
    """
    _configure()
    return stripe.Refund.create(
        payment_intent=str(payment_intent_id),
        idempotency_key=f"refund:{reason}:{payment_intent_id}",
        refund_application_fee=refund_application_fee,
    )


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
        payment = (
            package.payments.filter(is_completed=False).order_by("-created_at").first()
        )
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

    Concurrency: the package row is locked while an attempt is claimed and a
    PackagePayment row (with a "pending:" placeholder session id) is inserted.
    A concurrent checkout therefore sees the in-flight attempt instead of
    racing ahead to create a second session — previously two simultaneous
    payers could both be charged with transfers on both PaymentIntents.

    Idempotency: the Stripe key is derived from the payment row's primary key,
    so a lost response retried by Dramatiq resolves to the SAME session at
    Stripe rather than minting a duplicate. (Timestamp-salted keys were unique
    per attempt and deduplicated nothing.)

    Returns the Stripe-hosted checkout URL on success, or a user-facing error
    message when the package is no longer payable, rates are unavailable, or
    Stripe rejects the session.
    """
    ok, error = package.can_be_paid_by(payer)
    if not ok:
        return CheckoutOutcome(error=error)

    try:
        with transaction.atomic():
            locked = package.lock_for_payment()
            if locked is None:
                return CheckoutOutcome(
                    error="This package is no longer available for payment."
                )

            latest_incomplete = (
                locked.payments.filter(is_completed=False)
                .order_by("-created_at")
                .first()
            )
            if latest_incomplete is not None:
                if latest_incomplete.stripe_checkout_session_id.startswith(
                    PENDING_SESSION_PREFIX
                ):
                    if (
                        timezone.now() - latest_incomplete.created_at
                        < PENDING_SESSION_STALENESS
                    ):
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

    # Stable per-attempt key: retries reuse it, concurrent attempts cannot.
    idempotency_key = hashlib.sha256(f"checkout:{payment.pk}".encode()).hexdigest()

    try:
        checkout_session = create_checkout_session(
            **checkout_args, idempotency_key=idempotency_key
        )
    except stripe.error.StripeError:
        logger.exception(
            "Failed to create Stripe Checkout Session for package %s", package.uuid
        )
        # Remove the claim so the next attempt starts clean; nothing financial
        # was recorded yet.
        payment.delete()
        return CheckoutOutcome(
            error="Unable to initiate payment session with Stripe. Please try again later."
        )

    # Point the payment row at the real session. Until this save lands, webhook
    # handlers that look the session up re-raise and retry, so settlement waits
    # for the row instead of being lost.
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
