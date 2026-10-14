from decimal import Decimal

from django.db import migrations, models


def set_defaults(apps, schema_editor):
    Record = apps.get_model("records", "Record")
    Record.objects.filter(balance__isnull=True).update(balance=Decimal("0.00"))
    Record.objects.filter(transaction_date__isnull=True).update(
        transaction_date=models.F("date_added")
    )


class Migration(migrations.Migration):
    dependencies = [
        ("records", "0026_remove_mergelog_idx_mergelog_search_trgm"),
    ]

    operations = [
        migrations.AddField(
            model_name="record",
            name="payment_method",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
        migrations.AddField(
            model_name="record",
            name="payment_method_locked",
            field=models.BooleanField(default=False),
        ),
        migrations.RunPython(set_defaults, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="record",
            name="balance",
            field=models.DecimalField(
                db_index=True, decimal_places=2, default=Decimal("0.00"), max_digits=12
            ),
        ),
        migrations.AlterField(
            model_name="record",
            name="merchant",
            field=models.CharField(default="", max_length=255),
        ),
        migrations.AlterField(
            model_name="record",
            name="transaction_date",
            field=models.DateField(db_index=True),
        ),
    ]
