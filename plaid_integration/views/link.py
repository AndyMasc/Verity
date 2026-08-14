"""Bank linking views: token creation, exchange, and connect page."""

import logging
from typing import ClassVar

import plaid
import posthog
from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import render
from plaid.model.country_code import CountryCode
from plaid.model.link_token_create_request import LinkTokenCreateRequest
from plaid.model.link_token_create_request_user import LinkTokenCreateRequestUser
from plaid.model.products import Products
from rest_framework import authentication, permissions
from rest_framework.decorators import api_view, permission_classes
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from billing import features
from billing.entitlements import has_feature
from billing.mixins import FeatureRequiredMixin

from ..models import PlaidItem
from ..plaid_client import client
from ..services import (
    fetch_accounts,
    fetch_institution_name,
    public_token_exchange,
    trigger_initial_sync,
)

logger: logging.Logger = logging.getLogger(__name__)


@api_view(["GET"])
@permission_classes([permissions.IsAuthenticated])
def plaid_connect_page(request: Request) -> HttpResponse:
    """Render the bank connection management page for the authenticated user."""
    if not has_feature(request.user, features.BANK_TRANSACTION_SYNC):
        return render(
            request,
            "plaid/connect.html",
            {"plaid_items": [], "upgrade_required": True},
        )
    plaid_items = PlaidItem.objects.filter(user=request.user).prefetch_related("records")
    return render(request, "plaid/connect.html", {"plaid_items": plaid_items})


class CreateLinkTokenView(FeatureRequiredMixin, APIView):
    """Create a Plaid Link token for a new bank connection."""

    required_feature = features.BANK_TRANSACTION_SYNC
    authentication_classes: ClassVar[list] = [authentication.SessionAuthentication]
    permission_classes: ClassVar[list] = [permissions.IsAuthenticated]

    def post(self, request: Request) -> Response:
        """Issue a new link token for the requesting user."""
        try:
            request_obj = LinkTokenCreateRequest(
                user=LinkTokenCreateRequestUser(client_user_id=str(request.user.id)),
                client_name="Verity",
                products=[Products("transactions")],
                country_codes=[CountryCode("US")],
                language="en",
                webhook=settings.PLAID_WEBHOOK_URL,
            )
            response = client.link_token_create(request_obj)
            return Response({"link_token": response["link_token"]})
        except plaid.ApiException:
            logger.exception("Link token creation failed for user %s", request.user.id)
            return Response({"error": "Failed to create link token with Plaid"}, status=400)


class CreateUpdateLinkTokenView(FeatureRequiredMixin, APIView):
    """Create a Plaid Link token to update credentials for an existing bank item."""

    required_feature = features.BANK_TRANSACTION_SYNC
    authentication_classes: ClassVar[list] = [authentication.SessionAuthentication]
    permission_classes: ClassVar[list] = [permissions.IsAuthenticated]

    def post(self, request: Request, item_id: str) -> Response:
        """Issue an update-mode link token for the specified bank item."""
        try:
            plaid_item = PlaidItem.objects.get(user=request.user, item_id=item_id)
        except PlaidItem.DoesNotExist:
            return Response({"error": "Bank connection not found"}, status=404)

        try:
            request_obj = LinkTokenCreateRequest(
                user=LinkTokenCreateRequestUser(client_user_id=str(request.user.id)),
                client_name="Verity",
                products=[Products("transactions")],
                country_codes=[CountryCode("US")],
                language="en",
                webhook=settings.PLAID_WEBHOOK_URL,
                access_token=plaid_item.access_token,
            )
            response = client.link_token_create(request_obj)
            return Response({"link_token": response["link_token"]})
        except plaid.ApiException:
            logger.exception("Update link token creation failed for item %s", item_id)
            return Response({"error": "Failed to create update token with Plaid"}, status=400)


class PublicTokenExchange(FeatureRequiredMixin, APIView):
    """Exchange a Plaid public token for a persistent access token."""

    required_feature = features.BANK_TRANSACTION_SYNC
    authentication_classes: ClassVar[list] = [authentication.SessionAuthentication]
    permission_classes: ClassVar[list] = [permissions.IsAuthenticated]

    def post(self, request: Request) -> Response:
        """Exchange the public token and persist the new PlaidItem."""
        public_token: str | None = request.data.get("public_token")
        if not public_token:
            return Response({"error": "public_token is required"}, status=400)

        try:
            access_token, item_id = public_token_exchange(public_token)
            institution_name = fetch_institution_name(access_token, item_id)
            accounts_data = fetch_accounts(access_token, item_id)

            with transaction.atomic():
                plaid_item = PlaidItem.objects.create(
                    user=request.user,
                    item_id=item_id,
                    access_token=access_token,
                    institution_name=institution_name,
                    accounts_data=accounts_data,
                )

            trigger_initial_sync(plaid_item)
            cache.delete(f"plaid_status:{request.user.id}")

            posthog.capture(
                "bank_linked",
                distinct_id=str(request.user.id),
                properties={
                    "institution_name": institution_name,
                    "account_count": len(accounts_data),
                },
            )
            return Response({"success": "Bank linked successfully! Syncing transactions…"})
        except Exception:
            logger.exception("Failed to exchange public token for user %s", request.user.id)
            return Response({"error": "Failed to exchange token"}, status=400)
