from django.db.models import QuerySet

from records.models import Record

from .resources import RecordResource


def export_records_to_excel(queryset: QuerySet[Record]) -> bytes:
    """Export a specific queryset of records to xlsx."""
    dataset = RecordResource().export(queryset=queryset)
    return dataset.xlsx
