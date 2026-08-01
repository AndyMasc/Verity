import logging

from django.shortcuts import render, redirect
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.http import HttpResponseRedirect, HttpResponseBadRequest
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.core.exceptions import PermissionDenied
from django.views.decorators.http import require_POST

import stripe
from djstripe.models import Product, Subscription
from djstripe.settings import djstripe_settings

from . import metadata

logger = logging.getLogger(__name__)


@login_required
def pricing_page(request):
    products = Product.objects.filter(active=True).prefetch_related("prices")

    for product in products:
        meta = metadata.PRODUCTS.get(product.id)
        product.features_list = meta.features if meta else []

    return render(
        request,
        "billing/pricing_page.html",
        context={
            "stripe_public_key": settings.STRIPE_PRICING_TABLE_KEY,
            "stripe_pricing_table_id": settings.STRIPE_PRICING_TABLE_ID,
            "products": products,
        },
    )


@login_required
def subscription_confirm(request):
    stripe.api_key = djstripe_settings.STRIPE_SECRET_KEY

    session_id = request.GET.get("session_id")
    if not session_id:
        return HttpResponseBadRequest("Missing session ID.")

    # Retrieve checkout session from Stripe
    session = stripe.checkout.Session.retrieve(session_id)

    if session.payment_status != "paid":
        return HttpResponseBadRequest("Subscription is not paid.")

    # Ownership check. The embedded pricing table can reuse a cached Checkout
    # Session whose client_reference_id was captured for a different user, so
    # prefer matching the actual payer: the session's Stripe customer or the
    # email used at checkout. Fall back to client_reference_id for payers who
    # have no customer record yet.
    customer_email = (session.get("customer_details") or {}).get("email")
    session_customer_matches = (
        session.customer
        and request.user.customer is not None
        and session.customer == request.user.customer.id
    )
    email_matches = customer_email and customer_email == request.user.email
    if session_customer_matches or email_matches:
        subscription_holder = request.user
    else:
        if not session.client_reference_id:
            return HttpResponseBadRequest("Invalid session payload.")

        client_reference_id = int(session.client_reference_id)
        try:
            subscription_holder = get_user_model().objects.get(id=client_reference_id)
        except get_user_model().DoesNotExist:
            raise PermissionDenied("You do not have permission to confirm this subscription.")

        # Secure check: ensure logged-in user matches the session owner
        if subscription_holder != request.user:
            logger.warning(
                "Subscription confirm ownership mismatch: session=%s holder=%s user=%s customer=%s email=%s",
                session_id, subscription_holder.id, request.user.id, request.user.customer_id, customer_email,
            )
            raise PermissionDenied("You do not have permission to confirm this subscription.")

    # Sync subscription data with djstripe
    subscription = stripe.Subscription.retrieve(session.subscription)
    djstripe_subscription = Subscription.sync_from_stripe_data(subscription)

    # Attach relations to custom user model
    subscription_holder.subscription = djstripe_subscription
    subscription_holder.customer = djstripe_subscription.customer
    subscription_holder.save()

    messages.success(request, "You're account was successfully upgraded!")
    return HttpResponseRedirect(reverse("core:dashboard"))


@login_required
@require_POST
def create_portal_session(request):
    customer = request.user.customer
    if customer is None:
        return HttpResponseBadRequest("No Stripe customer associated with this account.")

    stripe.api_key = djstripe_settings.STRIPE_SECRET_KEY
    portal_session = stripe.billing_portal.Session.create(
        customer=customer.id,
        return_url=request.build_absolute_uri(reverse("core:profile_page")),
    )
    return HttpResponseRedirect(portal_session.url)
