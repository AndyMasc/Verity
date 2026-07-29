"""Shared Plaid API client instance.

Configures and exports a singleton ``PlaidApi`` client used by all
other modules in the plaid_integration app. The environment (sandbox,
development, production) is determined by the ``PLAID_ENV`` setting.
"""

import plaid
from django.conf import settings
from plaid.api import plaid_api

PLAID_ENV_MAP = {
    "sandbox": plaid.Environment.Sandbox,
    "development": "https://development.plaid.com",
    "production": plaid.Environment.Production,
}

env_name = settings.PLAID_ENV.lower()
host = PLAID_ENV_MAP.get(env_name, plaid.Environment.Sandbox)

configuration = plaid.Configuration(
    host=host,
    api_key={
        "clientId": settings.PLAID_CLIENT_ID,
        "secret": settings.PLAID_SECRET,
        "version": "2020-09-14",
    },
)
client = plaid_api.PlaidApi(plaid.ApiClient(configuration))
