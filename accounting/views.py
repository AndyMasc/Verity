import logging

from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.views.decorators.http import require_POST
from django_ratelimit.decorators import ratelimit

from core.apps import posthog_client
from records.models import Record
from Verity.views import parse_record_ids

from .services import export_records_to_excel

logger = logging.getLogger(__name__)


XLSX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _capture_export(request: HttpRequest, event: str, count: int) -> None:
    """Record that a user pulled their records out as a spreadsheet."""
    if posthog_client is None:
        return
    posthog_client.capture(
        event,
        distinct_id=str(request.user.pk),
        properties={"record_count": count},
    )


def _xlsx_response(excel_data: bytes) -> HttpResponse:
    response = HttpResponse(excel_data, content_type=XLSX_CONTENT_TYPE)
    response["Content-Disposition"] = 'attachment; filename="Record_export.xlsx"'
    return response


@login_required
@ratelimit(key="user", rate="5/h", method="GET", block=True)
def ExportExcelAll(request: HttpRequest) -> HttpResponse:
    try:
        queryset = Record.objects.filter(user=request.user)
        excel_data = export_records_to_excel(queryset=queryset)
    except Exception:
        logger.exception("Failed to export records for user %s", request.user.pk)
        return HttpResponse("Export failed. Please try again later.", status=500)

    _capture_export(request, "all_records_exported", queryset.count())
    return _xlsx_response(excel_data)


@login_required
@ratelimit(key="user", rate="10/h", method="POST", block=True)
@require_POST
def ExportSelectedExcel(request: HttpRequest) -> HttpResponse:
    """Export a subset of records to xlsx.

    Accepts a JSON body with "{"record_ids": [1, 2, 3]}" and exports
    only those records belonging to the user.
    """
    record_ids, error = parse_record_ids(request)
    if error:
        return error

    try:
        queryset = Record.objects.filter(id__in=record_ids, user=request.user)
        if record_ids and not queryset.exists():
            return JsonResponse(
                {"error": "None of the selected records can be exported."}, status=400
            )
        excel_data = export_records_to_excel(queryset=queryset)
    except Exception:
        logger.exception(
            "Failed to export selected records for user %s", request.user.pk
        )
        return HttpResponse("Export failed. Please try again later.", status=500)

    exported = queryset.count()
    _capture_export(request, "selected_records_exported", exported)
    return _xlsx_response(excel_data)
