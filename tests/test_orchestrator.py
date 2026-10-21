"""respond() against the seeded schema, with ASI1 replaced by fixed extractions and specialists
called in-process (FakeIO.ask runs their logic directly instead of going over the network)."""

import asyncio
import json
from uuid import uuid4

import pytest

from agents.billing import logic as billing
from agents.collections import logic as collections
from agents.inventory import logic as inventory
from agents.insights import logic as insights
from agents.ledger import logic as ledger
from agents.procurement import logic as procurement
from agents.orchestrator import handler
from core import llm
from core.llm import Extraction, ExtractionError, Line
from core.models import Commit, Plan, Query, Undo

OWNER = "agent1qowner"
SPECIALISTS = {"ledger": ledger, "inventory": inventory, "billing": billing, "collections": collections,
               "procurement": procurement, "insights": insights}


class FakeStorage(dict):
    def set(self, key, value):
        self[key] = json.loads(json.dumps(value))       # storage must be JSON-serialisable


class FakeIO:
    def __init__(self):
        self.storage = FakeStorage()
        self.down: set[str] = set()                     # agents that don't answer
        self.calls: list[tuple[str, str]] = []
        self.suppliers = None                           # SupplierIO for Procurement (see test_procurement)
        self.outbox: list[tuple[str, object]] = []      # messages sent to outside addresses (the owner)

    async def ask(self, agent, msg, response_type):
        self.calls.append((agent, type(msg).__name__))
        if agent in self.down:
            return None
        mod = SPECIALISTS[agent]
        if isinstance(msg, Plan):
            extra = (self.suppliers,) if mod is procurement else ()
            res = mod.plan(msg.request_id, msg.business_id, msg.intent, msg.fields, *extra)
        elif isinstance(msg, Commit):
            res = mod.commit(msg.request_id, msg.action_id, msg.business_id, msg.effects)
        elif isinstance(msg, Query):
            res = mod.query(msg.request_id, msg.business_id, msg.intent, msg.fields)
        elif isinstance(msg, Undo):
            res = mod.undo(msg.action_id)
        if asyncio.iscoroutine(res):
            res = await res
        return response_type.parse_raw(res.json())       # round-trip like the wire does

    async def tell(self, agent, msg):
        """Fire-and-forget: run Procurement's pay_order now and deliver its OrderResult like the wire would."""
        self.calls.append((agent, type(msg).__name__))
        if agent in self.down:
            return
        res = await procurement.pay_order(msg.request_id, msg.business_id, msg.po_id, msg.user_tx, msg.buyer_wallet,
                                          int(msg.paid_atestfet), self.suppliers)
        await handler.on_order_result(type(res).parse_raw(res.json()), self)

    async def send(self, address, msg):
        self.outbox.append((address, msg))


@pytest.fixture
def io():
    return FakeIO()


@pytest.fixture
def extracted(monkeypatch):
    """Set what llm.extract returns; records the texts it was called with."""
    state = {"result": Extraction(intent="help"), "texts": []}

    async def extract(text, shop):
        state["texts"].append(text)
        state["shop"] = shop
        if isinstance(state["result"], Exception):
            raise state["result"]
        return state["result"]

    monkeypatch.setattr(llm, "extract", extract)
    return state


def say(text, io, sender=OWNER, msg_id=None):
    return asyncio.run(handler.respond(sender, msg_id or str(uuid4()), text, io))


def texts(replies):
    return [c.text for m in replies if hasattr(m, "content") for c in m.content if c.type == "text"]


def card(reply):
    meta = next((c.metadata for c in reply.content if c.type == "metadata"), None)
    return meta and {**meta, "card_payload": json.loads(meta["card_payload"])}


def balance(conn, name):
    return conn.execute("select balance_paise from party_balance where name = %s", (name,)).fetchone()["balance_paise"]


def stock(conn, name):
    return conn.execute("select stock_qty from item where name = %s", (name,)).fetchone()["stock_qty"]


RAMESH_SALE = Extraction(intent="sale", party="Ramesh Kumar", amount=4200, paid_fraction=0.5, mode="cash",
                         lines=[Line(item="Cement", qty=10, unit="bag")])


# Onboarding, help, idempotency

def test_first_message_claims_demo_shop_and_gets_help(seeded_db, io, extracted):
    replies = say("help", io)
    assert texts(replies)[0] == "Namaste! Arth is set up for Sharma Building Supplies."
    help_ = card(replies[1])
    assert help_["card_kind"] == "custom"
    buttons = help_["card_payload"]["root"]["children"][2]["children"]
    assert len(buttons) == 5 and buttons[0]["action"]["selection"]["action"] == "example"
    assert seeded_db.execute("select owner_address from business").fetchone()["owner_address"] == OWNER
    assert "Cement" in extracted["shop"].items and "Ramesh Kumar" in extracted["shop"].parties


def test_second_sender_gets_own_empty_shop(seeded_db, io, extracted):
    say("help", io)
    assert texts(say("help", io, sender="agent1qsomeoneelse"))[0] == "Namaste! Arth is set up for My Shop."
    assert extracted["shop"].items == []


def test_duplicate_msg_id_is_ignored(seeded_db, io, extracted):
    assert say("help", io, msg_id="m1")
    assert say("help", io, msg_id="m1") == []
    assert len(extracted["texts"]) == 1


def test_example_button_is_treated_as_typed_text(seeded_db, io, extracted):
    extracted["result"] = Extraction(intent="dues")
    say(json.dumps({"action": "example", "text": "Kis kis ka udhaar baaki hai?"}), io)
    assert extracted["texts"] == ["Kis kis ka udhaar baaki hai?"]


def test_missing_field_and_extraction_error_ask(seeded_db, io, extracted):
    extracted["result"] = Extraction(intent="payment_in", amount=2000, missing=["party"])
    assert "Kiske naam" in texts(say("2000 mile", io))[-1]
    extracted["result"] = ExtractionError("Thoda aur detail mein bataiye?")
    assert texts(say("asdf", io))[-1] == "Thoda aur detail mein bataiye?"


def test_db_down_gives_error_not_silence(io, extracted, monkeypatch):
    monkeypatch.setattr(handler, "ensure_business", lambda s: (_ for _ in ()).throw(ConnectionError()))
    assert "can't reach the shop records" in texts(say("help", io))[0]


# The demo sale: plan -> review card -> confirm -> committed by ledger and inventory

def test_sale_with_partial_payment_end_to_end(seeded_db, io, extracted):
    say("help", io)
    extracted["result"] = RAMESH_SALE
    replies = say("Ramesh ko 10 bag cement becha 4200, aadha cash", io)
    review = card(replies[-1])
    assert review["card_kind"] == "review"
    p = review["card_payload"]
    assert p["title"] == "Sale to Ramesh Kumar"
    rows = {r["label"]: r["value"] for r in p["summary_rows"]}
    assert rows["Total"] == "₹4,200" and rows["Paid now"] == "₹2,100 cash"
    assert rows["Baaki (this entry)"] == "₹2,100" and rows["Ramesh Kumar ka total baaki"] == "₹7,500"
    assert rows["Stock: Cement"] == "12 → 2 bag"
    assert p["approve_cta"]["label"] == "Confirm"
    # nothing written before confirm
    assert balance(seeded_db, "Ramesh Kumar") == 540000 and stock(seeded_db, "Cement") == 12

    done = texts(say(json.dumps(p["approve_cta"]["selection"]), io))[-1]
    assert done.startswith("✅ Ho gaya!")
    assert "Ramesh Kumar ka total baaki: ₹7,500" in done
    assert "⚠ Cement kam hai: sirf 2 bag bacha" in done
    assert balance(seeded_db, "Ramesh Kumar") == 750000 and stock(seeded_db, "Cement") == 2
    rows = seeded_db.execute("select t.kind, t.mode, t.amount_paise, a.kind as action_kind from txn t "
                             "join action a on a.id = t.action_id where a.payload->>'party' = 'Ramesh Kumar' "
                             "and a.payload->>'seed' is null order by t.id").fetchall()
    assert [(r["kind"], r["mode"], r["amount_paise"]) for r in rows] == [("sale", "credit", 420000),
                                                                       ("receipt", "cash", 210000)]
    # a second confirm finds nothing pending
    assert "Nothing is waiting" in texts(say("haan", io))[-1]


def test_prose_confirm_works(seeded_db, io, extracted):
    extracted["result"] = RAMESH_SALE
    say("Ramesh ko 10 bag cement becha", io)
    assert texts(say("haan", io))[-1].startswith("✅")


def test_cancel_writes_nothing(seeded_db, io, extracted):
    extracted["result"] = RAMESH_SALE
    say("Ramesh ko 10 bag cement becha", io)
    assert "Cancelled" in texts(say("nahi", io))[-1]
    assert balance(seeded_db, "Ramesh Kumar") == 540000 and stock(seeded_db, "Cement") == 12


def test_stale_card_is_rejected(seeded_db, io, extracted):
    extracted["result"] = RAMESH_SALE
    say("first", io)
    say("second", io)
    first_sel = {"action": "confirm", "request_id": "not-the-latest"}
    assert "out of date" in texts(say(json.dumps(first_sel), io))[-1]


def test_failed_commit_can_be_retried_without_double_posting(seeded_db, io, extracted):
    extracted["result"] = RAMESH_SALE
    say("Ramesh ko 10 bag cement becha", io)
    io.down = {"inventory"}
    assert "Arth Inventory" in texts(say("confirm", io))[-1]
    assert balance(seeded_db, "Ramesh Kumar") == 750000        # ledger committed
    assert stock(seeded_db, "Cement") == 12                    # inventory did not
    io.down = set()
    assert texts(say("confirm", io))[-1].startswith("✅")
    assert balance(seeded_db, "Ramesh Kumar") == 750000        # not double-posted
    assert stock(seeded_db, "Cement") == 2


def test_specialist_down_at_plan_time(seeded_db, io, extracted):
    extracted["result"] = RAMESH_SALE
    io.down = {"ledger"}
    assert "Arth Ledger isn't responding" in texts(say("Ramesh ko 10 bag cement becha", io))[-1]


def test_oversell_changes_approve_label(seeded_db, io, extracted):
    extracted["result"] = Extraction(intent="sale", party="Ramesh Kumar", mode="credit",
                                     lines=[Line(item="Cement", qty=20, unit="bag")])
    p = card(say("Ramesh ko 20 bag cement udhaar", io)[-1])["card_payload"]
    assert p["approve_cta"]["label"] == "Sell anyway"
    assert any(r["label"] == "⚠ Stock kam hai" for r in p["summary_rows"])


def test_walk_in_cash_sale(seeded_db, io, extracted):
    extracted["result"] = Extraction(intent="sale", lines=[Line(item="Sand", qty=10)])
    p = card(say("10 cft sand becha", io)[-1])["card_payload"]
    rows = {r["label"]: r["value"] for r in p["summary_rows"]}
    assert p["title"] == "Walk-in sale" and rows["Total"] == "₹550" and rows["Paid"] == "₹550 cash"
    say("confirm", io)
    assert stock(seeded_db, "Sand") == 370


def test_sale_to_party_without_payment_info_asks(seeded_db, io, extracted):
    extracted["result"] = Extraction(intent="sale", party="Sunita Devi", lines=[Line(item="Nails", qty=2)])
    assert "Poora, aadha, ya udhaar" in texts(say("Sunita ko 2 kg nails", io))[-1]


def test_new_customer_is_created_on_confirm(seeded_db, io, extracted):
    extracted["result"] = Extraction(intent="sale", party="Mohan Lal", mode="credit", lines=[Line(item="Nails", qty=2)])
    rows = {r["label"]: r["value"] for r in card(say("Mohan Lal ko 2 kg nails udhaar", io)[-1])["card_payload"]["summary_rows"]}
    assert rows["Customer"] == "Mohan Lal (new)"
    say("confirm", io)
    assert balance(seeded_db, "Mohan Lal") == 22000


def test_expense_and_payments(seeded_db, io, extracted):
    extracted["result"] = Extraction(intent="expense", amount=500, category="transport", mode="cash")
    say("tempo 500", io)
    assert texts(say("confirm", io))[-1].startswith("✅")

    extracted["result"] = Extraction(intent="payment_in", party="Verma Constructions", amount=10000, mode="bank")
    rows = {r["label"]: r["value"] for r in card(say("Verma se 10000 aaye", io)[-1])["card_payload"]["summary_rows"]}
    assert rows["Baaki"] == "₹36,000 → ₹26,000"
    say("confirm", io)
    assert balance(seeded_db, "Verma Constructions") == 2600000

    extracted["result"] = Extraction(intent="payment_out", party="Shree Traders", amount=22000, mode="upi")
    say("Shree ko 22000 diye", io)
    say("confirm", io)
    assert balance(seeded_db, "Shree Traders") == 0


def test_payment_to_unknown_party_asks(seeded_db, io, extracted):
    extracted["result"] = Extraction(intent="payment_in", party="Nobody Singh", amount=100)
    assert "couldn't find 'Nobody Singh'" in texts(say("Nobody se 100 aaye", io))[-1]


def test_purchase_adds_stock(seeded_db, io, extracted):
    extracted["result"] = Extraction(intent="purchase", party="Gupta Cement", amount=19000, mode="credit",
                                     lines=[Line(item="Cement", qty=50, unit="bag")])
    say("Gupta se 50 bag cement liya 19000 udhaar", io)
    say("confirm", io)
    assert stock(seeded_db, "Cement") == 62
    assert balance(seeded_db, "Gupta Cement") == -1900000


def test_short_answer_continues_the_question(seeded_db, io, extracted):
    extracted["result"] = Extraction(intent="sale", party="Sunita Devi", lines=[Line(item="Nails", qty=2)])
    assert "Poora, aadha, ya udhaar" in texts(say("Sunita ko 2 kg nails becha", io))[-1]
    extracted["result"] = Extraction(intent="sale", party="Sunita Devi", mode="upi", lines=[Line(item="Nails", qty=2)])
    reply = say("UPI se poora", io)
    assert extracted["texts"][-1] == "Sunita ko 2 kg nails becha. UPI se poora"
    assert card(reply[-1])["card_kind"] == "review"
    # answered: the next short message stands alone again
    say("help", io)
    assert extracted["texts"][-1] == "help"


def test_long_message_after_question_is_a_new_request(seeded_db, io, extracted):
    extracted["result"] = Extraction(intent="sale", party="Sunita Devi", lines=[Line(item="Nails", qty=2)])
    say("Sunita ko 2 kg nails becha", io)
    say("Verma Constructions se aaj das hazaar rupaye bank mein aaye", io)
    assert extracted["texts"][-1] == "Verma Constructions se aaj das hazaar rupaye bank mein aaye"


# GST bills

def rows_of(reply):
    return {r["label"]: r["value"] for r in card(reply)["card_payload"]["summary_rows"]}


VERMA_BILL = Extraction(intent="bill", party="Verma Constructions", lines=[Line(item="TMT Bar 12mm", qty=200, unit="kg")])


def fy_number(n):
    from datetime import date
    from core import gst
    return gst.invoice_number(gst.financial_year(date.today()), n)


def test_bill_draft_edit_and_issue(seeded_db, io, extracted):
    extracted["result"] = VERMA_BILL
    draft = say("Bill bana do Verma Constructions ke liye, 200 kg TMT 12mm", io)[-1]
    c = card(draft)
    assert c["card_kind"] == "detail" and c["card_payload"]["title"] == "GST bill for Verma Constructions"
    rows = rows_of(draft)
    assert rows["Invoice no"] == f"{fy_number(1)} (on confirm)"
    assert rows["IGST"] == "₹2,074.58" and "CGST" not in rows          # Haryana customer
    assert rows["Bill total"] == "₹13,600"
    assert rows["Paid now"] == "₹0 (udhaar)" and rows["Verma Constructions ka total baaki"] == "₹49,600"
    assert rows["Stock: TMT Bar 12mm"] == "850 → 650 kg"
    first_request = c["card_payload"]["ctas"][0]["selection"]["request_id"]

    # edit: the draft is re-planned, nothing written
    extracted["result"] = Extraction(intent="bill", discount_pct=5)
    edited = say("5% discount", io)[-1]
    assert "bill for Verma Constructions: 200 kg TMT Bar 12mm" in extracted["shop"].draft
    rows = rows_of(edited)
    assert rows["Discount"] == "5%" and rows["Bill total"] == "₹12,920"
    assert seeded_db.execute("select count(*) as n from invoice").fetchone()["n"] == 0
    assert "out of date" in texts(say(json.dumps({"action": "confirm", "request_id": first_request}), io))[-1]

    sel = card(edited)["card_payload"]["ctas"][0]["selection"]
    done = say(json.dumps(sel), io)[-1]
    final = card(done)
    assert final["card_payload"]["title"] == f"Invoice {fy_number(1)}" and final["is_terminal"] == "true"
    assert f"Bill {fy_number(1)} ban gaya." in texts([done])[0]
    inv = seeded_db.execute("select * from invoice").fetchone()
    assert (inv["number"], inv["status"], inv["total_paise"], inv["discount_pct"]) == (fy_number(1), "issued", 1292000, 5)
    assert inv["igst_paise"] + inv["taxable_paise"] == 1292000
    sale = seeded_db.execute("select * from txn where invoice_id = %s", (inv["id"],)).fetchone()
    assert sale["kind"] == "sale" and sale["amount_paise"] == 1292000
    assert balance(seeded_db, "Verma Constructions") == 3600000 + 1292000
    assert stock(seeded_db, "TMT Bar 12mm") == 650


def test_bill_numbers_increase_and_local_bill_splits(seeded_db, io, extracted):
    extracted["result"] = VERMA_BILL
    say("bill verma", io)
    say("confirm", io)
    extracted["result"] = Extraction(intent="bill", party="Ramesh Kumar", mode="cash",
                                     lines=[Line(item="Cement", qty=5, unit="bag")])
    rows = rows_of(say("bill ramesh 5 bag cement cash", io)[-1])
    assert rows["Invoice no"] == f"{fy_number(2)} (on confirm)"
    assert rows["CGST"] == "₹229.69" and rows["SGST"] == "₹229.68" and "IGST" not in rows
    assert rows["Paid now"] == "₹2,100 cash"
    say("confirm", io)
    inv = seeded_db.execute("select number, status from invoice order by number").fetchall()
    assert [(r["number"], r["status"]) for r in inv] == [(fy_number(1), "issued"), (fy_number(2), "paid")]


def test_bill_edit_adds_and_changes_lines(seeded_db, io, extracted):
    extracted["result"] = VERMA_BILL
    say("bill verma", io)
    extracted["result"] = Extraction(intent="bill", lines=[Line(item="Wall Putty 40kg", qty=2),
                                                           Line(item="TMT Bar 12mm", qty=100)])
    rows = rows_of(say("100 kg kar do aur 2 bag putty add karo", io)[-1])
    assert "100 kg TMT Bar 12mm @ ₹68" in rows and "2 bag Wall Putty 40kg @ ₹850" in rows
    assert rows["Bill total"] == "₹8,500"


def test_bill_for_other_party_is_a_new_draft(seeded_db, io, extracted):
    extracted["result"] = VERMA_BILL
    say("bill verma", io)
    extracted["result"] = Extraction(intent="bill", party="Sunita Devi", lines=[Line(item="Nails", qty=1)])
    assert rows_of(say("Sunita ka bill 1 kg nails", io)[-1])["Bill to"] == "Sunita Devi"


def test_bill_retry_does_not_take_a_second_number(seeded_db, io, extracted):
    extracted["result"] = VERMA_BILL
    say("bill verma", io)
    io.down = {"inventory"}
    say("confirm", io)
    io.down = set()
    say("confirm", io)
    assert [r["number"] for r in seeded_db.execute("select number from invoice")] == [fy_number(1)]
    assert seeded_db.execute("select last_no from invoice_counter").fetchone()["last_no"] == 1


# Collections: dues, reminders, follow-ups, invoice status

def test_dues_card_with_remind_buttons(seeded_db, io, extracted):
    extracted["result"] = Extraction(intent="dues")
    c = card(say("kis kis ka udhaar baaki hai", io)[-1])
    assert c["card_kind"] == "custom" and "is_terminal" not in c
    section = c["card_payload"]["root"]
    assert section["subtitle"] == "Total udhaar ₹43,500, 3 customers se."
    body = [g for g in section["children"] if g["type"] == "group"][1:]          # skip header row
    cells = [[el.get("value") or el.get("label") for el in g["children"]] for g in body]
    assert cells == [["Verma Constructions", "₹36,000", "26 din", "Remind"],
                     ["Ramesh Kumar", "₹5,400", "22 din", "Remind"],
                     ["Sunita Devi", "₹2,100", "9 din", "Remind"]]
    assert body[1]["children"][3]["action"]["selection"]["action"] == "remind"
    assert "Shree Traders" in section["children"][-1]["value"]                   # payables footer


def test_remind_button_to_whatsapp_link(seeded_db, io, extracted):
    extracted["result"] = Extraction(intent="dues")
    section = card(say("udhaar list", io)[-1])["card_payload"]["root"]
    ramesh_sel = [g for g in section["children"] if g["type"] == "group"][2]["children"][3]["action"]["selection"]
    draft = card(say(json.dumps(ramesh_sel), io)[-1])
    p = draft["card_payload"]
    assert draft["card_kind"] == "detail" and p["title"] == "Reminder for Ramesh Kumar"
    assert p["hero_image"].startswith("data:image/png;base64,")
    rows = {r["label"]: r["value"] for r in p["summary_rows"]}
    assert rows["Baaki"] == "₹5,400"
    assert "₹5,400" in rows["Message"] and "upi://pay?pa=sharmabuilding%40upi" in rows["Message"]
    assert "am=5400.00" in rows["Message"]
    assert seeded_db.execute("select count(*) as n from reminder").fetchone()["n"] == 0

    done = texts(say(json.dumps(p["ctas"][0]["selection"]), io))[-1]
    assert "https://wa.me/919829012345?text=" in done and "UPI link: upi://pay?" in done
    r = seeded_db.execute("select amount_paise, sent_at, message from reminder").fetchone()
    assert r["amount_paise"] == 540000 and r["sent_at"] is not None and "Ramesh Kumar" in r["message"]


def test_typed_reminder_request(seeded_db, io, extracted):
    extracted["result"] = Extraction(intent="dues", party="Sunita Devi")
    p = card(say("Sunita ko payment reminder bhejo", io)[-1])["card_payload"]
    assert p["title"] == "Reminder for Sunita Devi"


def test_reminder_for_supplier_or_settled_customer(seeded_db, io, extracted):
    extracted["result"] = Extraction(intent="dues", party="Gupta Cement")
    assert "Gupta Cement owes nothing" in texts(say("Gupta ko remind karo", io))[-1]
    extracted["result"] = Extraction(intent="payment_in", party="Sunita Devi", amount=2100, mode="cash")
    say("Sunita se 2100 mile", io)
    say("confirm", io)
    extracted["result"] = Extraction(intent="dues", party="Sunita Devi")
    assert "owes nothing" in texts(say("Sunita ko remind karo", io))[-1]


def test_credit_sale_sets_follow_up(seeded_db, io, extracted):
    from datetime import date, timedelta
    extracted["result"] = RAMESH_SALE
    say("Ramesh ko 10 bag cement becha", io)
    done = texts(say("confirm", io))[-1]
    when = date.today() + timedelta(days=7)
    assert f"Ramesh Kumar ko {when:%d %b} ko yaad dilaunga." in done
    r = seeded_db.execute("select amount_paise, follow_up, sent_at from reminder").fetchone()
    assert (r["amount_paise"], r["follow_up"], r["sent_at"]) == (750000, when, None)


def test_receipts_settle_oldest_invoices_first(seeded_db, io, extracted):
    for qty_ in (100, 50):                              # two credit bills: ₹6,800 then ₹3,400
        extracted["result"] = Extraction(intent="bill", party="Verma Constructions",
                                         lines=[Line(item="TMT Bar 12mm", qty=qty_)])
        say("bill verma", io)
        say("confirm", io)

    def statuses():
        return [(r["number"], r["status"]) for r in seeded_db.execute("select number, status from invoice order by number")]

    assert statuses() == [(fy_number(1), "issued"), (fy_number(2), "issued")]
    extracted["result"] = Extraction(intent="payment_in", party="Verma Constructions", amount=8000, mode="bank")
    say("Verma se 8000 aaye", io)
    done = texts(say("confirm", io))[-1]
    assert f"Bill status: {fy_number(1)} paid, {fy_number(2)} partly paid." in done
    assert statuses() == [(fy_number(1), "paid"), (fy_number(2), "partly_paid")]

    extracted["result"] = Extraction(intent="dues", party="Verma Constructions")
    rows = {r["label"]: r["value"] for r in card(say("Verma ko remind karo", io)[-1])["card_payload"]["summary_rows"]}
    assert rows["Bills"] == fy_number(2)                 # only the open one is mentioned


def test_bill_paid_at_counter_is_paid(seeded_db, io, extracted):
    extracted["result"] = Extraction(intent="bill", party="Ramesh Kumar", mode="cash",
                                     lines=[Line(item="Cement", qty=5)])
    say("Ramesh ka bill 5 bag cement cash", io)
    say("confirm", io)
    assert seeded_db.execute("select status from invoice").fetchone()["status"] == "paid"
    assert seeded_db.execute("select amount_paise from invoice_payment").fetchone()["amount_paise"] == 210000


def test_after_money_commit_is_idempotent(seeded_db, io, extracted):
    extracted["result"] = Extraction(intent="bill", party="Ramesh Kumar", paid_fraction=0.5, mode="cash",
                                     lines=[Line(item="Cement", qty=5)])
    say("bill", io)
    say("confirm", io)
    action = str(seeded_db.execute("select action_id from invoice").fetchone()["action_id"])
    collections.commit("r", action, "x", {"after_money": True})
    collections.commit("r", action, "x", {"after_money": True})
    assert [r["amount_paise"] for r in seeded_db.execute("select amount_paise from invoice_payment")] == [105000]
    assert seeded_db.execute("select status from invoice").fetchone()["status"] == "partly_paid"
    assert seeded_db.execute("select count(*) as n from reminder").fetchone()["n"] == 1
