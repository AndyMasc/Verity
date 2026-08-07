"""Document detail service for context building and record association.

Provides reusable logic for building document detail context (presigned URLs,
record search with pagination) and handling record association updates.
"""

from dataclasses import dataclass

from django.core.paginator import EmptyPage, PageNotAnInteger, Paginator
from django.shortcuts import get_object_or_404

from documents.storage import generate_read_presigned_url
from records.models import Record

RECORD_SEARCH_PAGE_SIZE = 5


@dataclass
class DocumentContext:
    """Pre-built context data for the document detail view."""

    view_url: str
    records: list
    page_obj: object
    is_paginated: bool


class DocumentDetailService:
    """Encapsulates business logic for the document detail view."""

    @staticmethod
    def build_context(document, request) -> DocumentContext:
        """Build the full context needed for the document detail template.

        Generates a presigned view URL and searches/paginates user records
        for association.
        """
        view_url = generate_read_presigned_url(document.filepath)

        records_list = DocumentDetailService._search_records(request)
        paginator = Paginator(records_list, RECORD_SEARCH_PAGE_SIZE)
        page = request.GET.get("page", 1)

        try:
            page_obj = paginator.page(page)
        except PageNotAnInteger:
            page_obj = paginator.page(1)
        except EmptyPage:
            page_obj = paginator.page(paginator.num_pages)

        return DocumentContext(
            view_url=view_url,
            records=page_obj.object_list,
            page_obj=page_obj,
            is_paginated=page_obj.has_other_pages(),
        )

    @staticmethod
    def _search_records(request):
        """Return user records matching the current search query for association."""
        queryset = Record.objects.for_user(request.user).active()
        search_query = request.GET.get("search", "").strip()
        if search_query:
            queryset = queryset.smart_search(search_query)
        return queryset

    @staticmethod
    def associate_record(document, record_id: str, user) -> None:
        """Update a document's record association based on the provided record_id.

        Pass an empty string or whitespace to clear the association.
        Raises 404 if the record_id is non-empty but the record doesn't exist
        or doesn't belong to the user.
        """
        if not record_id:
            document.associated_record = None
        else:
            record = get_object_or_404(Record, pk=record_id, user=user)
            document.associated_record = record
