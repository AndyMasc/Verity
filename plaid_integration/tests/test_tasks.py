import hashlib
import json
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone

from plaid_integration.models import PlaidItem
from plaid_integration.services import public_token_exchange
from plaid_integration.tasks import sync_and_convert_for_item_task
from plaid_integration.views import verify_plaid_webhook
from records.models import Record


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

    def test_missing_verification_header(self):
        self.assertFalse(verify_plaid_webhook(b"body", None))
        self.assertFalse(verify_plaid_webhook(b"body", ""))

    @patch("plaid_integration.views.webhook._get_plaid_jwk")
    def test_invalid_jwt_returns_false(self, mock_jwk):
        result = verify_plaid_webhook(b"body", "not-a-valid-jwt")
        self.assertFalse(result)
        mock_jwk.assert_not_called()

    @patch("plaid_integration.views.webhook._get_plaid_jwk")
    def test_no_jwk_found_returns_false(self, mock_jwk):
        mock_jwk.return_value = None
        import jwt as pyjwt
        from datetime import UTC, datetime

        token = pyjwt.encode(
            {"kid": "unknown-kid", "exp": datetime.max.replace(tzinfo=UTC).timestamp()},
            "secret",
            algorithm="HS256",
        )
        result = verify_plaid_webhook(b"body", token)
        self.assertFalse(result)

    def test_body_hash_mismatch_returns_false(self):
        import jwt as pyjwt
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.hazmat.primitives import serialization
        from datetime import UTC, datetime
        import base64

        def _int_to_base64url(n):
            byte_length = (n.bit_length() + 7) // 8
            return (
                base64.urlsafe_b64encode(n.to_bytes(byte_length, byteorder="big"))
                .rstrip(b"=")
                .decode()
            )

        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        public_key = private_key.public_key()

        body_hash = hashlib.sha256(b"original body").hexdigest()
        token = pyjwt.encode(
            {
                "kid": "test-kid",
                "request_body_sha256": body_hash,
                "exp": datetime.max.replace(tzinfo=UTC).timestamp(),
            },
            private_key,
            algorithm="RS256",
        )

        with patch("plaid_integration.views.webhook._get_plaid_jwk") as mock_jwk:
            pub_numbers = public_key.public_numbers()
            public_jwk = {
                "kty": "RSA",
                "n": _int_to_base64url(pub_numbers.n),
                "e": _int_to_base64url(pub_numbers.e),
                "kid": "test-kid",
            }
            mock_jwk.return_value = public_jwk
            result = verify_plaid_webhook(b"different body", token)
            self.assertFalse(result)


class SyncAndConvertTaskTest(TestCase):
    """Tests for the sync_and_convert_for_item_task background task."""

    def setUp(self):
        self.user = User.objects.create_user(username="testuser", password="pass")
        self.plaid_item = PlaidItem.objects.create(
            user=self.user,
            item_id="item-1",
            access_token="access-1",
        )

    @patch("records.matching.try_match_plaid_record")
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

    @patch("records.matching.try_match_plaid_record")
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

    @patch("records.matching.try_match_plaid_record")
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

    @patch("records.matching.try_match_plaid_record")
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

    @patch("records.matching.try_match_plaid_record")
    @patch("plaid_integration.tasks.client")
    def test_nonexistent_plaid_item_returns_error(self, mock_client, mock_match):
        result = sync_and_convert_for_item_task(99999)
        self.assertIn("error", result)
