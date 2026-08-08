from datetime import date

from django.contrib.auth import get_user_model

User = get_user_model()
from django.test import TestCase, override_settings
from django.urls import reverse

from records.models import Record


@override_settings(
    CACHES={"default": {"BACKEND": "django.core.cache.backends.dummy.DummyCache"}},
    SESSION_ENGINE="django.contrib.sessions.backends.db",
)
class RecordListViewTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="listuser", password="pass")
        self.url = reverse("records:view_all_records")

    def test_login_required(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 302)

    def test_authenticated_access(self):
        self.client.force_login(self.user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "records/record_list_view.html")

    def test_context_has_filter(self):
        self.client.force_login(self.user)
        response = self.client.get(self.url)
        self.assertIn("filter", response.context)

    def test_only_own_records_shown(self):
        user2 = User.objects.create_user(username="otherlist", password="pass")
        Record.objects.create(
            user=user2,
            title="Other's",
            record_type="expense_receipt",
            transaction_date=date(2024, 6, 15),
        )
        self.client.force_login(self.user)
        response = self.client.get(self.url)
        self.assertEqual(len(response.context["records"]), 0)

    def test_records_visible(self):
        self.client.force_login(self.user)
        Record.objects.create(
            user=self.user,
            title="My Record",
            record_type="expense_receipt",
            transaction_date=date(2024, 6, 15),
        )
        response = self.client.get(self.url)
        self.assertEqual(len(response.context["records"]), 1)

    def test_filter_by_search_query(self):
        self.client.force_login(self.user)
        Record.objects.create(
            user=self.user,
            title="UniqueWidget",
            record_type="expense_receipt",
            transaction_date=date(2024, 6, 15),
        )
        response = self.client.get(self.url, {"search": "UniqueWidget"})
        self.assertEqual(len(response.context["records"]), 1)

    def test_filter_no_match(self):
        self.client.force_login(self.user)
        response = self.client.get(self.url, {"search": "NOMATCH"})
        self.assertEqual(len(response.context["records"]), 0)

    def test_pagination_first_page(self):
        self.client.force_login(self.user)
        for i in range(30):
            Record.objects.create(
                user=self.user,
                title=f"Record {i}",
                record_type="expense_receipt",
                transaction_date=date(2024, 6, 15),
            )
        response = self.client.get(self.url)
        self.assertTrue(response.context["is_paginated"])
        self.assertEqual(len(response.context["records"]), 20)

    def test_pagination_second_page(self):
        self.client.force_login(self.user)
        for i in range(30):
            Record.objects.create(
                user=self.user,
                title=f"Record {i}",
                record_type="expense_receipt",
                transaction_date=date(2024, 6, 15),
            )
        response = self.client.get(self.url, {"page": 2})
        self.assertEqual(len(response.context["records"]), 10)

    def test_pagination_invalid_page_returns_404(self):
        self.client.force_login(self.user)
        Record.objects.create(
            user=self.user,
            title="Test",
            record_type="expense_receipt",
            transaction_date=date(2024, 6, 15),
        )
        response = self.client.get(self.url, {"page": "abc"})
        self.assertEqual(response.status_code, 404)

    def test_pagination_out_of_range_page_returns_404(self):
        self.client.force_login(self.user)
        for i in range(30):
            Record.objects.create(
                user=self.user,
                title=f"Record {i}",
                record_type="expense_receipt",
                transaction_date=date(2024, 6, 15),
            )
        response = self.client.get(self.url, {"page": 999})
        self.assertEqual(response.status_code, 404)

    def test_pagination_empty_page_returns_no_records(self):
        self.client.force_login(self.user)
        response = self.client.get(self.url)
        self.assertFalse(response.context["is_paginated"])
        self.assertEqual(len(response.context["records"]), 0)
