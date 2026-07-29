import json
import logging
from datetime import timedelta
from decimal import Decimal
from typing import Any

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
from django.views.generic import DetailView, ListView
from django_ratelimit.decorators import ratelimit

from core.exchange_rates import convert as convert_currency
from core.exchange_rates import get_rates
from records.models import Record

from ..mixins import StripeAccountRequiredMixin
from ..models import ReimbursementPackage

logger = logging.getLogger(__name__)


class PackageListView(LoginRequiredMixin, ListView):
    model = ReimbursementPackage
    template_name = "reimbursements/package_list.html"
    context_object_name = "packages"
    paginate_by = 15

    def get_queryset(self):
        return (
            super()
            .get_queryset()
            .filter(creator=self.request.user, deleted_at__isnull=True)
            .with_annotated_total()
            .with_prefetched_active_records()
            .select_related("recipient", "paid_by")
            .order_by("-id")
        )

    def _precompute_displays(self, packages, to_currency: str) -> None:
        if not packages:
            return
        rates = get_rates("USD")
        for pkg in packages:
            active = [r for r in pkg.records.all() if r.is_active and r.balance]
            if not active:
                pkg._prefetched_converted_total = Decimal("0.00")
                continue
            total = Decimal("0.00")
            for r in active:
                total += convert_currency(r.balance, r.currency, to_currency, rates=rates)
            pkg._prefetched_converted_total = total

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        sent_packages = list(context["packages"])

        paid_by_me = list(
            ReimbursementPackage.objects.filter(paid_by=self.request.user, deleted_at__isnull=True)
            .with_annotated_total()
            .with_prefetched_active_records()
            .select_related("creator", "recipient")
            .order_by("-paid_at")[:25]
        )
        sent_to_me = list(
            ReimbursementPackage.objects.filter(
                recipient=self.request.user, deleted_at__isnull=True
            )
            .with_annotated_total()
            .with_prefetched_active_records()
            .select_related("creator")
            .order_by("-created_at")[:25]
        )

        user_currency = getattr(
            getattr(self.request.user, "settings", None), "default_currency", "usd"
        )
        self._precompute_displays(sent_packages + paid_by_me + sent_to_me, user_currency)

        context["sections"] = [
            ("Sent by You", sent_packages, "sent"),
            ("Paid by You", paid_by_me, "paid"),
            ("Sent to You", sent_to_me, "received"),
        ]
        stripe_account = getattr(self.request.user, "stripe_account", None)
        context["stripe_connected"] = bool(stripe_account and stripe_account.is_active)
        return context


@method_decorator(ratelimit(key="user", rate="30/m", method="GET"), name="dispatch")
class PackageDetailView(LoginRequiredMixin, DetailView):
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

        payer_settings = getattr(self.request.user, "settings", None)
        user_currency = getattr(payer_settings, "default_currency", "usd")
        context["user_currency"] = user_currency

        all_records = list(package.records.all())

        user_rates = get_rates("USD")
        record_items = []
        converted_total = Decimal("0")
        original_total = Decimal("0")

        if all_records:
            HistoricalRecord = Record.history.model
            record_ids = [r.id for r in all_records]
            first_histories: dict[int, object] = {}
            for h in HistoricalRecord.objects.filter(id__in=record_ids).order_by("history_date"):
                if h.id not in first_histories:
                    first_histories[h.id] = h

            for rec in all_records:
                first = first_histories.get(rec.id)
                orig_bal = first.balance if first else rec.balance
                orig_cc = first.currency if first else rec.currency

                orig_converted = convert_currency(
                    orig_bal, orig_cc, user_currency, rates=user_rates
                )
                current_converted = (
                    convert_currency(rec.balance, rec.currency, user_currency, rates=user_rates)
                    if rec.balance
                    else orig_converted
                )

                converted_total += current_converted
                original_total += convert_currency(
                    orig_bal, orig_cc, package.currency, rates=user_rates
                )

                record_items.append(
                    {
                        "record": rec,
                        "original_converted": orig_converted,
                        "requested_converted": current_converted,
                        "converted_currency": user_currency,
                        "is_inactive": not rec.is_active,
                    }
                )

        context["record_items"] = record_items
        context["converted_total"] = converted_total
        context["original_total"] = original_total
        context["package_currency"] = package.currency
        context["is_recipient"] = package.recipient == self.request.user
        context["is_payer"] = package.paid_by == self.request.user
        context["can_delete"] = package.can_delete(self.request.user)
        return context


@method_decorator(ratelimit(key="user", rate="10/m", method="POST"), name="dispatch")
class PackageDeleteView(LoginRequiredMixin, View):
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
        if request.content_type and "application/json" in request.content_type:
            try:
                data: dict[str, Any] = json.loads(request.body)
            except json.JSONDecodeError:
                return JsonResponse({"error": "Invalid JSON payload."}, status=400)

            record_ids: list[int] = data.get("record_ids", [])
            title: str = data.get("title", "Reimbursement Package")
            recipient_email: str = data.get("recipient_email", "").strip()
            try:
                days_valid: int = max(1, min(365, int(data.get("days_valid", 7))))
            except TypeError, ValueError:
                days_valid = 7
        else:
            record_ids = [
                int(rid) for rid in request.POST.getlist("selected_records") if rid.isdigit()
            ]
            title = request.POST.get("title", "Reimbursement Package")
            recipient_email = request.POST.get("recipient_email", "").strip()
            try:
                days_valid = max(1, min(365, int(request.POST.get("days_valid", 7))))
            except TypeError, ValueError:
                days_valid = 7

        if not record_ids:
            return JsonResponse({"error": "No records selected."}, status=400)

        title = title.strip()[:255]
        if not title:
            title = "Reimbursement Package"

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

        records = Record.objects.filter(id__in=record_ids, user=request.user, is_active=True)
        if not records.exists():
            return JsonResponse({"error": "No valid records found."}, status=400)

        package_currency = getattr(request.user.settings, "default_currency", "usd")

        with transaction.atomic():
            package = ReimbursementPackage.objects.create(
                creator=request.user,
                recipient=recipient,
                title=title,
                currency=package_currency,
                expires_at=timezone.now() + timedelta(days=days_valid),
            )
            package.records.set(records)

        from ..notifications import send_package_created_notification

        send_package_created_notification(package, recipient)

        redirect_url = reverse(
            "reimbursements:package-detail", kwargs={"package_uuid": package.uuid}
        )

        if request.content_type and "application/json" in request.content_type:
            return JsonResponse({"redirect_url": redirect_url})

        return redirect(redirect_url)
