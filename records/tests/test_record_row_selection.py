"""Both record-row layouts must offer a selection checkbox with a usable target.

The mobile row originally had no checkbox at all -- the control lived only
inside the desktop grid -- so bulk actions were unreachable on phones.
"""

import datetime
import re

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from records.models import Record


class RecordRowSelectionTest(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="selector", password="p"
        )
        self.record = Record.objects.create(
            user=self.user,
            title="Row",
            record_type="expense_receipt",
            transaction_date=datetime.date(2024, 6, 15),
        )
        self.client.force_login(self.user)
        self.html = self.client.get(
            reverse("records:view_all_records")
        ).content.decode()

    def test_both_desktop_and_mobile_rows_render_a_checkbox(self):
        boxes = re.findall(r'<input type="checkbox"\s+value="(\d+)"', self.html)
        self.assertEqual(
            boxes.count(str(self.record.pk)),
            2,
            "expected a checkbox in the desktop row and the mobile row",
        )

    def test_select_target_is_larger_than_the_box_it_draws(self):
        """A near-miss must hit the checkbox, not the surrounding record link."""
        self.assertIn(
            'class="relative -m-2 flex h-9 w-9 shrink-0 items-center justify-center"',
            self.html,
            "enlarged tap target missing",
        )
        # The label fills that whole area, so every pixel of it toggles selection.
        self.assertIn(
            'class="absolute inset-0 flex cursor-pointer items-center justify-center"',
            self.html,
        )
        # And it stops the click from bubbling up to the record link.
        self.assertIn("@click.stop", self.html)
