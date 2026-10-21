"""GST maths and invoice numbering. All amounts are paise.

Each line is rounded to the paisa; the invoice total is rounded to the rupee with a round-off line.
Same state as the shop -> CGST + SGST (half each); different state -> IGST.
With PRICES_INCLUDE_GST the rate already contains the tax and the taxable value is backed out of it,
so a line always sums exactly to qty x rate (less discount).
"""

from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from core import config
from core.money import round_to_rupee

INVOICE_PREFIX = "ARTH"


def _round(x: Decimal) -> int:
    return int(x.quantize(Decimal(1), rounding=ROUND_HALF_UP))


@dataclass
class LineTax:
    qty: Decimal
    rate_paise: int
    gst_rate: Decimal
    taxable_paise: int
    tax_paise: int

    @property
    def total_paise(self) -> int:
        return self.taxable_paise + self.tax_paise


@dataclass
class InvoiceTax:
    lines: list[LineTax] = field(default_factory=list)
    taxable_paise: int = 0
    cgst_paise: int = 0
    sgst_paise: int = 0
    igst_paise: int = 0
    round_off_paise: int = 0
    total_paise: int = 0
    inter_state: bool = False


def line_tax(qty, rate_paise: int, gst_rate, discount_pct=0, inclusive: bool | None = None) -> LineTax:
    inclusive = config.PRICES_INCLUDE_GST if inclusive is None else inclusive
    qty, gst_rate = Decimal(str(qty)), Decimal(str(gst_rate))
    gross = Decimal(rate_paise) * qty * (100 - Decimal(str(discount_pct or 0))) / 100
    if inclusive:
        total = _round(gross)
        taxable = _round(Decimal(total) * 100 / (100 + gst_rate))
        tax = total - taxable
    else:
        taxable = _round(gross)
        tax = _round(Decimal(taxable) * gst_rate / 100)
    return LineTax(qty, rate_paise, gst_rate, taxable, tax)


def invoice_tax(lines: list[tuple], shop_state: str | None, party_state: str | None, discount_pct=0,
                inclusive: bool | None = None) -> InvoiceTax:
    """lines: (qty, rate_paise, gst_rate). A party with no state code is treated as local."""
    inter = bool(party_state and shop_state and party_state != shop_state)
    inv = InvoiceTax(inter_state=inter)
    for qty, rate, gst_rate in lines:
        lt = line_tax(qty, rate, gst_rate, discount_pct, inclusive)
        inv.lines.append(lt)
        inv.taxable_paise += lt.taxable_paise
        if inter:
            inv.igst_paise += lt.tax_paise
        else:
            cgst = _round(Decimal(lt.tax_paise) / 2)
            inv.cgst_paise += cgst
            inv.sgst_paise += lt.tax_paise - cgst
    exact = inv.taxable_paise + inv.cgst_paise + inv.sgst_paise + inv.igst_paise
    inv.total_paise, inv.round_off_paise = round_to_rupee(exact)
    return inv


def financial_year(d: date) -> str:
    """April to March: 2026-10-21 -> '26-27', 2027-03-31 -> '26-27', 2027-04-01 -> '27-28'."""
    start = d.year if d.month >= 4 else d.year - 1
    return f"{start % 100:02d}-{(start + 1) % 100:02d}"


def invoice_number(fy: str, n: int) -> str:
    return f"{INVOICE_PREFIX}/{fy}/{n:04d}"
