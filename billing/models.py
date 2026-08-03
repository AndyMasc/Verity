import logging

import stripe
from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.db import models
from djstripe.models import Subscription

from . import metadata

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

    @property
    def has_active_subscription(self) -> bool:
        if not self.subscription:
            return False
        return self.subscription.status in ["active", "trialing"]

    def get_verified_session_holder(self, session: stripe.checkout.Session) -> CustomUser | None:
        """Validates session ownership against the logged-in user or client reference ID."""

        customer_email = (getattr(session, "customer_details", None) or {}).get(
            "email"
        )  # Get customer email from session
        session_customer_matches = (
            (session.customer)
            and (self.customer is not None)
            and (self.customer.id == session.customer)  # Session customer matches logged-in user
        )
        email_matches = (
            customer_email and customer_email == self.email
        )  # Email matches logged-in user

        if session_customer_matches or email_matches:
            return self
        if not session.client_reference_id:
            return None

        try:
            client_reference_id = int(session.client_reference_id)
        except TypeError, ValueError:
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
        stripe.api_key = settings.STRIPE_SECRET_KEY
        raw_subscription = stripe.Subscription.retrieve(str(djstripe_subscription.id))

        # Determine the category of the newly purchased subscription item(s)
        new_category = None
        for item in raw_subscription.get("items", {}).get("data", []):
            product_id = item.get("price", {}).get("product")
            meta = metadata.PRODUCTS.get(product_id)
            if meta and hasattr(meta, "category"):
                new_category = meta.category
                break

        # Check existing active subscriptions for the user and cancel duplicates in the SAME category
        if self.customer:
            active_subs = Subscription.objects.filter(customer=self.customer)
            for old_sub in active_subs:
                if getattr(old_sub, "status", None) not in ["active", "trialing"]:
                    continue

                if old_sub.id == djstripe_subscription.id:
                    continue

                for old_item in old_sub.items.select_related("price__product").all():
                    old_product = old_item.price.product if old_item.price else None
                    old_meta = metadata.PRODUCTS.get(old_product.id) if old_product else None
                    old_cat = (
                        old_meta.category if old_meta and hasattr(old_meta, "category") else None
                    ) or (
                        old_product.metadata.get("category")
                        if old_product and old_product.metadata
                        else None
                    )

                    if new_category and old_cat == new_category:
                        try:
                            stripe.Subscription.cancel(old_sub.id)
                            logger.info(
                                "Canceled previous conflicting subscription %s (category: %s) for user %s",
                                old_sub.id,
                                old_cat,
                                self.pk,
                            )
                        except stripe.error.StripeError as e:
                            logger.error("Failed to cancel old subscription %s: %s", old_sub.id, e)

        # Attach new relations:
        self.subscription = djstripe_subscription
        self.customer = djstripe_subscription.customer
        self.save()


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
        constraints = [
            models.UniqueConstraint(fields=["user", "period"], name="unique_scan_usage_period"),
        ]

    def __str__(self):
        return f"{self.user_id} {self.period}: {self.count}"
