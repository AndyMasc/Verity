BYTES_PER_GB = 10**9

# Stripe entitlement lookup keys. Must match the lookup_key configured on each
# feature in Stripe, and the feature must be attached to a product for dj-stripe
# to sync it.
RECORD_SHARING_KEY = "record-sharing"
TRANSACTION_SYNC_KEY = "transaction-sync"
REIMBURSEMENT_CREATION_KEY = "reimbursement-creation"

FREE_MONTHLY_UPLOAD_LIMIT = 25
PRO_MONTHLY_UPLOAD_LIMIT = 200
FREE_MONTHLY_SCAN_LIMIT = 15
PRO_MONTHLY_SCAN_LIMIT = 100

# Free plan card copy. Paid-plan bullets come from Stripe.
RECORD_RETENTION = "7 year record retention period"
EXPORT_RECORDS = "Export your records at any time"
LIMITED_SCANS = f"{FREE_MONTHLY_SCAN_LIMIT} quick scans / month"
SUPPORTING_FILE_UPLOAD = "Supporting file uploads"
EXPIRY_REMINDERS = "Record expiry reminders"
UPLOAD_ALLOWANCE = f"{FREE_MONTHLY_UPLOAD_LIMIT} document uploads per month"
