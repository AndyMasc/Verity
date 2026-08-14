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

from .. import services
from ..models import ReimbursementPackage
from ..verification import send_verification_code, verify_code

_VERIFIED_SESSION_PREFIX = "_reimbursement_verified"


def _code_step_url(pay_url: str, email: str) -> str:
    """The pay page URL at the code-entry step, carrying the verified address.

    The code step needs the recipient email to re-submit it with the code.
    Without it the email field renders empty and verification can't complete.
    """
    return f"{pay_url}?step=code&{urlencode({'email': email})}"


def _verified_in_session(request: HttpRequest, package: ReimbursementPackage) -> bool:
    return bool(request.session.get(f"{_VERIFIED_SESSION_PREFIX}:{package.uuid}"))


def _mark_verified_in_session(request: HttpRequest, package: ReimbursementPackage) -> None:
    request.session[f"{_VERIFIED_SESSION_PREFIX}:{package.uuid}"] = True


@method_decorator(ratelimit(key="ip", rate="60/m", method="GET"), name="dispatch")
class PackagePayView(View):
    """Public view of a package for external payment.

    Unverified visitors only see the verification step; the package amount,
    line items, and pay button are hidden until the recipient's email is
    verified in this session. Opening the page for real also moves a queued
    package to open.
    """

    template_name = "reimbursements/package_pay.html"

    def get(self, request: HttpRequest, package_uuid: str) -> HttpResponse:
        package = get_object_or_404(
            ReimbursementPackage.objects.select_related("creator"),
            uuid=package_uuid,
            deleted_at__isnull=True,
        )

        if package.status == ReimbursementPackage.Status.PAID:
            return self._render(request, package, state="paid")
        if package.is_expired:
            return self._render(request, package, state="expired")

        if not _verified_in_session(request, package):
            step = request.GET.get("step", "email")
            if step not in ("email", "code"):
                step = "email"
            email = request.GET.get("email", "")
            return self._render(request, package, state="verify", verify_step=step, email=email)

        services.activate_queued_package(package)
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
    """Email the recipient a one-time code for the package."""

    def post(self, request: HttpRequest, package_uuid: str) -> HttpResponse:
        package = get_object_or_404(
            ReimbursementPackage, uuid=package_uuid, deleted_at__isnull=True
        )
        email = request.POST.get("email", "").strip()
        pay_url = reverse("reimbursements:pay-package", kwargs={"package_uuid": package.uuid})

        if not email:
            messages.error(request, "Enter your email to continue.")
            return redirect(pay_url)

        if not send_verification_code(package, email):
            messages.error(request, "That email does not match the recipient for this request.")
            return redirect(pay_url)

        messages.success(
            request, "A verification code was sent to your inbox. It expires in 10 minutes."
        )
        return redirect(_code_step_url(pay_url, email))


@method_decorator(ratelimit(key="ip", rate="10/m", method="POST", block=True), name="dispatch")
class VerifyEmailCodeView(View):
    """Confirm the emailed code and unlock the package for this session."""

    def post(self, request: HttpRequest, package_uuid: str) -> HttpResponse:
        package = get_object_or_404(
            ReimbursementPackage, uuid=package_uuid, deleted_at__isnull=True
        )
        email = request.POST.get("email", "").strip()
        code = request.POST.get("code", "").strip()
        pay_url = reverse("reimbursements:pay-package", kwargs={"package_uuid": package.uuid})

        if not email or not code:
            messages.error(request, "Enter both your email and the verification code.")
            return redirect(_code_step_url(pay_url, email))

        ok, error = verify_code(package, email, code)
        if not ok:
            messages.error(request, error)
            return redirect(_code_step_url(pay_url, email))

        _mark_verified_in_session(request, package)
        return redirect(pay_url)


@method_decorator(ratelimit(key="ip", rate="10/m", method="POST", block=True), name="dispatch")
class PayPackageCheckoutView(View):
    """Start a Stripe checkout for an external payer.

    Requires a verified session for anonymous visitors; authenticated users
    must be the package creator or registered recipient (the usual payment
    eligibility checks still apply).
    """

    def post(self, request: HttpRequest, package_uuid: str) -> HttpResponse:
        package = get_object_or_404(
            ReimbursementPackage.objects.select_related("creator"),
            uuid=package_uuid,
            deleted_at__isnull=True,
        )
        pay_url = reverse("reimbursements:pay-package", kwargs={"package_uuid": package.uuid})

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

        services.activate_queued_package(package)

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
        return redirect(outcome.redirect_url)
