"""Plaid status, sync, and disconnect views."""

import logging

import plaid
from django.conf import settings
from django.core.cache import cache
from django.db.models import Count
from plaid.model.item_remove_request import ItemRemoveRequest
from plaid.model.sandbox_item_fire_webhook_request import SandboxItemFireWebhookRequest
from rest_framework import authentication, permissions
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from ..models import PlaidItem
from ..plaid_client import client
from ..tasks import sync_and_convert_for_item_task

logger: logging.Logger = logging.getLogger(__name__)

PLAID_STATUS_CACHE_TTL = 30


class PlaidStatusView(APIView):
    """Return connection status and metadata for all user-linked Plaid items.

    Responses are cached briefly to avoid repeated DB queries when the
    frontend polls for connection health.
    """

    authentication_classes = [authentication.SessionAuthentication]
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request: Request) -> Response:
        """Return cached Plaid connection status for the authenticated user."""
        cache_key = f"plaid_status:{request.user.id}"
        cached = cache.get(cache_key)
        if cached is not None:
            return Response(cached)

        plaid_items = list(
            PlaidItem.objects.filter(user=request.user).annotate(record_count=Count("records"))
        )

        data = {
            "connected": bool(plaid_items),
            "items": [
                {
                    "item_id": item.item_id,
                    "created_at": (item.created_at.isoformat() if item.created_at else None),
                    "has_cursor": bool(item.next_cursor),
                    "record_count": item.record_count,
                    "account_name": item.institution_name,
                    "accounts_data": item.accounts_data,
                    "last_error_code": item.last_error_code,
                    "last_error_message": item.last_error_message,
                    "last_error_at": (
                        item.last_error_at.isoformat() if item.last_error_at else None
                    ),
                }
                for item in plaid_items
            ],
        }
        cache.set(cache_key, data, PLAID_STATUS_CACHE_TTL)
        return Response(data)


class SyncTransactionsView(APIView):
    """Trigger an on-demand transaction sync for a Plaid bank item."""

    authentication_classes = [authentication.SessionAuthentication]
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request: Request) -> Response:
        """Trigger transaction sync (direct background task in Prod, sandbox webhook in Sandbox)."""
        item_id: str | None = request.data.get("item_id")

        try:
            if item_id:
                plaid_item = PlaidItem.objects.get(user=request.user, item_id=item_id)
            else:
                plaid_item = PlaidItem.objects.filter(user=request.user).first()
                if not plaid_item:
                    raise PlaidItem.DoesNotExist
        except PlaidItem.DoesNotExist:
            return Response(
                {"error": "No Plaid link found for the specified item"},
                status=400,
            )

        if getattr(settings, "PLAID_ENV", "") == "sandbox":
            try:
                client.sandbox_item_fire_webhook(
                    SandboxItemFireWebhookRequest(
                        access_token=plaid_item.access_token,
                        webhook_code="DEFAULT_UPDATE",
                    )
                )
            except plaid.ApiException:
                logger.exception(
                    "Failed to fire Plaid sandbox webhook for item %s",
                    plaid_item.item_id,
                )
                return Response({"error": "Failed to trigger sync via Plaid"}, status=502)
        else:
            # Direct background task dispatch for Live/Production
            sync_and_convert_for_item_task.send(plaid_item.id)

        # Invalidate status cache on manual sync trigger
        cache.delete(f"plaid_status:{request.user.id}")
        return Response({"status": "sync_initiated"})


class DisconnectBankView(APIView):
    """Remove a linked bank item from both Plaid and the local database."""

    authentication_classes = [authentication.SessionAuthentication]
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request: Request, item_id: str) -> Response:
        """Disconnect and delete the specified bank item."""
        try:
            plaid_item = PlaidItem.objects.get(user=request.user, item_id=item_id)
        except PlaidItem.DoesNotExist:
            return Response({"error": "Bank connection not found"}, status=404)

        try:
            client.item_remove(ItemRemoveRequest(access_token=plaid_item.access_token))
        except plaid.ApiException:
            logger.exception("Failed to remove Plaid item %s from Plaid dashboard", item_id)

        plaid_item.delete()
        cache.delete(f"plaid_status:{request.user.id}")
        return Response({"success": "Bank disconnected"})
