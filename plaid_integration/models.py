"""Django models for storing Plaid banking integration data.

Tracks linked bank items, access tokens, sync cursors, and error
state needed to maintain ongoing transaction synchronization.
"""

import json
from typing import Any

from django.conf import settings
from django.db import models
from fernet_fields import EncryptedCharField, EncryptedTextField


class EncryptedJSONField(EncryptedTextField):
    """A custom field that encrypts JSON data and safely stores it as text.

    Overrides get_prep_value to serialize dict/list → JSON string before
    Fernet encryption, and from_db_value / to_python to deserialize the
    decrypted string back into Python objects.

    NOTE: get_prep_value intentionally skips super() to avoid Django 6's
    TextField.get_prep_value calling self.to_python(), which would
    json.loads the JSON string back into a Python object before encryption,
    resulting in "str(list)" (single-quoted Python repr) being stored
    instead of valid JSON.
    """

    def get_prep_value(self, value: Any) -> Any:
        """Serialize dict/list into a JSON string before Fernet encryption."""
        if value is None:
            return None
        if isinstance(value, str):
            return value
        return json.dumps(value)

    def from_db_value(self, value: Any, expression: Any, connection: Any) -> Any:
        """Deserialize decrypted text string back into Python dict/list."""
        value = super().from_db_value(value, expression, connection)
        if value is not None and isinstance(value, str):
            try:
                return json.loads(value)
            except ValueError, TypeError:
                return value
        return value

    def to_python(self, value: Any) -> Any:
        """Ensure form cleaning and model assignments return Python objects."""
        if value is not None and isinstance(value, str):
            try:
                return json.loads(value)
            except ValueError, TypeError:
                return value
        return super().to_python(value)


class PlaidItem(models.Model):
    """Represents a connected Plaid bank item (e.g. one bank account).

    Stores the encrypted access token and sync cursor needed to fetch transactions
    incrementally via the Plaid Transactions Sync endpoint. Also tracks
    institution metadata and error state for user-facing diagnostics.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="plaid_items"
    )
    item_id = models.CharField(max_length=255, unique=True)
    access_token = EncryptedCharField(max_length=512)
    next_cursor = models.CharField(
        max_length=255,
        null=True,
        blank=True,
        db_index=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    last_error_code = models.CharField(max_length=255, null=True, blank=True)
    last_error_message = models.TextField(null=True, blank=True)
    last_error_at = models.DateTimeField(null=True, blank=True)
    institution_name = models.CharField(max_length=255, null=True, blank=True)
    accounts_data = EncryptedJSONField(null=True, blank=True)

    def __str__(self) -> str:
        label = self.institution_name or self.item_id
        return f"{label} ({self.item_id})"
