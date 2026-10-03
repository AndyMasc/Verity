from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model

User = get_user_model()
from django.test import TestCase

from records.models import MergeLog, Record, Folder

from ._helpers import make_plaid_record, make_doc_record


class MergeLogModelTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="merge_log_model", password="pass")
        self.plaid = make_plaid_record(self.user, "Model Test")
        self.doc = make_doc_record(self.user, "Model Test")
        self.log = MergeLog.objects.create(
            plaid_record=self.plaid,
            document_record=self.doc,
            plaid_snapshot={},
            document_snapshot={},
        )

    def test_str_representation(self):
        expected = f"Merge {self.log.pk}: plaid={self.plaid.pk} <- doc={self.doc.pk}"
        self.assertEqual(str(self.log), expected)

    def test_default_ordering(self):
        other_plaid = make_plaid_record(self.user, "Order Plaid")
        other_doc = make_doc_record(self.user, "Order Doc")
        log2 = MergeLog.objects.create(
            plaid_record=other_plaid,
            document_record=other_doc,
            plaid_snapshot={},
            document_snapshot={},
        )
        qs = MergeLog.objects.all()
        self.assertEqual(qs.first(), log2)

    def test_null_fks_allowed(self):
        log = MergeLog.objects.create(
            plaid_snapshot={},
            document_snapshot={},
        )
        self.assertIsNone(log.plaid_record)
        self.assertIsNone(log.document_record)


class AutoMatchSignalTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="signal_test", password="pass")

    def _assert_on_commit_called(self, record_save_fn):
        with patch("records.signals.transaction.on_commit") as mock_cb:
            record_save_fn()
            mock_cb.assert_called_once()

    def _assert_on_commit_not_called(self, record_save_fn):
        with patch("records.signals.transaction.on_commit") as mock_cb:
            record_save_fn()
            mock_cb.assert_not_called()

    def test_skip_on_create(self):
        self._assert_on_commit_not_called(
            lambda: Record.objects.create(
                user=self.user, title="Test", record_type="expense_receipt"
            )
        )

    def test_skip_on_inactive(self):
        record = Record.objects.create(user=self.user, title="Test", record_type="expense_receipt")
        record.is_active = False
        self._assert_on_commit_not_called(lambda: record.save())

    def test_skip_on_skip_flag(self):
        record = Record.objects.create(user=self.user, title="Test", record_type="expense_receipt")
        record._skip_auto_match = True
        self._assert_on_commit_not_called(lambda: record.save())

    def test_runs_on_update(self):
        record = Record.objects.create(user=self.user, title="Test", record_type="expense_receipt")
        record.title = "Updated"
        self._assert_on_commit_called(lambda: record.save())

    def test_runs_on_plaid_record_update(self):
        plaid = make_plaid_record(self.user, "Signal Plaid")
        plaid.title = "Updated Plaid"
        self._assert_on_commit_called(lambda: plaid.save())
