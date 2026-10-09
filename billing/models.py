from __future__ import annotations

from typing import ClassVar

from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.db import models

from . import metadata


class CustomUser(AbstractUser):
    customer = models.ForeignKey(
        "djstripe.Customer",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        help_text="The user's Stripe Customer object, if it exists",
    )

    @property
    def has_active_subscription(self) -> bool:
        """True if the user has any active or trialing subscription."""
        return bool(metadata.active_subscriptions(self))


class MonthlyUploadUsage(models.Model):
    """Per-calendar-month counter for a rate-limited action."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="monthly_usage",
    )
    period = models.CharField(max_length=7, help_text="Calendar month, e.g. 2026-08")
    action = models.CharField(
        max_length=16, help_text="The type of action being tracked. E.g. 'scan' or 'upload'."
    )
    count = models.PositiveIntegerField(default=0)

    class Meta:
        constraints: ClassVar[list[models.UniqueConstraint]] = [
            models.UniqueConstraint(
                fields=["user", "period", "action"], name="unique_monthly_usage"
            )
        ]

    def __str__(self):
        return f"{self.user_id} {self.period} {self.action}: {self.count}"
