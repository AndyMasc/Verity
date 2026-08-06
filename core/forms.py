"""Forms for authentication flows and user settings management.

Extends django-allauth signup and login forms to support a passwordless
authentication flow, and provides a ModelForm for UserSettings preferences.
"""

from allauth.account.forms import LoginForm, SignupForm
from django import forms

from .models import UserSettings


class PasswordlessSignupForm(SignupForm):
    """Signup form that omits password fields and sets an unusable password.

    Used alongside the passwordless login flow so users authenticate via
    magic link rather than a traditional credential.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields.pop("password1", None)
        self.fields.pop("password2", None)

    def save(self, request):
        user = super().save(request)
        user.set_unusable_password()
        user.save()
        return user


class PasswordlessLoginForm(LoginForm):
    """Login form that removes the password field for magic-link-only auth."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields.pop("password", None)


class UpdateUserSettingsForm(forms.ModelForm):
    """ModelForm for editing UserSettings automation and notification preferences."""

    class Meta:
        model = UserSettings
        fields = [
            "default_currency",
            "auto_archive_expired_records",
            "auto_delete_archived_records",
            "expiring_notifications_advance_time",
            "enable_push_notifications",
            "enable_email_notifications",
            "auto_create_and_organize_folders",
        ]
