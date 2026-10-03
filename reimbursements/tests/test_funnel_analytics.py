"""Analytics for the reimbursement funnel.

The funnel has two audiences that PostHog would otherwise blur together: the
signed-in creator viewing a package from their own dashboard, and the payer --
who may be external and unauthenticated, known only by a verified email address.
These tests pin the event names, the identity each is attributed to, and the
properties that let the two sides be told apart.
"""

from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase

from ..models import external_payer_distinct_id
from ._helpers import _package, _record, _user

User = get_user_model()


class _CaptureMixin:
    def _captured(self):
        """Collect (event, distinct_id, properties) for every capture in the app.

        "posthog_client" is bound at module level in the view modules but imported
        inside the function in models, so each binding must be patched where it is
        used. Patching only "core.apps" misses the view-side events and makes these
        tests pass or fail depending on import order.
        """
        captured = []

        def _capture(event, *args, **kwargs):
            captured.append(
                (event, kwargs.get("distinct_id"), kwargs.get("properties") or {})
            )

        for target in (
            "core.apps.posthog_client",
            "reimbursements.views.verify.posthog_client",
            "reimbursements.views.packages.posthog_client",
        ):
            patcher = mock.patch(target)
            patcher.start().capture.side_effect = _capture
            self.addCleanup(patcher.stop)
        return captured


class ExternalPayerIdentityTests(TestCase):
    def test_hash_is_stable_and_hides_the_address(self):
        first = external_payer_distinct_id("Sam.Example@ACME.com ")
        second = external_payer_distinct_id("sam.example@acme.com")
        self.assertEqual(first, second, "same payer must land on one person")
        self.assertNotIn("sam", first.lower())
        self.assertTrue(first.startswith("external-payer-"))


class RecipientPaidCaptureTests(_CaptureMixin, TestCase):
    def setUp(self):
        self.creator = _user("funder@example.com")
        self.payer = _user("claimer@example.com")
        self.record = _record(self.creator, balance=Decimal("120.00"))

    def test_registered_payer_gets_its_own_person(self):
        package = _package(self.creator, recipient=self.payer)
        package.records.add(self.record)
        captured = self._captured()
        package.mark_as_paid(self.payer)

        names = [name for name, _, _ in captured]
        self.assertIn("recipient_paid_reimbursement", names)
        self.assertIn("sender_reimbursement_settled", names)

        event, distinct_id, props = next(
            (n, d, p) for n, d, p in captured if n == "recipient_paid_reimbursement"
        )
        self.assertEqual(event, "recipient_paid_reimbursement")
        self.assertEqual(distinct_id, str(self.payer.pk))
        self.assertEqual(props["payer_type"], "registered")
        self.assertEqual(props["package_uuid"], str(package.uuid))

    def test_creator_side_still_attributed_to_the_funder(self):
        package = _package(self.creator, recipient=self.payer)
        package.records.add(self.record)
        captured = self._captured()
        package.mark_as_paid(self.payer)

        _, distinct_id, _ = next(
            (n, d, p) for n, d, p in captured if n == "sender_reimbursement_settled"
        )
        self.assertEqual(distinct_id, str(self.creator.pk))

    def test_external_payer_gets_a_stable_pseudonymous_person(self):
        package = _package(self.creator, recipient_email="outside@acme.com")
        package.records.add(_record(self.creator, balance=Decimal("75.00")))
        captured = self._captured()
        package.mark_as_paid(None)

        _, distinct_id, props = next(
            (n, d, p) for n, d, p in captured if n == "recipient_paid_reimbursement"
        )
        self.assertEqual(props["payer_type"], "external")
        self.assertEqual(distinct_id, external_payer_distinct_id("outside@acme.com"))
        self.assertNotIn("outside@acme.com", distinct_id)

    def test_both_events_share_the_money_properties(self):
        package = _package(self.creator, recipient=self.payer)
        package.records.add(self.record)
        captured = self._captured()
        package.mark_as_paid(self.payer)

        by_name = {name: props for name, _, props in captured}
        self.assertEqual(
            by_name["sender_reimbursement_settled"]["total_amount"],
            by_name["recipient_paid_reimbursement"]["total_amount"],
        )
        self.assertEqual(
            by_name["sender_reimbursement_settled"]["currency"],
            by_name["recipient_paid_reimbursement"]["currency"],
        )


class PayPageViewedTests(_CaptureMixin, TestCase):
    def setUp(self):
        self.creator = _user("funder2@example.com")
        self.package = _package(self.creator, recipient_email="outside@acme.com")
        self.package.records.add(_record(self.creator, balance=Decimal("75.00")))

    def test_external_pay_page_carries_a_client_side_capture(self):
        """The view is captured in the browser, not on the server.

        An unverified external payer has no server-side identity, so capturing
        here would drop the event; the browser's anonymous id is what PostHog
        later merges once the payer verifies.
        """
        captured = self._captured()
        response = self.client.get(f"/reimbursements/pay/{self.package.uuid}/")
        self.assertEqual(response.status_code, 200)

        self.assertNotIn(
            "reimbursement_package_viewed_by_recipient",
            [name for name, _, _ in captured],
            "the pay page must not capture on the server",
        )
        body = response.content.decode()
        self.assertIn("reimbursement_package_viewed_by_recipient", body)
        self.assertIn("posthog.capture", body)
        self.assertIn('"audience": "recipient"', body)
        self.assertIn('"payer_type": "external"', body)
        self.assertIn('"requires_verification": true', body)
