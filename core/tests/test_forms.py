from django.contrib.auth import get_user_model

User = get_user_model()
from django.test import TestCase
from unittest.mock import patch

from core.forms import UpdateUserSettingsForm


class UpdateUserSettingsFormTest(TestCase):
    def test_form_fields(self):
        form = UpdateUserSettingsForm()
        expected = [
            "default_currency",
            "auto_archive_expired_records",
            "auto_delete_archived_records",
            "expiring_notifications_advance_time",
            "enable_push_notifications",
            "enable_email_notifications",
            "auto_create_and_organize_folders",
        ]
        self.assertEqual(list(form.fields.keys()), expected)

    def test_form_valid_data(self):
        form = UpdateUserSettingsForm(
            data={
                "default_currency": "usd",
                "auto_archive_expired_records": False,
                "auto_delete_archived_records": False,
                "enable_push_notifications": False,
                "enable_email_notifications": False,
                "expiring_notifications_advance_time": "30",
            }
        )
        self.assertTrue(form.is_valid())


class PasswordlessSignupFormTurnstileTest(TestCase):
    """Server-side Turnstile gating on the passwordless signup form."""

    def _form(self, data, request=None):
        from core.forms import PasswordlessSignupForm

        kwargs = {"data": data}
        if request is not None:
            kwargs["request"] = request
        return PasswordlessSignupForm(**kwargs)

    def test_verify_turnstile_with_request(self):
        from django.test import RequestFactory

        from core.forms import verify_turnstile_token

        request = RequestFactory().post("/accounts/signup/")
        with (
            patch("core.forms.turnstile_enabled", return_value=True),
            patch(
                "core.forms.verify_turnstile_token", return_value={"success": True}
            ) as mock_verify,
        ):
            form = self._form(
                {
                    "username": "bob",
                    "email": "bob@example.com",
                    "cf_turnstile_response": "token-123",
                },
                request=request,
            )
            self.assertTrue(form.is_valid())
            mock_verify.assert_called_once_with("token-123", "signup", request)

    def test_verify_turnstile_without_request_does_not_raise(self):
        # Regression: allauth's SignupView does not pass `request` to the form.
        with (
            patch("core.forms.turnstile_enabled", return_value=True),
            patch(
                "core.forms.verify_turnstile_token", return_value={"success": True}
            ) as mock_verify,
        ):
            form = self._form(
                {
                    "username": "bob",
                    "email": "bob@example.com",
                    "cf_turnstile_response": "token-123",
                },
            )
            self.assertTrue(form.is_valid())
            mock_verify.assert_called_once_with("token-123", "signup", None)

    def test_turnstile_disabled_skips_verification(self):
        with (
            patch("core.forms.turnstile_enabled", return_value=False),
            patch("core.forms.verify_turnstile_token") as mock_verify,
        ):
            form = self._form(
                {
                    "username": "bob",
                    "email": "bob@example.com",
                },
            )
            self.assertTrue(form.is_valid())
            mock_verify.assert_not_called()


class PasswordlessLoginFormTurnstileTest(TestCase):
    """Server-side Turnstile field cleaning on the passwordless login form."""

    def _clean(self, data, request):
        from allauth.core import context as allauth_context
        from django.test import RequestFactory

        from core.forms import PasswordlessLoginForm

        form = PasswordlessLoginForm(data=data, request=request)
        # allauth's login flow resolves the "current request" from its contextvar;
        # the test client would set it automatically, here we set it explicitly.
        with allauth_context.request_context(request):
            valid = form.is_valid()
        return form, valid

    def test_verify_turnstile_with_passed_request(self):
        # allauth's LoginForm pops `request` from kwargs; our form mirrors it so
        # the field clean can attach the client IP during siteverify.
        from django.test import RequestFactory

        request = RequestFactory().post("/accounts/login/")
        with (
            patch("core.forms.turnstile_enabled", return_value=True),
            patch(
                "core.forms.verify_turnstile_token", return_value={"success": True}
            ) as mock_verify,
        ):
            form, valid = self._clean(
                {"login": "bob@example.com", "cf_turnstile_response": "token-123"},
                request,
            )
            self.assertTrue(valid)
            mock_verify.assert_called_once_with("token-123", "login", request)

    def test_verify_turnstile_without_request_kwarg(self):
        # Defensive path: if a future allauth view ever fails to pass `request`,
        # siteverify must still run (the `remoteip` parameter is optional).
        from allauth.core import context as allauth_context
        from django.test import RequestFactory

        request = RequestFactory().post("/accounts/login/")
        with (
            patch("core.forms.turnstile_enabled", return_value=True),
            patch(
                "core.forms.verify_turnstile_token", return_value={"success": True}
            ) as mock_verify,
        ):
            form = PasswordlessLoginForm(
                data={"login": "bob@example.com", "cf_turnstile_response": "token-123"},
            )
            with allauth_context.request_context(request):
                valid = form.is_valid()
            self.assertTrue(valid)
            mock_verify.assert_called_once_with("token-123", "login", None)

    def test_turnstile_disabled_skips_verification(self):
        from django.test import RequestFactory

        request = RequestFactory().post("/accounts/login/")
        with (
            patch("core.forms.turnstile_enabled", return_value=False),
            patch("core.forms.verify_turnstile_token") as mock_verify,
        ):
            form, valid = self._clean({"login": "bob@example.com"}, request)
            self.assertTrue(valid)
            mock_verify.assert_not_called()
