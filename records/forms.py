"""Django forms for creating and editing records, folders, and manual merges.

Handles field validation, maxlength attributes for the UI, and conditional
business-requirement rules (e.g. notes and payment method are mandatory for
expense receipts and invoices).
"""

from typing import ClassVar

from django import forms
from django.core.exceptions import ValidationError
from django.forms.utils import flatatt
from django.utils import timezone
from django.utils.html import format_html

from core.currencies import CURRENCY_CHOICES

from .models import Folder, Record

CURRENCY_WIDGET_ATTRS = {
    "class": "w-full text-xs font-semibold bg-transparent border-transparent focus:outline-hidden cursor-pointer",
}


class FolderForm(forms.ModelForm):
    """Simple ModelForm for creating and renaming folders."""

    class Meta:
        model = Folder
        fields: ClassVar[list[str]] = ["name"]
        widgets: ClassVar[dict[str, object]] = {
            "name": forms.TextInput(
                attrs={
                    "class": "w-full bg-white dark:bg-zinc-950 border border-zinc-200/50 dark:border-zinc-800/50 py-2.5 px-3.5 text-xs rounded-xl dark:text-zinc-100 text-zinc-900 placeholder-zinc-400 dark:placeholder-zinc-500 focus:outline-none focus:border-zinc-400 dark:focus:border-zinc-600 transition-all duration-150 char-limit",
                    "placeholder": "e.g., General expenses, Vacation...",
                    "maxlength": "255",
                    "data-maxlength": "255",
                }
            )
        }


class TrimmedTextarea(forms.Textarea):
    """Textarea widget that renders without Django's default "value" attribute.

    Prevents whitespace from being injected into the "<textarea>"
    element on initial render, which would cause cursor-position issues
    and extra trailing newlines.
    """

    def render(self, name, value, attrs=None, renderer=None):  # noqa: ARG002
        if value is None:
            value = ""
        attrs = self.build_attrs(self.attrs, attrs)
        attrs["name"] = name
        return format_html("<textarea{}>{}</textarea>", flatatt(attrs), value)


_MAXLENGTH_HELP = {
    "title": 255,
    "merchant": 255,
    "notes": 500,
    "payment_method": 255,
    "nickname": 255,
}


def _with_maxlength(field: forms.Field, limit: int) -> None:
    existing = field.widget.attrs.get("class", "")
    field.widget.attrs["maxlength"] = str(limit)
    field.widget.attrs["data-maxlength"] = str(limit)
    field.widget.attrs["class"] = f"{existing} char-limit"


class BaseRecordForm(forms.ModelForm):
    """Shared form logic for creating and editing Record instances.

    Enforces business rules common to both creation and update flows:
    - Transaction date must not be in the future.
    - Balance must be non-negative.
    - Expiry date must be on or after the transaction date.
    - Expense receipts and invoices require a business purpose (notes) and
      payment method.
    """

    title = forms.CharField(max_length=255, required=True)
    products = forms.CharField(widget=TrimmedTextarea, required=False)
    merchant = forms.CharField(max_length=255, required=True)
    balance = forms.DecimalField(max_digits=10, decimal_places=2, required=True)
    transaction_date = forms.DateField(
        widget=forms.DateInput(attrs={"type": "date"}), required=True
    )
    expiry_date = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}), required=False)
    record_type = forms.ChoiceField(
        choices=Record.RecordTypes.choices,
        required=True,
    )
    notes = forms.CharField(
        widget=TrimmedTextarea,
        required=False,
        max_length=500,
        label="Business Purpose / Notes",
    )
    payment_method = forms.CharField(
        max_length=255,
        required=False,
    )
    nickname = forms.CharField(
        max_length=255,
        required=False,
        label="Nickname",
    )
    folder = forms.ModelChoiceField(
        queryset=Folder.objects.none(),
        required=False,
        widget=forms.Select(
            attrs={
                "class": "w-full text-xs font-semibold bg-transparent border-transparent focus:outline-hidden cursor-pointer",
            }
        ),
    )
    currency = forms.ChoiceField(
        choices=[("", "—"), *list(CURRENCY_CHOICES)],
        required=True,
        widget=forms.Select(attrs=CURRENCY_WIDGET_ATTRS),
    )

    class Meta:
        model = Record
        fields: ClassVar[list[str]] = [
            "title",
            "products",
            "merchant",
            "balance",
            "currency",
            "transaction_date",
            "expiry_date",
            "record_type",
            "notes",
            "payment_method",
            "nickname",
            "folder",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for fname, limit in _MAXLENGTH_HELP.items():
            if fname in self.fields:
                _with_maxlength(self.fields[fname], limit)

    def clean_transaction_date(self):
        """Reject transaction dates set in the future."""
        transaction_date = self.cleaned_data.get("transaction_date")
        if transaction_date and transaction_date > timezone.localdate():
            raise ValidationError("Transaction date cannot be in the future.")
        return transaction_date

    def clean_balance(self):
        """Reject negative balances."""
        balance = self.cleaned_data.get("balance")
        if balance is not None and balance < 0:
            raise ValidationError("Balance cannot be negative.")
        return balance

    def clean(self):
        """Cross-field validation: expiry vs. transaction date and record-type requirements."""
        cleaned_data = super().clean()
        expiry_date = cleaned_data.get("expiry_date")
        transaction_date = cleaned_data.get("transaction_date")
        if expiry_date and transaction_date and expiry_date < transaction_date:
            raise ValidationError({"expiry_date": "Expiry date cannot be before transaction date."})
        notes = cleaned_data.get("notes", "")
        payment_method = cleaned_data.get("payment_method", "")
        record_type = cleaned_data.get("record_type")
        if record_type in (
            Record.RecordTypes.EXPENSE_RECEIPT,
            Record.RecordTypes.VENDOR_INVOICE,
            Record.RecordTypes.CUSTOMER_INVOICE,
        ):
            if not notes or not notes.strip():
                raise ValidationError(
                    {"notes": "Business purpose is required for this record type."}
                )
            if not payment_method or not payment_method.strip():
                raise ValidationError(
                    {"payment_method": "Payment method is required for this record type."}
                )
        return cleaned_data


class AddRecordForm(BaseRecordForm):
    """Form for creating a new record. Scopes the folder dropdown to the current user."""

    def __init__(self, *args, **kwargs):
        user = kwargs.pop("user", None)
        super().__init__(*args, **kwargs)
        if "folder" in self.fields:
            self.fields["folder"].queryset = Folder.objects.none()
            self.fields["folder"].required = False
            self.fields["folder"].empty_label = "Unfiled"
            if user is not None:
                self.fields["folder"].queryset = Folder.objects.filter(user=user)
        if "currency" in self.fields and not self.initial.get("currency") and user is not None:
            user_currency = getattr(user.settings, "default_currency", "usd")
            self.initial["currency"] = user_currency


class RecordUpdateForm(BaseRecordForm):
    """Form for editing an existing record. Disables payment_method for Plaid records."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        user = self.instance.user if self.instance and self.instance.pk else None
        if user:
            self.fields["folder"].queryset = Folder.objects.filter(user=user)
            self.fields["folder"].required = False
            self.fields["folder"].empty_label = "Unfiled"
        instance = getattr(self, "instance", None)
        if instance and instance.pk and instance.is_plaid_record:
            self.fields["payment_method"].disabled = True


class ManualMergeForm(forms.Form):
    """Hidden-field form that pairs a Plaid record with a document record for manual merging."""

    plaid_record_id = forms.IntegerField(
        widget=forms.HiddenInput(attrs={"id": "id_plaid_record_id"})
    )
    document_record_id = forms.IntegerField(
        widget=forms.HiddenInput(attrs={"id": "id_document_record_id"})
    )

    def __init__(self, *args, **kwargs):
        self.user = kwargs.pop("user")
        super().__init__(*args, **kwargs)
