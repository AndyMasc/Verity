"""Public, unauthenticated reimbursement package views.

External recipients reach these pages from the emailed payment link. They
must verify they are the intended recipient (email + one-time code) before
any package details or the Pay button are shown, and a verified session is
required to start a checkout.
"""

from typing import Any
from urllib.parse import urlencode

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
    RequestVerificationCodeForm,
    VerifyEmailCodeForm,
)
from ..models import ReimbursementPackage, external_payer_distinct_id
from ..verification import send_verification_code, verify_code

_VERIFIED_SESSION_PREFIX = "_reimbursement_verified"


def _code_step_url(pay_url: str, email: str) -> str:
    return f"{pay_url}?step=code&{urlencode({'email': email})}"


def _verified_in_session(request: HttpRequest, package: ReimbursementPackage) -> bool:
    return bool(request.session.get(f"{_VERIFIED_SESSION_PREFIX}:{package.uuid}"))


def _mark_verified_in_session(request: HttpRequest, package: ReimbursementPackage) -> None:
    request.session[f"{_VERIFIED_SESSION_PREFIX}:{package.uuid}"] = True


@method_decorator(ratelimit(key="ip", rate="60/m", method="GET", block=True), name="dispatch")
class PackagePayView(View):
    template_name = "reimbursements/package_pay.html"

    def get(self, request: HttpRequest, package_uuid: str) -> HttpResponse:
        package = get_object_or_404(
            ReimbursementPackage.objects.select_related("creator"),
            uuid=package_uuid,
            deleted_at__isnull=True,
        )

        verified = _verified_in_session(request, package)

        if package.status == ReimbursementPackage.Status.PAID:
            return self._render(request, package, state="paid")
        if package.is_expired:
            return self._render(request, package, state="expired")

        if not verified:
            step = request.GET.get("step", "email")
            if step not in ("email", "code"):
                step = "email"
            email = request.GET.get("email", "")
            return self._render(request, package, state="verify", verify_step=step, email=email)

        package.activate()
        package.refresh_from_db()
        if package.status == ReimbursementPackage.Status.PAID:
            return self._render(request, package, state="paid")

        user_currency = package.currency
        detail = package.detail_items(user_currency)
        return self._render(
            request,
            package,
            state="verified",
            user_currency=user_currency,
            record_items=detail.record_items,
            converted_total=detail.converted_total,
            original_total=detail.original_total,
        )

    def _render(
        self, request: HttpRequest, package: ReimbursementPackage, **extra: Any
    ) -> HttpResponse:
        context = {
            "package": package,
            "is_public": True,
            "email": "",
            **extra,
        }
        return render(request, self.template_name, context)


@method_decorator(ratelimit(key="ip", rate="5/m", method="POST", block=True), name="dispatch")
class RequestVerificationCodeView(View):
    def post(self, request: HttpRequest, package_uuid: str) -> HttpResponse:
        package = get_object_or_404(
            ReimbursementPackage, uuid=package_uuid, deleted_at__isnull=True
        )
        pay_url = reverse("reimbursements:pay-package", kwargs={"package_uuid": package.uuid})

        form = RequestVerificationCodeForm(request.POST, request=request)
        if not form.is_valid():
            for error_list in form.errors.values():
                for error in error_list:
                    messages.error(request, error)
            return redirect(pay_url)

        email = form.cleaned_data.get("email", "").strip()

        if not send_verification_code(package, email):
            messages.error(request, "That email does not match the recipient for this request.")
            return redirect(pay_url)

        messages.success(
            request,
            "A verification code was sent to your inbox. It expires in 10 minutes.",
        )
        return redirect(_code_step_url(pay_url, email))


@method_decorator(ratelimit(key="ip", rate="15/m", method="POST", block=True), name="dispatch")
class VerifyEmailCodeView(View):
    def post(self, request: HttpRequest, package_uuid: str) -> HttpResponse:
        package = get_object_or_404(
            ReimbursementPackage, uuid=package_uuid, deleted_at__isnull=True
        )
        pay_url = reverse("reimbursements:pay-package", kwargs={"package_uuid": package.uuid})

        form = VerifyEmailCodeForm(request.POST, request=request)
        if not form.is_valid():
            email = request.POST.get("email", "").strip()
            for error_list in form.errors.values():
                for error in error_list:
                    messages.error(request, error)
            return redirect(_code_step_url(pay_url, email))

        email = form.cleaned_data.get("email", "").strip()
        code = form.cleaned_data.get("code", "").strip()

        ok, error = verify_code(package, email, code)
        if not ok:
            messages.error(request, error)
            return redirect(_code_step_url(pay_url, email))

        _mark_verified_in_session(request, package)
        # The step that turns an anonymous visitor into a known payer, so the
        # recipient funnel has a middle step between "viewed" and "paid".
        if posthog_client is not None:
            posthog_client.capture(
                "reimbursement_recipient_verified",
                distinct_id=external_payer_distinct_id(email),
                properties={
                    "package_uuid": str(package.uuid),
                    "payer_type": "external",
                },
            )
        return redirect(pay_url)


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
            if not _verified_in_session(request, package):
                return redirect(pay_url)
            payer = None
            payer_currency = package.currency

        package.activate()

        outcome = services.create_package_checkout(
            package=package,
            payer=payer,
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
