from unittest.mock import patch

import requests
from django.contrib.auth import get_user_model

User = get_user_model()
from django.test import TestCase

from core.tasks import send_background_email


def _rejection_response(status_code: int) -> requests.Response:
    response = requests.Response()
    response.status_code = status_code
    response._content = b'{"message": "rejected"}'
    return response


class CoreTasksTest(TestCase):
    @patch("core.tasks.EmailMultiAlternatives")
    def test_send_background_email(self, mock_email_cls):
        send_background_email(
            subject="Test",
            message="Text body",
            from_email="from@example.com",
            recipient_list=["to@example.com"],
            html_message="<p>HTML</p>",
        )
        mock_email_cls.assert_called_once()
        mock_email_cls.return_value.send.assert_called_once()

    @patch("core.tasks.EmailMultiAlternatives")
    def test_send_background_email_permanent_rejection_no_retry(self, mock_email_cls):
        from anymail.exceptions import AnymailRequestsAPIError

        mock_email_cls.return_value.send.side_effect = AnymailRequestsAPIError(
            email_message=mock_email_cls.return_value,
            payload={},
            response=_rejection_response(422),
            backend=None,
        )

        send_background_email(
            subject="Test",
            message="Text body",
            from_email="from@example.com",
            recipient_list=["to@example.com"],
        )

    @patch("core.tasks.EmailMultiAlternatives")
    def test_send_background_email_transient_raises_for_retry(self, mock_email_cls):
        from anymail.exceptions import AnymailRequestsAPIError

        mock_email_cls.return_value.send.side_effect = AnymailRequestsAPIError(
            email_message=mock_email_cls.return_value,
            payload={},
            response=_rejection_response(500),
            backend=None,
        )

        with self.assertRaises(AnymailRequestsAPIError):
            send_background_email(
                subject="Test",
                message="Text body",
                from_email="from@example.com",
                recipient_list=["to@example.com"],
            )

    @patch("core.tasks.send_user_notification")
    def test_fire_single_webpush(self, mock_send):
        from core.tasks import fire_single_webpush

        user = User.objects.create_user(username="pushuser", password="pass")
        fire_single_webpush(user_id=user.id, payload={"head": "Test"}, ttl=1000)
        mock_send.assert_called_once()

    @patch("core.tasks.send_user_notification")
    def test_fire_single_webpush_user_not_found(self, mock_send):
        from core.tasks import fire_single_webpush

        fire_single_webpush(user_id=99999, payload={}, ttl=1000)
        mock_send.assert_not_called()
