"""Tests for record sharing: access matrix, attribution, gating, audit."""

from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from documents.models import DocumentData
from billing.tests.helpers import give_pro_subscription
from records.models import AuditLog, Record, RecordShare
from records import shares as share_services
from records.notifications import send_record_shared_notification

User = get_user_model()


@override_settings(
    CACHES={"default": {"BACKEND": "django.core.cache.backends.dummy.DummyCache"}},
    SESSION_ENGINE="django.contrib.sessions.backends.db",
)
class SharingTestCase(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            username="owner", email="owner@acme.com", password="pass"
        )
        self.recipient = User.objects.create_user(
            username="sarah", email="sarah@acme.com", password="pass"
        )
        self.stranger = User.objects.create_user(
            username="zoe", email="zoe@acme.com", password="pass"
        )
        self.record = Record.objects.create(
            user=self.owner,
            title="Acme invoice",
            merchant="Office Depot",
            balance="87.40",
            record_type=Record.RecordTypes.EXPENSE_RECEIPT,
            currency="usd",
        )

    def _share(self, emails):
        return share_services.share_record_with_users(
            record=self.record, owner=self.owner, emails=emails
        )

    def _detail_post(self, user, data):
        self.client.force_login(user)
        return self.client.post(reverse("records:record_detail", args=[self.record.pk]), data)


class TestQuerysetScoping(SharingTestCase):
    def test_visible_to_includes_own_and_shared(self):
        self._share([self.recipient.email])
        assert self.record.pk in Record.objects.visible_to(self.recipient).values_list(
            "pk", flat=True
        )
        assert self.record.pk in Record.objects.visible_to(self.owner).values_list("pk", flat=True)
        assert self.record.pk not in Record.objects.visible_to(self.stranger).values_list(
            "pk", flat=True
        )

    def test_shared_with_me_excludes_own(self):
        self._share([self.recipient.email])
        assert list(Record.objects.shared_with_me(self.recipient)) == [self.record]
        assert list(Record.objects.shared_with_me(self.owner)) == []

    def test_visible_to_no_duplicates(self):
        self._share([self.recipient.email])
        assert Record.objects.visible_to(self.owner).filter(pk=self.record.pk).count() == 1


class TestShareService(SharingTestCase):
    def test_share_creates_row_and_audit(self):
        shares, unknown = self._share([self.recipient.email])
        assert len(shares) == 1 and unknown == []
        share = shares[0]
        assert share.user == self.recipient
        assert share.shared_by == self.owner
        audit = AuditLog.objects.filter(record=self.record, action=AuditLog.Action.SHARE).first()
        assert audit is not None and audit.user == self.owner
        assert audit.details == {
            "user": self.recipient.email,
            "user_id": self.recipient.pk,
            "permission": RecordShare.Permission.EDIT,
            "purpose": "",
            "include_documents": True,
        }

    def test_share_idempotent(self):
        self._share([self.recipient.email])
        second, _ = self._share([self.recipient.email])
        assert second == []
        assert RecordShare.objects.filter(record=self.record, user=self.recipient).count() == 1

    def test_self_share_rejected(self):
        with self.assertRaises(share_services.SelfShare):
            self._share([self.owner.email])

    def test_unknown_emails_returned_not_shared(self):
        shared, unknown = self._share([self.recipient.email, "ghost@acme.com"])
        assert unknown == ["ghost@acme.com"]
        assert len(shared) == 1
        assert not RecordShare.objects.filter(
            record=self.record, user__email="ghost@acme.com"
        ).exists()

    def test_non_owner_cannot_share(self):
        self._share([self.stranger.email])
        with self.assertRaises(share_services.NotOwner):
            share_services.share_record_with_users(
                record=self.record, owner=self.stranger, emails=[self.recipient.email]
            )

    def test_revoke_removes_access_and_audits(self):
        shares, _ = self._share([self.recipient.email])
        share = shares[0]
        share_services.revoke_share(record=self.record, actor=self.owner, share=share)
        share.refresh_from_db()
        assert share.revoked_at is not None  # row kept for the audit trail
        assert not share.is_active
        assert self.record.pk not in Record.objects.visible_to(self.recipient).values_list(
            "pk", flat=True
        )
        audit = AuditLog.objects.filter(
            record=self.record, action=AuditLog.Action.REVOKE_SHARE
        ).first()
        assert audit is not None and audit.user == self.owner

    def test_share_idempotent_across_revocation(self):
        shares, _ = self._share([self.recipient.email])
        share_services.revoke_share(record=self.record, actor=self.owner, share=shares[0])
        renewed, _ = self._share([self.recipient.email])
        assert len(renewed) == 1  # re-grant reactivates the row
        share = RecordShare.objects.get(record=self.record, user=self.recipient)
        assert share.revoked_at is None
        assert self.record.pk in Record.objects.visible_to(self.recipient).values_list(
            "pk", flat=True
        )

    def test_view_only_share_cannot_edit(self):
        from records import shares as svc

        svc.grant_access(
            record=self.record,
            user=self.recipient,
            requester=self.owner,
            permission=RecordShare.Permission.VIEW,
        )
        response = self._detail_post(
            self.recipient,
            {
                "title": "Blocked edit",
                "merchant": "Office Depot",
                "balance": "10.00",
                "record_type": Record.RecordTypes.EXPENSE_RECEIPT,
                "currency": "usd",
                "transaction_date": "2026-01-15",
                "notes": "Business lunch",
                "payment_method": "Visa",
            },
        )
        assert response.status_code == 403
        self.record.refresh_from_db()
        assert self.record.title != "Blocked edit"

    def test_expired_share_loses_access(self):
        from django.utils import timezone

        from_share = share_services.grant_access(
            record=self.record,
            user=self.recipient,
            requester=self.owner,
            permission=RecordShare.Permission.VIEW,
            expires_at=timezone.now() - timezone.timedelta(hours=1),
        )[0]
        assert not from_share.is_active
        assert self.record.pk not in Record.objects.visible_to(self.recipient).values_list(
            "pk", flat=True
        )
        # The row remains for audit purposes.
        assert RecordShare.objects.filter(pk=from_share.pk).exists()


class TestRecordDetailAccess(SharingTestCase):
    def test_recipient_sees_shared_detail(self):
        self._share([self.recipient.email])
        self.client.force_login(self.recipient)
        response = self.client.get(reverse("records:record_detail", args=[self.record.pk]))
        assert response.status_code == 200
        assert b"Acme invoice" in response.content

    def test_stranger_gets_404(self):
        self.client.force_login(self.stranger)
        response = self.client.get(reverse("records:record_detail", args=[self.record.pk]))
        assert response.status_code == 404

    def test_history_visible_to_recipient(self):
        self._share([self.recipient.email])
        self.client.force_login(self.recipient)
        response = self.client.get(reverse("records:record_history", args=[self.record.pk]))
        assert response.status_code == 200

    def test_history_404_for_stranger(self):
        self.client.force_login(self.stranger)
        response = self.client.get(reverse("records:record_history", args=[self.record.pk]))
        assert response.status_code == 404


class TestEditAttribution(SharingTestCase):
    """The core promise: shared edits are attributed in the audit trail."""

    def test_recipient_edit_attributed_to_recipient(self):
        self._share([self.recipient.email])
        response = self._detail_post(
            self.recipient,
            {
                "title": "Edited by Sarah",
                "merchant": "Office Depot",
                "balance": "89.50",
                "record_type": Record.RecordTypes.EXPENSE_RECEIPT,
                "currency": "usd",
                "transaction_date": "2026-01-15",
                "notes": "Business lunch",
                "payment_method": "Visa",
            },
        )
        assert response.status_code in (200, 302)
        self.record.refresh_from_db()
        assert self.record.title == "Edited by Sarah"
        assert self.record.balance == 89.50

        latest = self.record.history.first()
        assert latest.history_type == "~"
        assert latest.history_user_id == self.recipient.pk
        assert "Edited by Sarah" in latest.title

    def test_owner_edit_attributed_to_owner(self):
        response = self._detail_post(
            self.owner,
            {
                "title": "Owner rename",
                "merchant": "Office Depot",
                "balance": "10.00",
                "record_type": Record.RecordTypes.EXPENSE_RECEIPT,
                "currency": "usd",
                "transaction_date": "2026-01-15",
                "notes": "Business lunch",
                "payment_method": "Visa",
            },
        )
        assert response.status_code in (200, 302)
        self.record.refresh_from_db()
        assert self.record.title == "Owner rename"
        assert self.record.history.first().history_user_id == self.owner.pk


class TestShareViews(SharingTestCase):
    def test_free_user_cannot_grant(self):
        self.client.force_login(self.owner)
        response = self.client.post(
            reverse("records:record_share", args=[self.record.pk]),
            {"emails": self.recipient.email},
        )
        assert response.status_code == 302
        assert not RecordShare.objects.filter(record=self.record).exists()

    def test_pro_user_can_grant(self):
        give_pro_subscription(self.owner)
        self.client.force_login(self.owner)
        response = self.client.post(
            reverse("records:record_share", args=[self.record.pk]),
            {"emails": self.recipient.email},
        )
        assert response.status_code == 302
        assert RecordShare.objects.filter(record=self.record, user=self.recipient).exists()

    def test_revoke_owner_only(self):
        give_pro_subscription(self.owner)
        shares, _ = self._share([self.recipient.email])
        share = shares[0]
        self.client.force_login(self.stranger)
        response = self.client.post(
            reverse("records:record_share_revoke", args=[self.record.pk, share.pk])
        )
        assert response.status_code == 404  # record invisible to stranger
        assert RecordShare.objects.filter(pk=share.pk).exists()  # untouched

    def test_panel_shows_sharees_to_owner(self):
        self._share([self.recipient.email])
        self.client.force_login(self.owner)
        response = self.client.get(reverse("records:record_shares_panel", args=[self.record.pk]))
        assert response.status_code == 200
        assert self.recipient.email.encode() in response.content


class TestShareNotifications(SharingTestCase):
    """The share service must notify only for new grants, never for duplicates,
    and never let notification failures affect the grant itself."""

    @mock.patch("records.notifications.send_record_shared_notification")
    def test_share_notifies_recipient_once(self, mock_notify):
        shares, _ = self._share([self.recipient.email])
        mock_notify.assert_called_once_with(record=self.record, share=shares[0], actor=self.owner)

    @mock.patch("records.notifications.send_record_shared_notification")
    def test_duplicate_share_does_not_re_notify(self, mock_notify):
        self._share([self.recipient.email])
        mock_notify.assert_called_once()
        self._share([self.recipient.email])
        assert mock_notify.call_count == 1  # no re-notification

    @mock.patch("records.notifications.send_record_shared_notification")
    def test_multi_recipient_notifies_each(self, mock_notify):
        stranger = User.objects.create_user(username="tom", email="tom@acme.com", password="pass")
        shares, _ = self._share([self.recipient.email, stranger.email])
        assert mock_notify.call_count == 2
        notified = {c.kwargs["share"].user for c in mock_notify.call_args_list}
        assert notified == {self.recipient, stranger}

    @mock.patch(
        "records.notifications.send_record_shared_notification",
        side_effect=Exception("broker down"),
    )
    def test_notification_failure_never_fails_the_grant(self, mock_notify):
        shares, unknown = self._share([self.recipient.email])
        assert len(shares) == 1 and unknown == []
        assert RecordShare.objects.filter(record=self.record, user=self.recipient).exists()
        assert AuditLog.objects.filter(record=self.record, action=AuditLog.Action.SHARE).exists()


class TestShareNotificationPayload(SharingTestCase):
    """The share notification module builds a correct multi-channel payload."""

    @mock.patch("core.services.notifications.send_multi_channel_notification")
    def test_payload_channels_subject_and_message(self, mock_send):
        shares, _ = self._share([self.recipient.email])
        send_record_shared_notification(record=self.record, share=shares[0], actor=self.owner)

        call = mock_send.call_args
        kwargs = call.kwargs
        assert kwargs["user"] == self.recipient
        assert "Acme invoice" in kwargs["subject"]
        assert kwargs["send_db"] is True
        assert "Acme invoice" in kwargs["db_message"]
        payload = kwargs["webpush_payload"]
        assert payload["head"] == "Record Shared"
        assert "Acme invoice" in payload["body"]
        assert f"/record_detail/{self.record.pk}/" in payload["url"]
        assert "Acme invoice" in kwargs["html_body"]
        assert "Acme invoice" in kwargs["text_body"]


class TestSharedDocuments(SharingTestCase):
    """Shared records make their attached documents view-only for sharees."""

    def setUp(self):
        super().setUp()
        self.doc = DocumentData.objects.create(
            user=self.owner,
            filepath="users/1/shared_receipt.pdf",
            file_hash="deadbeef" * 8,
            associated_record=self.record,
        )

    def test_sharee_can_view_shared_document(self):
        self._share([self.recipient.email])
        self.client.force_login(self.recipient)
        response = self.client.get(reverse("documents:view_document", args=[self.doc.pk]))
        assert response.status_code == 200

    def test_sharee_cannot_edit_shared_document(self):
        self._share([self.recipient.email])
        self.client.force_login(self.recipient)
        response = self.client.post(
            reverse("documents:view_document", args=[self.doc.pk]),
            {"title": "Rewritten by Sarah"},
        )
        assert response.status_code == 403
        self.doc.refresh_from_db()
        assert self.doc.title == "Untitled"

    def test_stranger_cannot_view_shared_document(self):
        self._share([self.recipient.email])
        self.client.force_login(self.stranger)
        response = self.client.get(reverse("documents:view_document", args=[self.doc.pk]))
        assert response.status_code == 404

    def test_share_without_documents_hides_attachments(self):
        share_services.grant_access(
            record=self.record,
            user=self.recipient,
            requester=self.owner,
            include_documents=False,
        )
        self.client.force_login(self.recipient)
        response = self.client.get(reverse("documents:view_document", args=[self.doc.pk]))
        assert response.status_code == 404

    def test_share_with_documents_can_view_attachments(self):
        share_services.grant_access(
            record=self.record,
            user=self.recipient,
            requester=self.owner,
            include_documents=True,
        )
        self.client.force_login(self.recipient)
        response = self.client.get(reverse("documents:view_document", args=[self.doc.pk]))
        assert response.status_code == 200

    def test_record_detail_hides_documents_when_not_included(self):
        self.doc.title = "Shared Receipt PDF"
        self.doc.save(update_fields=["title"])
        share_services.grant_access(
            record=self.record,
            user=self.recipient,
            requester=self.owner,
            include_documents=False,
        )
        self.client.force_login(self.recipient)
        response = self.client.get(reverse("records:record_detail", args=[self.record.pk]))
        assert response.status_code == 200
        assert b"Shared Receipt PDF" not in response.content


class TestShareeUI(SharingTestCase):
    def test_sharee_has_no_owner_actions_on_detail(self):
        self._share([self.recipient.email])
        self.client.force_login(self.recipient)
        response = self.client.get(reverse("records:record_detail", args=[self.record.pk]))
        assert b"Delete Permanently" not in response.content
        assert (
            reverse("documents:add_support_docs", args=[self.record.pk]).encode()
            not in response.content
        )

    def test_search_finds_shared_record(self):
        self._share([self.recipient.email])
        self.client.force_login(self.recipient)
        response = self.client.get(reverse("records:view_all_records"), {"search": "Office"})
        assert response.status_code == 200
        assert b"Acme invoice" in response.content

    def test_list_does_not_leak_to_stranger(self):
        self.client.force_login(self.stranger)
        response = self.client.get(reverse("records:view_all_records"), {"search": "Office"})
        assert response.status_code == 200
        assert b"Acme invoice" not in response.content
