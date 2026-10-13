"""Service layer for Stripe reimbursements operations."""

import hashlib
import logging
from dataclasses import dataclass
from typing import Any

import stripe
from django.contrib.auth import get_user_model
from django.db import transaction

from billing.services import _configure
from core.exchange_rates import ExchangeRateUnavailableError, get_rates
from records.models import Record

from .models import PackageDraft, PackagePayment, ReimbursementPackage

logger = logging.getLogger(__name__)

# How long a mailed invoice stays payable before it is abandoned.
# TODO: Allow user defined expiries
DAYS_UNTIL_DUE = 7


@dataclass
class CheckoutOutcome:
    redirect_url: str | None = None
    error: str | None = None


def retrieve_checkout_session(session_id: str) -> stripe.checkout.Session:
    _configure()
    return stripe.checkout.Session.retrieve(str(session_id))


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


def create_account_link(account_id: str, refresh_url: str,
                        return_url: str) -> stripe.AccountLink:
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


def retrieve_invoice(invoice_id: str,
                     *,
                     expand: list[str] | None = None) -> dict[str, Any]:
    _configure()
    return stripe.Invoice.retrieve(str(invoice_id), expand=expand
                                   or []).to_dict()


def invoice_payment_intent(invoice_id: str) -> str:
    """PaymentIntent behind a settled invoice.

    Invoice payloads carry no payment_intent/charge on the current API
    version - the intent only appears once "payments" is expanded, which is
    why this is resolved on demand rather than on every invoice fetch.
    """
    invoice = retrieve_invoice(invoice_id, expand=["payments"])
    for row in (invoice.get("payments") or {}).get("data") or []:
        intent = ((row.get("payment") or {}).get("payment_intent")) or ""
        if isinstance(intent, str) and intent:
            return intent
    return ""


def create_invoice_item(invoice_id: str, **kwargs: Any):
    _configure()
    return stripe.InvoiceItem.create(invoice=invoice_id, **kwargs)


def services_hosted_url(invoice_id: str) -> str:
    _configure()
    return stripe.Invoice.retrieve(invoice_id).hosted_invoice_url


def create_invoice(invoice_id: str | None = None, **kwargs: Any):
    _configure()
    if invoice_id is not None:
        return stripe.Invoice.modify(invoice_id, **kwargs)
    return stripe.Invoice.create(**kwargs)


def send_invoice(invoice_id: str, **kwargs: Any):
    _configure()
    return stripe.Invoice.send_invoice(invoice_id, **kwargs)


def find_or_create_customer(*, email: str, stripe_account: str):
    """Recipient payer, identified by email whether or not they have an account."""
    _configure()
    customers = stripe.Customer.list(email=email, limit=1)
    if customers.data:
        return customers.data[0].id
    return stripe.Customer.create(
        email=email,
        metadata={
            "stripe_connect_account": stripe_account
        },
    ).id


def create_package_invoice(
    *,
    package: ReimbursementPackage,
    payer,
    payer_email: str,
    currency: str,
) -> CheckoutOutcome:
    """Claim the package, raise a Stripe invoice for it, and email the payer.
    The hosted invoice replaces the public pay page: Stripe collects the card
    and hosts the payment UI, so nothing here serves anonymous visitors.
    """
    ok, error = package.can_be_paid_by(payer)
    if not ok:
        return CheckoutOutcome(error=error)

    email = payer_email or (payer.email if payer else "")
    if not email:
        return CheckoutOutcome(
            error="A payer email is required to send an invoice.")

    try:
        with transaction.atomic():
            locked = package.lock_for_payment()
            if locked is None:
                return CheckoutOutcome(
                    error="This package is no longer available for payment.")

            if locked.payments.filter(is_completed=False).exists():
                return CheckoutOutcome(
                    error="An invoice is already outstanding for this package."
                )

            items = locked.build_line_items(currency)
            if not items.line_items:
                return CheckoutOutcome(
                    error="This package has no payable items.")

            payment = PackagePayment.objects.create(
                package=locked,
                payer=payer,
                payer_email=email,
                amount_paid=items.total_amount,
                expected_amount_cents=items.total_cents,
                payer_currency=currency,
            )
    except ExchangeRateUnavailableError:
        logger.error(
            "Invoice blocked for package %s: exchange rates unavailable",
            package.uuid)
        return CheckoutOutcome(
            error=("Currency exchange rates are temporarily unavailable. "
                   "No charge was made — please try again shortly."))

    destination = locked.payout_account_id
    idempotency_key = hashlib.sha256(
        f"invoice:{payment.pk}".encode()).hexdigest()

    try:
        customer_id = find_or_create_customer(email=email,
                                              stripe_account=destination or "")
        # Draft first: auto_advance would finalise on the first line item and strand the rest.
        invoice = create_invoice(
            customer=customer_id,
            collection_method="send_invoice",
            days_until_due=DAYS_UNTIL_DUE,
            currency=currency.lower(),
            auto_advance=False,
            metadata={"package_uuid": str(package.uuid)},
            idempotency_key=idempotency_key,
            **({
                "application_fee_amount": package.platform_fee_cents(
                    items.total_cents, currency, get_rates("USD")),
                "transfer_data": {
                    "destination": destination
                },
            } if destination else {}),
        )

        for index, line in enumerate(items.line_items):
            price_data = line["price_data"]
            product_data = price_data.get("product_data", {})
            description = product_data.get("description") or product_data.get(
                "name", "Item")
            create_invoice_item(
                invoice.id,
                customer=customer_id,
                amount=price_data["unit_amount"],
                currency=price_data["currency"].lower(),
                description=description,
                idempotency_key=hashlib.sha256(
                    f"invoice-item:{payment.pk}:{index}".encode()).hexdigest(),
            )

        # Persist the id as soon as Stripe has one, so a later failure still
        # leaves a record reconciliation can void rather than an orphan invoice.
        payment.stripe_invoice_id = invoice.id
        payment.save(update_fields=["stripe_invoice_id"])

        create_invoice(
            invoice.id,
            auto_advance=True,
            idempotency_key=f"{idempotency_key}:advance",
        )

        send_invoice(invoice.id, idempotency_key=f"{idempotency_key}:send")
    except stripe.error.StripeError:
        logger.exception("Failed to raise Stripe invoice for package %s",
                         package.uuid)
        if not payment.stripe_invoice_id:
            # Nothing exists at Stripe yet, so the claim can be released.
            payment.delete()
        return CheckoutOutcome(
            error=
            "Unable to raise an invoice with Stripe. Please try again later.")

    return CheckoutOutcome(
        redirect_url=services_hosted_url(payment.stripe_invoice_id))


def create_reimbursement_package(
    *,
    creator,
    recipient_email: str,
    record_ids: list[int],
    title: str,
    days_valid: int,
) -> tuple[ReimbursementPackage | None, str | None]:
    recipient = get_user_model().objects.filter(
        email__iexact=recipient_email).first()
    if recipient == creator:
        return None, "You cannot send a reimbursement package to yourself."

    records = Record.objects.filter(id__in=record_ids,
                                    user=creator,
                                    is_active=True)
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
            status=(ReimbursementPackage.Status.OPEN if recipient is not None
                    else ReimbursementPackage.Status.QUEUED),
        ))
    if recipient is not None:
        grant_package_access(package)
    return package, None


def grant_package_access(package: ReimbursementPackage) -> None:
    if package.recipient is None:
        return
    from records.access import ShareConfig, grant_access
    from records.models import RecordShare

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
    from records.access import revoke_share
    from records.models import RecordShare

    shares = RecordShare.active_for(package.recipient).filter(
        record__in=package.records.all(),
        purpose=RecordShare.Purpose.REIMBURSEMENT,
    )
    for share in shares.select_related("record"):
        revoke_share(record=share.record, actor=package.creator, share=share)
