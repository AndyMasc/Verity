"""Django signals for automatic UserSettings provisioning and cache invalidation.

Listens for new User creation and ensures every user starts with a
sensible set of default preferences. Also invalidates the webpush
subscription count cache when subscriptions change.
"""

from django.conf import settings
from django.core.cache import cache
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from .models import UserSettings


@receiver(post_save, sender=settings.AUTH_USER_MODEL)
def create_user_settings(sender, instance, created, **kwargs):  # noqa: ARG001
    """Create a default UserSettings row whenever a new User is saved."""
    if created:
        UserSettings.objects.create(user=instance)


def _invalidate_webpush_count_cache(user_id):
    """Remove the cached webpush subscription count for *user_id*."""
    cache.delete(f"webpush_count:{user_id}")


@receiver(post_save, sender="webpush.PushInformation")
def _on_pushinfo_save(sender, instance, **kwargs):  # noqa: ARG001
    _invalidate_webpush_count_cache(instance.user_id)


@receiver(post_delete, sender="webpush.PushInformation")
def _on_pushinfo_delete(sender, instance, **kwargs):  # noqa: ARG001
    _invalidate_webpush_count_cache(instance.user_id)
