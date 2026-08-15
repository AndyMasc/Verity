import logging

from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.views.decorators.http import require_POST
from django_ratelimit.decorators import ratelimit

from records.models import Record
from Verity.views import parse_record_ids

from .services import export_records_to_excel

logger = logging.getLogger(__name__)


@login_required
@ratelimit(key="user", rate="5/h", method="GET", block=True)
def ExportExcelAll(request: HttpRequest) -> HttpResponse:
    try:
        queryset = Record.objects.filter(user=request.user)
        excel_data = export_records_to_excel(queryset=queryset)
    except Exception:
        logger.exception("Failed to export records for user %s", request.user.pk)
        return HttpResponse("Export failed. Please try again later.", status=500)

    response = HttpResponse(
        excel_data,
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    response["Content-Disposition"] = 'attachment; filename="Record_export.xlsx"'
    return response


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
        excel_data = export_records_to_excel(queryset=queryset)
    except Exception:
        logger.exception("Failed to export selected records for user %s", request.user.pk)
        return HttpResponse("Export failed. Please try again later.", status=500)

    response = HttpResponse(
        excel_data,
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    response["Content-Disposition"] = 'attachment; filename="Record_export.xlsx"'
    return response
