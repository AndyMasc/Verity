import hashlib
import logging
from typing import Any

import stripe
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

from core.exchange_rates import get_rates

from .. import services
from ..models import PackagePayment, ReimbursementPackage
from ..tasks import sync_payment_status

logger = logging.getLogger(__name__)


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


@method_decorator(ratelimit(key="user", rate="10/m", method="POST", block=True), name="dispatch")
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

        ok, error = package.can_be_paid_by(request.user)
        if not ok:
            messages.error(request, error)
            return redirect(self._detail_url(package.uuid))

        # Lock the package row and double-check it's still payable. Prevents
        # concurrent checkouts from different users.
        with transaction.atomic():
            locked = package.lock_for_payment()
            if locked is None:
                messages.error(request, "This package is no longer available for payment.")
                return redirect(self._detail_url(package.uuid))

            existing_url = package.resumable_session_url()
            if existing_url:
                return redirect(existing_url)

        payer_currency = getattr(request.user.settings, "default_currency", "usd")
        items = package.build_line_items(payer_currency)
        if not items.line_items:
            messages.error(request, "This package has no payable items.")
            return redirect(self._detail_url(package.uuid))

        checkout_args: dict[str, Any] = {
            "payment_method_types": ["card"],
            "line_items": items.line_items,
            "mode": "payment",
            "metadata": {
                "package_uuid": str(package.uuid),
            },
            "success_url": request.build_absolute_uri(
                reverse("reimbursements:payment-success") + f"?package={package.uuid}"
            ),
            "cancel_url": request.build_absolute_uri(
                reverse(
                    "reimbursements:package-detail",
                    kwargs={"package_uuid": package.uuid},
                )
            ),
        }

        if locked.payout_account_id:
            rates = get_rates("USD")
            checkout_args["payment_intent_data"] = {
                "application_fee_amount": package.platform_fee_cents(
                    items.total_cents, payer_currency, rates
                ),
                "transfer_data": {
                    "destination": locked.payout_account_id,
                },
            }

        try:
            idempotency_key = hashlib.sha256(
                f"checkout:{package.uuid}:{request.user.id}:{timezone.now().timestamp()}".encode()
            ).hexdigest()
            checkout_session = services.create_checkout_session(
                **checkout_args, idempotency_key=idempotency_key
            )
        except stripe.error.StripeError:
            logger.exception(
                "Failed to create Stripe Checkout Session for package %s", package.uuid
            )
            messages.error(
                request,
                "Unable to initiate payment session with Stripe. Please try again later.",
            )
            return redirect(self._detail_url(package.uuid))

        # Create the PackagePayment record and commit it before redirecting the
        # user to Stripe.  This ensures the row exists in the database before
        # Stripe can fire a checkout.session.completed webhook (which happens
        # only after the user completes payment on Stripe's hosted page).
        with transaction.atomic():
            PackagePayment.objects.create(
                package=package,
                payer=request.user,
                stripe_checkout_session_id=checkout_session.id,
                amount_paid=items.total_amount,
                payer_currency=payer_currency,
            )

        return redirect(checkout_session.url)

    def _detail_url(self, package_uuid) -> str:
        return reverse(
            "reimbursements:package-detail",
            kwargs={"package_uuid": package_uuid},
        )
