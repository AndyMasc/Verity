"""Forms for authentication flows and user settings management.

Extends django-allauth signup and login forms to support a passwordless
authentication flow, and provides a ModelForm for UserSettings preferences.
"""

from typing import ClassVar

from allauth.account.forms import LoginForm, SignupForm
from django import forms

from .models import UserSettings
from .turnstile import ACTION_LOGIN, ACTION_SIGNUP
from .turnstile_forms import TurnstileProtectedForm


class PasswordlessSignupForm(TurnstileProtectedForm, SignupForm):
    """Passwordless signup: no password fields, an unusable password afterwards."""

    turnstile_action = ACTION_SIGNUP

    def __init__(self, *args, **kwargs):
        # allauth's SignupForm does not pass/store the request; capture it here
        # (optional) so server-side siteverify can attach the client IP.
        self.request = kwargs.pop("request", None)
        super().__init__(*args, **kwargs)
        self.fields.pop("password1", None)
        self.fields.pop("password2", None)

    def save(self, request):
        user = super().save(request)
        user.set_unusable_password()
        user.save()
        return user


class PasswordlessLoginForm(TurnstileProtectedForm, LoginForm):
    """Passwordless login: the password field is removed for magic-link auth."""

    turnstile_action = ACTION_LOGIN

    def __init__(self, *args, **kwargs):
        # allauth's LoginForm already pops "request" (default None); mirror it
        # here so server-side siteverify can attach the client IP when present.
        self.request = kwargs.get("request")
        super().__init__(*args, **kwargs)
        self.fields.pop("password", None)


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
