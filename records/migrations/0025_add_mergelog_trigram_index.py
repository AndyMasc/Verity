from django.contrib.postgres.indexes import GinIndex, OpClass
from django.db import connection, migrations
from django.db.models import F


def create_index(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    concurrent = ""
    if not connection.in_atomic_block:
        concurrent = "CONCURRENTLY "
    schema_editor.execute(
        f"CREATE INDEX {concurrent}IF NOT EXISTS idx_mergelog_search_trgm "
        f"ON records_mergelog USING gin (search_text gin_trgm_ops)"
    )


def drop_index(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    schema_editor.execute("DROP INDEX IF EXISTS idx_mergelog_search_trgm")


class Migration(migrations.Migration):
    """Add GIN trigram index for full-text search on MergeLog."""

    atomic = False

    dependencies = [
        ("records", "0024_add_mergelog_search_text"),
    ]

    operations = [
        migrations.RunPython(create_index, reverse_code=drop_index),
    ]
