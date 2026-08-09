import logging
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
from djstripe.models import (
    Customer,
    Price,
    Subscription,
)
from djstripe.settings import djstripe_settings

from . import metadata, services
from .models import CustomUser

logger = logging.getLogger(__name__)


@login_required
def pricing_page(request: HttpRequest) -> HttpResponse:
    return render(request, "billing/pricing_page.html", services.pricing_context(request.user))


@login_required
@ratelimit(key="user", rate="10/m", method=["GET"], block=True)
def subscription_confirm(request: HttpRequest) -> HttpResponse:
    session_id = request.GET.get("session_id")
    if not session_id:
        return HttpResponseBadRequest("Missing session ID.")

    try:
        session = services.retrieve_checkout_session(session_id)
        if session.payment_status != "paid":
            return HttpResponseBadRequest("Subscription is not paid.")
        if not session.subscription:
            return HttpResponseBadRequest("Session is not a subscription checkout.")

        subscription = services.retrieve_subscription(str(session.subscription))
    except stripe.error.StripeError as e:
        logger.error("Stripe API error during subscription confirmation: %s", e)
        messages.error(
            request,
            "We encountered an error confirming your subscription. Please contact support.",
        )
        return redirect("core:dashboard")

    subscription_holder = request.user.get_verified_session_holder(session)
    if subscription_holder is None:
        return HttpResponseBadRequest("Invalid session payload.")

    djstripe_subscription = Subscription.sync_from_stripe_data(subscription)
    subscription_holder.handle_new_subscription(djstripe_subscription)

    messages.success(request, "Your subscription has been updated successfully!")
    return redirect("core:dashboard")


@login_required
@require_POST
@ratelimit(key="user", rate="10/m", method="POST", block=True)
def create_portal_session(request: HttpRequest) -> HttpResponse:
    user = cast(CustomUser, request.user)
    customer = user.customer
    if customer is None:
        return HttpResponseBadRequest("No Stripe customer associated with this account.")

    portal_session = services.create_billing_portal_session(
        customer=customer.id,
        return_url=request.build_absolute_uri(reverse("core:profile_page")),
    )
    return HttpResponseRedirect(portal_session.url)


def _validated_price(price_id: str | None, category: str) -> str | None:
    """Return the price ID only if it belongs to an active product in "category"."""
    if not price_id:
        return None
    price = Price.objects.filter(id=price_id, active=True).select_related("product").first()
    if price is None or price.product_id is None:
        return None
    meta = metadata.PRODUCTS.get(price.product_id)
    if meta is None or meta.category != category:
        return None
    return price_id


def _checkout_quantity(raw_qty: str | None, max_quantity: int = 100) -> int:
    """Get checkout quantity from POST request, for stackable plans and scalability"""
    try:
        quantity = int(raw_qty) if raw_qty else 1
    except TypeError, ValueError:
        quantity = 1
    return max(1, min(quantity, max_quantity))


@login_required
@require_POST
@ratelimit(key="user", rate="10/m", method="POST", block=True)
def create_checkout_session(request: HttpRequest) -> HttpResponse:
    user = cast(CustomUser, request.user)

    base_price_id = _validated_price(request.POST.get("base_price_id"), "base_plan")
    storage_price_id = _validated_price(request.POST.get("storage_price_id"), "storage_plan")

    if not base_price_id and not storage_price_id:
        return HttpResponseBadRequest("Select a valid plan to proceed to checkout.")

    line_items = []
    if base_price_id:
        line_items.append({"price": base_price_id, "quantity": 1})

    if storage_price_id:
        line_items.append(
            {
                "price": storage_price_id,
                "quantity": _checkout_quantity(request.POST.get("quantity")),
            }
        )

    customer = user.customer
    if (
        customer is None
        or customer.livemode != djstripe_settings.STRIPE_LIVE_MODE
        or services.customer_missing_in_stripe(customer.id)
    ):
        customer = None

    if customer is None:
        customer, _ = Customer.get_or_create(user)
        # get_or_create may return a stale row whose Stripe record was deleted;
        # unlink it so a brand-new customer is created instead.
        if services.customer_missing_in_stripe(customer.id):
            Customer.objects.filter(id=customer.id, subscriber=user).update(subscriber=None)
            customer, _ = Customer.get_or_create(user)
        if user.customer_id != customer.id:
            user.customer = customer
            user.save(update_fields=["customer"])

    try:
        checkout_session = services.create_checkout_session(
            customer=customer.id,
            line_items=line_items,
            success_url=request.build_absolute_uri(reverse("subscription_confirm"))
            + "?session_id={CHECKOUT_SESSION_ID}",
            cancel_url=request.build_absolute_uri(reverse("pricing_page")),
        )
        return HttpResponseRedirect(checkout_session.url)
    except stripe.error.StripeError as e:
        logger.error("Stripe error creating checkout session: %s", e)
        return HttpResponseBadRequest("Unable to start checkout. Please try again.")
    except Exception:
        logger.exception("Unexpected error creating checkout session")
        return HttpResponseBadRequest("Unable to start checkout. Please try again.")
