"""Record sharing services.

All share grants/revocations funnel through this module. Business rules:

* Only the record owner may share or revoke (no share-chain escalation).
* Shares are idempotent at the DB layer (unique constraint) and here.
* Grants are purpose- and permission-scoped: "permission=edit" lets the
  recipient view and edit (edits are attributed via simple_history's
  "history_user", set by HistoryRequestMiddleware in prod); "view" is
  read-only.
* "include_documents" controls whether attached documents follow the
  grant; "expires_at" ends access at a time; revoking sets "revoked_at"
  instead of deleting the row so the grant survives in the audit trail.
* Every grant/revoke is appended to "AuditLog" (record, actor).
"""

from __future__ import annotations

import logging

from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

from Verity.views import create_audit_log

from .models import AuditLog, Record, RecordShare

User = get_user_model()
logger = logging.getLogger(__name__)


class ShareError(Exception):
    """Base class for share validation failures."""


class NotOwnerError(ShareError):
    """Only the record owner can share/revoke."""


class SelfShareError(ShareError):
    pass


def can_share(user, record: Record) -> bool:
    """Only the record owner may initiate sharing."""
    return record.user_id == user.pk


def grant_access(
    *,
    record: Record,
    user,
    requester,
    permission: str = RecordShare.Permission.EDIT,
    purpose: str = "",
    include_documents: bool = True,
    expires_at=None,
) -> tuple[RecordShare, bool]:
    """Grant (or reactivate) a purpose- and permission-scoped share.

    Returns "(share, created)". Reactivates an existing grant that was
    revoked or expired instead of raising on the unique constraint, so
    re-granting (e.g. a refunded reimbursement) works without new rows.
    Raises "NotOwnerError" unless "requester" owns the record and "SelfShareError"
    if "user" is the owner.
    """
    if not can_share(requester, record):
        raise NotOwnerError("Only the record owner can share it")
    if user.pk == record.user_id:
        raise SelfShareError("You cannot share a record with yourself")

    defaults = {
        "permission": permission,
        "purpose": purpose,
        "include_documents": include_documents,
        "expires_at": expires_at,
        "shared_by": requester,
    }
    with transaction.atomic():
        share, created = RecordShare.objects.get_or_create(
            record=record, user=user, defaults=defaults
        )
        if created:
            create_audit_log(
                user=requester,
                action=AuditLog.Action.SHARE,
                record=record,
                details={
                    "user": user.email,
                    "user_id": user.pk,
                    "permission": permission,
                    "purpose": purpose,
                    "include_documents": include_documents,
                },
            )
            return share, created

        if share.is_active:
            return share, False

        # Reactivate a revoked/expired grant with the latest settings.
        share.permission = permission
        share.purpose = purpose
        share.include_documents = include_documents
        share.expires_at = expires_at
        share.revoked_at = None
        share.shared_by = requester
        share.save(
            update_fields=[
                "permission",
                "purpose",
                "include_documents",
                "expires_at",
                "revoked_at",
                "shared_by",
            ]
        )
        create_audit_log(
            user=requester,
            action=AuditLog.Action.SHARE,
            record=record,
            details={
                "user": user.email,
                "user_id": user.pk,
                "permission": permission,
                "purpose": purpose,
                "include_documents": include_documents,
                "reactivated": True,
            },
        )
        return share, True


def resolve_recipients(emails: list[str]) -> tuple[list[User], list[str]]:
    """Resolve recipient accounts from a list of emails using a single query.

    Returns "(recipients, unknown_emails)". Case-insensitive matching mirrors
    the per-email ``email__iexact`` lookup but avoids one query per address.
    """
    from django.db.models import Q

    email_set = {e.strip().lower() for e in emails if e.strip()}
    if not email_set:
        return [], []

    email_filter = Q()
    for email in email_set:
        email_filter |= Q(email__iexact=email)

    users_by_email = {u.email.lower(): u for u in User.objects.filter(email_filter)}

    recipients: list[User] = []
    unknown: list[str] = []
    for email in email_set:
        user = users_by_email.get(email)
        if user is None:
            unknown.append(email)
        else:
            recipients.append(user)
    return recipients, unknown


def share_record_with_users(
    *,
    record: Record,
    owner,
    emails: list[str],
    permission: str = RecordShare.Permission.EDIT,
    purpose: str = "",
    include_documents: bool = True,
    recipients: list[User] | None = None,
) -> tuple[list[RecordShare], list[str]]:
    """Share "record" with every existing account matching "emails".

    Returns "(shares, unknown_emails)" listing only "newly granted"
    recipient shares (idempotent: existing active grants are skipped without
    re-notification). Raises "NotOwnerError" when "owner" is not the record
    owner and "SelfShareError" when the owner appears in the recipient list.
    Emails without an account are returned (never silently dropped, never
    silently shared).

    "recipients" may be passed in to reuse a batch resolution across
    multiple records (see BulkShareView) and avoid re-querying per record.
    """
    if recipients is None:
        recipients, unknown = resolve_recipients(emails)
    else:
        unknown = []

    for user in recipients:
        if user.pk == record.user_id:
            raise SelfShareError("You cannot share a record with yourself")

    shares: list[RecordShare] = []
    if not recipients:
        return shares, unknown

    for user in recipients:
        share, created = grant_access(
            record=record,
            user=user,
            requester=owner,
            permission=permission,
            purpose=purpose,
            include_documents=include_documents,
        )
        if created:
            shares.append(share)

    for share in shares:
        _notify_share_recipient(record=record, share=share, actor=owner)

    return shares, unknown


def _notify_share_recipient(*, record: Record, share: RecordShare, actor) -> None:
    """Best-effort notification to the shared-with recipient.

    Runs after the share row is committed and can never fail the grant:
    deliverability issues are logged, not raised. Duplicate shares do not
    re-notify (only freshly created rows reach this point).
    """
    try:
        from .notifications import send_record_shared_notification

        send_record_shared_notification(record=record, share=share, actor=actor)
    except Exception:
        logger.exception(
            "Share notification delivery failed (record=%s, recipient=%s)",
            record.pk,
            share.user_id,
        )


def revoke_share(*, record: Record, actor, share: RecordShare) -> None:
    """Revoke a record share; only the owner may revoke.

    The row is kept with "revoked_at" set so the audit trail shows access
    was granted and later removed.
    """
    if not can_share(actor, record):
        raise NotOwnerError
    if share.revoked_at is None:
        share.revoked_at = timezone.now()
        share.save(update_fields=["revoked_at"])
        create_audit_log(
            user=actor,
            action=AuditLog.Action.REVOKE_SHARE,
            record=record,
            details={"user": share.user.email, "user_id": share.user_id},
        )


def shares_for_viewer(*, record: Record, viewer) -> list[RecordShare]:
    """Shares visible to the "viewer": the full list for the owner, none others."""
    if record.user_id == viewer.pk:
        return list(record.shares.select_related("user", "shared_by").all())
    return []
