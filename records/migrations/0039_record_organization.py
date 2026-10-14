"""Scope records to an organisation, backfilled from the owner's personal org.

Three steps so no intermediate state has a NOT NULL column with null data:
add nullable, backfill, then enforce. Every existing user has exactly one
membership from teams.0002, so the backfill is total; if a user somehow has
none the row is left null and the alter below will refuse, which is correct.
"""

from django.db import migrations, models


def backfill(apps, schema_editor):
    Membership = apps.get_model("teams", "Membership")
    Record = apps.get_model("records", "Record")

    owner_org = {}
    for m in Membership.objects.select_related("user").iterator():
        owner_org.setdefault(m.user_id, m.organization_id)

    for record in Record.objects.filter(organization__isnull=True).iterator():
        org = owner_org.get(record.user_id)
        if org is None:
            continue
        Record.objects.filter(pk=record.pk).update(organization_id=org)


class Migration(migrations.Migration):

    dependencies = [
        ("records", "0038_alter_historicalrecord_currency_and_more"),
        ("teams", "0002_personal_orgs"),
    ]

    operations = [
        migrations.AddField(
            model_name="record",
            name="organization",
            field=models.ForeignKey(
                null=True,
                on_delete=models.CASCADE,
                related_name="records",
                to="organizations.organization",
            ),
        ),
        migrations.RunPython(backfill, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="record",
            name="organization",
            field=models.ForeignKey(
                on_delete=models.CASCADE,
                related_name="records",
                to="organizations.organization",
            ),
        ),
    ]
