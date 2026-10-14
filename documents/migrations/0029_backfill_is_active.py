from django.db import migrations


def backfill_is_active(apps, schema_editor):
    DocumentData = apps.get_model("documents", "DocumentData")
    DocumentData.objects.filter(deleted_at__isnull=False, is_active=True).update(is_active=False)


class Migration(migrations.Migration):
    dependencies = [
        ("documents", "0028_add_is_active_to_documentdata"),
    ]

    operations = [
        migrations.RunPython(backfill_is_active, migrations.RunPython.noop),
    ]
