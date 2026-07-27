"""ISO 4217 currency definitions used across the application.

Each entry maps a currency code to its display name and symbol.
Stripe-compatible codes are used as keys (lowercase).
"""

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
    ("kes", "KEs – Kenyan Shilling"),
    ("ghc", "GHS – Ghanaian Cedi"),
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
    ("uzb", "UZS – Uzbekistani Som"),
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
    "ghc": "GH₵",
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
    "uzb": "so'm",
}

# Stripe uses lowercase 3-letter codes
DEFAULT_CURRENCY = "usd"


def get_currency_symbol(code: str) -> str:
    """Return the display symbol for a given currency code."""
    return CURRENCY_SYMBOLS.get(code, code.upper() + " ")


def format_currency(amount, code: str) -> str:
    """Format a decimal amount with the appropriate currency symbol."""
    from decimal import Decimal

    if amount is None:
        return "—"
    symbol = get_currency_symbol(code)
    amount = Decimal(str(amount))
    if code == "jpy":
        return f"{symbol}{amount:,.0f}"
    return f"{symbol}{amount:,.2f}"
