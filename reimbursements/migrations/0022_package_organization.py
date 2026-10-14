"""Scope reimbursement packages to the creator's organisation.

Same three-step shape as records.0039: add nullable, backfill from the
creator's membership, enforce.
"""

from django.db import migrations, models


def backfill(apps, schema_editor):
    Membership = apps.get_model("teams", "Membership")
    Package = apps.get_model("reimbursements", "ReimbursementPackage")

    creator_org = {}
    for m in Membership.objects.select_related("user").iterator():
        creator_org.setdefault(m.user_id, m.organization_id)

    for pkg in Package.objects.filter(organization__isnull=True).iterator():
        org = creator_org.get(pkg.creator_id)
        if org is None:
            continue
        Package.objects.filter(pk=pkg.pk).update(organization_id=org)


class Migration(migrations.Migration):

    dependencies = [
        ("reimbursements",
         "0021_alter_packagepayment_stripe_checkout_session_id"),
        ("teams", "0002_personal_orgs"),
    ]

    operations = [
        migrations.AddField(
            model_name="reimbursementpackage",
            name="organization",
            field=models.ForeignKey(
                null=True,
                on_delete=models.CASCADE,
                related_name="packages",
                to="organizations.organization",
            ),
        ),
        migrations.RunPython(backfill, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="reimbursementpackage",
            name="organization",
            field=models.ForeignKey(
                on_delete=models.CASCADE,
                related_name="packages",
                to="organizations.organization",
            ),
        ),
    ]
