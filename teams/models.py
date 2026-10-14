from django.conf import settings
from django.db import models
from organizations.models import Organization


class Membership(models.Model):
    """One user's standing inside an organisation.

    django-organizations supplies the Organisation itself and an
    is_admin flag; four roles need a real field, so membership lives here
    rather than in its boolean-only OrganizationUser.
    """

    class Role(models.TextChoices):
        OWNER = "owner", "Owner"
        ADMIN = "admin", "Admin"
        FINANCE = "finance", "Finance"
        EMPLOYEE = "employee", "Employee"

    # Ordering is weakest-privilege-first so anything iterating roles hits
    # the least privileged one first.
    CAN_MANAGE_MEMBERS = frozenset({Role.OWNER, Role.ADMIN})
    CAN_APPROVE_EXPENSES = frozenset({Role.OWNER, Role.ADMIN, Role.FINANCE})
    CAN_SET_POLICY = frozenset({Role.OWNER, Role.ADMIN})

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="memberships",
    )
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="memberships",
    )
    role = models.CharField(max_length=16,
                            choices=Role.choices,
                            default=Role.EMPLOYEE)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("user", "organization")
        ordering = ("organization", "user")

    def __str__(self) -> str:
        return f"{self.user_id}@{self.organization_id}:{self.role}"

    def can_manage_members(self) -> bool:
        return self.role in self.CAN_MANAGE_MEMBERS

    def can_approve_expenses(self) -> bool:
        return self.role in self.CAN_APPROVE_EXPENSES

    def can_set_policy(self) -> bool:
        return self.role in self.CAN_SET_POLICY
