import json
import logging
from typing import Any

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Q
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.generic import DetailView, ListView
from django_ratelimit.decorators import ratelimit

from billing import features

from .. import services
from ..mixins import ReimbursementRequestRequiredMixin
from ..models import ReimbursementPackage
from ..notifications import send_package_created_notification

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

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        sent_packages = list(context["packages"])

        paid_by_me = list(
            ReimbursementPackage.objects.filter(paid_by=self.request.user, deleted_at__isnull=True)
            .with_annotated_total()
            .with_prefetched_active_records()
            .select_related("creator", "recipient", "paid_by")
            .order_by("-paid_at")[:25]
        )
        sent_to_me = list(
            ReimbursementPackage.objects.filter(
                recipient=self.request.user, deleted_at__isnull=True
            )
            .with_annotated_total()
            .with_prefetched_active_records()
            .select_related("creator", "paid_by")
            .order_by("-created_at")[:25]
        )

        user_currency = getattr(
            getattr(self.request.user, "settings", None), "default_currency", "usd"
        )
        ReimbursementPackage.prefetch_converted_totals(
            sent_packages + paid_by_me + sent_to_me, user_currency
        )

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

        user_currency = getattr(
            getattr(self.request.user, "settings", None), "default_currency", "usd"
        )
        context["user_currency"] = user_currency

        detail = package.detail_items(user_currency)
        context["record_items"] = detail.record_items
        context["converted_total"] = detail.converted_total
        context["original_total"] = detail.original_total
        context["package_currency"] = package.currency
        context["is_recipient"] = package.recipient == self.request.user
        context["is_payer"] = package.paid_by == self.request.user
        context["can_delete"] = package.can_delete(self.request.user)
        return context


@method_decorator(ratelimit(key="user", rate="10/m", method="POST", block=True), name="dispatch")
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
                reverse(
                    "reimbursements:package-detail",
                    kwargs={"package_uuid": package.uuid},
                )
            )

        package.delete_package(request.user)
        logger.info("Package %s soft-deleted by user %s", package.uuid, request.user.id)
        messages.success(request, "Package deleted.")

        if request.headers.get("HX-Request"):
            return HttpResponse(headers={"HX-Redirect": reverse("reimbursements:package-list")})

        return redirect(reverse("reimbursements:package-list"))


def _clamp_days_valid(raw: Any) -> int:
    try:
        return max(1, min(365, int(raw)))
    except TypeError, ValueError:
        return 7


@method_decorator(ratelimit(key="user", rate="5/m", method="POST", block=True), name="dispatch")
class CreatePackageFromRecordsView(LoginRequiredMixin, ReimbursementRequestRequiredMixin, View):
    required_feature = features.QUICK_REIMBURSEMENT_REQUEST

    def post(self, request: HttpRequest) -> HttpResponse:
        if request.content_type and "application/json" in request.content_type:
            try:
                data: dict[str, Any] = json.loads(request.body)
            except json.JSONDecodeError:
                return JsonResponse({"error": "Invalid JSON payload."}, status=400)

            record_ids: list[int] = data.get("record_ids", [])
            title: str = data.get("title", "Reimbursement Package")
            recipient_email: str = data.get("recipient_email", "").strip()
            days_valid = _clamp_days_valid(data.get("days_valid", 7))
        else:
            record_ids = [
                int(rid) for rid in request.POST.getlist("selected_records") if rid.isdigit()
            ]
            title = request.POST.get("title", "Reimbursement Package")
            recipient_email = request.POST.get("recipient_email", "").strip()
            days_valid = _clamp_days_valid(request.POST.get("days_valid", 7))

        if not record_ids:
            return JsonResponse({"error": "No records selected."}, status=400)

        title = title.strip()[:255] or "Reimbursement Package"

        if not recipient_email:
            return JsonResponse(
                {"error": "Recipient email is required."},
                status=400,
            )

        package, error = services.create_reimbursement_package(
            creator=request.user,
            recipient_email=recipient_email,
            record_ids=record_ids,
            title=title,
            days_valid=days_valid,
        )
        if error:
            return JsonResponse({"error": error}, status=400)

        send_package_created_notification(package)

        redirect_url = reverse(
            "reimbursements:package-detail", kwargs={"package_uuid": package.uuid}
        )

        if request.content_type and "application/json" in request.content_type:
            return JsonResponse({"redirect_url": redirect_url})

        return redirect(redirect_url)
