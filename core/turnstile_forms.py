"""Turnstile-protected form mixin.

The verification logic is identical across signup, login, and the public
reimbursement forms — only the Turnstile *action* differs — so subclasses
just set ``turnstile_action``.
"""

import logging

from django import forms

from core.turnstile import turnstile_enabled, verify_turnstile_token

logger = logging.getLogger(__name__)


class TurnstileProtectedForm(forms.Form):
    """Adds a hidden Turnstile token field and verifies it for this action."""

    turnstile_action: str = ""
    request = None

    cf_turnstile_response = forms.CharField(
        widget=forms.HiddenInput(),
        required=False,
        label="",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["cf_turnstile_response"].required = turnstile_enabled()

    def clean_cf_turnstile_response(self):
        token = self.cleaned_data.get("cf_turnstile_response", "").strip()
        if not turnstile_enabled():
            return token

        if not token:
            raise forms.ValidationError(
                "Bot verification required. Please refresh and try again.",
                code="turnstile_missing",
            )

        result = verify_turnstile_token(token, self.turnstile_action, self.request)
        if not result.get("success"):
            logger.warning(
                "Turnstile %s verification failed: %s", self.turnstile_action, result
            )
            raise forms.ValidationError(
                result.get("message", "Bot verification failed. Please try again."),
                code="turnstile_failed",
            )
        return token
