"""Worker failures must reach error tracking, not just the log stream."""

from unittest import mock

from django.test import SimpleTestCase

from core.middleware_error_tracking import ErrorTracking, _safe_args


class FakeMessage:
    def __init__(self, args=None):
        self.actor_name = "send_background_email"
        self.queue_name = "default"
        self.args = args if args is not None else {"user_id": 7, "payload": "x"}


class ErrorTrackingTests(SimpleTestCase):
    def setUp(self):
        self.mw = ErrorTracking()

    @mock.patch("core.middleware_error_tracking.sentry_sdk")
    def test_actor_exception_is_captured(self, sentry):
        boom = ValueError("actor blew up")
        self.mw.after_process_message(None, FakeMessage(), result=None, exception=boom)
        sentry.capture_exception.assert_called_once_with(boom)

    @mock.patch("core.middleware_error_tracking.sentry_sdk")
    def test_success_is_not_reported(self, sentry):
        self.mw.after_process_message(None, FakeMessage(), result="ok", exception=None)
        sentry.capture_exception.assert_not_called()

    @mock.patch("core.middleware_error_tracking.sentry_sdk")
    def test_skipped_message_is_reported(self, sentry):
        """AgeLimit discards work silently, so it needs its own signal."""
        self.mw.after_skip_message(None, FakeMessage())
        sentry.capture_message.assert_called_once()

    @mock.patch("core.middleware_error_tracking.sentry_sdk")
    def test_actor_identity_is_tagged(self, sentry):
        """The traceback has no idea which record or payment a task was working on."""
        scope = sentry.get_current_scope.return_value
        self.mw.before_process_message(None, FakeMessage(args={"document_id": 42}))
        scope.set_tag.assert_any_call("dramatiq.actor", "send_background_email")
        scope.set_context.assert_called_once()
        self.assertEqual(scope.set_context.call_args.args[1], {"document_id": "42"})

    def test_unrepresentable_argument_does_not_break_capture(self):
        class Hostile:
            def __repr__(self):
                raise RuntimeError("nope")

        self.assertEqual(
            _safe_args(FakeMessage(args={"bad": Hostile()})),
            {"bad": "<unrepresentable Hostile>"},
        )

    def test_middleware_is_registered_on_the_broker(self):
        """Wiring it up in settings is the whole point; a typo would silently no-op."""
        from django.conf import settings

        self.assertIn(
            "core.middleware_error_tracking.ErrorTracking",
            settings.DRAMATIQ_BROKER["MIDDLEWARE"],
        )
