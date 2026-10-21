"""Money is integer paise everywhere; rupees exist only at the edges (LLM input, card text).

FET amounts are in atestfet (1 FET = 10**18 atestfet), converted at the fixed DEMO_INR_PER_FET rate.
"""

from decimal import ROUND_HALF_UP, Decimal

from core import config

ATTO = 10**18


def to_paise(rupees: float | int | str | Decimal) -> int:
    """₹ as spoken or typed -> paise, rounding half up. Floats go through str() so 0.1 stays 0.1."""
    value = Decimal(str(rupees).replace(",", "").replace("₹", "").strip())
    return int((value * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def to_rupees(paise: int) -> Decimal:
    return Decimal(paise) / 100


def group_indian(n: int) -> str:
    """1234567 -> '12,34,567' (last three digits, then pairs)."""
    s = str(n)
    if len(s) <= 3:
        return s
    head, tail = s[:-3], s[-3:]
    pairs = []
    while len(head) > 2:
        pairs.insert(0, head[-2:])
        head = head[:-2]
    return ",".join([head, *pairs, tail])


def inr(paise: int, always_decimals: bool = False) -> str:
    """12500000 -> '₹1,25,000'; paise are shown only when non-zero (or always_decimals)."""
    sign = "-" if paise < 0 else ""
    rupees, rem = divmod(abs(paise), 100)
    text = f"{sign}₹{group_indian(rupees)}"
    if rem or always_decimals:
        text += f".{rem:02d}"
    return text


def round_to_rupee(paise: int) -> tuple[int, int]:
    """Return (rounded_paise, round_off_paise); 50 paise rounds up, as on GST invoices."""
    rounded = int((Decimal(paise) / 100).quantize(Decimal(1), rounding=ROUND_HALF_UP)) * 100
    return rounded, rounded - paise


def percent_of(paise: int, pct: float | Decimal) -> int:
    return int((Decimal(paise) * Decimal(str(pct)) / 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def qty(q, unit: str | None = None) -> str:
    """Decimal('12.000'), 'bag' -> '12 bag'."""
    text = format(Decimal(str(q)).normalize(), "f")
    return f"{text} {unit}" if unit else text


def paise_to_atestfet(paise: int) -> int:
    """₹ at the demo rate -> atestfet. ₹19,000 at 10,000 ₹/FET -> 1.9 FET."""
    return paise * ATTO // (config.DEMO_INR_PER_FET * 100)


def fet(atestfet: int, places: int = 4) -> str:
    """1900000000000000000 -> '1.9 FET'."""
    value = (Decimal(atestfet) / ATTO).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    text = format(value.normalize(), "f")
    return f"{text} FET"
