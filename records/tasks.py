"""Background tasks for the records module, executed via Dramatiq.

Covers automated record matching on save, archival of expired records,
permanent deletion of records older than seven years, and expiry
notification dispatch (DB, email, and web push).
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import TYPE_CHECKING

import dramatiq
from django.contrib.auth import get_user_model
from django.db.models import F, Q
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from periodiq import cron

if TYPE_CHECKING:
    from django.contrib.auth.models import AbstractUser

from core.models import Notification
from core.services.notifications import (
    build_expiry_email_context,
    build_expiry_webpush_payload,
    build_site_context,
    send_multi_channel_notification,
)
from documents.services.cleanup import COMPLIANCE_RETENTION_YEARS

from .models import Record

logger = logging.getLogger(__name__)

User = get_user_model()


@dramatiq.actor
def run_auto_match(record_pk: int, has_plaid: bool) -> None:
    """Attempt to automatically match a newly saved record with its counterpart.

    When *has_plaid* is True the record is a Plaid transaction and is matched
    against document records; otherwise it is a document record matched against
    Plaid transactions. Failures are logged but never propagated.
    """
    try:
        record = Record.objects.get(pk=record_pk)
    except Record.DoesNotExist:
        logger.warning("Auto-match skipped: record %s not found", record_pk)
        return

    from records.matching import try_match_document_record, try_match_plaid_record

    try:
        if has_plaid:
            matched = try_match_plaid_record(record)
            if matched:
                logger.info(
                    "Auto-matched %d document(s) to plaid record %s",
                    len(matched),
                    record_pk,
                )
        else:
            result = try_match_document_record(record)
            if result:
                logger.info(
                    "Auto-matched document record %s to plaid record %s",
                    record_pk,
                    result.pk,
                )
    except Exception:
        logger.exception("Auto-match failed for record %s", record_pk)


@dramatiq.actor
def archive_expired_records() -> None:
    """Soft-delete all active records whose expiry date has passed.

    Only archives records belonging to users who opted into auto-archiving.
    Records added after expiry are excluded (``expiry_date >= date_added``).
    """
    today = timezone.now().date()
    active_expired_records = Record.objects.filter(
        expiry_date__lt=today,
        expiry_date__gte=F("date_added"),
        is_active=True,
        user__settings__auto_archive_expired_records=True,
    )
    count = active_expired_records.update(is_active=False)
    if count:
        logger.info("Archived %d expired records.", count)


@dramatiq.actor
def delete_7year_archived_records() -> None:
    """Permanently delete archived records older than seven years.

    Respects the user's ``auto_delete_archived_records`` setting. Merged
    document records (still referenced by an active MergeLog) are excluded.
    Associated S3 document objects are also deleted asynchronously.
    """
    from documents.models import DocumentData
    from documents.tasks import delete_document as delete_s3_object

    from .models import MergeLog

    merged_ids = set(
        MergeLog.objects.filter(undone_at__isnull=True).values_list("document_record_id", flat=True)
    )

    seven_years_ago = timezone.now() - timedelta(days=365 * COMPLIANCE_RETENTION_YEARS)
    seven_year_expired_records = Record.objects.filter(
        date_added__lte=seven_years_ago,
        is_active=False,
        user__settings__auto_delete_archived_records=True,
    ).exclude(pk__in=merged_ids)

    document_paths = list(
        DocumentData.objects.filter(associated_record__in=seven_year_expired_records).values_list(
            "filepath", flat=True
        )
    )

    deleted_count = 0
    pks = list(seven_year_expired_records.values_list("pk", flat=True))
    if pks:
        records_qs = Record.objects.filter(pk__in=pks).allow_bulk_delete()
        deleted_count, _ = records_qs.delete()

    if deleted_count:
        logger.info("Hard-deleted %d archived records.", deleted_count)

    for path in document_paths:
        if path:
            delete_s3_object.send(path)


@dramatiq.actor(periodic=cron("0 * * * *"))
def send_expiry_notifications() -> None:
    """Send expiry notifications for records approaching their expiry date.

    Respects each user's ``expiring_notifications_advance_time`` preference
    (1, 3, 7, or 30 days). Creates DB notifications and dispatches email
    and web-push notifications per user. Records already notified are
    skipped via the ``expiry_notification_sent`` flag.
    """
    today = timezone.now().date()

    expiring_records = (
        Record.objects.filter(
            is_active=True,
            expiry_notification_sent=False,
            expiry_date__isnull=False,
            expiry_date__gte=F("date_added"),
        )
        .filter(
            Q(
                user__settings__expiring_notifications_advance_time="1",
                expiry_date__lte=today + timedelta(days=1),
            )
            | Q(
                user__settings__expiring_notifications_advance_time="3",
                expiry_date__lte=today + timedelta(days=3),
            )
            | Q(
                user__settings__expiring_notifications_advance_time="7",
                expiry_date__lte=today + timedelta(days=7),
            )
            | Q(
                user__settings__expiring_notifications_advance_time="30",
                expiry_date__lte=today + timedelta(days=30),
            )
            | Q(user__settings__isnull=True, expiry_date__lte=today + timedelta(days=7))
        )
        .select_related("user__settings")
    )

    notifications_to_create: list = []
    user_records_map: dict[int, list] = {}
    user_object_map: dict[int, AbstractUser] = {}
    user_settings_cache = {}

    for record in expiring_records:
        user = record.user
        notifications_to_create.append(
            Notification(
                recipient=user,
                subject="Expiring Records on Papertrail",
                message=f"Your record '{record.title}' is expiring on {record.expiry_date}.",
            )
        )

        user_records_map.setdefault(user.id, []).append(record)
        if user.id not in user_object_map:
            user_object_map[user.id] = user

        if user.id not in user_settings_cache:
            user_settings_cache[user.id] = getattr(user, "settings", None)

    if not notifications_to_create:
        return

    Notification.objects.bulk_create(notifications_to_create)

    Record.objects.filter(id__in=[r.id for r in user_records_map.values() for r in r]).update(
        expiry_notification_sent=True
    )
    logger.info("Created %d DB notifications.", len(notifications_to_create))

    site_context = build_site_context()
    site_url = site_context["site_url"]

    for user_id, records in user_records_map.items():
        user = user_object_map[user_id]

        webpush_payload = build_expiry_webpush_payload(len(records))

        user_settings = user_settings_cache.get(user_id)
        auto_archive_msg = (
            "Since you have enabled auto-archiving, your records will be automatically archived once the expiry passes."
            if user_settings and getattr(user_settings, "auto_archive_expired_records", False)
            else ""
        )

        action_url = f"{site_url.rstrip('/')}{reverse('core:dashboard')}"
        MAX_DISPLAY_RECORDS = 5
        total_records_count = len(records)

        display_records = records[:MAX_DISPLAY_RECORDS]
        remaining_count = max(0, total_records_count - MAX_DISPLAY_RECORDS)

        context = build_expiry_email_context(
            user=user,
            records=display_records,
            remaining_count=remaining_count,
            total_records_count=total_records_count,
            auto_archive_msg=auto_archive_msg,
            action_url=action_url,
        )

        send_multi_channel_notification(
            user=user,
            subject="Expiring Records on Papertrail",
            text_body=render_to_string("notifications/expiring_record_email.txt", context),
            html_body=render_to_string("notifications/expiring_record_email.html", context),
            webpush_payload=webpush_payload,
            send_db=True,
            db_message=f"Your record '{', '.join(r.title for r in records[:3])}' is expiring soon.",
        )

    logger.info("Successfully scheduled notices for %d unique users.", len(user_records_map))
