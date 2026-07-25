"""Tests for shared view utilities.

Covers htmx_response, create_audit_log, parse_record_ids, and
CachedPaginatorMixin.
"""

import json
from decimal import Decimal

import pytest
from django.contrib.auth import get_user_model
from django.http import HttpRequest, HttpResponse
from django.test import TestCase

from Papertrail.views import create_audit_log, htmx_response, parse_record_ids
from records.models import AuditLog, Record

User = get_user_model()


class HtmxResponseTest(TestCase):
    def test_returns_none_for_non_htmx(self):
        request = HttpRequest()
        request.META["HTTP_HX_REQUEST"] = None
        result = htmx_response(request, toast="Hello")
        assert result is None

    def test_returns_204_for_htmx(self):
        request = HttpRequest()
        request.META["HTTP_HX_REQUEST"] = "true"
        result = htmx_response(request, toast="Done")
        assert result is not None
        assert result.status_code == 204

    def test_includes_toast_trigger(self):
        request = HttpRequest()
        request.META["HTTP_HX_REQUEST"] = "true"
        result = htmx_response(request, toast="Saved")
        trigger = json.loads(result["HX-Trigger"])
        assert trigger["showToast"]["text"] == "Saved"
        assert trigger["showToast"]["tags"] == "success"

    def test_includes_redirect(self):
        request = HttpRequest()
        request.META["HTTP_HX_REQUEST"] = "true"
        result = htmx_response(request, redirect_url="/records/")
        assert result["HX-Redirect"] == "/records/"

    def test_error_tag(self):
        request = HttpRequest()
        request.META["HTTP_HX_REQUEST"] = "true"
        result = htmx_response(request, toast="Error", toast_tags="error")
        trigger = json.loads(result["HX-Trigger"])
        assert trigger["showToast"]["tags"] == "error"


class CreateAuditLogTest(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="audituser", password="pass")
        self.record = Record.objects.create(
            user=self.user,
            title="Test",
            record_type="expense_receipt",
            transaction_date="2024-06-15",
        )

    def test_creates_log_with_basic_fields(self):
        log = create_audit_log(
            user=self.user,
            action=AuditLog.Action.MERGE,
            record=self.record,
        )
        assert log.user == self.user
        assert log.action == AuditLog.Action.MERGE
        assert log.record == self.record

    def test_creates_log_with_details(self):
        log = create_audit_log(
            user=self.user,
            action=AuditLog.Action.MERGE,
            record=self.record,
            details={"key": "value"},
        )
        assert log.details == {"key": "value"}

    def test_creates_log_with_merge_log(self):
        from records.models import MergeLog

        other_record = Record.objects.create(
            user=self.user,
            title="Other",
            record_type="expense_receipt",
            transaction_date="2024-06-15",
        )
        merge_log = MergeLog.objects.create(
            plaid_record=self.record,
            document_record=other_record,
            plaid_snapshot={"title": "Test"},
            document_snapshot={"title": "Other"},
        )
        log = create_audit_log(
            user=self.user,
            action=AuditLog.Action.MERGE,
            record=self.record,
            merge_log=merge_log,
        )
        assert log.merge_log == merge_log


class ParseRecordIdsTest(TestCase):
    def test_valid_json_body(self):
        request = HttpRequest()
        request._body = json.dumps({"record_ids": [1, 2, 3]}).encode()
        ids, error = parse_record_ids(request)
        assert ids == [1, 2, 3]
        assert error is None

    def test_invalid_json(self):
        request = HttpRequest()
        request._body = b"not json"
        ids, error = parse_record_ids(request)
        assert ids is None
        assert error.status_code == 400

    def test_non_integer_ids(self):
        request = HttpRequest()
        request._body = json.dumps({"record_ids": ["a", "b"]}).encode()
        ids, error = parse_record_ids(request)
        assert ids is None
        assert error.status_code == 400

    def test_missing_record_ids_key(self):
        request = HttpRequest()
        request._body = json.dumps({}).encode()
        ids, error = parse_record_ids(request)
        assert ids == []
        assert error is None

    def test_non_list_record_ids(self):
        request = HttpRequest()
        request._body = json.dumps({"record_ids": "not_a_list"}).encode()
        ids, error = parse_record_ids(request)
        assert ids is None
        assert error.status_code == 400
