import hashlib
import logging
import time
from typing import cast

import stripe
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import (
    HttpRequest,
    HttpResponse,
    HttpResponseBadRequest,
    HttpResponseRedirect,
)
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST
from django_ratelimit.decorators import ratelimit
from djstripe.models import Customer

from core.apps import posthog_client

from . import services
from .context_processors import invalidate_plan_usage_caches
from .models import CustomUser

logger = logging.getLogger(__name__)


@login_required
def pricing_page(request: HttpRequest) -> HttpResponse:
    if request.GET.get("checkout") == "canceled":
        messages.info(
            request,
            "Checkout canceled. You were not charged. Select a plan to try again.",
        )
    return render(request, "billing/pricing_page.html", services.pricing_context(request.user))


@login_required
@ratelimit(key="user", rate="10/m", method=["GET"], block=True)
def subscription_confirm(request: HttpRequest) -> HttpResponse:
    try:
        session_id = request.GET.get("session_id")
        if not session_id:
            return HttpResponseBadRequest("Missing session_id parameter.")
        services._configure()
        session = stripe.checkout.Session.retrieve(session_id)

        if session.status != "complete":
            return HttpResponseBadRequest("Checkout did not complete.")
        if not session.subscription:
            return HttpResponseBadRequest("Session is not a subscription checkout.")
        if session.get("client_reference_id") != str(request.user.pk):
            return HttpResponseBadRequest("Session does not belong to this user.")

    except stripe.error.StripeError as e:
        logger.error("Stripe API error during subscription confirmation: %s", e)
        messages.error(
            request,
            "We encountered an error confirming your subscription. Please contact support.",
        )
        return redirect("core:dashboard")

    invalidate_plan_usage_caches(request.user.pk)
    if posthog_client is not None:
        posthog_client.capture("subscription_activated")
    messages.success(
        request,
        "Your subscription is now active! You can manage your plan and billing in your profile.",
    )
    return redirect("core:dashboard")


@login_required
@require_POST
@ratelimit(key="user", rate="10/m", method="POST", block=True)
def create_portal_session(request: HttpRequest) -> HttpResponse:
    customer = Customer.get_or_create(request.user)[0]
    if customer is None:
        return HttpResponseBadRequest("No Stripe customer associated with this account.")

    services._configure()
    portal_session = stripe.billing_portal.Session.create(
        customer=customer.id,
        return_url=request.build_absolute_uri(reverse("core:profile_page")),
    )
    return HttpResponseRedirect(portal_session.url)


@login_required
@require_POST
@ratelimit(key="user", rate="15/m", method="POST", block=True)
def purchase_subscription(request: HttpRequest) -> HttpResponse:
    user = cast(CustomUser, request.user)
    customer = Customer.get_or_create(subscriber=request.user)[0]
    subscription = customer.subscriptions.active().order_by("-created").first()

    if subscription is None:
        line_items = services.fetch_line_items(request)
        if isinstance(line_items, HttpResponseBadRequest):
            return line_items

        idempotency_key = hashlib.sha256(  # Hourly-unique per user and line items, so a retry for the same user and line items cannot double-charge.
            f"{user.pk}:{','.join(sorted(item['price'] for item in line_items))}:{int(time.time() // 3600)}".encode()
        ).hexdigest()
        success_url = (
            request.build_absolute_uri(reverse("subscription_confirm"))
            + "?session_id={CHECKOUT_SESSION_ID}"
        )
        cancel_url = (
            request.build_absolute_uri(reverse("billing:pricing_page")) + "?checkout=canceled"
        )
        try:
            services._configure()
            checkout_session = stripe.checkout.Session.create(
                customer=customer.id,
                mode="subscription",
                line_items=line_items,
                client_reference_id=str(user.pk),
                success_url=success_url,
                cancel_url=cancel_url,
                idempotency_key=idempotency_key,
            )
            invalidate_plan_usage_caches(user.pk)
        except stripe.error.StripeError as e:
            logger.error("Stripe API error during checkout session creation: %s", e)
            messages.error(
                request,
                "We encountered an error creating your checkout session. Please try again.",
            )
            return redirect("billing:pricing_page")
        return HttpResponseRedirect(checkout_session.url)

    try:
        sanitized_line_items = services.sanitize_line_items(user, line_items)
        if not sanitized_line_items:
            messages.info(
                request,
                "No valid plans found after sanitization. Your subscription remains unchanged.",
            )
            return redirect("core:dashboard")
        subscription.update(
            items=sanitized_line_items,
            proration_behavior="create_prorations",
        )
        messages.success(
            request,
            "Your subscription has been updated successfully.",
        )
        invalidate_plan_usage_caches(user.pk)
    except stripe.error.StripeError as e:
        logger.error("Stripe API error during subscription update: %s", e)
        messages.error(
            request,
            "We encountered an error updating your subscription. Please contact support.",
        )
    return redirect("core:dashboard")
