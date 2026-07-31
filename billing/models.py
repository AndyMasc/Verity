from django.db import models
from django.contrib.auth.models import AbstractUser


class CustomUser(AbstractUser):
    subscription = models.ForeignKey(
        "djstripe.Subscription", null=True, blank=True, on_delete=models.SET_NULL,
        help_text="The user's Stripe Subscription object, if it exists"
    )
    customer = models.ForeignKey(
        "djstripe.Customer", null=True, blank=True, on_delete=models.SET_NULL,
        help_text="The user's Stripe Customer object, if it exists"
    )
