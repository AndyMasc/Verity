"""Forms for reimbursements payment verification.

Handles email verification and code verification with Turnstile CAPTCHA protection.
"""

import logging

from django import forms

from core.turnstile import verify_turnstile_token

logger = logging.getLogger(__name__)


class RequestVerificationCodeForm(forms.Form):
    """Form to request a verification code for reimbursement payment.

    Includes Turnstile CAPTCHA protection to prevent spam verification requests.
    """

    def __init__(self, *args, request=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.request = request

    email = forms.EmailField(
        label="Recipient Email",
        max_length=254,
        required=True,
        widget=forms.EmailInput(
            attrs={
                "placeholder": "you@example.com",
                "autocomplete": "email",
                "class": "shadow-2xs w-full rounded-xl border border-zinc-200 bg-zinc-50 px-4 py-3 text-sm text-zinc-900 placeholder-zinc-400 transition-all focus:border-[#5A67FF] focus:ring-2 focus:ring-[#5A67FF]/20 focus:outline-none dark:border-zinc-800 dark:bg-zinc-950/50 dark:text-zinc-100",
            }
        ),
    )

    cf_turnstile_response = forms.CharField(
        widget=forms.HiddenInput(),
        required=True,
        label="",
    )

    def clean_cf_turnstile_response(self):
        """Verify the Turnstile token."""
        token = self.cleaned_data.get("cf_turnstile_response", "").strip()

        if not token:
            raise forms.ValidationError(
                "Bot verification required. Please refresh and try again.",
                code="turnstile_missing",
            )

        result = verify_turnstile_token(token, "request_verification", self.request)

        if not result.get("success"):
            logger.warning(f"Turnstile verification failed on code request: {result}")
            raise forms.ValidationError(
                result.get("message", "Bot verification failed. Please try again."),
                code="turnstile_failed",
            )

        return token


class VerifyEmailCodeForm(forms.Form):
    """Form to verify the emailed verification code.

    Includes Turnstile CAPTCHA protection to prevent brute force code attacks.
    """

    def __init__(self, *args, request=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.request = request

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
                "class": "shadow-2xs w-full rounded-xl border border-zinc-200 bg-zinc-50 px-4 py-3 text-center text-lg tracking-widest text-zinc-900 placeholder-zinc-400 transition-all focus:border-[#5A67FF] focus:ring-2 focus:ring-[#5A67FF]/20 focus:outline-none dark:border-zinc-800 dark:bg-zinc-950/50 dark:text-zinc-100",
            }
        ),
    )

    cf_turnstile_response = forms.CharField(
        widget=forms.HiddenInput(),
        required=True,
        label="",
    )

    def clean_cf_turnstile_response(self):
        """Verify the Turnstile token."""
        token = self.cleaned_data.get("cf_turnstile_response", "").strip()

        if not token:
            raise forms.ValidationError(
                "Bot verification required. Please refresh and try again.",
                code="turnstile_missing",
            )

        result = verify_turnstile_token(token, "verify_code", self.request)

        if not result.get("success"):
            logger.warning(f"Turnstile verification failed on code verify: {result}")
            raise forms.ValidationError(
                result.get("message", "Bot verification failed. Please try again."),
                code="turnstile_failed",
            )

        return token


class CheckoutTurnstileForm(forms.Form):
    """Protect the final public checkout submission with Turnstile."""

    def __init__(self, *args, request=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.request = request

    cf_turnstile_response = forms.CharField(
        widget=forms.HiddenInput(),
        required=True,
        label="",
    )

    def clean_cf_turnstile_response(self):
        token = self.cleaned_data.get("cf_turnstile_response", "").strip()

        if not token:
            raise forms.ValidationError(
                "Bot verification required. Please refresh and try again.",
                code="turnstile_missing",
            )

        result = verify_turnstile_token(token, "checkout", self.request)

        if not result.get("success"):
            logger.warning(f"Turnstile verification failed on checkout: {result}")
            raise forms.ValidationError(
                result.get("message", "Bot verification failed. Please try again."),
                code="turnstile_failed",
            )

        return token
