"""Forms for document upload and metadata editing.

Provides validation for R2 presigned-URL uploads (filename, content type)
and a ModelForm for updating document title, notes, and record association.
"""

from pathlib import Path

from django import forms
from django.core.exceptions import ValidationError

from .models import DocumentData


class R2UploadForm(forms.Form):
    """Validates file metadata before generating a Cloudflare R2 presigned upload URL."""

    filename = forms.CharField(max_length=255, required=True)
    content_type = forms.CharField(max_length=100, required=True)
    notes = forms.CharField(required=False)
    file_size = forms.IntegerField(required=False, min_value=0)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for fname, limit in (("filename", 255), ("content_type", 100)):
            if fname in self.fields:
                self.fields[fname].widget.attrs["maxlength"] = str(limit)
                self.fields[fname].widget.attrs["data-maxlength"] = str(limit)
                cls = self.fields[fname].widget.attrs.get("class", "")
                self.fields[fname].widget.attrs["class"] = f"{cls} char-limit"

    def clean_filename(self):
        """Strip path components and reject invalid filenames like '.' or '..'."""
        filename = Path(self.cleaned_data["filename"]).name
        if not filename or filename in {".", ".."}:
            raise ValidationError("Invalid filename.")
        return filename

    def clean_content_type(self):
        """Validate that the MIME type is within the allowed set of supported formats."""
        content_type = self.cleaned_data["content_type"].lower().split(";")[0].strip()
        allowed = {
            "application/pdf",
            "image/jpeg",
            "image/png",
            "image/webp",
            "image/heic",
            "image/heif",
        }
        if content_type not in allowed:
            raise ValidationError("Unsupported content type.")
        return content_type


class DocumentUpdateForm(forms.ModelForm):
    """ModelForm for editing document title, notes, and associated record."""

    class Meta:
        model = DocumentData
        fields = ["title", "notes", "associated_record"]
        widgets = {
            "title": forms.TextInput(attrs={"maxlength": "200", "data-maxlength": "200"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["associated_record"].required = False
        self.fields["associated_record"].queryset = self.fields[
            "associated_record"
        ].queryset.active()
