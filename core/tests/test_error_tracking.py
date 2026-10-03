"""Worker failures and caught errors must both reach error tracking."""

import logging
from unittest import mock

from django.test import SimpleTestCase

from core.error_tracking import ErrorTracking, LogCapture

logger = logging.getLogger("billing.test")


class FakeMessage:
    def __init__(self, args=None):
        self.actor_name = "send_background_email"
        self.queue_name = "default"
        self.args = args if args is not None else {"user_id": 7}


class ErrorTrackingTests(SimpleTestCase):
    def setUp(self):
        self.mw = ErrorTracking()
        patcher = mock.patch("core.apps.posthog_client", mock.Mock())
        self.client = patcher.start()
        self.addCleanup(patcher.stop)

    def _fail(self, message=None):
        boom = ValueError("actor blew up")
        self.mw.after_process_message(None, message or FakeMessage(), result=None, exception=boom)
        return boom

    def test_actor_exception_is_captured(self):
        self.assertIs(self._fail(), self.client.capture_exception.call_args.args[0])

    def test_actor_context_travels_with_the_exception(self):
        self._fail(FakeMessage(args={"document_id": 42}))
        props = self.client.capture_exception.call_args.kwargs["properties"]
        self.assertEqual(props["actor"], "send_background_email")
        self.assertEqual(props["queue"], "default")
        self.assertEqual(props["arguments"], {"document_id": "42"})

    def test_success_is_not_reported(self):
        self.mw.after_process_message(None, FakeMessage(), result="ok", exception=None)
        self.client.capture_exception.assert_not_called()

    def test_skipped_message_is_reported(self):
        self.mw.after_skip_message(None, FakeMessage())
        self.assertEqual(self.client.capture.call_args.args[0], "dramatiq_message_skipped")

    def test_no_client_is_a_no_op(self):
        with mock.patch("core.apps.posthog_client", None):
            self._fail()

    def test_unrepresentable_argument_does_not_break_capture(self):
        class Hostile:
            def __repr__(self):
                raise RuntimeError("nope")

        self._fail(FakeMessage(args={"bad": Hostile()}))
        props = self.client.capture_exception.call_args.kwargs["properties"]
        self.assertEqual(props["arguments"], {"bad": "<unrepresentable Hostile>"})

    def test_middleware_is_registered(self):
        """A wrong path here would fail silently."""
        from django.conf import settings

        self.assertIn("core.error_tracking.ErrorTracking", settings.DRAMATIQ_BROKER["MIDDLEWARE"])


class LogCaptureTests(SimpleTestCase):
    def setUp(self):
        self.handler = LogCapture(level=logging.WARNING)
        patcher = mock.patch("core.apps.posthog_client", mock.Mock())
        self.client = patcher.start()
        self.addCleanup(patcher.stop)

    def _record(self, msg, level=logging.WARNING, name="billing.webhooks", exc_info=None):
        return logging.LogRecord(
            name=name,
            level=level,
            pathname=__file__,
            lineno=10,
            msg=msg,
            args=(),
            exc_info=exc_info,
        )

    def test_warning_becomes_an_error_not_an_analytics_event(self):
        self.handler.emit(self._record("no PackagePayment for session abc"))
        self.client.capture.assert_not_called()
        args, kwargs = self.client.capture_exception.call_args
        exc = args[0]
        self.assertIsInstance(exc, RuntimeError)
        self.assertEqual(str(exc), "no PackagePayment for session abc")
        self.assertEqual(kwargs["properties"]["level"], "WARNING")
        self.assertEqual(kwargs["properties"]["logger"], "billing.webhooks")

    def test_logging_inside_except_keeps_the_original_traceback(self):
        """The common shape: caught, logged without exc_info, execution continues."""
        try:
            raise ValueError("stripe said no")
        except ValueError:
            logger.warning("could not sync payment for order 1")

        self.client.capture_exception.assert_called_once()
        exc = self.client.capture_exception.call_args.args[0]
        self.assertIsInstance(exc, ValueError)
        self.assertEqual(str(exc), "stripe said no")
        self.assertIsNotNone(exc.__traceback__)

    def test_info_and_below_are_not_reported(self):
        for level in (logging.DEBUG, logging.INFO):
            with self.subTest(level=level):
                self.handler.emit(self._record("chatter", level=level))
        self.client.capture_exception.assert_not_called()

    def test_record_with_exception_keeps_the_traceback(self):
        import sys

        try:
            raise ValueError("boom")
        except ValueError:
            exc_info = sys.exc_info()
        self.handler.emit(self._record("failed", exc_info=exc_info))
        self.client.capture_exception.assert_called_once()

    def test_posthog_namespace_is_ignored(self):
        """A failure to report would recurse into another failure to report."""
        for name in ("posthog.client", "posthog.export"):
            with self.subTest(name=name):
                self.handler.emit(self._record("send failed", name=name))
        self.client.capture.assert_not_called()

    def test_posthog_export_is_not_wired_to_this_handler(self):
        """core.posthog_logs owns that logger and stops it propagating at runtime.

        Settings must not attach error_tracking to it, or the same record would
        be both exported to PostHog logs and reported as an error.
        """
        from django.conf import settings

        self.assertNotIn("posthog.export", settings.LOGGING.get("loggers", {}))

    def test_handler_errors_never_propagate(self):
        self.client.capture_exception.side_effect = RuntimeError("posthog down")
        self.handler.handle(self._record("something failed"))

    def test_handler_is_wired_where_propagate_is_off(self):
        """These loggers set propagate=False, so root alone would miss them."""
        from django.conf import settings

        for name in ("documents", "records"):
            self.assertIn("error_tracking", settings.LOGGING["loggers"][name]["handlers"])
        self.assertIn("error_tracking", settings.LOGGING["root"]["handlers"])
