import json
from datetime import date
from unittest.mock import patch

from django.contrib.auth import get_user_model

User = get_user_model()
from django.test import TestCase, RequestFactory, override_settings
from django.urls import reverse

from plaid_integration.models import PlaidItem
from plaid_integration.views import (
    plaid_webhook,
    CreateLinkTokenView,
    PublicTokenExchange,
    PlaidStatusView,
    DisconnectBankView,
)
from records.models import Record

from billing.tests_helpers import give_pro_subscription


class PlaidViewsTest(TestCase):
    """Tests for Plaid integration API views."""

    def setUp(self):
        self.user = User.objects.create_user(username="testuser", password="pass")
        give_pro_subscription(self.user)
        self.client.login(username="testuser", password="pass")
        self.plaid_item = PlaidItem.objects.create(
            user=self.user,
            item_id="item-123",
            access_token="access-123",
            institution_name="Test Bank",
        )

    @patch("plaid_integration.views.link.client")
    def test_create_link_token(self, mock_client):
        mock_client.link_token_create.return_value = {"link_token": "link-xxx"}
        response = self.client.post(reverse("plaid:create_link_token"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["link_token"], "link-xxx")

    @patch("plaid_integration.views.link.client")
    def test_create_link_token_api_error(self, mock_client):
        import plaid

        mock_client.link_token_create.side_effect = plaid.ApiException(
            status=400, reason="Bad Request"
        )
        response = self.client.post(reverse("plaid:create_link_token"))
        self.assertEqual(response.status_code, 400)

    @patch("plaid_integration.views.link.client")
    def test_create_update_link_token(self, mock_client):
        mock_client.link_token_create.return_value = {"link_token": "link-update"}
        response = self.client.post(reverse("plaid:create_update_link_token", args=["item-123"]))
        self.assertEqual(response.status_code, 200)

    def test_create_update_link_token_not_found(self):
        response = self.client.post(reverse("plaid:create_update_link_token", args=["nonexistent"]))
        self.assertEqual(response.status_code, 404)

    def test_plaid_status_connected(self):
        response = self.client.get(reverse("plaid:status"))
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["connected"])
        self.assertEqual(len(data["items"]), 1)

    def test_plaid_status_not_connected(self):
        from django.core.cache import cache

        cache.clear()
        self.plaid_item.delete()
        response = self.client.get(reverse("plaid:status"))
        data = response.json()
        self.assertFalse(data["connected"])

    def test_plaid_status_cached(self):
        response1 = self.client.get(reverse("plaid:status"))
        response2 = self.client.get(reverse("plaid:status"))
        self.assertEqual(response1.json(), response2.json())

    def test_disconnect_bank(self):
        response = self.client.post(reverse("plaid:disconnect", args=["item-123"]))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(PlaidItem.objects.filter(item_id="item-123").exists())

    def test_disconnect_bank_not_found(self):
        response = self.client.post(reverse("plaid:disconnect", args=["nonexistent"]))
        self.assertEqual(response.status_code, 404)

    @patch("plaid_integration.views.status.client")
    def test_sync_transactions(self, mock_client):
        response = self.client.post(
            reverse("plaid:sync"),
            data=json.dumps({"item_id": "item-123"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)

    @patch("plaid_integration.views.status.client")
    def test_sync_transactions_no_item(self, mock_client):
        self.plaid_item.delete()
        response = self.client.post(reverse("plaid:sync"))
        self.assertEqual(response.status_code, 400)

    def test_unauthenticated_access_redirects(self):
        self.client.logout()
        response = self.client.get(reverse("plaid:status"))
        self.assertEqual(response.status_code, 403)


class PlaidWebhookViewTest(TestCase):
    """Tests for the Plaid webhook receiver endpoint."""

    def setUp(self):
        self.factory = RequestFactory()
        self.user = User.objects.create_user(username="testuser", password="pass")
        self.plaid_item = PlaidItem.objects.create(
            user=self.user,
            item_id="item-webhook-1",
            access_token="access-wh-1",
        )

    @override_settings(PLAID_ENV="sandbox")
    @patch("plaid_integration.views.webhook.sync_and_convert_for_item_task")
    def test_sync_updates_available_webhook(self, mock_task):
        payload = {
            "webhook_type": "TRANSACTIONS",
            "webhook_code": "SYNC_UPDATES_AVAILABLE",
            "item_id": "item-webhook-1",
        }
        request = self.factory.post(
            reverse("plaid:webhook"),
            data=json.dumps(payload),
            content_type="application/json",
        )
        response = plaid_webhook(request)
        self.assertEqual(response.status_code, 200)
        mock_task.delay.assert_called_once_with(self.plaid_item.id)

    @override_settings(PLAID_ENV="sandbox", PLAID_SYNC_COOLDOWN_SECONDS=60)
    @patch("plaid_integration.views.webhook.sync_and_convert_for_item_task")
    def test_sync_webhook_debounced_within_cooldown(self, mock_task):
        from datetime import timedelta

        from django.utils import timezone as dj_tz

        payload = {
            "webhook_type": "TRANSACTIONS",
            "webhook_code": "SYNC_UPDATES_AVAILABLE",
            "item_id": "item-webhook-1",
        }
        request = self.factory.post(
            reverse("plaid:webhook"),
            data=json.dumps(payload),
            content_type="application/json",
        )
        plaid_webhook(request)
        mock_task.delay.assert_called_once_with(self.plaid_item.id)

        plaid_webhook(request)
        mock_task.delay.assert_called_once_with(self.plaid_item.id)

        PlaidItem.objects.filter(id=self.plaid_item.id).update(
            last_synced_at=dj_tz.now() - timedelta(seconds=120)
        )
        plaid_webhook(request)
        self.assertEqual(mock_task.delay.call_count, 2)

    @override_settings(PLAID_ENV="sandbox", PLAID_SYNC_COOLDOWN_SECONDS=60)
    @patch("plaid_integration.views.webhook.sync_and_convert_for_item_task")
    def test_sync_webhook_sets_last_synced_at(self, mock_task):
        payload = {
            "webhook_type": "TRANSACTIONS",
            "webhook_code": "SYNC_UPDATES_AVAILABLE",
            "item_id": "item-webhook-1",
        }
        request = self.factory.post(
            reverse("plaid:webhook"),
            data=json.dumps(payload),
            content_type="application/json",
        )
        plaid_webhook(request)
        self.plaid_item.refresh_from_db()
        self.assertIsNotNone(self.plaid_item.last_synced_at)
        mock_task.delay.assert_called_once_with(self.plaid_item.id)

    @override_settings(PLAID_ENV="sandbox")
    def test_item_login_required_webhook(self):
        payload = {
            "webhook_type": "ITEM",
            "webhook_code": "ITEM_LOGIN_REQUIRED",
            "item_id": "item-webhook-1",
        }
        request = self.factory.post(
            reverse("plaid:webhook"),
            data=json.dumps(payload),
            content_type="application/json",
        )
        response = plaid_webhook(request)
        self.assertEqual(response.status_code, 200)
        self.plaid_item.refresh_from_db()
        self.assertEqual(self.plaid_item.last_error_code, "ITEM_LOGIN_REQUIRED")

    @override_settings(PLAID_ENV="sandbox")
    def test_transactions_removed_webhook(self):
        Record.objects.create(
            user=self.user,
            title="To Remove",
            transaction_date=date(2024, 6, 1),
            plaid_transaction_id="txn-remove-me",
            plaid_item=self.plaid_item,
        )
        payload = {
            "webhook_type": "TRANSACTIONS",
            "webhook_code": "TRANSACTIONS_REMOVED",
            "item_id": "item-webhook-1",
            "removed_transactions": ["txn-remove-me"],
        }
        request = self.factory.post(
            reverse("plaid:webhook"),
            data=json.dumps(payload),
            content_type="application/json",
        )
        response = plaid_webhook(request)
        self.assertEqual(response.status_code, 200)
        record = Record.objects.get(plaid_transaction_id="txn-remove-me")
        self.assertFalse(record.is_active)

    @override_settings(PLAID_ENV="sandbox")
    def test_error_webhook(self):
        payload = {
            "webhook_type": "ITEM",
            "webhook_code": "ERROR",
            "item_id": "item-webhook-1",
            "error": {
                "error_code": "ITEM_NOT_FOUND",
                "error_message": "Item not found",
            },
        }
        request = self.factory.post(
            reverse("plaid:webhook"),
            data=json.dumps(payload),
            content_type="application/json",
        )
        response = plaid_webhook(request)
        self.assertEqual(response.status_code, 200)
        self.plaid_item.refresh_from_db()
        self.assertEqual(self.plaid_item.last_error_code, "ITEM_NOT_FOUND")

    @override_settings(PLAID_ENV="sandbox")
    def test_webhook_unknown_item_returns_ok(self):
        payload = {
            "webhook_type": "TRANSACTIONS",
            "webhook_code": "SYNC_UPDATES_AVAILABLE",
            "item_id": "unknown-item",
        }
        request = self.factory.post(
            reverse("plaid:webhook"),
            data=json.dumps(payload),
            content_type="application/json",
        )
        response = plaid_webhook(request)
        self.assertEqual(response.status_code, 200)

    @override_settings(PLAID_ENV="sandbox")
    def test_webhook_invalid_json(self):
        request = self.factory.post(
            reverse("plaid:webhook"),
            data="not json",
            content_type="application/json",
        )
        response = plaid_webhook(request)
        self.assertEqual(response.status_code, 400)

    @override_settings(PLAID_ENV="sandbox")
    def test_webhook_too_large(self):
        payload = json.dumps({"item_id": "x" * 200000})
        request = self.factory.post(
            reverse("plaid:webhook"),
            data=payload,
            content_type="application/json",
        )
        response = plaid_webhook(request)
        self.assertEqual(response.status_code, 400)

    @override_settings(PLAID_ENV="production")
    @patch("plaid_integration.views.webhook.verify_plaid_webhook")
    def test_production_mode_verifies_webhook(self, mock_verify):
        mock_verify.return_value = False
        payload = {
            "webhook_type": "TRANSACTIONS",
            "webhook_code": "SYNC_UPDATES_AVAILABLE",
            "item_id": "item-webhook-1",
        }
        request = self.factory.post(
            reverse("plaid:webhook"),
            data=json.dumps(payload),
            content_type="application/json",
        )
        response = plaid_webhook(request)
        self.assertEqual(response.status_code, 403)

    @override_settings(PLAID_ENV="sandbox")
    def test_pending_expiration_webhook(self):
        payload = {
            "webhook_type": "ITEM",
            "webhook_code": "PENDING_EXPIRATION",
            "item_id": "item-webhook-1",
        }
        request = self.factory.post(
            reverse("plaid:webhook"),
            data=json.dumps(payload),
            content_type="application/json",
        )
        response = plaid_webhook(request)
        self.assertEqual(response.status_code, 200)
        self.plaid_item.refresh_from_db()
        self.assertEqual(self.plaid_item.last_error_code, "PENDING_EXPIRATION")


class PlaidUrlsTest(TestCase):
    """Tests for URL resolution."""

    def test_webhook_url_resolves(self):
        url = reverse("plaid:webhook")
        self.assertEqual(url, "/plaid/webhook/")

    def test_status_url_resolves(self):
        url = reverse("plaid:status")
        self.assertEqual(url, "/plaid/status/")

    def test_sync_url_resolves(self):
        url = reverse("plaid:sync")
        self.assertEqual(url, "/plaid/sync/")

    def test_connect_url_resolves(self):
        url = reverse("plaid:connect")
        self.assertEqual(url, "/plaid/connect/")

    def test_disconnect_url_resolves(self):
        url = reverse("plaid:disconnect", args=["item-123"])
        self.assertEqual(url, "/plaid/disconnect/item-123/")
