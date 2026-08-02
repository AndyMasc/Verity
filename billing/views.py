import logging
from typing import cast

import stripe
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import HttpRequest, HttpResponse, HttpResponseBadRequest, HttpResponseRedirect
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

    products = list(Product.objects.filter(active=True).prefetch_related("prices"))
    for product in products:
        meta = metadata.PRODUCTS.get(product.id)
        product.features_list = meta.features if meta else []
        product.is_default = meta.is_default if meta else False

    free_plan = metadata.PAPERTRAIL_FREE
    free_plan.features_list = free_plan.features
    free_plan.prices = []
    free_plan.is_default = False
    products.insert(0, free_plan)

    return render(
        request,
        "billing/pricing_page.html",
        context={
            "stripe_public_key": settings.STRIPE_PRICING_TABLE_KEY,
            "stripe_pricing_table_id": settings.STRIPE_PRICING_TABLE_ID,
            "products": products,
            "free_plan": free_plan,
            "has_active_subscription": has_active_sub,
        },
    )


@login_required
@ratelimit(key="user", rate="10/m", method="GET", block=True)
def subscription_confirm(request: HttpRequest) -> HttpResponse:
    user = cast(CustomUser, request.user)
    stripe.api_key = djstripe_settings.STRIPE_SECRET_KEY

    session_id = request.GET.get("session_id")
    if not session_id:
        return HttpResponseBadRequest("Missing session ID.")

    # Retrieve checkout session from Stripe
    session = stripe.checkout.Session.retrieve(session_id)

    if session.payment_status != "paid":
        return HttpResponseBadRequest("Subscription is not paid.")

    # A checkout session without a subscription (e.g. a one-off payment) has
    # nothing to confirm here.
    if not session.subscription:
        return HttpResponseBadRequest("Session is not a subscription checkout.")

    customer_email = (getattr(session, "customer_details", None) or {}).get("email")
    session_customer_matches = (
        session.customer and user.customer is not None and session.customer == user.customer.id
    )
    email_matches = customer_email and customer_email == user.email
    if session_customer_matches or email_matches:
        subscription_holder = user
    else:
        if not session.client_reference_id:
            return HttpResponseBadRequest("Invalid session payload.")

        try:
            client_reference_id = int(session.client_reference_id)
        except TypeError, ValueError:
            return HttpResponseBadRequest("Invalid session payload.")

        try:
            subscription_holder = CustomUser.objects.get(id=client_reference_id)
        except CustomUser.DoesNotExist:
            raise PermissionDenied(
                "You do not have permission to confirm this subscription."
            ) from None

        # Secure check: ensure logged-in user matches the session owner
        if subscription_holder != user:
            logger.warning(
                "Subscription confirm ownership mismatch: session=%s holder=%s user=%s customer=%s email=%s",
                session_id,
                subscription_holder.pk,
                user.pk,
                user.customer,
                customer_email,
            )
            raise PermissionDenied("You do not have permission to confirm this subscription.")

    # Sync subscription data with djstripe
    subscription = stripe.Subscription.retrieve(str(session.subscription))
    djstripe_subscription = Subscription.sync_from_stripe_data(subscription)

    # Cancel previous active subscription if a new one is being confirmed
    if (
        subscription_holder.subscription
        and subscription_holder.subscription.id != djstripe_subscription.id
    ):
        old_sub_id = subscription_holder.subscription.id
        try:
            # Immediately cancel the older subscription in Stripe
            stripe.Subscription.cancel(old_sub_id)
            logger.info(
                "Canceled previous subscription %s for user %s", old_sub_id, subscription_holder.pk
            )
            messages.success(request, "Your subscription has been updated successfully!")
        except stripe.error.StripeError as e:
            logger.error("Failed to cancel old subscription %s: %s", old_sub_id, e)
            messages.error(request, "Failed to cancel your subscription. Please try again later.")
    # Attach new relations
    subscription_holder.subscription = djstripe_subscription
    subscription_holder.customer = djstripe_subscription.customer
    subscription_holder.save()
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
