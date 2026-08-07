def badge_style(color: str) -> str:
    """Generates consistent Tailwind badge styling for a given color family."""
    return (
        f"bg-{color}-500/10 text-{color}-700 border border-{color}-500/20 "
        f"backdrop-blur-md dark:bg-{color}-500/10 dark:text-{color}-400 dark:border-{color}-500/30"
    )


COLOR_SCHEME: dict[str, str] = {
    "expense_receipt": "emerald",
    "financial_document": "indigo",
    "voucher": "amber",
    "warranty_certificate": "green",
    "vendor_invoice": "blue",
    "customer_invoice": "indigo",
    "loan_document": "red",
    "credit_card_statement": "sky",
    "bank_statement": "cyan",
    "purchase_order": "violet",
    "payslip": "lime",
    "tax_document": "purple",
    "service_contract": "teal",
    "lease_agreement": "orange",
    "insurance_policy": "rose",
    "other": "slate",
}

RECORD_TYPE_COLOR_MAP: dict[str, str] = {
    record_type: badge_style(color) for record_type, color in COLOR_SCHEME.items()
}
