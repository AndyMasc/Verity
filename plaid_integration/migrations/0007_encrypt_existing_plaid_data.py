"""Encrypt existing plaintext Plaid access_token and accounts_data values.

Runs before migration 0008 (which changes the field types to encrypted).
At this point the fields are still plain CharField/TextField, so we manually
encrypt the raw values using the same Fernet key derivation that
fernet_fields will use after migration 0008.
"""

import json

from cryptography.fernet import Fernet
from django.conf import settings
from django.db import migrations


def _get_fernet():
    """Build a Fernet instance using the same key derivation as fernet_fields."""
    from fernet_fields import hkdf

    keys = getattr(settings, "FERNET_KEYS", None) or [settings.SECRET_KEY]
    if getattr(settings, "FERNET_USE_HKDF", True):
        keys = [hkdf.derive_fernet_key(k) for k in keys]
    if len(keys) == 1:
        return Fernet(keys[0])
    from cryptography.fernet import MultiFernet

    return MultiFernet([Fernet(k) for k in keys])


def encrypt_plaid_data(apps, schema_editor):
    PlaidItem = apps.get_model("plaid_integration", "PlaidItem")
    fernet = _get_fernet()
    connection = schema_editor.connection
    table = PlaidItem._meta.db_table

    # Fetch all rows with raw IDs to avoid ORM deserialization issues
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT id, access_token, accounts_data FROM {table}")
        rows = cursor.fetchall()

    for pk, access_token, accounts_data in rows:
        token_updates = []
        params = []

        # Encrypt plaintext access_token (skip if already encrypted)
        if (
            access_token
            and not access_token.startswith(b"gAAAAAB")
            and not str(access_token).startswith("gAAAAAB")
        ):
            token_bytes = (
                access_token if isinstance(access_token, bytes) else access_token.encode("utf-8")
            )
            encrypted_token = fernet.encrypt(token_bytes)
            token_updates.append("access_token = %s")
            params.append(encrypted_token)

        # Encrypt plaintext accounts_data (skip if already encrypted)
        if accounts_data:
            acct_str = (
                accounts_data if isinstance(accounts_data, str) else accounts_data.decode("utf-8")
            )
            if not acct_str.startswith("gAAAAAB"):
                # accounts_data was stored as a JSON string or a Python repr string
                # Ensure it's valid JSON before encrypting
                try:
                    parsed = json.loads(acct_str)
                    json_str = json.dumps(parsed)
                except (json.JSONDecodeError, TypeError):
                    json_str = acct_str
                encrypted_accts = fernet.encrypt(json_str.encode("utf-8"))
                token_updates.append("accounts_data = %s")
                params.append(encrypted_accts)

        if token_updates:
            params.append(pk)
            with connection.cursor() as cursor:
                cursor.execute(
                    f"UPDATE {table} SET {', '.join(token_updates)} WHERE id = %s",
                    params,
                )


def reverse_encrypt(apps, schema_editor):
    """Reverse migration: decrypt encrypted values back to plaintext."""
    PlaidItem = apps.get_model("plaid_integration", "PlaidItem")
    fernet = _get_fernet()
    connection = schema_editor.connection
    table = PlaidItem._meta.db_table

    with connection.cursor() as cursor:
        cursor.execute(f"SELECT id, access_token, accounts_data FROM {table}")
        rows = cursor.fetchall()

    for pk, access_token, accounts_data in rows:
        updates = []
        params = []

        if access_token:
            token_bytes = (
                access_token if isinstance(access_token, bytes) else access_token.encode("utf-8")
            )
            if token_bytes.startswith(b"gAAAAAB"):
                decrypted = fernet.decrypt(token_bytes)
                updates.append("access_token = %s")
                params.append(decrypted.decode("utf-8"))

        if accounts_data:
            acct_bytes = (
                accounts_data if isinstance(accounts_data, bytes) else accounts_data.encode("utf-8")
            )
            if acct_bytes.startswith(b"gAAAAAB"):
                decrypted = fernet.decrypt(acct_bytes)
                updates.append("accounts_data = %s")
                params.append(decrypted.decode("utf-8"))

        if updates:
            params.append(pk)
            with connection.cursor() as cursor:
                cursor.execute(
                    f"UPDATE {table} SET {', '.join(updates)} WHERE id = %s",
                    params,
                )


class Migration(migrations.Migration):
    dependencies = [
        (
            "plaid_integration",
            "0006_rename_account_name_plaiditem_institution_name_and_more",
        ),
    ]

    operations = [
        migrations.RunPython(encrypt_plaid_data, reverse_encrypt),
    ]
