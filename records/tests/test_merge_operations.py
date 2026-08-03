from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model

User = get_user_model()
from django.test import TestCase

from records.matching import (
    merge_document_into_plaid,
    try_match_document_record,
    try_match_plaid_record,
    undo_merge,
    _record_snapshot,
    BALANCE_TOLERANCE,
    DATE_TOLERANCE_DAYS,
    MERGE_SCORE_THRESHOLD,
)
from records.models import MergeLog, Record, Folder

from ._helpers import make_plaid_record, make_doc_record


class MergeDocumentIntoPlaidTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="merge_doc", password="pass")
        self.plaid = make_plaid_record(self.user, "Walmart")
        self.doc = make_doc_record(
            self.user, "Walmart", products="Milk|Eggs", notes="Weekly groceries"
        )
        self.doc_folder = Folder.objects.create(user=self.user, name="Groceries")
        self.plaid_with_folder = make_plaid_record(self.user, "Costco", folder=self.doc_folder)
        self.doc_with_docref = make_doc_record(
            self.user,
            "Receipt DocRef",
            products="Paper|Pens",
        )

    def test_merge_basic(self):
        result = merge_document_into_plaid(self.plaid, self.doc)
        self.assertIsNotNone(result)
        self.assertEqual(result.pk, self.plaid.pk)

    def test_merge_copies_products_and_notes(self):
        merge_document_into_plaid(self.plaid, self.doc)
        self.plaid.refresh_from_db()
        self.assertEqual(self.plaid.products, "Milk|Eggs")
        self.assertEqual(self.plaid.notes, "Weekly groceries")

    def test_merge_updates_record_type(self):
        self.doc.record_type = Record.RecordTypes.VOUCHER
        self.doc.save()
        merge_document_into_plaid(self.plaid, self.doc)
        self.plaid.refresh_from_db()
        self.assertEqual(self.plaid.record_type, Record.RecordTypes.VOUCHER)

    def test_merge_copies_folder(self):
        doc_with_folder = make_doc_record(
            self.user,
            "Folder Doc",
            folder=self.doc_folder,
        )
        merge_document_into_plaid(self.plaid, doc_with_folder)
        self.plaid.refresh_from_db()
        self.assertEqual(self.plaid.folder, self.doc_folder)

    def test_merge_deactivates_doc(self):
        merge_document_into_plaid(self.plaid, self.doc)
        self.doc.refresh_from_db()
        self.assertFalse(self.doc.is_active)

    def test_merge_creates_log(self):
        merge_document_into_plaid(self.plaid, self.doc)
        log = MergeLog.objects.filter(document_record=self.doc).first()
        self.assertIsNotNone(log)
        self.assertEqual(log.plaid_record, self.plaid)
        self.assertIsNone(log.undone_at)

    def test_merge_moves_all_documents(self):
        import hashlib
        from documents.models import DocumentData

        doc_data1 = DocumentData.objects.create(
            user=self.user,
            associated_record=self.doc_with_docref,
            filepath="users/1/test.pdf",
            file_hash=hashlib.sha256(b"test1").hexdigest(),
        )
        doc_data2 = DocumentData.objects.create(
            user=self.user,
            associated_record=self.doc_with_docref,
            filepath="users/2/other.pdf",
            file_hash=hashlib.sha256(b"test2").hexdigest(),
        )
        merge_document_into_plaid(self.plaid, self.doc_with_docref, doc_data1)
        doc_data1.refresh_from_db()
        doc_data2.refresh_from_db()
        self.assertEqual(doc_data1.associated_record, self.plaid)
        self.assertEqual(doc_data2.associated_record, self.plaid)
        self.assertFalse(
            DocumentData.objects.filter(associated_record=self.doc_with_docref).exists()
        )

    def test_merge_snapshot_tracks_document_ids(self):
        import hashlib
        from documents.models import DocumentData

        doc_data = DocumentData.objects.create(
            user=self.user,
            associated_record=self.doc_with_docref,
            filepath="users/1/test.pdf",
            file_hash=hashlib.sha256(b"test3").hexdigest(),
        )
        merge_document_into_plaid(self.plaid, self.doc_with_docref, doc_data)
        log = MergeLog.objects.filter(document_record=self.doc_with_docref).first()
        self.assertIn(doc_data.pk, log.document_snapshot.get("document_ids", []))

    def test_record_snapshot_includes_currency(self):
        snap = _record_snapshot(self.plaid)
        self.assertEqual(snap["currency"], self.plaid.currency)

    def test_merge_concurrency_guard_doc_inactive(self):
        self.doc.is_active = False
        self.doc.save()
        result = merge_document_into_plaid(self.plaid, self.doc)
        self.assertIsNone(result)

    def test_merge_concurrency_guard_doc_has_plaid_id(self):
        self.doc.plaid_transaction_id = "already_merged"
        self.doc.save()
        result = merge_document_into_plaid(self.plaid, self.doc)
        self.assertIsNone(result)

    def test_merge_preserves_plaid_type_when_doc_is_financial(self):
        plaid = make_plaid_record(
            self.user,
            "Bank Fee",
            record_type=Record.RecordTypes.EXPENSE_RECEIPT,
        )
        doc = make_doc_record(
            self.user,
            "Bank Fee",
            record_type=Record.RecordTypes.FINANCIAL_DOCUMENT,
        )
        merge_document_into_plaid(plaid, doc)
        plaid.refresh_from_db()
        self.assertEqual(plaid.record_type, Record.RecordTypes.EXPENSE_RECEIPT)


class UndoMergeTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="undo_merge", password="pass")
        self.plaid = make_plaid_record(self.user, "Home Depot")
        self.doc = make_doc_record(
            self.user, "Home Depot", products="Tools", notes="Renovation supplies"
        )
        self.merge_log = MergeLog.objects.create(
            plaid_record=self.plaid,
            document_record=self.doc,
            plaid_snapshot=_record_snapshot(self.plaid),
            document_snapshot=_record_snapshot(self.doc),
        )
        self.plaid.products = "Tools"
        self.plaid.notes = "Renovation supplies"
        self.plaid.save()
        self.doc.is_active = False
        self.doc.save()

    def test_undo_restores_doc_active(self):
        result = undo_merge(self.merge_log)
        self.assertIsNotNone(result)
        self.doc.refresh_from_db()
        self.assertTrue(self.doc.is_active)

    def test_undo_clears_doc_plaid_id(self):
        undo_merge(self.merge_log)
        self.doc.refresh_from_db()
        self.assertIsNone(self.doc.plaid_transaction_id)

    def test_undo_restores_plaid_snapshot(self):
        self.plaid.record_type = Record.RecordTypes.VOUCHER
        self.plaid.products = "Overwritten"
        self.plaid.notes = "Overwritten notes"
        self.plaid.save()
        undo_merge(self.merge_log)
        self.plaid.refresh_from_db()
        self.assertEqual(self.plaid.products, "")
        self.assertNotEqual(self.plaid.products, "Overwritten")
        self.assertNotEqual(self.plaid.notes, "Overwritten notes")

    def test_undo_marks_log_undone(self):
        undo_merge(self.merge_log)
        self.merge_log.refresh_from_db()
        self.assertIsNotNone(self.merge_log.undone_at)

    def test_undo_already_undone_returns_none(self):
        undo_merge(self.merge_log)
        result = undo_merge(self.merge_log)
        self.assertIsNone(result)

    def test_undo_restores_all_documents(self):
        import hashlib
        from documents.models import DocumentData

        doc1 = make_doc_record(self.user, "Doc With Files", products="Items", notes="Important")
        doc_data1 = DocumentData.objects.create(
            user=self.user,
            associated_record=doc1,
            filepath="users/1/file1.pdf",
            file_hash=hashlib.sha256(b"doc1").hexdigest(),
        )
        doc_data2 = DocumentData.objects.create(
            user=self.user,
            associated_record=doc1,
            filepath="users/1/file2.pdf",
            file_hash=hashlib.sha256(b"doc2").hexdigest(),
        )
        plaid = make_plaid_record(self.user, "Doc Restore")
        merge_document_into_plaid(plaid, doc1, doc_data1)
        log = MergeLog.objects.filter(document_record=doc1).first()
        undo_merge(log)
        doc_data1.refresh_from_db()
        doc_data2.refresh_from_db()
        self.assertEqual(doc_data1.associated_record, doc1)
        self.assertEqual(doc_data2.associated_record, doc1)


class TryMatchTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="try_match", password="pass")

    def test_try_match_document_record_found(self):
        plaid = make_plaid_record(self.user, "Staples")
        doc = make_doc_record(self.user, "Staples")
        result = try_match_document_record(doc)
        self.assertIsNotNone(result)
        self.assertEqual(result.pk, plaid.pk)

    def test_try_match_document_record_not_found(self):
        doc = make_doc_record(
            self.user,
            "Unique Item No Match",
            balance=Decimal("999.99"),
            transaction_date=date(2020, 1, 1),
            merchant="Nowhere",
        )
        result = try_match_document_record(doc)
        self.assertIsNone(result)

    def test_try_match_plaid_record_found(self):
        plaid = make_plaid_record(self.user, "Office Depot")
        make_doc_record(self.user, "Office Depot")
        merged = try_match_plaid_record(plaid)
        self.assertEqual(len(merged), 1)

    def test_try_match_plaid_record_multiple(self):
        plaid = make_plaid_record(self.user, "Multi Store")
        make_doc_record(self.user, "Multi Store")
        make_doc_record(
            self.user,
            "Multi Store",
            balance=Decimal("100.00"),
            transaction_date=date(2024, 6, 15),
        )
        merged = try_match_plaid_record(plaid)
        self.assertEqual(len(merged), 2)

    def test_try_match_plaid_record_not_found(self):
        plaid = make_plaid_record(self.user, "Solo")
        merged = try_match_plaid_record(plaid)
        self.assertEqual(merged, [])

    def test_try_match_plaid_record_no_double_merge(self):
        plaid = make_plaid_record(self.user, "Single")
        doc = make_doc_record(self.user, "Single")
        try_match_plaid_record(plaid)
        doc.refresh_from_db()
        self.assertFalse(doc.is_active)
        merged_again = try_match_plaid_record(plaid)
        self.assertEqual(merged_again, [])
