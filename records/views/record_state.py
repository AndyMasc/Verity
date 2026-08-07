"""Views for archiving, unarchiving, and deleting records.

Each action creates an AuditLog entry and, for HTMX requests, returns
a 204 response so the client can update the UI without a full page reload.
"""

import json

import posthog
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.http import require_POST
from django_ratelimit.decorators import ratelimit

from core.services.dashboard import invalidate_dashboard_cache
from Papertrail.views import parse_record_ids

from ..models import Record
from ..services import (
    BulkLimitExceededError,
    archive_record,
    bulk_toggle_archive,
    soft_delete_record,
    unarchive_record,
)


class ArchiveRecord(LoginRequiredMixin, View):
    """Soft-delete a record by marking it inactive and logging the action."""

    @method_decorator(ratelimit(key="user", rate="30/m", method="POST", block=True))
    def post(self, request: HttpRequest, record_id: int) -> HttpResponse:
        record = get_object_or_404(Record, id=record_id, user=request.user, is_active=True)
        archive_record(request.user, record)
        invalidate_dashboard_cache(request.user.id)
        posthog.capture(
            "record_archived",
            distinct_id=str(request.user.pk),
            properties={
                "record_type": record.record_type,
            },
        )
        if request.headers.get("HX-Request") == "true":
            response = HttpResponse(status=204)
            response["HX-Trigger"] = json.dumps({"recordChanged": {}})
            return response

        return redirect("records:view_all_records")


class UnarchiveRecord(LoginRequiredMixin, View):
    """Restore a soft-deleted record and log the action."""

    @method_decorator(ratelimit(key="user", rate="30/m", method="POST", block=True))
    def post(self, request: HttpRequest, record_id: int) -> HttpResponse:
        record = get_object_or_404(Record, id=record_id, user=request.user, is_active=False)
        unarchive_record(request.user, record)
        invalidate_dashboard_cache(request.user.id)
        if request.headers.get("HX-Request") == "true":
            response = HttpResponse(status=200)
            response["HX-Trigger"] = json.dumps({"recordChanged": {}})
            return response
        return redirect("records:view_all_records")


class DeleteRecordView(LoginRequiredMixin, View):
    """Soft-delete a record and log the action."""

    def post(self, request: HttpRequest, record_id: int) -> HttpResponse:
        record = get_object_or_404(Record, id=record_id, user=request.user)
        soft_delete_record(request.user, record)
        invalidate_dashboard_cache(request.user.id)
        posthog.capture(
            "record_deleted",
            distinct_id=str(request.user.pk),
            properties={
                "record_type": record.record_type,
            },
        )
        if request.headers.get("HX-Request") == "true":
            response = HttpResponse(status=200)
            response["HX-Trigger"] = json.dumps({"recordChanged": {}})
            return response
        return redirect("records:view_all_records")


def _bulk_response(
    request: HttpRequest,
    count: int,
    *,
    verb: str,
) -> HttpResponse:
    """Build an HTMX-compatible response for a bulk archive/unarchive operation."""
    if request.headers.get("HX-Request") == "true":
        response = HttpResponse(status=200)
        response["HX-Trigger"] = json.dumps(
            {
                "recordChanged": {},
                "showToast": {
                    "message": f"{count} record{'s' if count != 1 else ''} {verb}.",
                    "tags": "success",
                },
            }
        )
        return response
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return JsonResponse({"success": True, "count": count, "verb": verb})
    return redirect("records:view_all_records")


@login_required
@ratelimit(key="user", rate="10/m", method="POST", block=True)
@require_POST
def BulkArchiveView(request: HttpRequest) -> HttpResponse:
    """Archive multiple records at once.

    Accepts a JSON body with ``{"record_ids": [1, 2, 3]}`` and archives
    all active records belonging to the user.
    """
    record_ids, error = parse_record_ids(request)
    if error:
        return error

    try:
        count = bulk_toggle_archive(record_ids=record_ids, user=request.user, archive=True)  # type: ignore[arg-type]
    except BulkLimitExceededError as exc:
        return HttpResponse(
            json.dumps({"error": str(exc)}), status=400, content_type="application/json"
        )

    invalidate_dashboard_cache(request.user.id)
    return _bulk_response(request, count, verb="archived")


@login_required
@ratelimit(key="user", rate="10/m", method="POST", block=True)
@require_POST
def BulkUnarchiveView(request: HttpRequest) -> HttpResponse:
    """Restore multiple archived records at once.

    Accepts a JSON body with ``{"record_ids": [1, 2, 3]}`` and restores
    all inactive records belonging to the user.
    """
    record_ids, error = parse_record_ids(request)
    if error:
        return error

    try:
        count = bulk_toggle_archive(record_ids=record_ids, user=request.user, archive=False)  # type: ignore[arg-type]
    except BulkLimitExceededError as exc:
        return HttpResponse(
            json.dumps({"error": str(exc)}), status=400, content_type="application/json"
        )

    invalidate_dashboard_cache(request.user.id)
    return _bulk_response(request, count, verb="restored")
