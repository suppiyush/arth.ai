import asyncio

from agents.collections import logic


def test_upi_link_encoding():
    link = logic.upi_link("sharmabuilding@upi", "Sharma Building Supplies", 540000, "ARTH/26-27/0001")
    assert link == ("upi://pay?pa=sharmabuilding%40upi&pn=Sharma%20Building%20Supplies&am=5400.00&cu=INR"
                    "&tn=ARTH%2F26-27%2F0001")


def test_wa_link_keeps_digits_only():
    assert logic.wa_link("+91 98290-12345", "hi there") == "https://wa.me/919829012345?text=hi%20there"
    assert logic.wa_link(None, "x") == "https://wa.me/?text=x"


def test_qr_is_small_png():
    uri = logic.qr_data_uri("upi://pay?pa=a@b&am=1.00")
    assert uri.startswith("data:image/png;base64,") and len(uri) < 4000


def fake_explain(reply):
    async def explain(instruction, data, temperature=0.3):
        if isinstance(reply, Exception):
            raise reply
        return reply
    return explain


def test_template_from_asi1_is_used(monkeypatch):
    good = "Namaste {NAME} ji! {SHOP} se: {AMOUNT} baaki{BILLS}. Pay here: {LINK}"
    monkeypatch.setattr(logic.llm, "explain", fake_explain(good))
    assert asyncio.run(logic.reminder_template("Shop", "Ramesh")) == good


def test_bad_templates_fall_back(monkeypatch):
    for reply in ("Namaste {NAME}, pay ₹5400 now {AMOUNT} {LINK}",     # a number the model made up
                  "Namaste {NAME}, please pay.",                       # placeholders missing
                  ConnectionError()):                                  # ASI1 down
        monkeypatch.setattr(logic.llm, "explain", fake_explain(reply))
        assert asyncio.run(logic.reminder_template("Shop", "R")) == logic.FALLBACK_TEMPLATE
