"""Drop the four-role Membership table.

Data was moved to django-organizations' OrganizationUser in 0003, which also
promoted prior owners to OrganizationOwner rows.
"""

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("teams", "0003_membership_to_orguser"),
        ("records", "0041_alter_record_organization"),
        ("reimbursements", "0023_alter_reimbursementpackage_organization"),
    ]

    operations = [migrations.DeleteModel(name="Membership")]
