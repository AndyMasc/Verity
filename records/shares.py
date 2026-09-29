"""Record sharing services.

Every grant and revocation funnels through this module:

* Only the record owner may share or revoke.
* A grant is idempotent. Re-granting an already-active share changes nothing
  and does not re-notify; re-granting a revoked or expired one reactivates the
  same row (this is what a refunded-then-repaid reimbursement needs).
* Grants are scoped by permission, purpose, "include_documents" and
  "expires_at". Revoking stamps "revoked_at" rather than deleting, so the
  grant survives in the audit trail.
* Every grant and revoke is appended to AuditLog.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from Verity.views import create_audit_log

from .models import AuditLog, Record, RecordShare

User = get_user_model()
logger = logging.getLogger(__name__)


class ShareError(Exception):
    """Base class for share validation failures."""


class NotOwnerError(ShareError):
    """Only the record owner can share or revoke."""


class SelfShareError(ShareError):
    """The owner cannot share a record with themselves."""


@dataclass(frozen=True)
class ShareConfig:
    """The settings a grant is made under."""

    permission: str = RecordShare.Permission.EDIT
    purpose: str = ""
    include_documents: bool = True
    expires_at: datetime | None = None


def can_share(user, record: Record) -> bool:
    """Only the record owner may initiate sharing."""
    return record.user_id == user.pk


def grant_access(
    *,
    record: Record,
    user,
    requester,
    config: ShareConfig | None = None,
) -> tuple[RecordShare, bool]:
    """Grant (or reactivate) a permission-scoped share.

    Returns "(share, granted_now)" where granted_now is True for a new or
    reactivated grant and False when an active share was already in place —
    i.e. exactly the cases worth notifying about.
    """
    config = config or ShareConfig()

    if not can_share(requester, record):
        raise NotOwnerError("Only the record owner can share it")
    if user.pk == record.user_id:
        raise SelfShareError("You cannot share a record with yourself")

    with transaction.atomic():
        share, created = RecordShare.objects.get_or_create(
            record=record,
            user=user,
            defaults={
                "permission": config.permission,
                "purpose": config.purpose,
                "include_documents": config.include_documents,
                "expires_at": config.expires_at,
                "shared_by": requester,
            },
        )
        if created:
            reactivated = False
        elif share.is_active:
            return share, False
        else:
            reactivated = True
            share.permission = config.permission
            share.purpose = config.purpose
            share.include_documents = config.include_documents
            share.expires_at = config.expires_at
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

        details: dict[str, Any] = {
            "user": user.email,
            "user_id": user.pk,
            "permission": config.permission,
            "purpose": config.purpose,
            "include_documents": config.include_documents,
        }
        if reactivated:
            details["reactivated"] = True
        create_audit_log(
            user=requester,
            action=AuditLog.Action.SHARE,
            record=record,
            details=details,
        )
        return share, True


def resolve_recipients(emails: list[str]) -> tuple[list[User], list[str]]:
    """Match emails to accounts in one query.

    Returns "(recipients, unknown_emails)"; unknown emails are reported back
    to the user rather than silently dropped.
    """
    email_set = {e.strip().lower() for e in emails if e.strip()}
    if not email_set:
        return [], []

    email_filter = Q()
    for email in email_set:
        email_filter |= Q(email__iexact=email)

    users_by_email = {u.email.lower(): u for u in User.objects.filter(email_filter)}

    recipients = [users_by_email[e] for e in email_set if e in users_by_email]
    unknown = [e for e in email_set if e not in users_by_email]
    return recipients, unknown


def grant_shares(
    *,
    record: Record,
    owner,
    recipients: list[User],
    config: ShareConfig | None = None,
) -> list[RecordShare]:
    """Grant "record" to each recipient; return only the newly granted shares.

    The owner is skipped rather than rejected, so one self-addressed entry in
    a bulk list cannot stop the other recipients from being shared with.
    """
    granted: list[RecordShare] = []
    for user in recipients:
        if user.pk == record.user_id:
            continue
        share, granted_now = grant_access(
            record=record, user=user, requester=owner, config=config
        )
        if granted_now:
            granted.append(share)
            _notify_share_recipient(record=record, share=share, actor=owner)
    return granted


def _notify_share_recipient(*, record: Record, share: RecordShare, actor) -> None:
    """Best-effort notification to the recipient.

    Runs after the share row is committed and can never fail the grant:
    deliverability issues are logged, not raised. Only newly granted shares
    reach this point, so duplicates never re-notify.
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
        raise NotOwnerError("Only the record owner can revoke access")
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
