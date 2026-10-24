"""Insights figures and card from the seeded shop, with ASI1 unavailable (plain template)."""

import pytest

from core import llm
from core.llm import Extraction
from test_orchestrator import card, extracted, io, say, texts  # noqa: F401


@pytest.fixture
def no_asi1(monkeypatch):
    async def down(*_, **__):
        raise RuntimeError("ASI1 down")
    monkeypatch.setattr(llm, "explain", down)


def test_profit_question_answers_from_figures(seeded_db, io, extracted, no_asi1):
    extracted["result"] = Extraction(intent="question", period="this_month")
    reply = say("Is mahine kitna profit hua?", io)[-1]
    p = card(reply)["card_payload"]
    assert card(reply)["card_kind"] == "detail" and p["title"].startswith("Is mahine ka hisaab")
    rows = {r["label"]: r["value"] for r in p["summary_rows"]}
    assert rows["Lena hai / dena hai"] == "₹74,900 / ₹51,600"       # seeded dues
    labels = [c["label"] for c in p["ctas"]]
    assert labels == ["Remind Verma Constructions (₹36,000)", "Restock Cement"]
    assert texts([reply])[0].startswith("Is mahine bikri ₹")


def test_periods():
    from datetime import date
    from agents.insights.logic import period_range
    from core import clock
    start, end, label = period_range("last_week")
    assert label == "Pichhle hafte" and (end - start).days == 6 and start.weekday() == 0 and end < clock.today()
    start, end, _ = period_range("2026-02")
    assert (start, end) == (date(2026, 2, 1), date(2026, 2, 28))
    assert period_range(None)[2] == "Is mahine"


def test_summary_figures_reach_asi1_as_plain_json(seeded_db, io, extracted, monkeypatch):
    """Balances come from the database as Decimal; they must be ints by the time ASI1 words them."""
    import json

    async def explain(instruction, data, temperature=0.3, lang="en"):
        json.dumps(data)                                    # strict: no default=, so a Decimal would fail here
        return f"worded in {lang}"
    monkeypatch.setattr(llm, "explain", explain)
    extracted["result"] = Extraction(intent="question", period="this_month")
    assert texts(say("How much profit this month?", io))[-1] == "worded in en"
    assert texts(say("Is mahine kitna profit hua?", io))[-1] == "worded in hi"
