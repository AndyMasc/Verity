import logging
from typing import cast

import stripe
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import HttpRequest, HttpResponse, HttpResponseBadRequest, HttpResponseRedirect
from django.shortcuts import render
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

    # Ownership check. The embedded pricing table can reuse a cached Checkout
    # Session whose client_reference_id was captured for a different user, so
    # prefer matching the actual payer: the session's Stripe customer or the
    # email used at checkout. Fall back to client_reference_id for payers who
    # have no customer record yet.
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

    # Attach relations to custom user model
    subscription_holder.subscription = djstripe_subscription
    subscription_holder.customer = djstripe_subscription.customer
    subscription_holder.save()

    messages.success(request, "You're account was successfully upgraded!")
    return HttpResponseRedirect(reverse("core:dashboard"))


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
