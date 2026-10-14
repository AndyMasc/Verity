"""Django models for storing Plaid banking integration data.
Tracks linked bank items, access tokens, sync cursors, and error
state needed to maintain ongoing transaction synchronization."""

import json
from typing import Any

from django.conf import settings
from django.db import models
from fernet_fields import EncryptedCharField, EncryptedTextField


class EncryptedJSONField(EncryptedTextField):
    """A custom field that encrypts JSON data and safely stores it as text."""

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
            except (ValueError, TypeError):
                return value
        return value

    def to_python(self, value: Any) -> Any:
        """Ensure form cleaning and model assignments return Python objects."""
        if value is not None and isinstance(value, str):
            try:
                return json.loads(value)
            except (ValueError, TypeError):
                return value
        return super().to_python(value)


class PlaidItem(models.Model):
    """Represents a connected Plaid bank item (e.g. one bank account)."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="plaid_items"
    )
    item_id = models.CharField(max_length=255, unique=True)
    access_token = EncryptedCharField(max_length=512)
    next_cursor = models.CharField(
        max_length=255,
        blank=True,
        default="",
        db_index=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    last_error_code = models.CharField(max_length=255, blank=True, default="")
    last_error_message = models.TextField(blank=True, default="")
    last_error_at = models.DateTimeField(null=True, blank=True)
    institution_name = models.CharField(max_length=255, blank=True, default="")
    accounts_data = EncryptedJSONField(null=True, blank=True)

    def __str__(self) -> str:
        label = self.institution_name or self.item_id
        return f"{label} ({self.item_id})"
