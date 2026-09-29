"""ISO 4217 currency definitions and Stripe minor-unit helpers."""

from decimal import ROUND_HALF_UP, Decimal

DEFAULT_CURRENCY = "usd"

CURRENCY_CHOICES = sorted(
    [
        ("usd", "🇺🇸 American Dollar (USD)"),
        ("eur", "🇪🇺 Euro (EUR)"),
        ("gbp", "🇬🇧 British Pound (GBP)"),
        ("cad", "🇨🇦 Canadian Dollar (CAD)"),
        ("aud", "🇦🇺 Australian Dollar (AUD)"),
        ("nzd", "🇳🇿 New Zealand Dollar (NZD)"),
        ("chf", "🇨🇭 Swiss Franc (CHF)"),
        ("jpy", "🇯🇵 Japanese Yen (JPY)"),
        ("sek", "🇸🇪 Swedish Krona (SEK)"),
        ("nok", "🇳🇴 Norwegian Krone (NOK)"),
        ("dkk", "🇩🇰 Danish Krone (DKK)"),
        ("pln", "🇵🇱 Polish Zloty (PLN)"),
        ("czk", "🇨🇿 Czech Koruna (CZK)"),
        ("huf", "🇭🇺 Hungarian Forint (HUF)"),
        ("ron", "🇷🇴 Romanian Leu (RON)"),
        ("brl", "🇧🇷 Brazilian Real (BRL)"),
        ("mxn", "🇲🇽 Mexican Peso (MXN)"),
        ("ars", "🇦🇷 Argentine Peso (ARS)"),
        ("clp", "🇨🇱 Chilean Peso (CLP)"),
        ("cop", "🇨🇴 Colombian Peso (COP)"),
        ("pen", "🇵🇪 Peruvian Sol (PEN)"),
        ("inr", "🇮🇳 Indian Rupee (INR)"),
        ("sgd", "🇸🇬 Singapore Dollar (SGD)"),
        ("hkd", "🇭🇰 Hong Kong Dollar (HKD)"),
        ("twd", "🇹🇼 Taiwan Dollar (TWD)"),
        ("krw", "🇰🇷 South Korean Won (KRW)"),
        ("zar", "🇿🇦 South African Rand (ZAR)"),
        ("egp", "🇪🇬 Egyptian Pound (EGP)"),
        ("ngn", "🇳🇬 Nigerian Naira (NGN)"),
        ("kes", "🇰🇪 Kenyan Shilling (KES)"),
        ("ghs", "🇬🇭 Ghanaian Cedi (GHS)"),
        ("aed", "🇦🇪 UAE Dirham (AED)"),
        ("sar", "🇸🇦 Saudi Riyal (SAR)"),
        ("qar", "🇶🇦 Qatari Riyal (QAR)"),
        ("kwd", "🇰🇼 Kuwaiti Dinar (KWD)"),
        ("bhd", "🇧🇭 Bahraini Dinar (BHD)"),
        ("omr", "🇴🇲 Omani Rial (OMR)"),
        ("ils", "🇮🇱 Israeli Shekel (ILS)"),
        ("jod", "🇯🇴 Jordanian Dinar (JOD)"),
        ("lbp", "🇱🇧 Lebanese Pound (LBP)"),
        ("thb", "🇹🇭 Thai Baht (THB)"),
        ("myr", "🇲🇾 Malaysian Ringgit (MYR)"),
        ("idr", "🇮🇩 Indonesian Rupiah (IDR)"),
        ("php", "🇵🇭 Philippine Peso (PHP)"),
        ("vnd", "🇻🇳 Vietnamese Dong (VND)"),
        ("pkr", "🇵🇰 Pakistani Rupee (PKR)"),
        ("bdt", "🇧🇩 Bangladeshi Taka (BDT)"),
        ("lkr", "🇱🇰 Sri Lankan Rupee (LKR)"),
        ("npr", "🇳🇵 Nepalese Rupee (NPR)"),
        ("mmk", "🇲🇲 Myanmar Kyat (MMK)"),
        ("rub", "🇷🇺 Russian Ruble (RUB)"),
        ("uah", "🇺🇦 Ukrainian Hryvnia (UAH)"),
        ("try", "🇹🇷 Turkish Lira (TRY)"),
        ("gel", "🇬🇪 Georgian Lari (GEL)"),
        ("azn", "🇦🇿 Azerbaijani Manat (AZN)"),
        ("kzt", "🇰🇿 Kazakhstani Tenge (KZT)"),
        ("uzs", "🇺🇿 Uzbekistani Som (UZS)"),
    ]
)

CURRENCY_SYMBOLS = {
    "usd": "US$",
    "eur": "€",
    "gbp": "£",
    "cad": "CA$",
    "aud": "AU$",
    "nzd": "NZ$",
    "chf": "CHF",
    "jpy": "JP¥",
    "sek": "SEK kr",
    "nok": "NOK kr",
    "dkk": "DKK kr",
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
    "sgd": "SG$",
    "hkd": "HK$",
    "twd": "NT$",
    "krw": "₩",
    "zar": "R",
    "egp": "E£",
    "ngn": "₦",
    "kes": "KSh",
    "ghs": "GH₵",
    "aed": "AED",
    "sar": "SAR",
    "qar": "QAR",
    "kwd": "KWD",
    "bhd": "BHD",
    "omr": "OMR",
    "ils": "₪",
    "jod": "JOD",
    "lbp": "L£",
    "thb": "฿",
    "myr": "RM",
    "idr": "Rp",
    "php": "₱",
    "vnd": "₫",
    "pkr": "PKR ₨",
    "bdt": "৳",
    "lkr": "LKR Rs",
    "npr": "NPR Rs",
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
    return int(
        (Decimal(str(amount)) * multiplier).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )
    )


def from_stripe_amount(amount_cents: int, currency: str) -> Decimal:
    """Converts Stripe minor units back to major units."""
    decimals = get_currency_decimals(currency)
    divisor = Decimal(10**decimals)
    return Decimal(amount_cents) / divisor
