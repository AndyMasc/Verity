"""Service layer for Plaid API token operations.

Encapsulates the public-token-to-access-token exchange that converts
a short-lived Link token into a persistent credential stored server-side.
"""

import logging

import plaid
from plaid.model.item_public_token_exchange_request import (
    ItemPublicTokenExchangeRequest,
)

from .plaid_client import client as plaid_client

logger = logging.getLogger(__name__)


def public_token_exchange(public_token: str) -> tuple[str, str]:
    """Exchange a Plaid public token for a long-lived access token and item ID.

    This is the final step of the bank linking flow. The returned access
    token is used for all subsequent Plaid API calls for this bank item.
    """
    try:
        request = ItemPublicTokenExchangeRequest(public_token=public_token)
        response = plaid_client.item_public_token_exchange(request)

        access_token = response["access_token"]
        item_id = response["item_id"]

        return access_token, item_id

    except plaid.ApiException as e:
        logger.error("Plaid API error during exchange: %s", e)
        raise
    except Exception:
        logger.exception("Unexpected error in public_token_exchange")
        raise
