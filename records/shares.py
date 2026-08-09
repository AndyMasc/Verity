"""Record sharing services.

All share grants/revocations funnel through this module. Business rules:

* Only the record owner may share or revoke (no share-chain escalation).
* Shares are idempotent at the DB layer (unique constraint) and here.
* A share grants the recipient the full view/edit experience; edits are
  attributed via simple_history's ``history_user`` (prod: set by
  HistoryRequestMiddleware).
* Every grant/revoke is appended to ``AuditLog`` (record, actor).
"""

from __future__ import annotations

import logging

from django.contrib.auth import get_user_model
from django.db import transaction

from Papertrail.views import create_audit_log

from .models import AuditLog, Record, RecordShare

User = get_user_model()
logger = logging.getLogger(__name__)


class ShareError(Exception):
    """Base class for share validation failures."""


class NotOwner(ShareError):
    """Only the record owner can share/revoke."""


class SelfShare(ShareError):
    pass


def can_share(user, record: Record) -> bool:
    """Only the record owner may initiate sharing."""
    return record.user_id == user.pk


def share_record_with_users(
    *, record: Record, owner, emails: list[str]
) -> tuple[list[RecordShare], list[str]]:
    """Share *record* with every existing account matching *emails*.

    Returns ``(shares, unknown_emails)``. Raises ``NotOwner`` when *owner*
    is not the record owner and ``SelfShare`` when the owner appears in the
    recipient list. Emails without an account are returned (never silently
    dropped, never silently shared).
    """
    if not can_share(owner, record):
        raise NotOwner("Only the record owner can share it")

    recipients: list[User] = []
    unknown: list[str] = []
    for email in {e.strip().lower() for e in emails if e.strip()}:
        user = User.objects.filter(email__iexact=email).first()
        if user is None:
            unknown.append(email)
        elif user.pk == record.user_id:
            raise SelfShare("You cannot share a record with yourself")
        else:
            recipients.append(user)

    if not recipients:
        return [], unknown

    with transaction.atomic():
        shares = []
        for user in recipients:
            share, created = RecordShare.objects.get_or_create(
                record=record, user=user, defaults={"shared_by": owner}
            )
            if not created:
                continue
            shares.append(share)
            create_audit_log(
                user=owner,
                action=AuditLog.Action.SHARE,
                record=record,
                details={"user": user.email, "user_id": user.pk},
            )

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
    """Revoke a record share; only the owner may revoke."""
    if not can_share(actor, record):
        raise NotOwner
    share.delete()
    create_audit_log(
        user=actor,
        action=AuditLog.Action.REVOKE_SHARE,
        record=record,
        details={"user": share.user.email, "user_id": share.user_id},
    )


def shares_for_viewer(*, record: Record, viewer) -> list[RecordShare]:
    """Shares visible to the *viewer*: the full list for the owner, none others."""
    if record.user_id == viewer.pk:
        return list(record.shares.select_related("user", "shared_by").all())
    return []
