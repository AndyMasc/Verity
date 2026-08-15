from __future__ import annotations

import logging
from typing import ClassVar

import stripe
from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.db import models
from djstripe.models import Subscription

from . import metadata, services

logger = logging.getLogger(__name__)


class CustomUser(AbstractUser):
    subscription = models.ForeignKey(
        "djstripe.Subscription",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="user",
        help_text="The user's Stripe Subscription object, if it exists",
    )
    customer = models.ForeignKey(
        "djstripe.Customer",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        help_text="The user's Stripe Customer object, if it exists",
    )
    storage_used_bytes = models.BigIntegerField(
        default=0,
        help_text=(
            "Denormalized count of stored document bytes (active documents only), "
            "kept in sync by the documents storage signals so quota checks are O(1)."
        ),
    )

    @property
    def has_active_subscription(self) -> bool:
        """True if the user has any active or trialing subscription."""
        return bool(metadata._active_subscriptions(self))

    def get_verified_session_holder(self, session: stripe.checkout.Session) -> CustomUser | None:
        """Validates session ownership against the logged-in user or client reference ID."""
        customer_email = (getattr(session, "customer_details", None) or {}).get("email")

        session_customer_matches = (
            bool(session.customer)
            and (self.customer is not None)
            and (self.customer.id == session.customer)
        )
        email_matches = bool(customer_email and customer_email == self.email)

        if session_customer_matches or email_matches:
            return self
        if not session.client_reference_id:
            return None

        try:
            client_reference_id = int(session.client_reference_id)
        except (TypeError, ValueError):
            return None

        try:
            subscription_holder = CustomUser.objects.get(id=client_reference_id)
        except CustomUser.DoesNotExist:
            return None

        if subscription_holder != self:
            logger.warning(
                "Subscription confirm ownership mismatch: session=%s holder=%s user=%s customer=%s email=%s",
                session.id,
                subscription_holder.pk,
                self.pk,
                self.customer,
                customer_email,
            )
            return None

        return subscription_holder

    def handle_new_subscription(self, djstripe_subscription: Subscription) -> None:
        """Processes an incoming checkout, updating the primary subscription and
        canceling overlapping category subscriptions.
        """
        if not self.customer:
            self.customer = djstripe_subscription.customer
            self.save(update_fields=["customer"])

        raw_subscription = services.retrieve_subscription(djstripe_subscription.id)
        incoming_categories = set()

        for item in raw_subscription.get("items", {}).get("data", []):
            product_id = item.get("price", {}).get("product")
            category = metadata.category_for_product(product_id)
            if category:
                incoming_categories.add(category)

        if "base_plan" in incoming_categories:
            self.subscription = djstripe_subscription
            self.save(update_fields=["subscription"])

        active_subs = Subscription.objects.filter(customer=self.customer)
        for old_sub in active_subs:
            sub_status = (old_sub.stripe_data or {}).get("status")
            if sub_status not in ["active", "trialing"]:
                continue

            if old_sub.id == djstripe_subscription.id:
                continue

            for old_item in old_sub.items.select_related("price__product").all():
                old_product = old_item.price.product if old_item.price else None
                if not old_product:
                    continue

                old_cat = metadata.category_for_product(old_product.id)

                if old_cat in incoming_categories:
                    try:
                        services.cancel_subscription(old_sub.id)
                        logger.info(
                            "Replaced overlapping category plan %s with new subscription %s",
                            old_sub.id,
                            djstripe_subscription.id,
                        )
                    except stripe.error.StripeError as e:
                        logger.error(
                            "Failed to clear old conflicting subscription %s: %s",
                            old_sub.id,
                            e,
                        )
                    break


class ScanUsage(models.Model):
    """Monthly Quick Scan usage counter for enforcing the free plan limit."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="scan_usage",
    )
    period = models.CharField(max_length=7, help_text="Calendar month, e.g. 2026-08")
    count = models.PositiveIntegerField(default=0)

    class Meta:
        constraints: ClassVar[list[models.UniqueConstraint]] = [
            models.UniqueConstraint(fields=["user", "period"], name="unique_scan_usage_period")
        ]

    def __str__(self):
        return f"{self.user_id} {self.period}: {self.count}"
