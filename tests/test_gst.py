from datetime import date

import pytest

from core import gst


def test_inclusive_line_sums_exactly():
    lt = gst.line_tax(10, 42000, 28, inclusive=True)          # 10 bags cement @ ₹420 incl. 28%
    assert lt.total_paise == 420000
    assert lt.taxable_paise == 328125 and lt.tax_paise == 91875


def test_exclusive_line_adds_tax():
    lt = gst.line_tax(10, 42000, 18, inclusive=False)
    assert lt.taxable_paise == 420000 and lt.tax_paise == 75600


def test_discount_applies_before_tax():
    assert gst.line_tax(10, 42000, 28, discount_pct=5, inclusive=True).total_paise == 399000
    assert gst.line_tax(10, 42000, 18, discount_pct=5, inclusive=False).taxable_paise == 399000


def test_intra_state_splits_cgst_sgst():
    inv = gst.invoice_tax([(10, 42000, 28)], "08", "08", inclusive=True)
    assert not inv.inter_state
    assert (inv.cgst_paise, inv.sgst_paise, inv.igst_paise) == (45938, 45937, 0)   # odd paisa goes to CGST
    assert inv.taxable_paise + inv.cgst_paise + inv.sgst_paise == inv.total_paise == 420000


def test_inter_state_is_igst():
    inv = gst.invoice_tax([(200, 6800, 18)], "08", "06", inclusive=True)
    assert inv.inter_state and inv.cgst_paise == inv.sgst_paise == 0
    assert inv.taxable_paise == 1152542 and inv.igst_paise == 207458 and inv.total_paise == 1360000


def test_party_without_state_is_local():
    assert not gst.invoice_tax([(1, 10000, 18)], "08", None).inter_state


def test_round_off_line():
    inv = gst.invoice_tax([(3, 3333, 18)], "08", "08", inclusive=False)    # 99.99 + 18.00 = 117.99
    assert inv.taxable_paise == 9999 and inv.cgst_paise + inv.sgst_paise == 1800
    assert inv.total_paise == 11800 and inv.round_off_paise == 1


def test_mixed_rates():
    inv = gst.invoice_tax([(10, 42000, 28), (5, 5500, 5)], "08", "08", inclusive=True)
    assert inv.total_paise == 420000 + 27500
    assert sum(l.total_paise for l in inv.lines) == inv.total_paise


@pytest.mark.parametrize("d, fy", [
    (date(2026, 10, 21), "26-27"), (date(2027, 3, 31), "26-27"), (date(2027, 4, 1), "27-28"),
    (date(2026, 4, 1), "26-27"), (date(2026, 3, 31), "25-26"), (date(2099, 12, 1), "99-00"),
])
def test_financial_year(d, fy):
    assert gst.financial_year(d) == fy


def test_invoice_number():
    assert gst.invoice_number("26-27", 1) == "ARTH/26-27/0001"
    assert gst.invoice_number("26-27", 12345) == "ARTH/26-27/12345"
