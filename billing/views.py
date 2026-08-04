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
    Product,
    Subscription,
)
from djstripe.settings import djstripe_settings

from . import metadata, services
from .models import CustomUser

logger = logging.getLogger(__name__)


def pricing_context(request: HttpRequest) -> dict:
    """Build the context shared by the pricing page and the landing page."""
    user = request.user

    products = list(Product.objects.filter(active=True).prefetch_related("prices"))
    for product in products:
        meta = metadata.PRODUCTS.get(product.id)
        product.features_list = meta.features if meta else []

    free_plan = metadata.PAPERTRAIL_FREE
    free_plan.features_list = free_plan.features
    free_plan.prices = []
    free_plan.metadata = {"category": "base_plan"}
    products.insert(0, free_plan)

    def _by_category(category: str) -> list:
        return [
            p
            for p in products
            if isinstance(getattr(p, "metadata", None), dict)
            and p.metadata.get("category") == category
        ]

    return {
        "products": products,
        "free_plan": free_plan,
        "has_active_subscription": bool(user.is_authenticated and user.has_active_subscription),
        "base_plans": _by_category("base_plan"),
        "storage_plans": _by_category("storage_plan"),
    }


@login_required
def pricing_page(request: HttpRequest) -> HttpResponse:
    return render(request, "billing/pricing_page.html", pricing_context(request))


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

    stripe.api_key = djstripe_settings.STRIPE_SECRET_KEY
    portal_session = stripe.billing_portal.Session.create(
        customer=customer.id,
        return_url=request.build_absolute_uri(reverse("core:profile_page")),
    )
    return HttpResponseRedirect(portal_session.url)


def _validated_price(price_id: str | None, category: str) -> str | None:
    """Return the price ID only if it belongs to an active product in ``category``."""
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
    stripe.api_key = djstripe_settings.STRIPE_SECRET_KEY

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
        checkout_session = stripe.checkout.Session.create(
            customer=customer.id,
            line_items=line_items,
            mode="subscription",
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
