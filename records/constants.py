"""Constants for the records app.

Separated from models.py to keep presentation-layer data (Tailwind CSS classes)
out of the database layer.
"""

RECORD_TYPE_COLOR_MAP: dict[str, str] = {
    "expense_receipt": "bg-emerald-500/10 text-emerald-700 border border-emerald-500/20 backdrop-blur-md dark:bg-emerald-500/10 dark:text-emerald-400 dark:border-emerald-500/30",
    "financial_document": "bg-indigo-900/10 text-indigo-900 border border-indigo-500/30 backdrop-blur-md dark:bg-indigo-400/10 dark:text-indigo-300 dark:border-indigo-500/40",
    "voucher": "bg-amber-500/10 text-amber-700 border border-amber-500/20 backdrop-blur-md dark:bg-amber-500/10 dark:text-amber-400 dark:border-amber-500/30",
    "warranty_certificate": "bg-green-500/10 text-green-700 border border-green-500/20 backdrop-blur-md dark:bg-green-500/10 dark:text-green-400 dark:border-green-500/30",
    "vendor_invoice": "bg-blue-500/10 text-blue-700 border border-blue-500/20 backdrop-blur-md dark:bg-blue-500/10 dark:text-blue-400 dark:border-blue-500/30",
    "customer_invoice": "bg-indigo-500/10 text-indigo-700 border border-indigo-500/20 backdrop-blur-md dark:bg-indigo-500/10 dark:text-indigo-400 dark:border-indigo-500/30",
    "loan_document": "bg-red-500/10 text-red-700 border border-red-500/20 backdrop-blur-md dark:bg-red-500/10 dark:text-red-400 dark:border-red-500/30",
    "credit_card_statement": "bg-sky-500/10 text-sky-700 border border-sky-500/20 backdrop-blur-md dark:bg-sky-500/10 dark:text-sky-400 dark:border-sky-500/30",
    "bank_statement": "bg-cyan-500/10 text-cyan-700 border border-cyan-500/20 backdrop-blur-md dark:bg-cyan-500/10 dark:text-cyan-400 dark:border-cyan-500/30",
    "purchase_order": "bg-violet-500/10 text-violet-700 border border-violet-500/20 backdrop-blur-md dark:bg-violet-500/10 dark:text-violet-400 dark:border-violet-500/30",
    "payslip": "bg-lime-500/10 text-lime-700 border border-lime-500/20 backdrop-blur-md dark:bg-lime-500/10 dark:text-lime-400 dark:border-lime-500/30",
    "tax_document": "bg-purple-500/10 text-purple-700 border border-purple-500/20 backdrop-blur-md dark:bg-purple-500/10 dark:text-purple-400 dark:border-purple-500/30",
    "service_contract": "bg-teal-500/10 text-teal-700 border border-teal-500/20 backdrop-blur-md dark:bg-teal-500/10 dark:text-teal-400 dark:border-teal-500/30",
    "lease_agreement": "bg-orange-500/10 text-orange-700 border border-orange-500/20 backdrop-blur-md dark:bg-orange-500/10 dark:text-orange-400 dark:border-orange-500/30",
    "insurance_policy": "bg-rose-500/10 text-rose-700 border border-rose-500/20 backdrop-blur-md dark:bg-rose-500/10 dark:text-rose-400 dark:border-rose-500/30",
    "other": "bg-slate-500/10 text-slate-700 border border-slate-500/20 backdrop-blur-md dark:bg-slate-500/10 dark:text-slate-400 dark:border-slate-500/30",
}
