"""Views for folder CRUD operations.

All folder views are scoped to the current user's folders and support
HTMX partial responses for seamless in-page folder management.
"""

import json

import posthog
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import redirect, render
from django.urls import reverse_lazy
from django.views.generic import ListView
from django.views.generic.edit import CreateView, DeleteView, UpdateView

from core.services.dashboard import invalidate_dashboard_cache

from ..forms import FolderForm
from ..models import Folder


def _unfiled_count(user) -> int:
    """Return the count of active records not belonging to any folder."""
    return user.records.filter(folder__isnull=True, is_active=True).count()


def _folder_list_context(user):
    """Build the shared context for rendering the folder list partial."""
    folders = Folder.objects.filter(user=user).with_record_counts()
    return {"folders": folders, "unfiled_count": _unfiled_count(user), "page_obj": None}


class FolderListView(LoginRequiredMixin, ListView):
    """Paginated list of the current user's folders with active-record counts.

    Supports search filtering by folder name and returns an HTMX partial
    when the request is an HTMX swap.
    """

    model = Folder
    template_name = "records/folders.html"
    context_object_name = "folders"
    ordering = ["-created_at"]
    paginate_by = 12

    def get_template_names(self):
        if self.request.headers.get("HX-Request"):
            return ["records/partials/folders/folder_list_partial.html"]
        return [self.template_name]

    def get_queryset(self):
        qs = Folder.objects.filter(user=self.request.user).with_record_counts()
        search_query = self.request.GET.get("search", "").strip()
        if search_query:
            qs = qs.filter(name__icontains=search_query)
        return qs.order_by("-created_at")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["unfiled_count"] = _unfiled_count(self.request.user)
        return context


class CreateFolder(LoginRequiredMixin, CreateView):
    """Create a new folder and refresh the folder list inline via HTMX."""

    model = Folder
    form_class = FolderForm
    template_name = "records/partials/folders/create_folder_modal.html"

    def form_valid(self, form):
        form.instance.user = self.request.user
        self.object = form.save()
        posthog.capture(
            "folder_created",
            distinct_id=str(self.request.user.pk),
        )
        if self.request.headers.get("HX-Request"):
            ctx = _folder_list_context(self.request.user)
            response = render(
                self.request,
                "records/partials/folders/folder_list_partial.html",
                ctx,
            )
            response["HX-Trigger"] = json.dumps({"closeModal": True})
            return response
        return super().form_valid(form)

    def form_invalid(self, form):
        response = super().form_invalid(form)
        if self.request.headers.get("HX-Request"):
            response.status_code = 422
        return response


class FolderUpdateView(LoginRequiredMixin, UpdateView):
    """Inline-edit a folder's name. Returns the updated folder partial via HTMX."""

    model = Folder
    form_class = FolderForm
    template_name = "records/partials/folders/edit_folder_inline.html"
    pk_url_kwarg = "folder_id"

    def get_queryset(self):
        return Folder.objects.filter(user=self.request.user).with_record_counts()

    def form_valid(self, form):
        self.object = form.save()
        if self.request.headers.get("HX-Request"):
            return render(
                self.request,
                "records/partials/folders/folder_item_partial.html",
                {"folder": self.object},
            )
        return super().form_valid(form)

    def form_invalid(self, form):
        response = super().form_invalid(form)
        if self.request.headers.get("HX-Request"):
            response.status_code = 422
        return response


class FolderDeleteView(LoginRequiredMixin, DeleteView):
    """Delete a folder and un-file all its records before removing the row."""

    model = Folder
    pk_url_kwarg = "folder_id"
    success_url = reverse_lazy("records:view_folders")

    def get_queryset(self):
        return Folder.objects.filter(user=self.request.user)

    def delete(self, request, *_, **__):
        folder = self.get_object()
        folder.records.update(folder=None)
        folder.delete()
        invalidate_dashboard_cache(request.user.id)
        posthog.capture(
            "folder_deleted",
            distinct_id=str(request.user.pk),
        )
        if request.headers.get("HX-Request"):
            ctx = _folder_list_context(request.user)
            response = render(
                request,
                "records/partials/folders/folder_list_partial.html",
                ctx,
            )
            response["HX-Trigger"] = json.dumps({"recordChanged": {}})
            return response
        messages.info(request, "Folder deleted. Records unfiled.")
        return redirect(self.success_url)
