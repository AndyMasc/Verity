import hashlib
import logging
from decimal import ROUND_DOWN, Decimal
from typing import Any

import stripe
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import transaction
from django.db.models import Q
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views import View
from django.views.generic import TemplateView
from django_ratelimit.decorators import ratelimit

from core.currencies import to_stripe_amount
from core.exchange_rates import convert as convert_currency
from core.exchange_rates import get_rates

from ..models import STRIPE_MINIMUM_FEE_CENTS, PackagePayment, ReimbursementPackage
from ..tasks import sync_payment_status

stripe.api_key = settings.STRIPE_SECRET_KEY
logger = logging.getLogger(__name__)

PLATFORM_FEE_PERCENT = Decimal("0.03")


@method_decorator(ratelimit(key="user", rate="30/m", method="GET"), name="dispatch")
class PaymentSuccessView(LoginRequiredMixin, TemplateView):
    template_name = "reimbursements/payment_success.html"

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        package_uuid = self.request.GET.get("package")
        if package_uuid:
            package = (
                ReimbursementPackage.objects.select_related("creator")
                .filter(
                    Q(creator=self.request.user)
                    | Q(paid_by=self.request.user)
                    | Q(payments__payer=self.request.user),
                    uuid=package_uuid,
                    deleted_at__isnull=True,
                )
                .distinct()
                .first()
            )
            if package:
                if package.status == ReimbursementPackage.Status.OPEN:
                    self._sync_payment_status(package)
                    package.refresh_from_db()
                context["package"] = package
        return context

    def _sync_payment_status(self, package: ReimbursementPackage) -> None:
        payment = package.payments.filter(is_completed=False).order_by("-created_at").first()
        if not payment:
            return
        sync_payment_status.delay(str(package.uuid), payment.pk)


@method_decorator(ratelimit(key="user", rate="10/m", method="POST"), name="dispatch")
class CreatePackageCheckoutView(LoginRequiredMixin, View):
    def post(self, request: HttpRequest, package_uuid: str) -> HttpResponse:
        package = get_object_or_404(
            ReimbursementPackage.objects.select_related("creator", "recipient").prefetch_related(
                "records"
            ),
            Q(creator=request.user) | Q(recipient=request.user),
            uuid=package_uuid,
            deleted_at__isnull=True,
        )

        if request.user == package.creator:
            messages.error(request, "You cannot pay for your own reimbursement package.")
            return redirect(
                reverse("reimbursements:package-detail", kwargs={"package_uuid": package.uuid})
            )

        if package.is_expired:
            messages.error(request, "This reimbursement package has expired.")
            return redirect(
                reverse("reimbursements:package-detail", kwargs={"package_uuid": package.uuid})
            )

        if package.status == ReimbursementPackage.Status.PAID:
            messages.error(request, "This package has already been paid.")
            return redirect(
                reverse("reimbursements:package-detail", kwargs={"package_uuid": package.uuid})
            )

        # Lock the package row and double-check it's still payable.
        # This prevents concurrent checkouts from different users.
        with transaction.atomic():
            locked_package = (
                ReimbursementPackage.objects.select_for_update()
                .filter(pk=package.pk, status=ReimbursementPackage.Status.OPEN)
                .first()
            )
            if not locked_package:
                messages.error(request, "This package is no longer available for payment.")
                return redirect(
                    reverse("reimbursements:package-detail", kwargs={"package_uuid": package.uuid})
                )

            existing_payment = (
                package.payments.filter(is_completed=False).order_by("-created_at").first()
            )
            if existing_payment:
                try:
                    session = stripe.checkout.Session.retrieve(
                        existing_payment.stripe_checkout_session_id
                    )
                    if session.status == "open" and session.url:
                        return redirect(session.url)
                except stripe.error.StripeError:
                    logger.warning(
                        "Failed to retrieve existing session %s, creating new one",
                        existing_payment.stripe_checkout_session_id,
                    )

        stripe_account = getattr(package.creator, "stripe_account", None)
        stripe_account_id = (
            stripe_account.stripe_account_id
            if stripe_account and stripe_account.is_active
            else None
        )

        payer_currency = getattr(request.user.settings, "default_currency", "usd")
        payer_rates = get_rates("USD")

        line_items: list[dict[str, Any]] = []
        actual_total_cents = 0
        actual_total_amount = Decimal("0")
        for record in package.records.filter(is_active=True):
            if record.balance and record.balance > 0:
                converted = convert_currency(
                    record.balance, record.currency, payer_currency, rates=payer_rates
                )
                converted_stripe = to_stripe_amount(converted, payer_currency)
                if converted_stripe <= 0:
                    continue

                product_data: dict[str, Any] = {
                    "name": record.title or "Expense Item",
                }
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
            fallback_cents = package.converted_total_cents(payer_currency)
            if fallback_cents <= 0:
                messages.error(request, "This package has no payable items.")
                return redirect(
                    reverse("reimbursements:package-detail", kwargs={"package_uuid": package.uuid})
                )
            line_items.append(
                {
                    "price_data": {
                        "currency": payer_currency,
                        "product_data": {"name": package.title},
                        "unit_amount": fallback_cents,
                    },
                    "quantity": 1,
                }
            )
            actual_total_cents = fallback_cents
            actual_total_amount = package.converted_total(payer_currency)

        checkout_args: dict[str, Any] = {
            "payment_method_types": ["card"],
            "line_items": line_items,
            "mode": "payment",
            "metadata": {
                "package_uuid": str(package.uuid),
            },
            "success_url": request.build_absolute_uri(
                reverse("reimbursements:payment-success") + f"?package={package.uuid}"
            ),
            "cancel_url": request.build_absolute_uri(
                reverse("reimbursements:package-detail", kwargs={"package_uuid": package.uuid})
            ),
        }

        if stripe_account_id:
            platform_fee_cents = int(
                (Decimal(str(actual_total_cents)) * PLATFORM_FEE_PERCENT).quantize(
                    Decimal("1"), rounding=ROUND_DOWN
                )
            )
            min_fee_converted = convert_currency(
                Decimal(STRIPE_MINIMUM_FEE_CENTS) / Decimal("100"),
                "usd",
                payer_currency,
                rates=payer_rates,
            )
            min_fee_units = to_stripe_amount(min_fee_converted, payer_currency)

            if platform_fee_cents < min_fee_units:
                platform_fee_cents = min_fee_units
            if platform_fee_cents > actual_total_cents:
                platform_fee_cents = actual_total_cents

            checkout_args["payment_intent_data"] = {
                "application_fee_amount": platform_fee_cents,
                "transfer_data": {
                    "destination": stripe_account_id,
                },
            }

        try:
            idempotency_key = hashlib.sha256(
                f"checkout:{package.uuid}:{request.user.id}:{timezone.now().timestamp()}".encode()
            ).hexdigest()
            checkout_session = stripe.checkout.Session.create(
                **checkout_args, idempotency_key=idempotency_key
            )
        except stripe.error.StripeError:
            logger.exception(
                "Failed to create Stripe Checkout Session for package %s", package.uuid
            )
            messages.error(
                request, "Unable to initiate payment session with Stripe. Please try again later."
            )
            return redirect(
                reverse("reimbursements:package-detail", kwargs={"package_uuid": package.uuid})
            )

        # Create the PackagePayment record and commit it before redirecting the
        # user to Stripe.  This ensures the row exists in the database before
        # Stripe can fire a checkout.session.completed webhook (which happens
        # only after the user completes payment on Stripe's hosted page).
        with transaction.atomic():
            PackagePayment.objects.create(
                package=package,
                payer=request.user,
                stripe_checkout_session_id=checkout_session.id,
                amount_paid=actual_total_amount,
                payer_currency=payer_currency,
            )

        return redirect(checkout_session.url)
