"""Shared Plaid API client instance.

Configures and exports a singleton "PlaidApi" client used by all
other modules in the plaid_integration app. The environment (sandbox,
development, production) is determined by the "PLAID_ENV" setting.
"""

import plaid
from django.conf import settings
from plaid.api import plaid_api

PLAID_ENV_MAP = {
    "sandbox": "https://sandbox.plaid.com",
    "development": "https://development.plaid.com",
    "production": "https://production.plaid.com",
}

configuration = plaid.Configuration(
    host=PLAID_ENV_MAP.get(settings.PLAID_ENV.lower(), PLAID_ENV_MAP["sandbox"]),
    api_key={
        "clientId": settings.PLAID_CLIENT_ID,
        "secret": settings.PLAID_SECRET,
        "version": "2020-09-14",
    },
)
client = plaid_api.PlaidApi(plaid.ApiClient(configuration))
