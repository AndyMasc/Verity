"""Public, unauthenticated reimbursement package views.

External recipients reach these pages from an emailed link whose unguessable UUID
is the capability: holding it grants access to the package details and checkout.
Packages expire, so the link has a bounded lifetime.
"""

from typing import Any

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.views import View
from django_ratelimit.decorators import ratelimit

from core.apps import posthog_client

from .. import services
from ..forms import (
    CheckoutTurnstileForm,
)
from ..models import ReimbursementPackage


@method_decorator(ratelimit(key="ip", rate="60/m", method="GET", block=True), name="dispatch")
class PackagePayView(View):
    template_name = "reimbursements/package_pay.html"

    def get(self, request: HttpRequest, package_uuid: str) -> HttpResponse:
        package = get_object_or_404(
            ReimbursementPackage.objects.select_related("creator"),
            uuid=package_uuid,
            deleted_at__isnull=True,
        )

        if package.status == ReimbursementPackage.Status.PAID:
            return self.render_page(request, package, state="paid")
        if package.is_expired:
            return self.render_page(request, package, state="expired")

        package.activate()
        package.refresh_from_db()
        if package.status == ReimbursementPackage.Status.PAID:
            return self.render_page(request, package, state="paid")

        user_currency = package.currency
        detail = package.detail_items(user_currency)
        return self.render_page(
            request,
            package,
            state="ready",
            user_currency=user_currency,
            record_items=detail.record_items,
            converted_total=detail.converted_total,
            original_total=detail.original_total,
        )

    def render_page(
        self, request: HttpRequest, package: ReimbursementPackage, **extra: Any
    ) -> HttpResponse:
        context = {
            "package": package,
            "is_public": True,
            "email": "",
            **extra,
        }
        return render(request, self.template_name, context)


@method_decorator(ratelimit(key="ip", rate="15/m", method="POST", block=True), name="dispatch")
class PayPackageCheckoutView(View):
    def post(self, request: HttpRequest, package_uuid: str) -> HttpResponse:
        package = get_object_or_404(
            ReimbursementPackage.objects.select_related("creator"),
            uuid=package_uuid,
            deleted_at__isnull=True,
        )
        pay_url = reverse("reimbursements:pay-package", kwargs={"package_uuid": package.uuid})

        form = CheckoutTurnstileForm(request.POST, request=request)
        if not form.is_valid():
            for error_list in form.errors.values():
                for error in error_list:
                    messages.error(request, error)
            return redirect(pay_url)

        if request.user.is_authenticated:
            if request.user not in (package.creator, package.recipient):
                raise PermissionDenied
            payer = request.user
            payer_currency = getattr(
                getattr(payer, "settings", None), "default_currency", package.currency
            )
        else:
            payer = None
            payer_currency = package.currency

        package.activate()

        outcome = services.create_package_checkout(
            package=package,
            payer=payer,
            payer_email=form.cleaned_data.get("email", "").strip(),
            currency=payer_currency,
            success_url=request.build_absolute_uri(
                reverse("reimbursements:payment-success") + f"?package={package.uuid}"
            ),
            cancel_url=request.build_absolute_uri(pay_url),
        )
        if outcome.error:
            messages.error(request, outcome.error)
            return redirect(pay_url)
        # The external payer reaches Stripe through here, so without this the
        # checkout-started event only ever described signed-in payers.
        if posthog_client is not None:
            posthog_client.capture(
                "reimbursement_checkout_started",
                properties={
                    "payer_type": "external",
                },
            )
        return redirect(outcome.redirect_url)
