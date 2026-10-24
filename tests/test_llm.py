"""extract() with ASI1 replaced by canned replies; no network."""

import asyncio

import pytest

from core import llm
from core.llm import Extraction, ExtractionError, Line, ShopContext

SHOP = ShopContext(items=["Cement", "TMT Bar 12mm", "Sand", "Wall Putty 40kg"],
                   parties=["Ramesh Kumar", "Sunita Devi", "Gupta Cement"])


def run(coro):
    return asyncio.run(coro)


def fake_replies(monkeypatch, *replies):
    """Each _chat call returns the next reply; an Exception instance is raised instead."""
    calls = []
    queue = list(replies)

    async def _chat(messages, temperature=0):
        calls.append(messages)
        r = queue.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    monkeypatch.setattr(llm, "_chat", _chat)
    return calls


def test_valid_reply(monkeypatch):
    fake_replies(monkeypatch, '{"intent": "sale", "party": "Ramesh Kumar", "lines": [{"item": "Cement", '
                              '"qty": 10, "unit": "bag"}], "amount": 4200, "paid_fraction": 0.5, "mode": "cash"}')
    ext = run(llm.extract("Ramesh ko 10 bag cement becha 4200, aadha cash", SHOP))
    assert ext.intent == "sale" and ext.party == "Ramesh Kumar"
    assert ext.total_paise() == 420000
    assert ext.paid_paise(420000) == 210000
    assert ext.missing == []


def test_fenced_reply(monkeypatch):
    fake_replies(monkeypatch, 'Sure!\n```json\n{"intent": "dues"}\n```')
    assert run(llm.extract("kisne paise dene hain", SHOP)).intent == "dues"


def test_repair_attempt(monkeypatch):
    calls = fake_replies(monkeypatch, '{"intent": "sell"}', '{"intent": "sale", "amount": 500}')
    ext = run(llm.extract("500 ka maal becha", SHOP))
    assert ext.intent == "sale" and ext.amount == 500
    assert len(calls) == 2
    assert "not valid" in calls[1][-1]["content"]


def test_two_bad_replies_ask_user(monkeypatch):
    fake_replies(monkeypatch, "no idea", '{"intent": "dance"}')
    with pytest.raises(ExtractionError) as err:
        run(llm.extract("asdf", SHOP))
    assert "didn't get that" in err.value.question                       # "asdf" reads as English


def test_model_missing_is_recomputed(monkeypatch):
    fake_replies(monkeypatch, '{"intent": "payment_in", "amount": 2000, "missing": ["mode", "date"]}')
    ext = run(llm.extract("2000 mile", SHOP))
    assert ext.missing == ["party"]
    assert llm.question_for(ext.missing, "hi") == "Kiske naam pe?"
    assert llm.question_for(ext.missing, "en") == "Which customer or supplier?"


def test_shop_names_in_prompt(monkeypatch):
    calls = fake_replies(monkeypatch, '{"intent": "help"}')
    run(llm.extract("hi", SHOP))
    assert "Ramesh Kumar" in calls[0][0]["content"] and "TMT Bar 12mm" in calls[0][0]["content"]


def test_asi1_down_uses_fallback(monkeypatch):
    fake_replies(monkeypatch, ConnectionError("down"))
    ext = run(llm.extract("Ramesh ko 10 bag cement becha 4200 mein, half paid cash", SHOP))
    assert ext.source == "fallback"
    assert ext.intent == "sale" and ext.party == "Ramesh Kumar"
    assert ext.lines[0].item == "Cement" and ext.lines[0].qty == 10
    assert ext.amount == 4200 and ext.paid_fraction == 0.5 and ext.mode == "cash"


def test_earlier_question_goes_to_asi1(monkeypatch):
    calls = fake_replies(monkeypatch, '{"intent": "payment_in", "party": "Sunita Devi", "amount": 500}')
    earlier = {"text": "verma ko bil bnao 100 kg sariya", "question": "'sariya' mere stock list mein nahi hai."}
    ext = run(llm.extract("sunita se 500 aaye gpay pe", SHOP, earlier))
    prompt = calls[0][1]["content"]
    assert "Earlier message: verma ko bil bnao 100 kg sariya" in prompt and "New message: sunita se 500" in prompt
    assert ext.intent == "payment_in" and not ext.follows_up                   # ASI1 decided it stands alone


def test_asi1_down_short_reply_continues(monkeypatch):
    fake_replies(monkeypatch, ConnectionError("down"), ConnectionError("down"))
    earlier = {"text": "Ramesh ko 10 bag cement becha 4200 mein", "question": "Poora, aadha, ya udhaar?"}
    ext = run(llm.extract("aadha cash", SHOP, earlier))
    assert ext.follows_up and ext.intent == "sale" and ext.paid_fraction == 0.5
    ext = run(llm.extract("Ramesh ka khata dikhao abhi ke abhi please", SHOP, earlier))   # too long to be an answer
    assert not ext.follows_up and ext.intent == "statement"


def test_asi1_down_unknown_message(monkeypatch):
    fake_replies(monkeypatch, ConnectionError("down"))
    with pytest.raises(ExtractionError) as err:
        run(llm.extract("mausam kaisa hai aaj", SHOP))
    assert "phir try" in err.value.question                              # Hinglish message, Hinglish reply


@pytest.mark.parametrize("text, intent", [
    ("help", "help"), ("undo", "undo"), ("udhaar list dikhao", "dues"),
    ("cement kitna bacha hai", "stock_query"), ("50 bag cement mangwa do", "restock"),
])
def test_fallback_intents(text, intent):
    assert llm.fallback_extract(text, SHOP).intent == intent


@pytest.mark.parametrize("text, period", [
    ("Is mahine kitna profit hua?", "this_month"), ("Pichhle hafte ka business kaisa raha?", "last_week"),
    ("last month ka munafa", "last_month"), ("aaj kitni sale hui", "today"), ("business kaisa chal raha hai", "this_month"),
])
def test_fallback_questions(text, period):
    ext = llm.fallback_extract(text, SHOP)
    assert ext.intent == "question" and ext.period == period


def test_daybook_wins_over_question():
    assert llm.fallback_extract("aaj ka hisaab", SHOP).intent == "daybook"


def test_paid_rules():
    e = Extraction(intent="sale", amount=1000)
    assert e.paid_paise(100000) is None                                  # unknown -> agent asks
    assert e.model_copy(update={"mode": "credit"}).paid_paise(100000) == 0
    assert e.model_copy(update={"mode": "upi"}).paid_paise(100000) == 100000
    assert e.model_copy(update={"paid": 300}).paid_paise(100000) == 30000
    assert e.model_copy(update={"paid": 5000}).paid_paise(100000) == 100000   # never more than total


def test_lines_total():
    e = Extraction(intent="bill", lines=[Line(item="Cement", qty=10, rate=420), Line(item="Sand", qty=5, rate=55)])
    assert e.lines_total_paise() == 447500
    assert Extraction(intent="sale", lines=[Line(item="Cement", qty=10)]).lines_total_paise() is None


def test_bad_fraction_rejected():
    with pytest.raises(Exception):
        Extraction(intent="sale", paid_fraction=1.5)


def test_fallback_bill():
    shop = ShopContext(items=["Cement", "TMT Bar 12mm", "TMT Bar 8mm"], parties=["Ramesh Kumar", "Verma Constructions"])
    ext = llm.fallback_extract("Bill bana do Verma Constructions ke liye, 200 kg TMT 12mm", shop)
    assert (ext.intent, ext.party, ext.lines[0].item, ext.lines[0].qty) == ("bill", "Verma Constructions", "TMT Bar 12mm", 200)
    assert llm.fallback_extract("Ramesh ka bill 5 bag cement cash", shop).mode == "cash"


def test_fallback_top_dues():
    assert llm.fallback_extract("Sabse zyada kiska udhar baaki hai?", SHOP).top == 1
    assert llm.fallback_extract("top 3 dues", SHOP).top == 3
    assert llm.fallback_extract("kis kis ka udhaar baaki hai?", SHOP).top is None
    owe = llm.fallback_extract("mujhe sabse zyada kisko dena h", SHOP)
    assert owe.intent == "dues" and owe.payable and owe.top == 1


def test_fallback_discount_needs_a_draft():
    assert llm.fallback_extract("5% discount", SHOP) is None
    draft = ShopContext(items=SHOP.items, parties=SHOP.parties, draft="bill for Ramesh Kumar: 5 bag Cement")
    assert llm.fallback_extract("5% discount", draft).discount_pct == 5
