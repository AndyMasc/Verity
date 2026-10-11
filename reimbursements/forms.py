"""Turnstile-protected forms for the reimbursement payment flow."""

from django import forms

from core.turnstile import ACTION_CHECKOUT
from core.turnstile_forms import TurnstileProtectedForm


class CheckoutTurnstileForm(TurnstileProtectedForm):
    turnstile_action = ACTION_CHECKOUT

    email = forms.EmailField(
        required=False,
        widget=forms.EmailInput(
            attrs={
                "autocomplete": "email",
                "placeholder": "you@example.com",
            }
        ),
    )

    def __init__(self, *args, request=None, **kwargs):
        self.request = request
        super().__init__(*args, **kwargs)
