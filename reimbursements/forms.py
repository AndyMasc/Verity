"""Forms for reimbursements payment verification.

Handles email verification and code verification with Turnstile CAPTCHA
protection (see core.turnstile_forms.TurnstileProtectedForm).
"""

from django import forms

from core.turnstile import (
    ACTION_CHECKOUT,
    ACTION_REQUEST_VERIFICATION,
    ACTION_VERIFY_CODE,
)
from core.turnstile_forms import TurnstileProtectedForm


class RequestVerificationCodeForm(TurnstileProtectedForm):
    turnstile_action = ACTION_REQUEST_VERIFICATION

    email = forms.EmailField(
        label="Recipient Email",
        max_length=254,
        required=True,
        widget=forms.EmailInput(
            attrs={
                "placeholder": "you@example.com",
                "autocomplete": "email",
                "class": "shadow-2xs w-full rounded-lg border border-zinc-200 bg-zinc-50 px-4 py-3 text-sm text-zinc-900 placeholder-zinc-400 transition-all focus:border-[#5A67FF] focus:ring-2 focus:ring-[#5A67FF]/20 focus:outline-none dark:border-zinc-800 dark:bg-zinc-950/50 dark:text-zinc-100",
            }
        ),
    )

    def __init__(self, *args, request=None, **kwargs):
        self.request = request
        super().__init__(*args, **kwargs)
        self.order_fields(["email", "cf_turnstile_response"])


class VerifyEmailCodeForm(TurnstileProtectedForm):
    turnstile_action = ACTION_VERIFY_CODE

    email = forms.EmailField(
        widget=forms.HiddenInput(),
        required=True,
    )

    code = forms.CharField(
        label="Verification Code",
        max_length=6,
        min_length=6,
        required=True,
        widget=forms.TextInput(
            attrs={
                "placeholder": "123 456",
                "inputmode": "numeric",
                "autocomplete": "one-time-code",
                "class": "shadow-2xs w-full rounded-lg border border-zinc-200 bg-zinc-50 px-4 py-3 text-center text-lg tracking-widest text-zinc-900 placeholder-zinc-400 transition-all focus:border-[#5A67FF] focus:ring-2 focus:ring-[#5A67FF]/20 focus:outline-none dark:border-zinc-800 dark:bg-zinc-950/50 dark:text-zinc-100",
            }
        ),
    )

    def __init__(self, *args, request=None, **kwargs):
        self.request = request
        super().__init__(*args, **kwargs)
        self.order_fields(["email", "code", "cf_turnstile_response"])


class CheckoutTurnstileForm(TurnstileProtectedForm):
    turnstile_action = ACTION_CHECKOUT

    def __init__(self, *args, request=None, **kwargs):
        self.request = request
        super().__init__(*args, **kwargs)
