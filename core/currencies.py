"""ISO 4217 currency definitions and Stripe minor-unit helpers."""

from decimal import ROUND_HALF_UP, Decimal

DEFAULT_CURRENCY = "usd"

CURRENCY_CHOICES = [
    ("usd", "USD – US Dollar"),
    ("eur", "EUR – Euro"),
    ("gbp", "GBP – British Pound"),
    ("cad", "CAD – Canadian Dollar"),
    ("aud", "AUD – Australian Dollar"),
    ("nzd", "NZD – New Zealand Dollar"),
    ("chf", "CHF – Swiss Franc"),
    ("jpy", "JPY – Japanese Yen"),
    ("sek", "SEK – Swedish Krona"),
    ("nok", "NOK – Norwegian Krone"),
    ("dkk", "DKK – Danish Krone"),
    ("pln", "PLN – Polish Zloty"),
    ("czk", "CZK – Czech Koruna"),
    ("huf", "HUF – Hungarian Forint"),
    ("ron", "RON – Romanian Leu"),
    ("brl", "BRL – Brazilian Real"),
    ("mxn", "MXN – Mexican Peso"),
    ("ars", "ARS – Argentine Peso"),
    ("clp", "CLP – Chilean Peso"),
    ("cop", "COP – Colombian Peso"),
    ("pen", "PEN – Peruvian Sol"),
    ("inr", "INR – Indian Rupee"),
    ("sgd", "SGD – Singapore Dollar"),
    ("hkd", "HKD – Hong Kong Dollar"),
    ("twd", "TWD – Taiwan Dollar"),
    ("krw", "KRW – South Korean Won"),
    ("zar", "ZAR – South African Rand"),
    ("egp", "EGP – Egyptian Pound"),
    ("ngn", "NGN – Nigerian Naira"),
    ("kes", "KES – Kenyan Shilling"),
    ("ghs", "GHS – Ghanaian Cedi"),
    ("aed", "AED – UAE Dirham"),
    ("sar", "SAR – Saudi Riyal"),
    ("qar", "QAR – Qatari Riyal"),
    ("kwd", "KWD – Kuwaiti Dinar"),
    ("bhd", "BHD – Bahraini Dinar"),
    ("omr", "OMR – Omani Rial"),
    ("ils", "ILS – Israeli Shekel"),
    ("jod", "JOD – Jordanian Dinar"),
    ("lbp", "LBP – Lebanese Pound"),
    ("thb", "THB – Thai Baht"),
    ("myr", "MYR – Malaysian Ringgit"),
    ("idr", "IDR – Indonesian Rupiah"),
    ("php", "PHP – Philippine Peso"),
    ("vnd", "VND – Vietnamese Dong"),
    ("pkr", "PKR – Pakistani Rupee"),
    ("bdt", "BDT – Bangladeshi Taka"),
    ("lkr", "LKR – Sri Lankan Rupee"),
    ("npr", "NPR – Nepalese Rupee"),
    ("mmk", "MMK – Myanmar Kyat"),
    ("rub", "RUB – Russian Ruble"),
    ("uah", "UAH – Ukrainian Hryvnia"),
    ("try", "TRY – Turkish Lira"),
    ("gel", "GEL – Georgian Lari"),
    ("azn", "AZN – Azerbaijani Manat"),
    ("kzt", "KZT – Kazakhstani Tenge"),
    ("uzs", "UZS – Uzbekistani Som"),
]

CURRENCY_SYMBOLS = {
    "usd": "$",
    "eur": "€",
    "gbp": "£",
    "cad": "C$",
    "aud": "A$",
    "nzd": "NZ$",
    "chf": "CHF ",
    "jpy": "¥",
    "sek": "kr",
    "nok": "kr",
    "dkk": "kr",
    "pln": "zł",
    "czk": "Kč",
    "huf": "Ft",
    "ron": "lei",
    "brl": "R$",
    "mxn": "MX$",
    "ars": "AR$",
    "clp": "CL$",
    "cop": "CO$",
    "pen": "S/",
    "inr": "₹",
    "sgd": "S$",
    "hkd": "HK$",
    "twd": "NT$",
    "krw": "₩",
    "zar": "R",
    "egp": "E£",
    "ngn": "₦",
    "kes": "KSh",
    "ghs": "GH₵",
    "aed": "د.إ",
    "sar": "﷼",
    "qar": "﷼",
    "kwd": "د.ك",
    "bhd": "BD",
    "omr": "﷼",
    "ils": "₪",
    "jod": "JD",
    "lbp": "L£",
    "thb": "฿",
    "myr": "RM",
    "idr": "Rp",
    "php": "₱",
    "vnd": "₫",
    "pkr": "₨",
    "bdt": "৳",
    "lkr": "Rs",
    "npr": "Rs",
    "mmk": "K",
    "rub": "₽",
    "uah": "₴",
    "try": "₺",
    "gel": "₾",
    "azn": "₼",
    "kzt": "₸",
    "uzs": "so'm",
}

# ISO 4217 currencies with 0 decimal places
ZERO_DECIMAL_CURRENCIES = frozenset(
    {
        "bif",
        "clp",
        "djf",
        "gnf",
        "jpy",
        "kmf",
        "krw",
        "mga",
        "pyg",
        "rwf",
        "ugx",
        "vnd",
        "vuv",
        "xaf",
        "xof",
        "xpf",
    }
)

# ISO 4217 currencies with 3 decimal places
THREE_DECIMAL_CURRENCIES = frozenset({"bhd", "jod", "kwd", "omr", "tnd"})


def get_currency_decimals(currency: str) -> int:
    """Returns the number of decimal places for a given currency code."""
    code = (currency or DEFAULT_CURRENCY).lower()
    if code in ZERO_DECIMAL_CURRENCIES:
        return 0
    if code in THREE_DECIMAL_CURRENCIES:
        return 3
    return 2


def get_currency_symbol(currency: str) -> str:
    """Returns the symbol for a given currency code or default uppercase fallback."""
    code = (currency or DEFAULT_CURRENCY).lower()
    return CURRENCY_SYMBOLS.get(code, code.upper())


def format_currency(amount: Decimal | float | int, currency: str) -> str:
    """Formats a numerical amount with its currency symbol and correct decimal places."""
    if amount is None:
        return ""

    code = (currency or DEFAULT_CURRENCY).lower()
    symbol = get_currency_symbol(code)
    decimals = get_currency_decimals(code)
    quant_target = Decimal("1") if decimals == 0 else Decimal(f"0.{'0' * decimals}")
    amount = Decimal(str(amount)).quantize(quant_target, rounding=ROUND_HALF_UP)
    return f"{symbol}{amount:,.{decimals}f}"


def to_stripe_amount(amount: Decimal, currency: str) -> int:
    """Converts major currency units (e.g. 8.17 SGD) to Stripe minor units (cents/pesos/fils)."""
    decimals = get_currency_decimals(currency)
    multiplier = Decimal(10**decimals)
    return int((Decimal(str(amount)) * multiplier).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def from_stripe_amount(amount_cents: int, currency: str) -> Decimal:
    """Converts Stripe minor units back to major units."""
    decimals = get_currency_decimals(currency)
    divisor = Decimal(10**decimals)
    return Decimal(amount_cents) / divisor
