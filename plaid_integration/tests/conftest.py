"""Shared fixtures for plaid_integration tests."""

from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _bypass_webhook_verification():
    """Skip Plaid webhook JWT verification in tests.

    Verification is mandatory outside local sandbox runs (see views/webhook.py);
    unit tests here exercise routing logic against unsigned payloads, so the
    verifier is stubbed instead of fetching live JWKS keys from plaid.com.
    Signature behavior itself is covered by WebhookVerificationTest.
    """
    with patch("plaid_integration.views.webhook.verify_plaid_webhook", return_value=True):
        yield
