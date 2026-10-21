"""Read-only queries (stock, statement, day book) and undo across agents, through respond()."""

import json

from test_orchestrator import (RAMESH_SALE, VERMA_BILL, balance, card, extracted, fy_number, io,  # noqa: F401
                               say, stock, texts)

from core.llm import Extraction, Line
from core.money import inr


def section(reply):
    return card(reply)["card_payload"]["root"]


def body_rows(reply):
    groups = [g for g in section(reply)["children"] if g.get("type") == "group"][1:]
    return [[el.get("value") or el.get("label") for el in g["children"]] for g in groups]


def cash(conn):
    return conn.execute("""select coalesce(sum(case when kind in ('sale','receipt') then 1 else -1 end * amount_paise), 0) as c
                           from txn t where mode = 'cash' and not exists (select 1 from action a
                           where a.id = t.action_id and a.status = 'undone')""").fetchone()["c"]


# Queries

def test_stock_table_puts_low_items_first(seeded_db, io, extracted):
    extracted["result"] = Extraction(intent="stock_query")
    reply = say("stock dikhao", io)[-1]
    assert section(reply)["subtitle"] == "1 item kam hain: Cement"
    rows = body_rows(reply)
    assert len(rows) == 12 and rows[0] == ["⚠ Cement", "12 bag", "20"]
    assert card(reply)["is_terminal"] == "true"


def test_stock_single_item(seeded_db, io, extracted):
    extracted["result"] = Extraction(intent="stock_query", lines=[Line(item="Cement")])
    assert texts(say("cement kitna bacha", io))[-1] == "Cement: 12 bag bacha hai. ⚠ Kam hai (reorder level 20)."
    extracted["result"] = Extraction(intent="stock_query", lines=[Line(item="Sand")])
    assert texts(say("sand kitni hai", io))[-1] == "Sand: 380 cft bacha hai."


def test_customer_statement(seeded_db, io, extracted):
    extracted["result"] = Extraction(intent="statement", party="Ramesh Kumar")
    reply = say("Ramesh ka khata dikhao", io)[-1]
    s = section(reply)
    assert s["title"] == "Khata: Ramesh Kumar" and s["subtitle"] == "Ramesh Kumar ka baaki: ₹5,400"
    rows = body_rows(reply)
    assert [r[1:] for r in rows] == [["Received (UPI)", "−₹3,000"], ["Sale: 20 bags cement", "+₹8,400"]]
    assert s["children"][-1]["action"]["selection"]["action"] == "remind"


def test_supplier_statement_and_missing_party(seeded_db, io, extracted):
    extracted["result"] = Extraction(intent="statement", party="Shree Traders")
    assert section(say("Shree ka hisaab", io)[-1])["subtitle"] == "Aapko Shree Traders ko dena hai: ₹22,000"
    extracted["result"] = Extraction(intent="statement", missing=["party"])
    assert "Kiske naam" in texts(say("khata dikhao", io))[-1]


def test_daybook_today_and_cash_in_hand(seeded_db, io, extracted):
    extracted["result"] = Extraction(intent="daybook", period="today")
    before = cash(seeded_db)
    assert texts(say("aaj ka hisaab", io))[-1] == f"Aaj koi entry nahi hai. Galle mein cash: {inr(before)}."
    extracted["result"] = Extraction(intent="sale", lines=[Line(item="Sand", qty=10)], mode="cash")
    say("10 cft sand becha", io)
    say("confirm", io)
    extracted["result"] = Extraction(intent="daybook", period="today")
    reply = say("aaj ka hisaab", io)[-1]
    assert body_rows(reply) == [["Sale (cash): 10 cft Sand", "Walk-in", "₹550"]]
    assert cash(seeded_db) == before + 55000
    assert section(reply)["subtitle"].startswith("Sale ₹550 · Aaya ₹0 · Kharcha ₹0 · Galle mein cash ")


def test_daybook_yesterday_has_seed_sales(seeded_db, io, extracted):
    extracted["result"] = Extraction(intent="daybook", period="yesterday")
    reply = say("kal ka hisaab", io)[-1]
    assert card(reply)["card_payload"]["root"]["title"].startswith("Day book: ")
    assert len(body_rows(reply)) >= 2


# Undo

def confirm_last_card(io, reply):
    p = card(reply)["card_payload"]
    sel = p["approve_cta"]["selection"] if "approve_cta" in p else p["ctas"][0]["selection"]
    return say(json.dumps(sel), io)


def test_undo_sale_reverses_everything(seeded_db, io, extracted):
    extracted["result"] = RAMESH_SALE
    say("Ramesh ko 10 bag cement becha", io)
    say("confirm", io)
    assert balance(seeded_db, "Ramesh Kumar") == 750000 and stock(seeded_db, "Cement") == 2

    extracted["result"] = Extraction(intent="undo")
    ask = say("galti ho gayi, hata do", io)[-1]
    p = card(ask)["card_payload"]
    assert p["title"] == "Undo: Sale to Ramesh Kumar?" and p["approve_cta"]["label"] == "Undo"
    assert balance(seeded_db, "Ramesh Kumar") == 750000                 # nothing changes before confirm

    done = texts(confirm_last_card(io, ask))[-1]
    assert done.startswith("↩ Undo ho gaya: Sale to Ramesh Kumar.")
    assert "Ramesh Kumar ka baaki ab ₹5,400." in done and "Stock wapas: Cement 12 bag." in done
    assert balance(seeded_db, "Ramesh Kumar") == 540000 and stock(seeded_db, "Cement") == 12
    assert seeded_db.execute("select count(*) as n from reminder").fetchone()["n"] == 0
    assert seeded_db.execute("select count(*) as n from action where status = 'undone'").fetchone()["n"] == 1
    assert "nothing to undo" in texts(say("undo", io))[-1]


def test_undo_bill_cancels_invoice_and_keeps_number(seeded_db, io, extracted):
    extracted["result"] = VERMA_BILL
    say("bill verma", io)
    say("confirm", io)
    extracted["result"] = Extraction(intent="undo")
    done = texts(confirm_last_card(io, say("undo", io)[-1]))[-1]
    assert f"Bill {fy_number(1)} cancel ho gaya." in done
    assert seeded_db.execute("select status from invoice").fetchone()["status"] == "cancelled"
    assert balance(seeded_db, "Verma Constructions") == 3600000 and stock(seeded_db, "TMT Bar 12mm") == 850
    extracted["result"] = VERMA_BILL
    assert f"{fy_number(2)} (on confirm)" in json.dumps(card(say("bill verma", io)[-1]), ensure_ascii=False)


def test_undo_receipt_reopens_invoice(seeded_db, io, extracted):
    extracted["result"] = Extraction(intent="bill", party="Verma Constructions", lines=[Line(item="TMT Bar 12mm", qty=100)])
    say("bill", io)
    say("confirm", io)
    extracted["result"] = Extraction(intent="payment_in", party="Verma Constructions", amount=6800, mode="bank")
    say("Verma se 6800 aaye", io)
    say("confirm", io)
    assert seeded_db.execute("select status from invoice").fetchone()["status"] == "paid"
    extracted["result"] = Extraction(intent="undo")
    done = texts(confirm_last_card(io, say("undo", io)[-1]))[-1]
    assert f"{fy_number(1)} ab issued." in done
    assert seeded_db.execute("select status from invoice").fetchone()["status"] == "issued"


def test_typing_undo_at_the_undo_card_confirms(seeded_db, io, extracted):
    extracted["result"] = RAMESH_SALE
    say("sale", io)
    say("confirm", io)
    extracted["result"] = Extraction(intent="undo")
    say("undo", io)
    assert texts(say("undo", io))[-1].startswith("↩ Undo ho gaya")
    assert balance(seeded_db, "Ramesh Kumar") == 540000


def test_keep_it_changes_nothing(seeded_db, io, extracted):
    extracted["result"] = RAMESH_SALE
    say("sale", io)
    say("confirm", io)
    extracted["result"] = Extraction(intent="undo")
    say("undo", io)
    assert "Kept it" in texts(say("nahi", io))[-1]
    assert balance(seeded_db, "Ramesh Kumar") == 750000


def test_undo_retry_reverses_stock_once(seeded_db, io, extracted):
    extracted["result"] = RAMESH_SALE
    say("sale", io)
    say("confirm", io)
    extracted["result"] = Extraction(intent="undo")
    say("undo", io)
    io.down = {"inventory"}
    assert "Arth Inventory" in texts(say("confirm", io))[-1]
    assert balance(seeded_db, "Ramesh Kumar") == 540000 and stock(seeded_db, "Cement") == 2
    io.down = set()
    assert texts(say("confirm", io))[-1].startswith("↩")
    say("undo", io)                                    # nothing left; must not touch stock again
    assert stock(seeded_db, "Cement") == 12


def test_undo_walks_back_through_history(seeded_db, io, extracted):
    extracted["result"] = Extraction(intent="expense", amount=500, category="transport", mode="cash")
    say("tempo 500", io)
    say("confirm", io)
    extracted["result"] = RAMESH_SALE
    say("sale", io)
    say("confirm", io)
    extracted["result"] = Extraction(intent="undo")
    assert card(say("undo", io)[-1])["card_payload"]["title"] == "Undo: Sale to Ramesh Kumar?"
    say("confirm", io)
    assert card(say("undo", io)[-1])["card_payload"]["title"] == "Undo: Expense?"
    say("confirm", io)
    assert seeded_db.execute("select count(*) as n from action where status = 'undone'").fetchone()["n"] == 2
