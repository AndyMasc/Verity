import logging
from typing import cast

import stripe
from django.conf import settings
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
from djstripe.models import Product, Subscription
from djstripe.settings import djstripe_settings

from . import metadata
from .models import CustomUser

logger = logging.getLogger(__name__)


@login_required
def pricing_page(request: HttpRequest) -> HttpResponse:
    user = cast(CustomUser, request.user)

    has_active_sub = user.has_active_subscription

    # Map Stripe product metadata category -> pricing table config. Add a new category + env key here to surface another pricing table.
    pricing_tables = {
        "base_plan": {
            "id": settings.STRIPE_PRICING_TABLE_ID,
            "key": settings.STRIPE_PRICING_TABLE_KEY,
        },
        "storage_plan": {
            "id": settings.STRIPE_STORAGE_TABLE_ID,
            "key": settings.STRIPE_STORAGE_TABLE_KEY,
        },
    }
    default_table = pricing_tables["base_plan"]

    products = list(Product.objects.filter(active=True).prefetch_related("prices"))
    for product in products:
        meta = metadata.PRODUCTS.get(product.id)
        product.features_list = meta.features if meta else []
        product.is_default = meta.is_default if meta else False

        category = (product.metadata or {}).get("category")
        table = pricing_tables.get(category, default_table)
        product.pricing_table_id = table["id"]
        product.pricing_table_key = table["key"]

    free_plan = metadata.PAPERTRAIL_FREE
    free_plan.features_list = free_plan.features
    free_plan.prices = []
    free_plan.is_default = False
    products.insert(0, free_plan)

    base_plans = [
        p
        for p in products
        if hasattr(p, "metadata")
        and isinstance(p.metadata, dict)
        and p.metadata.get("category") == "base_plan"
    ]

    storage_plans = [
        p
        for p in products
        if hasattr(p, "metadata")
        and isinstance(p.metadata, dict)
        and p.metadata.get("category") == "storage_plan"
    ]

    return render(
        request,
        "billing/pricing_page.html",
        context={
            "pricing_tables": pricing_tables,
            "products": products,
            "free_plan": free_plan,
            "has_active_subscription": has_active_sub,
            "base_plans": base_plans,
            "storage_plans": storage_plans,
        },
    )


@login_required
@ratelimit(key="user", rate="10/m", method=["GET"], block=True)
def subscription_confirm(request: HttpRequest) -> HttpResponse:
    stripe.api_key = djstripe_settings.STRIPE_SECRET_KEY

    session_id = request.GET.get("session_id")
    if not session_id:
        return HttpResponseBadRequest("Missing session ID.")

    try:
        session = stripe.checkout.Session.retrieve(session_id)
        if session.payment_status != "paid":
            return HttpResponseBadRequest("Subscription is not paid.")
        if not session.subscription:
            return HttpResponseBadRequest("Session is not a subscription checkout.")

        subscription = stripe.Subscription.retrieve(str(session.subscription))
    except stripe.error.StripeError as e:
        logger.error("Stripe API error during subscription confirmation: %s", e)
        messages.error(
            request, "We encountered an error confirming your subscription. Please contact support."
        )
        return redirect("core:dashboard")

    djstripe_subscription = Subscription.sync_from_stripe_data(subscription)

    subscription_holder = request.user.get_verified_session_holder(session)
    if subscription_holder is None:
        return HttpResponseBadRequest("Invalid session payload.")

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

    stripe.api_key = djstripe_settings.STRIPE_SECRET_KEY
    portal_session = stripe.billing_portal.Session.create(
        customer=customer.id,
        return_url=request.build_absolute_uri(reverse("core:profile_page")),
    )
    return HttpResponseRedirect(portal_session.url)


@login_required
@require_POST
@ratelimit(key="user", rate="10/m", method="POST", block=True)
def create_checkout_session(request: HttpRequest) -> HttpResponse:
    base_price_id = request.POST.get("base_price_id") if request.POST.get("base_price_id") else None
    storage_price_id = (
        request.POST.get("storage_price_id") if request.POST.get("storage_price_id") else None
    )

    if not base_price_id and not storage_price_id:
        return HttpResponseBadRequest("Select a plan to proceed to checkout.")

    line_items = []
    if base_price_id:
        line_items.append({"price": base_price_id, "quantity": 1})
    if storage_price_id:
        quantity = request.POST.get("quantity") if request.POST.get("quantity") else 0
        line_items.append({"price": storage_price_id, "quantity": quantity})

    stripe.api_key = djstripe_settings.STRIPE_SECRET_KEY
    checkout_session = stripe.checkout.Session.create(
        line_items=line_items,
        mode="subscription",
        success_url=(
            request.build_absolute_uri(reverse("subscription_confirm")) + "?session_id={CHECKOUT_SESSION_ID}"
        ),
        cancel_url=request.build_absolute_uri(reverse("pricing_page")),
    )
    return HttpResponseRedirect(checkout_session.url)
