import logging
from typing import cast
from uuid import uuid4

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

from core.apps import posthog_client

from . import metadata, services
from .context_processors import invalidate_plan_usage_caches
from .metadata import plan_for_user
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
    overlaps_cleared = subscription_holder.handle_new_subscription(djstripe_subscription)
    if posthog_client is not None:
        posthog_client.capture(
            "subscription_activated",
            properties={"overlapping_subscription_cleared": overlaps_cleared},
        )

    if overlaps_cleared:
        messages.success(request, "Your subscription has been updated successfully!")
    else:
        messages.warning(
            request,
            "Your new plan is active, but we couldn't automatically cancel your previous "
            "overlapping plan. Please cancel it from the billing portal or contact support.",
        )
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


def _purchasable_prices(price_ids) -> dict:
    """Return {price_id: Price} for the ids that are active in this Stripe mode."""
    ids = [price_id for price_id in price_ids if price_id]
    if not ids:
        return {}
    return {
        price.id: price
        for price in Price.objects.filter(
            id__in=ids,
            active=True,
            livemode=djstripe_settings.STRIPE_LIVE_MODE,
        ).select_related("product")
    }


def _price_is_usable(price, category: str | None = None) -> bool:
    """Whether a price belongs to a known product, optionally in "category".

    "category" is the caller's own read of the product's Stripe metadata, so the two
    cannot disagree.
    """
    if price is None or price.product is None:
        return False
    if price.product.id not in metadata.PRODUCTS:
        return False
    return category is None or metadata.product_category(price.product) == category


def _validated_price(price_id: str | None, category: str | None = None) -> str | None:
    """Return the price ID only if it is an active, purchasable price."""
    price = _purchasable_prices([price_id]).get(price_id)
    return price_id if _price_is_usable(price, category) else None


def customer_needs_refresh(customer: Customer | None) -> bool:
    """Return True when a stored customer ID is stale or missing in Stripe."""
    if customer is None:
        return True
    if customer.livemode != djstripe_settings.STRIPE_LIVE_MODE:
        return True
    return services.customer_missing_in_stripe(customer.id)


@login_required
@require_POST
@ratelimit(key="user", rate="15/m", method="POST", block=True)
def create_checkout_session(request: HttpRequest) -> HttpResponse:
    user = cast(CustomUser, request.user)

    # A plan and usage-based storage can be bought in one order; a second plan cannot.
    submitted = request.POST.getlist("price_ids")
    # One query for the whole selection, instead of fetching each price twice.
    catalog = _purchasable_prices(submitted)
    selected = [
        price_id
        for price_id in submitted
        if price_id in catalog and _price_is_usable(catalog[price_id])
    ]
    if not selected:
        return HttpResponseBadRequest("Select a valid plan to proceed to checkout.")

    # Resolve the selected prices to their products.
    products = Price.objects.filter(id__in=selected).select_related("product")
    metas = {
        price.id: meta
        for price in products
        if (meta := metadata.PRODUCTS.get(price.product_id)) is not None
    }

    licensed = [price_id for price_id in selected if not metas[price_id].metered]
    if len(licensed) > 1:
        return HttpResponseBadRequest("Select a single plan to proceed to checkout.")

    # The rate follows the plan the user ends up on, so an explicit selection wins and a downgrade is not billed at the tier they are leaving. A metered-only
    # order keeps the current plan's tier.
    base_plan = metas[licensed[0]] if licensed else plan_for_user(user)

    # A licensed line item must carry a quantity. A metered one must not.
    line_items = [{"price": price_id, "quantity": 1} for price_id in licensed]
    for price_id in selected:
        meta = metas[price_id]
        if not meta.metered:
            continue
        # The selected catalog price only says the user wants metered storage; the
        # tier decides which price Stripe will actually charge. Validate that price
        # itself, so a stale or wrong-mode id fails here rather than at Stripe.
        tier_price_id = meta.price_id_for_plan(base_plan)
        if tier_price_id is None or _validated_price(tier_price_id) is None:
            return HttpResponseBadRequest(
                "This plan is not available for purchase. Please try again."
            )
        line_items.append({"price": tier_price_id})

    customer = user.customer
    if customer_needs_refresh(customer):
        customer = None

    if customer is None:
        customer, _ = Customer.get_or_create(user)
        # get_or_create may return a stale row whose Stripe record was deleted; unlink it so a brand-new customer is created instead.
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
            client_reference_id=str(user.pk),
            success_url=request.build_absolute_uri(reverse("subscription_confirm"))
            + "?session_id={CHECKOUT_SESSION_ID}",
            cancel_url=request.build_absolute_uri(reverse("pricing_page")) + "?checkout=canceled",
            idempotency_key=uuid4().hex,
        )
        if posthog_client is not None:
            invalidate_plan_usage_caches(user.pk)
            posthog_client.capture("subscription_checkout_started", properties={})
        return HttpResponseRedirect(checkout_session.url)
    except Exception as e:
        logger.error("Could not create checkout session: %s", e, exc_info=True)
        return HttpResponseBadRequest("Unable to start checkout. Please try again.")
