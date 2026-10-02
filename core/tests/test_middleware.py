import json
import logging
from unittest.mock import patch

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model

User = get_user_model()
from django.contrib.messages.storage.fallback import FallbackStorage
from django.http import HttpRequest, HttpResponse
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from posthog import new_context
from posthog.contexts import get_context_session_id

from core.middleware import HtmxMessageMiddleware, PostHogSessionIdMiddleware


class HtmxMessageMiddlewareTest(TestCase):
    def test_htmx_request_gets_trigger_header(self):
        from django.http import HttpRequest
        from django.contrib.messages.storage.fallback import FallbackStorage
        from django.contrib import messages

        request = HttpRequest()
        request.META["HTTP_HX_REQUEST"] = "true"
        request.session = {}
        setattr(request, "_messages", FallbackStorage(request))
        messages.success(request, "Test message")

        response = HttpResponse()

        middleware = HtmxMessageMiddleware(lambda req: response)
        middleware(request)

        self.assertIn("HX-Trigger", response)
        trigger = response["HX-Trigger"]
        self.assertIn("djangoMessages", trigger)


class DramatiqEmailBackendTest(TestCase):
    @patch("core.backends.send_background_email")
    def test_send_messages(self, mock_task):
        from core.backends import DramatiqEmailBackend
        from django.core.mail import EmailMultiAlternatives

        backend = DramatiqEmailBackend()
        email = EmailMultiAlternatives(
            subject="Test",
            body="Text body",
            from_email="test@example.com",
            to=["to@example.com"],
        )
        email.attach_alternative("<p>HTML body</p>", "text/html")
        count = backend.send_messages([email])
        self.assertEqual(count, 1)
        mock_task.send.assert_called_once()

    @patch("core.backends.send_background_email")
    def test_empty_messages(self, mock_task):
        from core.backends import DramatiqEmailBackend

        backend = DramatiqEmailBackend()
        count = backend.send_messages([])
        self.assertEqual(count, 0)
        mock_task.send.assert_not_called()


class RequestIDMiddlewareTest(TestCase):
    def test_generates_uuid_when_no_header(self):
        from core.middleware import RequestIDMiddleware

        middleware = RequestIDMiddleware(lambda req: HttpResponse("ok"))
        request = HttpRequest()
        response = middleware(request)
        self.assertIn("X-Request-ID", response)
        self.assertEqual(len(response["X-Request-ID"]), 32)

    def test_uses_incoming_header(self):
        from core.middleware import RequestIDMiddleware

        middleware = RequestIDMiddleware(lambda req: HttpResponse("ok"))
        request = HttpRequest()
        request.META["HTTP_X_REQUEST_ID"] = "my-custom-id"
        response = middleware(request)
        self.assertEqual(response["X-Request-ID"], "my-custom-id")

    def test_sets_request_id_on_request(self):
        from core.middleware import RequestIDMiddleware

        middleware = RequestIDMiddleware(lambda req: HttpResponse("ok"))
        request = HttpRequest()
        middleware(request)
        self.assertTrue(hasattr(request, "request_id"))
        self.assertEqual(len(request.request_id), 32)


class RequestIDLogFilterTest(TestCase):
    def test_injects_request_id(self):
        from core.middleware import RequestIDLogFilter, request_id_var

        filter_obj = RequestIDLogFilter()
        record = logging.LogRecord("test", logging.INFO, "", 0, "msg", (), None)
        request_id_var.set("test-id-123")
        result = filter_obj.filter(record)
        self.assertTrue(result)
        self.assertEqual(record.request_id, "test-id-123")


class TimezoneMiddlewareTest(TestCase):
    def test_activates_valid_timezone(self):
        from core.middleware import TimezoneMiddleware

        middleware = TimezoneMiddleware(lambda req: HttpResponse("ok"))
        request = HttpRequest()
        request.COOKIES["user_timezone"] = "US/Eastern"
        with patch("django.utils.timezone.activate") as mock_activate:
            middleware(request)
            mock_activate.assert_called_once()

    def test_deactivates_when_no_cookie(self):
        from core.middleware import TimezoneMiddleware

        middleware = TimezoneMiddleware(lambda req: HttpResponse("ok"))
        request = HttpRequest()
        with patch("django.utils.timezone.deactivate") as mock_deactivate:
            middleware(request)
            mock_deactivate.assert_called_once()

    def test_deactivates_on_invalid_timezone(self):
        from core.middleware import TimezoneMiddleware

        middleware = TimezoneMiddleware(lambda req: HttpResponse("ok"))
        request = HttpRequest()
        request.COOKIES["user_timezone"] = "Invalid/Timezone"
        with patch("django.utils.timezone.deactivate") as mock_deactivate:
            middleware(request)
            mock_deactivate.assert_called_once()


class HtmxMessageMiddlewareDetailedTest(TestCase):
    def test_injects_messages_for_htmx(self):
        request = HttpRequest()
        request.META["HTTP_HX_REQUEST"] = "true"
        request.session = {}
        setattr(request, "_messages", FallbackStorage(request))
        messages.success(request, "Saved!")
        response = HttpResponse()
        middleware = HtmxMessageMiddleware(lambda req: response)
        middleware(request)
        self.assertIn("HX-Trigger", response)
        trigger = json.loads(response["HX-Trigger"])
        self.assertIn("djangoMessages", trigger)

    def test_no_trigger_for_non_htmx(self):
        request = HttpRequest()
        request.session = {}
        setattr(request, "_messages", FallbackStorage(request))
        messages.success(request, "Saved!")
        response = HttpResponse()
        middleware = HtmxMessageMiddleware(lambda req: response)
        middleware(request)
        self.assertNotIn("HX-Trigger", response)

    def test_no_trigger_when_redirect(self):
        request = HttpRequest()
        request.META["HTTP_HX_REQUEST"] = "true"
        request.session = {}
        setattr(request, "_messages", FallbackStorage(request))
        messages.success(request, "Saved!")
        response = HttpResponse()
        response["HX-Redirect"] = "/records/"
        middleware = HtmxMessageMiddleware(lambda req: response)
        middleware(request)
        self.assertNotIn("HX-Trigger", response)

    def test_merges_with_existing_trigger(self):
        request = HttpRequest()
        request.META["HTTP_HX_REQUEST"] = "true"
        request.session = {}
        setattr(request, "_messages", FallbackStorage(request))
        messages.success(request, "Saved!")
        response = HttpResponse()
        response["HX-Trigger"] = json.dumps({"existingEvent": {}})
        middleware = HtmxMessageMiddleware(lambda req: response)
        middleware(request)
        trigger = json.loads(response["HX-Trigger"])
        self.assertIn("existingEvent", trigger)
        self.assertIn("djangoMessages", trigger)

    def test_no_messages_no_trigger(self):
        request = HttpRequest()
        request.META["HTTP_HX_REQUEST"] = "true"
        request.session = {}
        setattr(request, "_messages", FallbackStorage(request))
        response = HttpResponse()
        middleware = HtmxMessageMiddleware(lambda req: response)
        middleware(request)
        self.assertNotIn("HX-Trigger", response)


class PostHogSessionIdMiddlewareTest(SimpleTestCase):
    """Session linking, which fails silently if the middleware order slips."""

    CONTEXT_MW = "posthog.integrations.django.PosthogContextMiddleware"
    SESSION_MW = "core.middleware.PostHogSessionIdMiddleware"

    def _request(self, cookies=None, headers=None):
        request = HttpRequest()
        request.COOKIES = cookies or {}
        request.META = {
            f"HTTP_{key.upper().replace('-', '_')}": value
            for key, value in (headers or {}).items()
        }
        return request

    def _session_seen(self, request):
        seen = {}

        def get_response(req):
            seen["session_id"] = get_context_session_id()
            return HttpResponse()

        # Stands in for PosthogContextMiddleware, the only thing that opens a
        # context. posthog.set_context_session() does nothing without one.
        with new_context():
            PostHogSessionIdMiddleware(get_response)(request)
        return seen["session_id"]

    def test_must_be_listed_after_the_posthog_context_middleware(self):
        order = settings.MIDDLEWARE
        self.assertLess(
            order.index(self.CONTEXT_MW),
            order.index(self.SESSION_MW),
            "set_context_session is a no-op unless a context is already open, "
            "so cookie-based session ids are dropped when this runs first",
        )

    def test_cookie_supplies_the_session_id(self):
        request = self._request(cookies={"posthog_session_cookie": "sess_COOKIE"})
        self.assertEqual(self._session_seen(request), "sess_COOKIE")

    def test_tracing_header_is_left_to_the_context_middleware(self):
        request = self._request(
            cookies={"posthog_session_cookie": "sess_COOKIE"},
            headers={"X-POSTHOG-SESSION-ID": "sess_HEADER"},
        )
        self.assertIsNone(self._session_seen(request))

    def test_malformed_cookie_is_ignored(self):
        for bad in ("sess has spaces", "sess;drop=1", "x" * 65, ""):
            with self.subTest(bad=bad):
                request = self._request(cookies={"posthog_session_cookie": bad})
                self.assertIsNone(self._session_seen(request))

    def test_absent_cookie_is_not_an_error(self):
        self.assertIsNone(self._session_seen(self._request()))
