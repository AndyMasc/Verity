"""List views for documents: main list view."""

from typing import Any

from django.conf import settings
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpResponse
from django.utils.decorators import method_decorator
from django_filters.views import FilterView
from django_ratelimit.decorators import ratelimit

from Verity.views import CachedPaginatorMixin

from ..filters import DocumentFilter
from ..models import DocumentData

LIST_FIELDS = (
    "pk",
    "title",
    "did_ocr",
    "notes",
    "date_added",
    "filepath",
    "file_extension",
    "associated_record_id",
)


class DocumentListView(LoginRequiredMixin, CachedPaginatorMixin, FilterView):
    """Main document listing with search, filtering, and paginated results."""

    model = DocumentData
    template_name = "documents/document_list.html"
    context_object_name = "documents"
    filterset_class = DocumentFilter
    paginate_by = settings.PAGINATE_BY

    @method_decorator(ratelimit(key="user", rate="120/m", method="GET", block=True))
    def dispatch(self, *args: Any, **kwargs: Any) -> HttpResponse:
        return super().dispatch(*args, **kwargs)

    def get_queryset(self):
        qs = (
            DocumentData.objects.for_user(self.request.user)
            .with_record()
            .only(*LIST_FIELDS, "associated_record__title")
            .order_by("-date_added")
        )
        search_query = self.request.GET.get("search", "").strip()
        if search_query:
            return qs.search(search_query)
        return qs

    def get_template_names(self) -> list[str]:
        if self.request.headers.get("HX-Target") == "query-results-container":
            return ["documents/partials/document_list_partial.html"]
        return [self.template_name]
