import base64
import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from unittest.mock import patch

import jwt as pyjwt
import plaid
from cryptography.hazmat.primitives.asymmetric import rsa
from django.contrib.auth import get_user_model
from django.test import TestCase
from jwt import PyJWK
from jwt.exceptions import PyJWKClientConnectionError, PyJWKClientError

from plaid_integration.models import PlaidItem
from plaid_integration.services import public_token_exchange
from plaid_integration.tasks import sync_and_convert_for_item_task
from plaid_integration.views import verify_plaid_webhook
from records.models import Record

User = get_user_model()


def _int_to_base64url(n: int) -> str:
    """Encode an RSA modulus/exponent as base64url, as JWK requires."""
    length = (n.bit_length() + 7) // 8
    return base64.urlsafe_b64encode(n.to_bytes(length, "big")).rstrip(b"=").decode()


class PublicTokenExchangeTest(TestCase):
    """Tests for the public_token_exchange service."""

    @patch("plaid_integration.services.plaid_client")
    def test_successful_exchange(self, mock_client):
        mock_response = {
            "access_token": "access-xxx",
            "item_id": "item-yyy",
        }
        mock_client.item_public_token_exchange.return_value = mock_response

        access_token, item_id = public_token_exchange("public-token-123")
        self.assertEqual(access_token, "access-xxx")
        self.assertEqual(item_id, "item-yyy")

    @patch("plaid_integration.services.plaid_client")
    def test_api_error_raises(self, mock_client):
        import plaid

        mock_client.item_public_token_exchange.side_effect = plaid.ApiException(
            status=400, reason="Bad Request"
        )
        with self.assertRaises(plaid.ApiException):
            public_token_exchange("bad-token")

    @patch("plaid_integration.services.plaid_client")
    def test_unexpected_error_raises(self, mock_client):
        mock_client.item_public_token_exchange.side_effect = RuntimeError("Unexpected")
        with self.assertRaises(RuntimeError):
            public_token_exchange("token")


class WebhookVerificationTest(TestCase):
    """Tests for Plaid webhook signature verification."""

    def setUp(self):
        self.private_key = rsa.generate_private_key(
            public_exponent=65537, key_size=2048
        )
        numbers = self.private_key.public_key().public_numbers()
        self.jwk = {
            "kty": "RSA",
            "n": _int_to_base64url(numbers.n),
            "e": _int_to_base64url(numbers.e),
            "kid": "test-kid",
        }
        self.signing_key = PyJWK.from_dict(self.jwk)

    def _token(self, body: bytes, **claims) -> str:
        payload = {
            "request_body_sha256": hashlib.sha256(body).hexdigest(),
            "exp": (datetime.now(UTC) + timedelta(days=1)).timestamp(),
            **claims,
        }
        return pyjwt.encode(
            payload, self.private_key, algorithm="RS256", headers={"kid": "test-kid"}
        )

    def test_missing_verification_header(self):
        self.assertFalse(verify_plaid_webhook(b"body", None))
        self.assertFalse(verify_plaid_webhook(b"body", ""))

    @patch.object(pyjwt.PyJWKClient, "fetch_data")
    def test_invalid_jwt_returns_false_without_network_call(self, mock_fetch):
        self.assertFalse(verify_plaid_webhook(b"body", "not-a-valid-jwt"))
        mock_fetch.assert_not_called()

    @patch("plaid_integration.views.webhook.jwks_client")
    def test_unknown_kid_returns_false(self, mock_client):
        mock_client.get_signing_key_from_jwt.side_effect = PyJWKClientError(
            "no such kid"
        )
        token = self._token(b"body")
        self.assertFalse(verify_plaid_webhook(b"body", token))

    @patch("plaid_integration.views.webhook.jwks_client")
    def test_jwks_fetch_failure_returns_false(self, mock_client):
        mock_client.get_signing_key_from_jwt.side_effect = PyJWKClientConnectionError(
            "unreachable"
        )
        token = self._token(b"body")
        self.assertFalse(verify_plaid_webhook(b"body", token))

    @patch("plaid_integration.views.webhook.jwks_client")
    def test_valid_token_returns_true(self, mock_client):
        mock_client.get_signing_key_from_jwt.return_value = self.signing_key
        token = self._token(b"body")
        self.assertTrue(verify_plaid_webhook(b"body", token))

    @patch("plaid_integration.views.webhook.jwks_client")
    def test_body_hash_mismatch_returns_false(self, mock_client):
        mock_client.get_signing_key_from_jwt.return_value = self.signing_key
        token = self._token(b"original body")
        self.assertFalse(verify_plaid_webhook(b"different body", token))

    @patch("plaid_integration.views.webhook.jwks_client")
    def test_expired_token_returns_false(self, mock_client):
        mock_client.get_signing_key_from_jwt.return_value = self.signing_key
        token = self._token(b"body", exp=datetime.min.replace(tzinfo=UTC).timestamp())
        self.assertFalse(verify_plaid_webhook(b"body", token))


class SyncAndConvertTaskTest(TestCase):
    """Tests for the sync_and_convert_for_item_task background task."""

    def setUp(self):
        self.user = User.objects.create_user(username="testuser", password="pass")
        self.plaid_item = PlaidItem.objects.create(
            user=self.user,
            item_id="item-1",
            access_token="access-1",
        )

    @patch("plaid_integration.tasks.try_match_plaid_record")
    @patch("plaid_integration.tasks.client")
    def test_sync_creates_records(self, mock_client, mock_match):
        mock_response = {
            "added": [
                {
                    "transaction_id": "txn-001",
                    "name": "Coffee Shop",
                    "amount": 5.50,
                    "date": "2024-06-15",
                    "account_id": "acc1",
                    "category": ["Food and Drink"],
                }
            ],
            "modified": [],
            "removed": [],
            "next_cursor": "cursor-abc",
            "has_more": False,
        }
        mock_client.transactions_sync.return_value = mock_response

        result = sync_and_convert_for_item_task(self.plaid_item.id)
        self.assertEqual(result["added"], 1)
        self.assertEqual(result["modified"], 0)
        self.assertEqual(result["removed"], 0)
        self.assertTrue(Record.objects.filter(plaid_transaction_id="txn-001").exists())

    @patch("plaid_integration.tasks.try_match_plaid_record")
    @patch("plaid_integration.tasks.client")
    def test_sync_removes_deactivated_records(self, mock_client, mock_match):
        record = Record.objects.create(
            user=self.user,
            title="Old Transaction",
            transaction_date=date(2024, 6, 1),
            plaid_transaction_id="txn-old",
            plaid_item=self.plaid_item,
        )
        mock_response = {
            "added": [],
            "modified": [],
            "removed": [{"transaction_id": "txn-old"}],
            "next_cursor": "cursor-xyz",
            "has_more": False,
        }
        mock_client.transactions_sync.return_value = mock_response

        result = sync_and_convert_for_item_task(self.plaid_item.id)
        self.assertEqual(result["removed"], 1)
        record.refresh_from_db()
        self.assertFalse(record.is_active)

    @patch("plaid_integration.tasks.client")
    def test_sync_handles_api_error_with_retry(self, mock_client):
        mock_client.transactions_sync.side_effect = Exception("API Error")

        with self.assertRaises(Exception):
            sync_and_convert_for_item_task(self.plaid_item.id)

    @patch("plaid_integration.tasks.client")
    def test_sync_records_login_required_without_retrying(self, mock_client):
        error = plaid.ApiException(
            status=400,
            reason="Bad Request",
            http_resp=type(
                "Response",
                (),
                {
                    "status": 400,
                    "reason": "Bad Request",
                    "data": json.dumps(
                        {
                            "error_code": "ITEM_LOGIN_REQUIRED",
                            "error_message": "A user login is required.",
                        }
                    ),
                    "getheaders": lambda _self: {},
                },
            )(),
        )
        mock_client.transactions_sync.side_effect = error

        result = sync_and_convert_for_item_task(self.plaid_item.id)

        self.assertEqual(result, {"error": "ITEM_LOGIN_REQUIRED"})
        self.plaid_item.refresh_from_db()
        self.assertEqual(self.plaid_item.last_error_code, "ITEM_LOGIN_REQUIRED")
        self.assertEqual(
            self.plaid_item.last_error_message, "A user login is required."
        )

    @patch("plaid_integration.tasks.try_match_plaid_record")
    @patch("plaid_integration.tasks.client")
    def test_sync_updates_cursor(self, mock_client, mock_match):
        mock_response = {
            "added": [],
            "modified": [],
            "removed": [],
            "next_cursor": "new-cursor-123",
            "has_more": False,
        }
        mock_client.transactions_sync.return_value = mock_response

        sync_and_convert_for_item_task(self.plaid_item.id)
        self.plaid_item.refresh_from_db()
        self.assertEqual(self.plaid_item.next_cursor, "new-cursor-123")

    @patch("plaid_integration.tasks.try_match_plaid_record")
    @patch("plaid_integration.tasks.client")
    def test_sync_handles_pagination(self, mock_client, mock_match):
        page1 = {
            "added": [
                {
                    "transaction_id": "txn-1",
                    "name": "T1",
                    "amount": 10,
                    "date": "2024-06-15",
                    "account_id": "a1",
                }
            ],
            "modified": [],
            "removed": [],
            "next_cursor": "cursor-2",
            "has_more": True,
        }
        page2 = {
            "added": [
                {
                    "transaction_id": "txn-2",
                    "name": "T2",
                    "amount": 20,
                    "date": "2024-06-16",
                    "account_id": "a1",
                }
            ],
            "modified": [],
            "removed": [],
            "next_cursor": "cursor-done",
            "has_more": False,
        }
        mock_client.transactions_sync.side_effect = [page1, page2]

        result = sync_and_convert_for_item_task(self.plaid_item.id)
        self.assertEqual(result["added"], 2)

    @patch("plaid_integration.tasks.try_match_plaid_record")
    @patch("plaid_integration.tasks.client")
    def test_nonexistent_plaid_item_returns_error(self, mock_client, mock_match):
        result = sync_and_convert_for_item_task(99999)
        self.assertIn("error", result)
