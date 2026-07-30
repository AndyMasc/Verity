"""Domain models for user preferences and in-app notifications.

Provides the UserSettings model for per-user automation and notification
preferences, and the Notification model for persisting messages that are
surfaced in the dashboard sidebar.
"""

from django.conf import settings
from django.db import models
from django.utils import timezone

from core.currencies import CURRENCY_CHOICES, DEFAULT_CURRENCY


class UserSettings(models.Model):
    """Per-user preferences controlling automation and notification behavior.

    Automatically created for every new user via the post_save signal in
    ``core.signals``. A single row exists per user through the OneToOneField.
    """

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="settings",
    )
    auto_archive_expired_records = models.BooleanField(default=True)
    auto_delete_archived_records = models.BooleanField(default=True)
    auto_delete_deleted_documents = models.BooleanField(default=True)
    enable_push_notifications = models.BooleanField(default=True)
    enable_email_notifications = models.BooleanField(default=True)
    auto_create_and_organize_folders = models.BooleanField(default=True)

    default_currency = models.CharField(
        max_length=3,
        choices=CURRENCY_CHOICES,
        default=DEFAULT_CURRENCY,
    )

    class AdvanceTimeChoices(models.TextChoices):
        ONE_DAY = "1", "1 Day"
        THREE_DAYS = "3", "3 Days"
        ONE_WEEK = "7", "1 Week"
        ONE_MONTH = "30", "1 Month"

    expiring_notifications_advance_time = models.CharField(
        max_length=2,
        choices=AdvanceTimeChoices.choices,
        default=AdvanceTimeChoices.THREE_DAYS,
    )

    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "User Settings"
        verbose_name_plural = "User Settings"

    def __str__(self):
        return f"Settings for {self.user.email}"


class Notification(models.Model):
    """An in-app notification message delivered to a specific user.

    Used to persist alerts (e.g. record expiry warnings) that appear in the
    UI until the user marks them as read.
    """

    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="notifications",
    )
    subject = models.CharField(max_length=255, blank=True, default="")
    message = models.TextField()
    is_read = models.BooleanField(default=False)
    sent_at = models.DateTimeField(auto_now_add=True)
