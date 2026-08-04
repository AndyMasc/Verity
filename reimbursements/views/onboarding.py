import logging

import stripe
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.decorators import method_decorator
from django.views import View
from django_ratelimit.decorators import ratelimit

from .. import services
from ..models import StripeAccount

logger = logging.getLogger(__name__)


@method_decorator(ratelimit(key="user", rate="10/m", method="GET", block=True), name="dispatch")
class StripeOnboardView(LoginRequiredMixin, View):
    def get(self, request: HttpRequest) -> HttpResponse:
        stripe_account = getattr(request.user, "stripe_account", None)

        if not stripe_account:
            stripe_account = StripeAccount.objects.create(user=request.user)

        if stripe_account.is_active:
            return redirect(reverse("reimbursements:package-list"))

        if stripe_account.stripe_account_id:
            try:
                if stripe_account.sync_from_stripe():
                    return redirect(reverse("reimbursements:package-list"))
            except stripe.error.StripeError:
                logger.warning("Failed to retrieve Stripe account for user %s", request.user.id)

        if not stripe_account.stripe_account_id:
            try:
                account = services.create_stripe_account(request.user.email, request.user.id)
                stripe_account.stripe_account_id = account.id
                stripe_account.save(update_fields=["stripe_account_id"])
            except stripe.error.StripeError:
                logger.exception(
                    "Failed to create Stripe express account for user %s",
                    request.user.id,
                )
                messages.error(request, "Unable to initiate Stripe onboarding. Please try again.")
                return redirect(reverse("reimbursements:package-list"))

        refresh_url = request.build_absolute_uri(reverse("reimbursements:stripe-onboard"))
        return_url = request.build_absolute_uri(reverse("reimbursements:package-list"))

        try:
            account_link = services.create_account_link(
                stripe_account.stripe_account_id,
                refresh_url,
                return_url,
            )
            return redirect(account_link.url)
        except stripe.error.StripeError:
            logger.exception("Failed to create Stripe account link for user %s", request.user.id)
            messages.error(
                request,
                "Something went wrong connecting your Stripe account. Please try again.",
            )
            return redirect(reverse("reimbursements:package-list"))
