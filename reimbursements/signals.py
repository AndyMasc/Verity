from django.contrib.auth import get_user_model
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import StripeAccount

User = get_user_model()


@receiver(post_save, sender=User)
def create_stripe_account(sender, instance, created, **kwargs):
    if created:
        StripeAccount.objects.create(user=instance)
