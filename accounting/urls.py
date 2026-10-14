"""URL configuration for the accounting application.

All routes live under the "accounting" namespace. The app
allows for accounting integrations and export options.
"""

from django.urls import path

from . import views

app_name = "accounting"
urlpatterns = [
    path("export_all_excel/", views.ExportExcelAll, name="export_all_to_excel"),
    path(
        "export_selected_excel/",
        views.ExportSelectedExcel,
        name="export_selected_to_excel",
    ),
]
