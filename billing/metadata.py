from dataclasses import dataclass

from . import features


@dataclass
class ProductMetadata:
    """
    Metadata for a Stripe product.
    """

    stripe_id: str
    name: str
    features: list[str]
    description: str = ""
    is_default: bool = False


PAPERTRAIL_PRO = ProductMetadata(
    stripe_id="prod_UynmT2hCmUF08u",
    name="Papertrail Pro",
    description="For small businesses and teams",
    is_default=True,
    features=[
        features.UNLIMITED_SCANS,
        features.BANK_TRANSACTION_SYNC,
        features.QUICK_REIMBURSEMENT_REQUEST,
    ],
)

PRODUCTS = {
    PAPERTRAIL_PRO.stripe_id: PAPERTRAIL_PRO,
}
