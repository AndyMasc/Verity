from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.db import models


class CustomUser(AbstractUser):
    subscription = models.ForeignKey(
        "djstripe.Subscription",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        help_text="The user's Stripe Subscription object, if it exists",
    )
    customer = models.ForeignKey(
        "djstripe.Customer",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        help_text="The user's Stripe Customer object, if it exists",
    )


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
