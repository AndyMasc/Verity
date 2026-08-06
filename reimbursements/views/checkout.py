from typing import Any

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db.models import Q
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.generic import TemplateView
from django_ratelimit.decorators import ratelimit

from .. import services
from ..models import ReimbursementPackage


@method_decorator(ratelimit(key="user", rate="30/m", method="GET"), name="dispatch")
class PaymentSuccessView(LoginRequiredMixin, TemplateView):
    template_name = "reimbursements/payment_success.html"

    def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
        context = super().get_context_data(**kwargs)
        package_uuid = self.request.GET.get("package")
        if package_uuid:
            package = services.get_payment_success_package(self.request.user, package_uuid)
            if package:
                context["package"] = package
        return context


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

        detail_url = reverse(
            "reimbursements:package-detail",
            kwargs={"package_uuid": package.uuid},
        )
        payer_currency = getattr(request.user.settings, "default_currency", "usd")

        outcome = services.create_package_checkout(
            package=package,
            payer=request.user,
            currency=payer_currency,
            success_url=request.build_absolute_uri(
                reverse("reimbursements:payment-success") + f"?package={package.uuid}"
            ),
            cancel_url=request.build_absolute_uri(detail_url),
        )
        if outcome.error:
            messages.error(request, outcome.error)
            return redirect(detail_url)
        return redirect(outcome.redirect_url)
