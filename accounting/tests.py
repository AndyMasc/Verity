import io
import json
from unittest.mock import patch

import openpyxl
from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from records.models import Record


EXPORT_URL = reverse("accounting:export_all_to_excel")
EXPORT_SELECTED_URL = reverse("accounting:export_selected_to_excel")


def _parse_xlsx_sheet_rows(content: bytes) -> list[list[str]]:
    """Parse xlsx bytes and return the header + data rows."""
    wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True)
    ws = wb.active
    rows = []
    for row in ws.iter_rows(values_only=True):
        rows.append([str(cell) if cell is not None else "" for cell in row])
    wb.close()
    return rows


@patch("django_ratelimit.decorators.is_ratelimited", return_value=False)
class ExportExcelAllViewTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="exporter", password="pass")
        self.other_user = User.objects.create_user(username="other", password="pass")

    def test_login_required(self, _mock_rl):
        response = self.client.get(EXPORT_URL)
        self.assertEqual(response.status_code, 302)

    def test_returns_xlsx(self, _mock_rl):
        Record.objects.create(user=self.user, title="Groceries")
        self.client.force_login(self.user)
        response = self.client.get(EXPORT_URL)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response["Content-Type"],
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        self.assertIn("Record_export.xlsx", response["Content-Disposition"])
        self.assertGreater(len(response.content), 0)

    def test_only_exports_own_records(self, _mock_rl):
        Record.objects.create(user=self.user, title="My receipt")
        Record.objects.create(user=self.other_user, title="Their receipt")
        self.client.force_login(self.user)
        response = self.client.get(EXPORT_URL)
        self.assertEqual(response.status_code, 200)
        rows = _parse_xlsx_sheet_rows(response.content)
        titles = [row[rows[0].index("title")] for row in rows[1:] if "title" in rows[0]]
        self.assertIn("My receipt", titles)
        self.assertNotIn("Their receipt", titles)

    def test_includes_soft_deleted_records(self, _mock_rl):
        record = Record.objects.create(user=self.user, title="Trashed record")
        record.is_active = False
        record.save(update_fields=["is_active"])
        self.client.force_login(self.user)
        response = self.client.get(EXPORT_URL)
        self.assertEqual(response.status_code, 200)
        rows = _parse_xlsx_sheet_rows(response.content)
        titles = [row[rows[0].index("title")] for row in rows[1:] if "title" in rows[0]]
        self.assertIn("Trashed record", titles)

    def test_excludes_internal_fields(self, _mock_rl):
        Record.objects.create(
            user=self.user,
            title="Test",
            plaid_transaction_id="plaid-123",
            expiry_notification_sent=True,
        )
        self.client.force_login(self.user)
        response = self.client.get(EXPORT_URL)
        self.assertEqual(response.status_code, 200)
        rows = _parse_xlsx_sheet_rows(response.content)
        headers = rows[0]
        self.assertNotIn("plaid_transaction_id", headers)
        self.assertNotIn("expiry_notification_sent", headers)
        self.assertNotIn("is_active", headers)

    def test_empty_export(self, _mock_rl):
        self.client.force_login(self.user)
        response = self.client.get(EXPORT_URL)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Record_export.xlsx", response["Content-Disposition"])

    @patch("accounting.views.export_records_to_excel", side_effect=RuntimeError("db down"))
    def test_returns_500_on_export_failure(self, _mock_export, _mock_rl):
        self.client.force_login(self.user)
        response = self.client.get(EXPORT_URL)
        self.assertEqual(response.status_code, 500)


class ExportExcelAllServiceTest(TestCase):
    def test_user_scoping(self):
        from accounting.services import export_records_to_excel

        user = User.objects.create_user(username="svc_user", password="pass")
        other = User.objects.create_user(username="svc_other", password="pass")
        Record.objects.create(user=user, title="owned")
        Record.objects.create(user=other, title="not owned")

        queryset = Record.objects.filter(user=user)
        result = export_records_to_excel(queryset=queryset)
        self.assertIsInstance(result, bytes)
        self.assertGreater(len(result), 0)

        rows = _parse_xlsx_sheet_rows(result)
        titles = [row[rows[0].index("title")] for row in rows[1:] if "title" in rows[0]]
        self.assertIn("owned", titles)
        self.assertNotIn("not owned", titles)


@patch("django_ratelimit.decorators.is_ratelimited", return_value=False)
class ExportSelectedExcelViewTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="export_sel", password="pass")
        self.other_user = User.objects.create_user(username="export_other", password="pass")

    def test_login_required(self, _mock_rl):
        response = self.client.post(
            EXPORT_SELECTED_URL,
            data=json.dumps({"record_ids": []}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 302)

    def test_exports_selected_records(self, _mock_rl):
        r1 = Record.objects.create(user=self.user, title="Keep this")
        Record.objects.create(user=self.user, title="Not this")
        self.client.force_login(self.user)
        response = self.client.post(
            EXPORT_SELECTED_URL,
            data=json.dumps({"record_ids": [r1.pk]}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        rows = _parse_xlsx_sheet_rows(response.content)
        titles = [row[rows[0].index("title")] for row in rows[1:] if "title" in rows[0]]
        self.assertIn("Keep this", titles)
        self.assertNotIn("Not this", titles)

    def test_does_not_export_other_users_records(self, _mock_rl):
        other_record = Record.objects.create(user=self.other_user, title="Their record")
        self.client.force_login(self.user)
        response = self.client.post(
            EXPORT_SELECTED_URL,
            data=json.dumps({"record_ids": [other_record.pk]}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        rows = _parse_xlsx_sheet_rows(response.content)
        titles = [row[rows[0].index("title")] for row in rows[1:] if "title" in rows[0]]
        self.assertNotIn("Their record", titles)

    def test_invalid_json_returns_400(self, _mock_rl):
        self.client.force_login(self.user)
        response = self.client.post(
            EXPORT_SELECTED_URL,
            data="not json",
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)

    def test_invalid_record_ids_returns_400(self, _mock_rl):
        self.client.force_login(self.user)
        response = self.client.post(
            EXPORT_SELECTED_URL,
            data=json.dumps({"record_ids": "not a list"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)

    def test_empty_selection_returns_valid_xlsx(self, _mock_rl):
        self.client.force_login(self.user)
        response = self.client.post(
            EXPORT_SELECTED_URL,
            data=json.dumps({"record_ids": []}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("Record_export.xlsx", response["Content-Disposition"])

    def test_get_not_allowed(self, _mock_rl):
        self.client.force_login(self.user)
        response = self.client.get(EXPORT_SELECTED_URL)
        self.assertEqual(response.status_code, 405)
