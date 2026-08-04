import logging

import stripe
from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.db import models

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

    def handle_new_subscription(self, djstripe_subscription) -> None:
        """Associate a subscription with the user."""
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
