import pytest

from core import config
from core.money import fet, group_indian, inr, paise_to_atestfet, percent_of, round_to_rupee, to_paise


@pytest.mark.parametrize("n, s", [
    (0, "0"), (999, "999"), (1000, "1,000"), (12345, "12,345"), (125000, "1,25,000"),
    (1234567, "12,34,567"), (123456789, "12,34,56,789"),
])
def test_group_indian(n, s):
    assert group_indian(n) == s


@pytest.mark.parametrize("paise, s", [
    (12500000, "₹1,25,000"), (420000, "₹4,200"), (420050, "₹4,200.50"), (5, "₹0.05"),
    (-2200000, "-₹22,000"), (0, "₹0"),
])
def test_inr(paise, s):
    assert inr(paise) == s


def test_inr_always_decimals():
    assert inr(420000, always_decimals=True) == "₹4,200.00"


@pytest.mark.parametrize("rupees, paise", [
    (4200, 420000), ("4,200", 420000), ("₹4,200.5", 420050), (0.1, 10), (19.995, 2000), ("0.005", 1),
])
def test_to_paise(rupees, paise):
    assert to_paise(rupees) == paise


def test_round_to_rupee():
    assert round_to_rupee(420049) == (420000, -49)
    assert round_to_rupee(420050) == (420100, 50)
    assert round_to_rupee(420000) == (420000, 0)


def test_percent_of():
    assert percent_of(420000, 50) == 210000
    assert percent_of(1900000, 1) == 19000
    assert percent_of(333, 50) == 167          # half up


def test_fet_conversion(monkeypatch):
    monkeypatch.setattr(config, "DEMO_INR_PER_FET", 10000)
    atto = paise_to_atestfet(1900000)            # ₹19,000
    assert atto == 19 * 10**17
    assert fet(atto) == "1.9 FET"
    assert fet(paise_to_atestfet(1919000)) == "1.919 FET"
