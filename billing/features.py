BYTES_PER_GB = 10**9

# Pro plan features
PRO_SCAN_LIMIT = 500  # Maximum quick scans per month before fair use review
UNLIMITED_SCANS = f"{PRO_SCAN_LIMIT} quick scans / month"
BANK_TRANSACTION_SYNC = "Bank transaction sync (US, CA, & supported EU institutions)"
QUICK_REIMBURSEMENT_REQUEST = (
    "One-click reimbursement requests"  # Can generate and pay reimbursement requests
)
INCLUDES_ALL_FREE = "All free features"
PRO_STORAGE_LIMIT_GB = 5
PRO_STORAGE_LIMIT = f"{PRO_STORAGE_LIMIT_GB} GB cloud storage"
AUTO_TXN_CATEGORIZATION = "Automatic transaction categorization and matching"
RECORD_SHARING = "Collaborative records"  # Gate for granting record access to others

# Free plan features
RECORD_RETENTION = "7 year record retention period"
FREE_MONTHLY_SCAN_LIMIT = 15
LIMITED_SCANS = f"{FREE_MONTHLY_SCAN_LIMIT} quick scans / month"  # ie, 10-30 / month
SUPPORTING_FILE_UPLOAD = "Supporting file uploads"
EXPIRY_REMINDERS = "Record expiry reminders"
FREE_STORAGE_LIMIT_GB = 0.5
FREE_STORAGE_LIMIT = f"{FREE_STORAGE_LIMIT_GB * 1000} MB cloud storage"

# Storage upgrade tiers
USAGE_BASED_BILLING = "Usage-based billing for storage used beyond your plan's included allowance"
NO_STORAGE_LIMIT = "No storage limit (unlimited cloud storage)"
PRICE = "Free up to your quota. Then, $0.15 per GB per month."
