"""Bank linking views: token creation, exchange, and connect page."""

import logging

import plaid
import posthog
from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import render
from plaid.model.accounts_get_request import AccountsGetRequest
from plaid.model.country_code import CountryCode
from plaid.model.institutions_get_by_id_request import InstitutionsGetByIdRequest
from plaid.model.institutions_get_by_id_request_options import InstitutionsGetByIdRequestOptions
from plaid.model.item_get_request import ItemGetRequest
from plaid.model.link_token_create_request import LinkTokenCreateRequest
from plaid.model.link_token_create_request_user import LinkTokenCreateRequestUser
from plaid.model.products import Products
from plaid.model.sandbox_item_fire_webhook_request import SandboxItemFireWebhookRequest
from rest_framework import authentication, permissions
from rest_framework.decorators import api_view, permission_classes
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from ..models import PlaidItem
from ..plaid_client import client
from ..services import public_token_exchange

logger: logging.Logger = logging.getLogger(__name__)


@api_view(["GET"])
@permission_classes([permissions.IsAuthenticated])
def plaid_connect_page(request: Request) -> HttpResponse:
    """Render the bank connection management page for the authenticated user."""
    plaid_items = PlaidItem.objects.filter(user=request.user).prefetch_related("records")
    return render(request, "plaid/connect.html", {"plaid_items": plaid_items})


class CreateLinkTokenView(APIView):
    """Create a Plaid Link token for a new bank connection."""

    authentication_classes = [authentication.SessionAuthentication]
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request: Request) -> Response:
        """Issue a new link token for the requesting user."""
        try:
            request_obj = LinkTokenCreateRequest(
                user=LinkTokenCreateRequestUser(client_user_id=str(request.user.id)),
                client_name="Papertrail",
                products=[Products("transactions")],
                country_codes=[CountryCode("US")],
                language="en",
                webhook=settings.PLAID_WEBHOOK_URL,
            )
            response = client.link_token_create(request_obj)
            return Response({"link_token": response["link_token"]})
        except plaid.ApiException as e:
            logger.exception("Link token creation failed for user %s", request.user)
            return Response({"error": str(e)}, status=400)


class CreateUpdateLinkTokenView(APIView):
    """Create a Plaid Link token to update credentials for an existing bank item."""

    authentication_classes = [authentication.SessionAuthentication]
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request: Request, item_id: str) -> Response:
        """Issue an update-mode link token for the specified bank item."""
        try:
            plaid_item = PlaidItem.objects.get(user=request.user, item_id=item_id)
        except PlaidItem.DoesNotExist:
            return Response({"error": "Bank connection not found"}, status=404)

        try:
            request_obj = LinkTokenCreateRequest(
                user=LinkTokenCreateRequestUser(client_user_id=str(request.user.id)),
                client_name="Papertrail",
                products=[Products("transactions")],
                country_codes=[CountryCode("US")],
                language="en",
                webhook=settings.PLAID_WEBHOOK_URL,
                access_token=plaid_item.access_token,
            )
            response = client.link_token_create(request_obj)
            return Response({"link_token": response["link_token"]})
        except plaid.ApiException as e:
            logger.exception("Update link token creation failed for item %s", item_id)
            return Response({"error": str(e)}, status=400)


class PublicTokenExchange(APIView):
    """Exchange a Plaid public token for a persistent access token.

    Completes the bank linking flow: exchanges the short-lived public token,
    fetches institution metadata and account info, creates a PlaidItem, and
    fires an initial sync webhook.
    """

    authentication_classes = [authentication.SessionAuthentication]
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request: Request) -> Response:
        """Exchange the public token and persist the new PlaidItem."""
        public_token: str | None = request.data.get("public_token")
        if not public_token:
            return Response({"error": "public_token is required"}, status=400)

        try:
            access_token, item_id = public_token_exchange(public_token)
            institution_name = _fetch_institution_name(access_token, item_id)
            accounts_data = _fetch_accounts(access_token, item_id)

            with transaction.atomic():
                PlaidItem.objects.create(
                    user=request.user,
                    item_id=item_id,
                    access_token=access_token,
                    institution_name=institution_name,
                    accounts_data=accounts_data,
                )

            _fire_initial_sync_webhook(access_token, item_id)
            cache.delete(f"plaid_status:{request.user.id}")
            posthog.capture(
                "bank_linked",
                distinct_id=str(request.user.id),
                properties={
                    "institution_name": institution_name,
                    "account_count": len(accounts_data),
                },
            )
            return Response({"success": "Bank linked successfully! Syncing transactions\u2026"})
        except Exception:
            logger.exception("Failed to exchange public token for user %s", request.user)
            return Response({"error": "Failed to exchange token"}, status=400)


def _fetch_institution_name(access_token: str, item_id: str) -> str:
    """Fetch the institution name from Plaid, returning a default on failure."""
    try:
        item_resp = client.item_get(ItemGetRequest(access_token=access_token))
        item_data = item_resp if isinstance(item_resp, dict) else item_resp.to_dict()
        inst_id = item_data.get("item", {}).get("institution_id", "")
        if inst_id:
            inst_req = InstitutionsGetByIdRequest(
                institution_id=inst_id,
                country_codes=[CountryCode("US")],
                options=InstitutionsGetByIdRequestOptions(include_optional_metadata=False),
            )
            inst_resp = client.institutions_get_by_id(inst_req)
            inst_data = inst_resp if isinstance(inst_resp, dict) else inst_resp.to_dict()
            return inst_data.get("institution", {}).get("name", "Bank Account")
    except Exception:
        logger.warning("Failed to fetch institution name for item %s", item_id)
    return "Bank Account"


def _fetch_accounts(access_token: str, item_id: str) -> list[dict[str, str]]:
    """Fetch account metadata from Plaid, returning an empty list on failure."""
    try:
        acct_resp = client.accounts_get(AccountsGetRequest(access_token=access_token))
        acct_data = acct_resp if isinstance(acct_resp, dict) else acct_resp.to_dict()
        return [
            {
                "id": a["account_id"],
                "name": a["name"],
                "mask": a.get("mask", ""),
                "type": a.get("type", ""),
                "subtype": a.get("subtype", ""),
            }
            for a in acct_data.get("accounts", [])
        ]
    except Exception:
        logger.warning("Failed to fetch accounts for item %s", item_id)
    return []


def _fire_initial_sync_webhook(access_token: str, item_id: str) -> None:
    """Fire a sandbox webhook to trigger the initial transaction sync."""
    try:
        client.sandbox_item_fire_webhook(
            SandboxItemFireWebhookRequest(
                access_token=access_token,
                webhook_code="DEFAULT_UPDATE",
            )
        )
    except plaid.ApiException:
        logger.warning("Failed to fire initial sync webhook for item %s", item_id)
