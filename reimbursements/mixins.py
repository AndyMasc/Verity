from django.contrib import messages
from django.contrib.auth.mixins import UserPassesTestMixin
from django.http import JsonResponse
from django.shortcuts import redirect
from django.urls import reverse

from billing.mixins import FeatureRequiredMixin


class StripeAccountRequiredMixin(UserPassesTestMixin):
    """Ensures the user has an active, onboarded Stripe Connect account.

    Checks for the presence of 'stripe_account' and whether 'is_active' returns True.
    """

    def test_func(self) -> bool:
        stripe_account = getattr(self.request.user, "stripe_account", None)
        return bool(stripe_account and stripe_account.is_active)

    def handle_no_permission(self):
        if not self.request.user.is_authenticated:
            return super().handle_no_permission()

        onboard_url = reverse("reimbursements:stripe-onboard")

        if self.request.content_type and "application/json" in self.request.content_type:
            return JsonResponse(
                {
                    "error": "You must connect your Stripe account before requesting reimbursements.",
                    "redirect_url": onboard_url,
                },
                status=403,
            )

        messages.warning(
            self.request,
            "Please connect your Stripe account to receive payments before continuing.",
        )
        return redirect(onboard_url)


class ReimbursementRequestRequiredMixin(StripeAccountRequiredMixin, FeatureRequiredMixin):
    """Requires a connected Stripe account and the Quick Reimbursement feature.

    Combines the Stripe Connect onboarding check ("StripeAccountRequiredMixin")
    with the paid-only "QUICK_REIMBURSEMENT_REQUEST" feature gate, reusing the
    shared feature-gate logic from "billing.mixins.FeatureRequiredMixin".
    """

    def test_func(self) -> bool:
        if not super().test_func():
            return False
        return FeatureRequiredMixin.test_func(self)

    def handle_no_permission(self):
        if not StripeAccountRequiredMixin.test_func(self):
            return super().handle_no_permission()
        return FeatureRequiredMixin.handle_no_permission(self)
