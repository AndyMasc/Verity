"""Forms for authentication flows and user settings management.

Extends django-allauth signup and login forms to support a passwordless
authentication flow, and provides a ModelForm for UserSettings preferences.
"""

import logging
from typing import ClassVar

from allauth.account.forms import LoginForm, SignupForm
from django import forms

from .models import UserSettings
from .turnstile import verify_turnstile_token

logger = logging.getLogger(__name__)


class PasswordlessSignupForm(SignupForm):
    """Signup form that omits password fields and sets an unusable password.

    Used alongside the passwordless login flow so users authenticate via
    magic link rather than a traditional credential.
    Includes Turnstile CAPTCHA verification.
    """

    cf_turnstile_response = forms.CharField(
        widget=forms.HiddenInput(),
        required=True,
        label="",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields.pop("password1", None)
        self.fields.pop("password2", None)

    def clean_cf_turnstile_response(self):
        """Verify the Turnstile token on this field specifically."""
        token = self.cleaned_data.get("cf_turnstile_response", "").strip()

        if not token:
            raise forms.ValidationError(
                "Bot verification required. Please refresh and try again.",
                code="turnstile_missing",
            )

        result = verify_turnstile_token(token, "signup", self.request)

        if not result.get("success"):
            logger.warning(f"Turnstile signup verification failed: {result}")
            raise forms.ValidationError(
                result.get("message", "Bot verification failed. Please try again."),
                code="turnstile_failed",
            )

        return token

    def save(self, request):
        user = super().save(request)
        user.set_unusable_password()
        user.save()
        return user


class PasswordlessLoginForm(LoginForm):
    """Login form that removes the password field for magic-link-only auth.

    Includes Turnstile CAPTCHA verification.
    """

    cf_turnstile_response = forms.CharField(
        widget=forms.HiddenInput(),
        required=True,
        label="",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields.pop("password", None)

    def clean_cf_turnstile_response(self):
        """Verify the Turnstile token on this field specifically."""
        token = self.cleaned_data.get("cf_turnstile_response", "").strip()

        if not token:
            raise forms.ValidationError(
                "Bot verification required. Please refresh and try again.",
                code="turnstile_missing",
            )

        result = verify_turnstile_token(token, "login", self.request)

        if not result.get("success"):
            logger.warning(f"Turnstile login verification failed: {result}")
            raise forms.ValidationError(
                result.get("message", "Bot verification failed. Please try again."),
                code="turnstile_failed",
            )

        return token


class UpdateUserSettingsForm(forms.ModelForm):
    """ModelForm for editing UserSettings automation and notification preferences."""

    class Meta:
        model = UserSettings
        fields: ClassVar[list[str]] = [
            "default_currency",
            "auto_archive_expired_records",
            "auto_delete_archived_records",
            "expiring_notifications_advance_time",
            "enable_push_notifications",
            "enable_email_notifications",
            "auto_create_and_organize_folders",
        ]
