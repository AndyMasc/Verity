from datetime import date
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model

User = get_user_model()
from django.test import TestCase

from records.matching import (
    MERGE_SCORE_THRESHOLD,
    calculate_match_score,
    find_best_plaid_match,
    find_document_matches_for_plaid,
)
from records.models import Record, Folder

from ._helpers import make_plaid_record, make_doc_record


class CalculateMatchScoreTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="match_score", password="pass")
        self.plaid = make_plaid_record(self.user, "Amazon Purchase")
        self.doc = make_doc_record(self.user, "Amazon Purchase")

    def test_perfect_match(self):
        score = calculate_match_score(self.plaid, self.doc)
        self.assertEqual(score, 120)

    def test_no_match(self):
        doc = make_doc_record(
            self.user,
            "Something Completely Different",
            balance=Decimal("999.99"),
            transaction_date=date(2024, 1, 1),
            merchant="Nowhere",
        )
        score = calculate_match_score(self.plaid, doc)
        self.assertLess(score, MERGE_SCORE_THRESHOLD)

    def test_balance_only_match(self):
        doc = make_doc_record(self.user, "Unrelated", merchant="", transaction_date=None)
        score = calculate_match_score(self.plaid, doc)
        self.assertGreaterEqual(score, 40)
        self.assertLess(score, 50)

    def test_date_only_match(self):
        doc = make_doc_record(self.user, "Unrelated", balance=None, merchant="")
        score = calculate_match_score(self.plaid, doc)
        self.assertGreaterEqual(score, 30)
        self.assertLess(score, 40)

    def test_merchant_partial_match(self):
        doc = make_doc_record(
            self.user, "Something", merchant="amazon", balance=None, transaction_date=None
        )
        score = calculate_match_score(self.plaid, doc)
        self.assertEqual(score, 10)

    def test_title_partial_match(self):
        doc = make_doc_record(self.user, "amazon", merchant="", balance=None, transaction_date=None)
        score = calculate_match_score(self.plaid, doc)
        self.assertEqual(score, 8)

    def test_balance_within_tolerance(self):
        doc = make_doc_record(self.user, "Amazon Purchase", balance=Decimal("100.50"))
        score = calculate_match_score(self.plaid, doc)
        self.assertEqual(score, 110)

    def test_date_one_day_off(self):
        doc = make_doc_record(self.user, "Amazon Purchase", transaction_date=date(2024, 6, 16))
        score = calculate_match_score(self.plaid, doc)
        self.assertEqual(score, 110)

    def test_both_none_balance_and_date(self):
        doc = make_doc_record(self.user, "Amazon Purchase", balance=None, transaction_date=None)
        score = calculate_match_score(self.plaid, doc)
        self.assertGreaterEqual(score, 50)

    def test_empty_strings_do_not_raise(self):
        self.plaid.merchant = ""
        self.plaid.title = ""
        doc = make_doc_record(
            self.user, "Something", merchant="", balance=None, transaction_date=None
        )
        score = calculate_match_score(self.plaid, doc)
        self.assertEqual(score, 0)


class FindBestPlaidMatchTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="best_plaid", password="pass")
        self.plaid = make_plaid_record(self.user, "Target Purchase")
        self.doc = make_doc_record(self.user, "Target Purchase")

    def test_finds_match(self):
        match = find_best_plaid_match(self.doc)
        self.assertIsNotNone(match)
        self.assertEqual(match.pk, self.plaid.pk)

    def test_no_match_below_threshold(self):
        doc = make_doc_record(
            self.user,
            "Completely Different",
            balance=Decimal("999.99"),
            transaction_date=date(2020, 1, 1),
            merchant="Nowhere",
        )
        match = find_best_plaid_match(doc)
        self.assertIsNone(match)

    def test_excludes_own_pk(self):
        other = User.objects.create_user(username="exclude_pk", password="pass")
        plaid = make_plaid_record(other, "Self")
        match = find_best_plaid_match(plaid)
        self.assertIsNone(match)

    def test_user_isolation(self):
        other = User.objects.create_user(username="other_best", password="pass")
        make_plaid_record(other, "Other User Purchase")
        doc = make_doc_record(
            other,
            "Other User Purchase",
            balance=Decimal("100.00"),
            transaction_date=date(2024, 6, 15),
        )
        match = find_best_plaid_match(doc)
        self.assertEqual(match.title, "Other User Purchase")
        self.plaid.delete()
        my_doc = make_doc_record(
            self.user,
            "Other User Purchase",
            balance=Decimal("100.00"),
            transaction_date=date(2024, 6, 15),
        )
        my_match = find_best_plaid_match(my_doc)
        self.assertIsNone(my_match)

    def test_ignores_inactive_plaid(self):
        self.plaid.is_active = False
        self.plaid.save()
        match = find_best_plaid_match(self.doc)
        self.assertIsNone(match)

    def test_best_score_wins(self):
        make_plaid_record(
            self.user, "Worse Match", balance=Decimal("50.00"), transaction_date=date(2024, 1, 1)
        )
        match = find_best_plaid_match(self.doc)
        self.assertEqual(match.pk, self.plaid.pk)


class FindDocumentMatchesForPlaidTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="find_doc", password="pass")
        self.plaid = make_plaid_record(self.user, "Best Buy")

    def test_finds_matching_docs(self):
        doc = make_doc_record(self.user, "Best Buy")
        matches = find_document_matches_for_plaid(self.plaid)
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0][0].pk, doc.pk)

    def test_returns_empty_when_no_match(self):
        make_doc_record(self.user, "Not Matching", balance=Decimal("999.99"), merchant="Different")
        matches = find_document_matches_for_plaid(self.plaid)
        self.assertEqual(matches, [])

    def test_multiple_matches_sorted_by_score(self):
        perfect = make_doc_record(self.user, "Best Buy")
        partial = make_doc_record(
            self.user,
            "Best",
            balance=Decimal("100.50"),
            transaction_date=date(2024, 6, 16),
            merchant="Best Buy",
        )
        matches = find_document_matches_for_plaid(self.plaid)
        self.assertEqual(len(matches), 2)
        self.assertEqual(matches[0][0].pk, perfect.pk)
        self.assertGreater(matches[0][1], matches[1][1])

    def test_excludes_self(self):
        match = find_document_matches_for_plaid(self.plaid)
        self.assertEqual(match, [])

    def test_user_isolation(self):
        other = User.objects.create_user(username="other_find", password="pass")
        make_doc_record(other, "Best Buy")
        matches = find_document_matches_for_plaid(self.plaid)
        self.assertEqual(matches, [])

    def test_ignores_inactive_docs(self):
        make_doc_record(self.user, "Best Buy", is_active=False)
        matches = find_document_matches_for_plaid(self.plaid)
        self.assertEqual(matches, [])

    def test_ignores_docs_with_plaid_id(self):
        make_doc_record(self.user, "Best Buy", plaid_transaction_id="already_merged")
        matches = find_document_matches_for_plaid(self.plaid)
        self.assertEqual(matches, [])
