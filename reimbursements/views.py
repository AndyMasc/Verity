import json
import logging
from datetime import timedelta
from decimal import ROUND_DOWN, Decimal
from typing import Any

import stripe
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import transaction
from django.db.models import Q
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.http import require_POST
from django.views.generic import DetailView, ListView, TemplateView
from django_ratelimit.decorators import ratelimit

from records.models import Record

from .mixins import StripeAccountRequiredMixin
from .models import (
    STRIPE_MINIMUM_FEE_CENTS,
    PackagePayment,
    ReimbursementPackage,
    StripeAccount,
)

stripe.api_key = settings.STRIPE_SECRET_KEY
logger = logging.getLogger(__name__)

PLATFORM_FEE_PERCENT = Decimal("0.03")


@method_decorator(ratelimit(key="user", rate="10/m", method="GET"), name="dispatch")
class StripeOnboardView(LoginRequiredMixin, View):
    """Creates a Stripe Connect account link and redirects to hosted onboarding."""

    def get(self, request: HttpRequest) -> HttpResponse:
        stripe_account = getattr(request.user, "stripe_account", None)

        if not stripe_account:
            stripe_account = StripeAccount.objects.create(user=request.user)

        if stripe_account.is_active:
            return redirect(reverse("reimbursements:package-list"))

        if stripe_account.stripe_account_id:
            try:
                live_account = stripe.Account.retrieve(stripe_account.stripe_account_id)
                if live_account.details_submitted:
                    stripe_account.stripe_details_submitted = True
                    stripe_account.save(update_fields=["stripe_details_submitted"])
                    return redirect(reverse("reimbursements:package-list"))
            except stripe.error.StripeError:
                logger.warning("Failed to retrieve Stripe account for user %s", request.user.id)

        if not stripe_account.stripe_account_id:
            account = stripe.Account.create(
                type="express",
                email=request.user.email,
                metadata={"user_id": request.user.id},
            )
            stripe_account.stripe_account_id = account.id
            stripe_account.save(update_fields=["stripe_account_id"])

        refresh_url = request.build_absolute_uri(reverse("reimbursements:stripe-onboard"))
        return_url = request.build_absolute_uri(reverse("reimbursements:package-list"))

        try:
            account_link = stripe.AccountLink.create(
                account=stripe_account.stripe_account_id,
                refresh_url=refresh_url,
                return_url=return_url,
                type="account_onboarding",
            )
            return redirect(account_link.url)
        except stripe.error.StripeError:
            logger.exception("Failed to create Stripe account link for user %s", request.user.id)
            messages.error(
                request, "Something went wrong connecting your Stripe account. Please try again."
            )
            return redirect(reverse("reimbursements:package-list"))


@require_POST
@ratelimit(key="user", rate="30/m", method="POST", block=True)
def validate_recipient_email(request: HttpRequest) -> JsonResponse:
    """Check if an email belongs to an existing Papertrail user."""
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        return JsonResponse({"valid": False, "error": "Invalid request."}, status=400)

    email = data.get("email", "").strip()
    if not email:
        return JsonResponse({"valid": False, "error": "Email is required."}, status=400)

    user_model = get_user_model()
    try:
        recipient = user_model.objects.get(email__iexact=email)
        if recipient == request.user:
            return JsonResponse({"valid": False, "error": "You cannot send a package to yourself."})
        return JsonResponse({"valid": True, "name": recipient.get_full_name() or recipient.email})
    except user_model.DoesNotExist:
        return JsonResponse(
            {"valid": False, "error": "No Papertrail user found with that email address."}
        )


class PackageListView(LoginRequiredMixin, ListView):
    """
    Dashboard view listing all reimbursement packages created by the current user.
    """

    model = ReimbursementPackage
    template_name = "reimbursements/package_list.html"
    context_object_name = "packages"
    paginate_by = 15

    def get_queryset(self):
        return (
            super()
            .get_queryset()
            .filter(creator=self.request.user, deleted_at__isnull=True)
            .prefetch_related("records")
            .select_related("recipient", "paid_by")
            .order_by("-id")
        )

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        paid_by_me = (
            ReimbursementPackage.objects.filter(paid_by=self.request.user, deleted_at__isnull=True)
            .select_related("creator", "recipient")
            .prefetch_related("records")
            .order_by("-paid_at")
        )
        sent_to_me = (
            ReimbursementPackage.objects.filter(
                recipient=self.request.user, deleted_at__isnull=True
            )
            .select_related("creator")
            .prefetch_related("records")
            .order_by("-created_at")
        )
        context["sections"] = [
            ("Sent by You", context["packages"], "sent"),
            ("Paid by You", paid_by_me, "paid"),
            ("Sent to You", sent_to_me, "received"),
        ]
        stripe_account = getattr(self.request.user, "stripe_account", None)
        context["stripe_connected"] = bool(stripe_account and stripe_account.is_active)
        return context


@method_decorator(ratelimit(key="user", rate="30/m", method="GET"), name="dispatch")
class PackageDetailView(LoginRequiredMixin, DetailView):
    """
    Detail view for a specific reimbursement package.
    Accessible to the creator and the designated recipient only.
    """

    model = ReimbursementPackage
    template_name = "reimbursements/package_detail.html"
    context_object_name = "package"
    slug_field = "uuid"
    slug_url_kwarg = "package_uuid"

    def get_queryset(self):
        return (
            super()
            .get_queryset()
            .filter(
                Q(creator=self.request.user) | Q(recipient=self.request.user),
                deleted_at__isnull=True,
            )
            .prefetch_related("records")
            .select_related("creator", "paid_by", "recipient")
        )

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        package: ReimbursementPackage = self.object
        context["records"] = package.records.filter(is_active=True)
        context["is_creator"] = self.request.user == package.creator
        context["is_payer"] = self.request.user == package.paid_by
        context["is_recipient"] = self.request.user == package.recipient
        context["can_delete"] = package.can_delete(self.request.user)
        return context


@method_decorator(ratelimit(key="user", rate="10/m", method="POST"), name="dispatch")
class PackageDeleteView(LoginRequiredMixin, View):
    """Soft-deletes a reimbursement package. Only the creator (always) and the
    recipient (if paid) may delete. Returns 200 JSON on success for HTMX/JS
    callers, or redirects to the package list for traditional form posts."""

    def post(self, request: HttpRequest, package_uuid: str) -> HttpResponse:
        package = get_object_or_404(
            ReimbursementPackage,
            Q(creator=request.user) | Q(recipient=request.user),
            uuid=package_uuid,
            deleted_at__isnull=True,
        )

        if not package.can_delete(request.user):
            messages.error(request, "You do not have permission to delete this package.")
            return redirect(
                reverse("reimbursements:package-detail", kwargs={"package_uuid": package.uuid})
            )

        package.delete_package(request.user)
        logger.info("Package %s soft-deleted by user %s", package.uuid, request.user.id)
        messages.success(request, "Package deleted.")

        if request.headers.get("HX-Request"):
            return HttpResponse(headers={"HX-Redirect": reverse("reimbursements:package-list")})

        return redirect(reverse("reimbursements:package-list"))


@method_decorator(ratelimit(key="user", rate="5/m", method="POST"), name="dispatch")
class CreatePackageFromRecordsView(LoginRequiredMixin, StripeAccountRequiredMixin, View):
    def post(self, request: HttpRequest) -> HttpResponse:
        if request.content_type == "application/json":
            try:
                data: dict[str, Any] = json.loads(request.body)
            except json.JSONDecodeError:
                return JsonResponse({"error": "Invalid JSON payload."}, status=400)

            record_ids: list[int] = data.get("record_ids", [])
            title: str = data.get("title", "Reimbursement Package")
            recipient_email: str = data.get("recipient_email", "").strip()
            try:
                days_valid: int = max(1, min(365, int(data.get("days_valid", 7))))
            except (TypeError, ValueError):
                days_valid = 7
        else:
            record_ids = [
                int(rid) for rid in request.POST.getlist("selected_records") if rid.isdigit()
            ]
            title = request.POST.get("title", "Reimbursement Package")
            recipient_email = request.POST.get("recipient_email", "").strip()
            try:
                days_valid = max(1, min(365, int(request.POST.get("days_valid", 7))))
            except (TypeError, ValueError):
                days_valid = 7

        if not record_ids:
            return JsonResponse({"error": "No records selected."}, status=400)

        if not recipient_email:
            return JsonResponse(
                {"error": "Recipient email is required."},
                status=400,
            )

        user_model = get_user_model()
        try:
            recipient = user_model.objects.get(email__iexact=recipient_email)
        except user_model.DoesNotExist:
            return JsonResponse(
                {"error": "No Papertrail user found with that email address."},
                status=400,
            )

        if recipient == request.user:
            return JsonResponse(
                {"error": "You cannot send a reimbursement package to yourself."},
                status=400,
            )

        with transaction.atomic():
            package = ReimbursementPackage.objects.create(
                creator=request.user,
                recipient=recipient,
                title=title,
                expires_at=timezone.now() + timedelta(days=days_valid),
            )
            records = Record.objects.filter(id__in=record_ids, user=request.user, is_active=True)
            package.records.set(records)

        _send_package_created_notification(package, recipient)

        redirect_url = reverse(
            "reimbursements:package-detail", kwargs={"package_uuid": package.uuid}
        )

        if request.content_type == "application/json":
            return JsonResponse({"redirect_url": redirect_url})

        return redirect(redirect_url)


@method_decorator(ratelimit(key="user", rate="30/m", method="GET"), name="dispatch")
class PaymentSuccessView(LoginRequiredMixin, TemplateView):
    """
    Renders the success confirmation landing page following Stripe payment.
    Falls back to checking Stripe directly if the webhook hasn't fired yet.
    """

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
        """Check Stripe directly for completed payments (webhook fallback)."""
        payment = package.payments.filter(is_completed=False).order_by("-created_at").first()
        if not payment:
            return

        try:
            session = stripe.checkout.Session.retrieve(payment.stripe_checkout_session_id)
        except stripe.error.StripeError:
            logger.warning(
                "Failed to retrieve Stripe session %s", payment.stripe_checkout_session_id
            )
            return

        if session.payment_status == "paid":
            payment.is_completed = True
            payment_intent_id = session.get("payment_intent")
            if payment_intent_id:
                payment.stripe_payment_intent_id = payment_intent_id
            payment.save(update_fields=["is_completed", "stripe_payment_intent_id"])
            package.mark_as_paid(payer=payment.payer)
            logger.info("Fallback: marked package %s as paid via success page", package.uuid)


@method_decorator(ratelimit(key="user", rate="10/m", method="POST"), name="dispatch")
class CreatePackageCheckoutView(LoginRequiredMixin, View):
    """
    Constructs a Stripe Checkout Session for a package and redirects the client.

    Uses POST to prevent prefetchers/crawlers from triggering payment sessions.
    """

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

        existing_payment = (
            package.payments.filter(is_completed=False).order_by("-created_at").first()
        )
        if existing_payment:
            try:
                session = stripe.checkout.Session.retrieve(
                    existing_payment.stripe_checkout_session_id
                )
                if session.status == "open":
                    return redirect(session.url)
            except stripe.error.StripeError:
                logger.warning(
                    "Failed to retrieve existing session %s, creating new one",
                    existing_payment.stripe_checkout_session_id,
                )

        stripe_account = getattr(package.creator, "stripe_account", None)
        stripe_account_id = stripe_account.stripe_account_id if stripe_account else None

        line_items: list[dict[str, Any]] = []
        for record in package.records.filter(is_active=True):
            if record.balance and record.balance > 0:
                product_data: dict[str, Any] = {
                    "name": record.title or "Expense Item",
                }
                if getattr(record, "merchant", None):
                    product_data["description"] = f"Merchant: {record.merchant}"

                line_items.append(
                    {
                        "price_data": {
                            "currency": "usd",
                            "product_data": product_data,
                            "unit_amount": int(record.balance * 100),
                        },
                        "quantity": 1,
                    }
                )

        if not line_items:
            if package.total_amount_cents <= 0:
                messages.error(request, "This package has no payable items.")
                return redirect(
                    reverse("reimbursements:package-detail", kwargs={"package_uuid": package.uuid})
                )
            line_items.append(
                {
                    "price_data": {
                        "currency": "usd",
                        "product_data": {"name": package.title},
                        "unit_amount": package.total_amount_cents,
                    },
                    "quantity": 1,
                }
            )

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
                (Decimal(str(package.total_amount_cents)) * PLATFORM_FEE_PERCENT).quantize(
                    Decimal("1"), rounding=ROUND_DOWN
                )
            )
            if platform_fee_cents < STRIPE_MINIMUM_FEE_CENTS:
                platform_fee_cents = STRIPE_MINIMUM_FEE_CENTS
            checkout_args["payment_intent_data"] = {
                "application_fee_amount": platform_fee_cents,
                "transfer_data": {
                    "destination": stripe_account_id,
                },
            }

        checkout_session = stripe.checkout.Session.create(**checkout_args)

        with transaction.atomic():
            PackagePayment.objects.create(
                package=package,
                payer=request.user,
                stripe_checkout_session_id=checkout_session.id,
                amount_paid=package.total_amount,
            )

        return redirect(checkout_session.url)


def _send_package_created_notification(package, recipient) -> None:
    from .notifications import send_package_created_notification

    send_package_created_notification(package, recipient)
