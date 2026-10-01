import json
import re
from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model

User = get_user_model()
from django.test import TestCase, override_settings
from django.urls import reverse

from records.models import MergeLog, Record


@override_settings(
    CACHES={"default": {"BACKEND": "django.core.cache.backends.dummy.DummyCache"}},
    SESSION_ENGINE="django.contrib.sessions.backends.db",
)
class AddRecordViewTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="adduser", password="pass")
        self.url = reverse("records:add_record_manual")

    def test_login_required(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 302)

    def test_get_form(self):
        self.client.force_login(self.user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "records/add_record.html")

    def test_post_valid(self):
        self.client.force_login(self.user)
        response = self.client.post(
            self.url,
            {
                "title": "New Record",
                "products": "Test Item",
                "record_type": "expense_receipt",
                "transaction_date": "2024-06-15",
                "merchant": "Test Merchant",
                "balance": "25.00",
                "currency": "usd",
                "notes": "Business purpose",
                "payment_method": "Credit Card",
            },
        )
        self.assertIn(response.status_code, [200, 302])
        self.assertTrue(
            Record.objects.filter(title="New Record", user=self.user).exists()
        )

    def test_post_invalid(self):
        self.client.force_login(self.user)
        response = self.client.post(self.url, {"title": ""})
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "records/add_record.html")

    def test_post_with_expiry(self):
        self.client.force_login(self.user)
        response = self.client.post(
            self.url,
            {
                "title": "With Expiry",
                "products": "Item",
                "record_type": "warranty_certificate",
                "transaction_date": "2024-01-01",
                "expiry_date": "2024-12-31",
                "merchant": "Test Merchant",
                "balance": "100.00",
                "currency": "usd",
            },
        )
        self.assertIn(response.status_code, [200, 302])
        record = Record.objects.get(title="With Expiry")
        self.assertEqual(record.expiry_date, date(2024, 12, 31))

    def test_post_with_folder(self):
        from records.models import Folder

        self.client.force_login(self.user)
        folder = Folder.objects.create(user=self.user, name="Test Folder")
        response = self.client.post(
            self.url,
            {
                "title": "In Folder",
                "products": "Item",
                "record_type": "expense_receipt",
                "folder": folder.id,
                "transaction_date": "2024-06-15",
                "merchant": "Test Merchant",
                "balance": "50.00",
                "currency": "usd",
                "notes": "Business purpose",
                "payment_method": "Credit Card",
            },
        )
        self.assertIn(response.status_code, [200, 302])
        record = Record.objects.get(title="In Folder")
        self.assertEqual(record.folder, folder)

    def test_post_with_balance(self):
        self.client.force_login(self.user)
        response = self.client.post(
            self.url,
            {
                "title": "With Balance",
                "products": "Item",
                "record_type": "expense_receipt",
                "balance": "250.00",
                "currency": "usd",
                "transaction_date": "2024-06-15",
                "merchant": "Test Merchant",
                "notes": "Business purpose",
                "payment_method": "Credit Card",
            },
        )
        self.assertIn(response.status_code, [200, 302])
        record = Record.objects.get(title="With Balance")
        self.assertEqual(record.balance, Decimal("250.00"))


class RecordDetailViewTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="detailuser", password="pass")
        self.record = Record.objects.create(
            user=self.user,
            title="Detail View",
            record_type="expense_receipt",
            transaction_date=date(2024, 6, 15),
        )
        self.url = reverse("records:record_detail", args=[self.record.id])

    def test_login_required(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 302)

    def test_owner_can_view(self):
        self.client.force_login(self.user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "records/record_detail_view.html")
        self.assertEqual(response.context["record"], self.record)

    def test_other_user_cannot_view(self):
        user2 = User.objects.create_user(username="otherdet", password="pass")
        self.client.force_login(user2)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 404)

    def test_nonexistent_record(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("records:record_detail", args=[99999]))
        self.assertEqual(response.status_code, 404)

    def test_legacy_merge_snapshot_without_currency_renders(self):
        plaid_record = Record.objects.create(
            user=self.user,
            title="Bank Tx",
            record_type="expense_receipt",
            transaction_date=date(2024, 6, 15),
            plaid_transaction_id="txn_snap_1",
            balance=Decimal("1000.00"),
            currency="eur",
        )
        doc_record = Record.objects.create(
            user=self.user,
            title="Receipt",
            record_type="expense_receipt",
            transaction_date=date(2024, 6, 15),
        )
        MergeLog.objects.create(
            plaid_record=plaid_record,
            document_record=doc_record,
            plaid_snapshot={
                "title": "Bank Tx",
                "merchant": "Bank",
                "balance": "1000.00",
                "payment_method": "Card",
            },
            document_snapshot={"title": "Receipt", "balance": "10.00"},
        )
        self.client.force_login(self.user)
        response = self.client.get(
            reverse("records:record_detail", args=[plaid_record.id])
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "€")

    def test_update_via_post_with_hx(self):
        self.client.force_login(self.user)
        response = self.client.post(
            self.url,
            {
                "title": "Updated Title",
                "products": "Updated Item",
                "record_type": "voucher",
                "transaction_date": "2024-06-15",
                "merchant": "Test Merchant",
                "balance": "100.00",
                "currency": "usd",
            },
            headers={"hx-request": "true"},
        )
        self.assertIn(response.status_code, [200, 204])
        self.record.refresh_from_db()
        self.assertEqual(self.record.title, "Updated Title")
        self.assertEqual(self.record.record_type, "voucher")

    def test_invalid_hx_post_returns_errored_form(self):
        """A rejected autosave swaps in an error summary and leaves the form alone.

        Autosave validates every field on every keystroke, so re-rendering the form
        here would destroy the field being typed into on each attempt.
        """
        self.client.force_login(self.user)
        response = self.client.post(
            self.url,
            {
                "title": "",
                "products": "Updated Item",
                "record_type": "expense_receipt",
                "transaction_date": "2024-06-15",
                "merchant": "",
                "balance": "",
                "currency": "",
                "notes": "",
                "payment_method": "",
                "nickname": "",
                "folder": "",
            },
            headers={"hx-request": "true"},
        )
        self.assertEqual(response.status_code, 200)
        # The form itself must not be re-rendered, or the user's focus, cursor
        # and in-progress input are thrown away.
        self.assertEqual(response.headers.get("HX-Reswap"), "none")
        # Exactly one element is swapped out of band; the form is not replaced.
        self.assertContains(response, 'hx-swap-oob="innerHTML:#record-errors"')
        self.assertContains(response, "Not saved yet")
        # It names the specific blocking fields rather than a generic message.
        self.assertContains(response, "Merchant")
        self.assertContains(response, "Title")
        trigger = json.loads(response.headers["HX-Trigger"])
        self.assertEqual(trigger["showToast"]["tags"], "error")
        self.assertIn("required", trigger["showToast"]["text"])
        # Nothing was written.
        self.record.refresh_from_db()
        self.assertNotEqual(self.record.title, "")

    def test_textareas_render_without_leading_whitespace(self):
        """Django's textarea template indents the value; it must not reach the user.

        The parser drops the newline after <textarea> but keeps the spaces, which
        would leave the cursor and first word sitting four columns in.
        """
        self.client.force_login(self.user)
        response = self.client.get(self.url)
        html = response.content.decode()
        for name in ("products", "notes"):
            with self.subTest(field=name):
                match = re.search(
                    rf'<textarea[^>]*name="{name}"[^>]*>(.*?)</textarea>', html, re.S
                )
                self.assertIsNotNone(match, f"{name} textarea not found")
                self.assertEqual(
                    match.group(1).lstrip(),
                    match.group(1),
                    f"{name} textarea value has leading whitespace",
                )

    def test_detail_page_has_error_summary_slot(self):
        """The form carries the empty target the summary is swapped into."""
        self.client.force_login(self.user)
        response = self.client.get(self.url)
        self.assertContains(response, 'id="record-errors"')
        self.assertContains(response, "record-form")

    def test_valid_hx_post_saves_and_reports_success(self):
        """A good autosave returns 204 with a toast the client actually shows."""
        self.client.force_login(self.user)
        response = self.client.post(
            self.url,
            {
                "title": "Autosaved",
                "products": "Item",
                "record_type": "expense_receipt",
                "transaction_date": "2024-06-15",
                "merchant": "Acme",
                "balance": "12.50",
                "currency": "usd",
                "notes": "Business purpose",
                "payment_method": "Visa",
                "nickname": "",
                "folder": "",
                "expiry_date": "",
            },
            headers={"hx-request": "true"},
        )
        self.assertEqual(response.status_code, 204)
        trigger = json.loads(response.headers["HX-Trigger"])
        self.assertEqual(trigger["showToast"]["tags"], "success")
        self.record.refresh_from_db()
        self.assertEqual(self.record.title, "Autosaved")

    def test_other_user_cannot_update(self):
        user2 = User.objects.create_user(username="otherupd", password="pass")
        self.client.force_login(user2)
        response = self.client.post(
            self.url,
            {
                "title": "Hacked Title",
                "products": "Item",
                "record_type": "expense_receipt",
            },
        )
        self.assertEqual(response.status_code, 404)
        self.record.refresh_from_db()
        self.assertEqual(self.record.title, "Detail View")
