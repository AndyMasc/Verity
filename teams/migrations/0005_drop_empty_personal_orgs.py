"""Stop minting a personal organisation for every new account.

Personal orgs were scaffolding for the org backfills in records.0039 and
reimbursements.0022. Records and packages now carry a nullable org, so a user
who has not joined or created an organisation simply has none.

Only organisations that look auto-generated AND hold no data are removed, so
this can never cascade into records or packages. Anything holding data, or
named by hand, is left alone.
"""

from django.db import migrations

GENERATED_MARKERS = ("organisation", "organization")


def looks_generated(name: str) -> bool:
    return name.strip().lower().endswith(GENERATED_MARKERS)


def forwards(apps, schema_editor):
    Organization = apps.get_model("organizations", "Organization")
    Record = apps.get_model("records", "Record")
    Package = apps.get_model("reimbursements", "ReimbursementPackage")

    for org in Organization.objects.all().iterator():
        if not looks_generated(org.name):
            continue
        holds_data = (
            Record.objects.filter(organization_id=org.pk).exists()
            or Package.objects.filter(organization_id=org.pk).exists()
        )
        if holds_data:
            continue
        Organization.objects.filter(pk=org.pk).delete()


def backwards(apps, schema_editor):
    """No-op: personal orgs are no longer meaningful, so they are not recreated."""


class Migration(migrations.Migration):
    dependencies = [
        ("teams", "0004_delete_membership"),
        ("records", "0041_alter_record_organization"),
        ("reimbursements", "0023_alter_reimbursementpackage_organization"),
    ]

    operations = [migrations.RunPython(forwards, backwards)]
