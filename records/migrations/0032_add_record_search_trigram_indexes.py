from django.contrib.postgres.indexes import GinIndex
from django.db import connection, migrations

_FIELDS = ("title", "merchant", "products", "notes")
_DROP_TEMPLATE = "DROP INDEX IF EXISTS {name};"


def _index_sql(field: str) -> str:
    """Return CREATE INDEX SQL, omitting CONCURRENTLY when inside an atomic block."""
    name = f"idx_record_{field}_trgm"
    concurrent = ""
    if not connection.in_atomic_block:
        concurrent = "CONCURRENTLY "
    return (
        f"CREATE INDEX {concurrent}IF NOT EXISTS {name} "
        f"ON records_record USING gin ({field} gin_trgm_ops)"
    )


def create_indexes(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    for field in _FIELDS:
        schema_editor.execute(_index_sql(field))


def drop_indexes(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    for field in _FIELDS:
        name = f"idx_record_{field}_trgm"
        schema_editor.execute(_DROP_TEMPLATE.format(name=name))


class Migration(migrations.Migration):
    """Add GIN trigram indexes for fast ILIKE search on Record text fields."""

    atomic = False

    dependencies = [
        ("records", "0031_alter_historicalrecord_balance_and_more"),
    ]

    operations = [
        migrations.RunPython(create_indexes, reverse_code=drop_indexes),
    ]
