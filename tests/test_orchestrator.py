"""respond() against the seeded schema, with ASI1 replaced by fixed extractions and specialists
called in-process (FakeIO.ask runs their logic directly instead of going over the network)."""

import asyncio
import inspect
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
from core import config, llm
from core.fuzzy import best_match
from core.llm import Extraction, ExtractionError, Line
from core.models import Commit, Plan, Query, Undo

OWNER = "agent1qowner"
SPECIALISTS = {"ledger": ledger, "inventory": inventory, "billing": billing, "collections": collections,
               "procurement": procurement, "insights": insights}


def lang_kw(fn, msg) -> dict:
    """Pass the owner's language the way core/specialist.py does: only to functions that take it."""
    return {"lang": msg.lang} if "lang" in inspect.signature(fn).parameters else {}


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
            res = mod.commit(msg.request_id, msg.action_id, msg.business_id, msg.effects, **lang_kw(mod.commit, msg))
        elif isinstance(msg, Query):
            res = mod.query(msg.request_id, msg.business_id, msg.intent, msg.fields)
        elif isinstance(msg, Undo):
            res = mod.undo(msg.action_id, **lang_kw(mod.undo, msg))
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

    async def extract(text, shop, earlier=None):
        state["texts"].append(text)
        state["earlier"] = earlier
        state["shop"] = shop
        if isinstance(state["result"], Exception):
            raise state["result"]
        return state["result"]

    monkeypatch.setattr(llm, "extract", extract)
    return state


def list_items(root):
    """The rows of a table card (core/cards.table renders them as a list)."""
    return next(c for c in root["children"] if c.get("type") == "list")["items"]


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


def test_second_sender_gets_own_copy_of_the_demo_shop(seeded_db, io, extracted):
    say("help", io)
    said = texts(say("help", io, sender="agent1qjudge"))
    assert said[0] == "Namaste! Arth is set up for Sharma Building Supplies."
    assert said[1].startswith("It's a demo shop with 30 days of sample records, just for you.")
    assert "Cement" in extracted["shop"].items and "Kailash Builders" in extracted["shop"].parties
    copy = seeded_db.execute("select * from business where owner_address = 'agent1qjudge'").fetchone()
    assert str(copy["id"]) != config.DEMO_BUSINESS_ID and copy["gstin"] == "08ABCPS1234F1Z5"

    def balances(owner):
        return {r["name"]: r["balance_paise"] for r in seeded_db.execute(
            "select pb.name, pb.balance_paise from party_balance pb join business b on b.id = pb.business_id "
            "where b.owner_address = %s", (owner,))}
    assert balances("agent1qjudge") == balances(OWNER) and balances(OWNER)["Verma Constructions"] == 3600000

    # the copy is private: a sale there leaves the original shop alone, and the next message is not new
    extracted["result"] = RAMESH_SALE
    say("Ramesh ko 10 bag cement becha 4200 mein, aadha cash mila", io, sender="agent1qjudge")
    assert texts(say("confirm", io, sender="agent1qjudge"))[-1].startswith("✅")
    assert balances("agent1qjudge")["Ramesh Kumar"] == 750000 and balances(OWNER)["Ramesh Kumar"] == 540000
    stock_of = "select i.stock_qty from item i join business b on b.id = i.business_id " \
               "where b.owner_address = %s and i.name = 'Cement'"
    assert seeded_db.execute(stock_of, ("agent1qjudge",)).fetchone()["stock_qty"] == 2
    assert seeded_db.execute(stock_of, (OWNER,)).fetchone()["stock_qty"] == 12


def test_new_sender_gets_empty_shop_when_copies_are_off(seeded_db, io, extracted, monkeypatch):
    monkeypatch.setattr(config, "DEMO_COPY_FOR_NEW_USERS", False)
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
    assert p["title"] == "Ramesh Kumar ko bikri"
    rows = {r["label"]: r["value"] for r in p["summary_rows"]}
    assert rows["Total"] == "₹4,200" and rows["Abhi mila"] == "₹2,100 cash"
    assert rows["Is entry ka baaki"] == "₹2,100" and rows["Ramesh Kumar ka total baaki"] == "₹7,500"
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
    assert texts(say("haan", io))[-1] == "Abhi confirm karne ke liye kuch nahi hai."


def test_english_message_gets_english_replies(seeded_db, io, extracted):
    extracted["result"] = RAMESH_SALE
    reply = say("Sold 10 bags of cement to Ramesh for 4200, half paid in cash", io)[-1]
    assert texts([reply]) == ["Please check and confirm:"]                 # one line; the card carries the rows
    p = card(reply)["card_payload"]
    rows = {r["label"]: r["value"] for r in p["summary_rows"]}
    assert p["title"] == "Sale to Ramesh Kumar"
    assert rows["Paid now"] == "₹2,100 cash" and rows["Due on this entry"] == "₹2,100"
    assert rows["Ramesh Kumar owes in total"] == "₹7,500"
    done = texts(say("confirm", io))[-1]                                 # a neutral word keeps the language
    assert done.startswith("✅ Done!") and "Ramesh Kumar now owes ₹7,500 in total." in done
    assert "⚠ Cement is running low: only 2 bag left" in done


def test_prose_confirm_works(seeded_db, io, extracted):
    extracted["result"] = RAMESH_SALE
    say("Ramesh ko 10 bag cement becha", io)
    assert texts(say("haan", io))[-1].startswith("✅")


def test_cancel_writes_nothing(seeded_db, io, extracted):
    extracted["result"] = RAMESH_SALE
    say("Ramesh ko 10 bag cement becha", io)
    assert texts(say("nahi", io))[-1] == "Theek hai, cancel kar diya. Kuch nahi likha."
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
    assert "Arth Ledger abhi jawab nahi de raha" in texts(say("Ramesh ko 10 bag cement becha", io))[-1]


def test_oversell_changes_approve_label(seeded_db, io, extracted):
    extracted["result"] = Extraction(intent="sale", party="Ramesh Kumar", mode="credit",
                                     lines=[Line(item="Cement", qty=20, unit="bag")])
    p = card(say("Ramesh ko 20 bag cement udhaar", io)[-1])["card_payload"]
    assert p["approve_cta"]["label"] == "Phir bhi becho"
    assert any(r["label"] == "⚠ Stock kam hai" for r in p["summary_rows"])


def test_vague_item_asks_which_and_the_answer_continues(seeded_db, io, extracted):
    extracted["result"] = Extraction(intent="sale", party="Ramesh Kumar", mode="credit",
                                     lines=[Line(item="TMT Bar", qty=10, unit="kg")])
    question = texts(say("Ramesh ko 10 kg sariya udhaar", io))[-1]
    assert question == "TMT Bar 12mm ya TMT Bar 8mm? Kaun sa?"
    extracted["result"] = Extraction(intent="sale", party="Ramesh Kumar", mode="credit",
                                     lines=[Line(item="TMT Bar 12mm", qty=10, unit="kg")], follows_up=True)
    rows = {r["label"]: r["value"] for r in card(say("12mm", io)[-1])["card_payload"]["summary_rows"]}
    assert extracted["earlier"]["question"] == question and rows["Items"] == "10 kg TMT Bar 12mm"


def test_colour_or_size_not_stocked_is_asked_not_swapped(seeded_db, io, extracted):
    extracted["result"] = Extraction(intent="sale", party="Ramesh Kumar", mode="credit",
                                     lines=[Line(item="Red Paint", qty=1)])
    assert texts(say("Ramesh ko 1 red paint udhaar", io))[-1] == \
        "'Red Paint' aapki item list mein nahi hai. White Paint 20L chahiye tha?"
    extracted["result"] = Extraction(intent="sale", party="Ramesh Kumar", mode="credit",
                                     lines=[Line(item="TMT Bar 10mm", qty=10, unit="kg")])
    assert texts(say("Ramesh ko 10 kg TMT 10mm udhaar", io))[-1] == \
        "'TMT Bar 10mm' aapki item list mein nahi hai. TMT Bar 12mm ya TMT Bar 8mm? Kaun sa?"


def test_item_matching_keeps_every_word():
    names = ["Cement", "TMT Bar 12mm", "TMT Bar 8mm", "White Paint 20L", "PVC Pipe 1 inch", "Electric Wire 1.5mm",
             "Binding Wire", "Wall Putty 40kg"]
    assert best_match("tmt 8mm", names, every_word=True).name == "TMT Bar 8mm"
    assert best_match("TMT Bar 12 mm", names, every_word=True).name == "TMT Bar 12mm"
    assert best_match("cemnt", names, every_word=True).name == "Cement"
    assert best_match("paint", names, every_word=True).name == "White Paint 20L"
    assert best_match("wall putty 40 kg", names, every_word=True).name == "Wall Putty 40kg"
    assert best_match("wire", names, every_word=True).candidates == ["Electric Wire 1.5mm", "Binding Wire"]
    assert best_match("Blue Paint 20L", names, every_word=True).near == ["White Paint 20L"]
    assert best_match("pipes", names, every_word=True).near == ["PVC Pipe 1 inch"]
    assert best_match("Red Paint", names).name == "White Paint 20L"     # parties keep the loose match


def test_walk_in_cash_sale(seeded_db, io, extracted):
    extracted["result"] = Extraction(intent="sale", lines=[Line(item="Sand", qty=10)])
    p = card(say("10 cft sand becha", io)[-1])["card_payload"]
    rows = {r["label"]: r["value"] for r in p["summary_rows"]}
    assert p["title"] == "Walk-in bikri" and rows["Total"] == "₹550" and rows["Payment"] == "₹550 cash"
    say("confirm", io)
    assert stock(seeded_db, "Sand") == 370


def test_sale_to_party_without_payment_info_asks(seeded_db, io, extracted):
    extracted["result"] = Extraction(intent="sale", party="Sunita Devi", lines=[Line(item="Nails", qty=2)])
    assert texts(say("Sunita ko 2 kg nails", io))[-1] == "Sunita Devi ne kitna payment kiya? Poora, aadha, ya udhaar?"


def test_new_customer_is_created_on_confirm(seeded_db, io, extracted):
    extracted["result"] = Extraction(intent="sale", party="Mohan Lal", mode="credit", lines=[Line(item="Nails", qty=2)])
    rows = {r["label"]: r["value"] for r in card(say("Mohan Lal ko 2 kg nails udhaar", io)[-1])["card_payload"]["summary_rows"]}
    assert rows["Customer"] == "Mohan Lal (naya)"
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
    assert texts(say("Nobody se 100 aaye", io))[-1] == "'Nobody Singh' naam ka koi khata nahi mila."


def test_purchase_adds_stock(seeded_db, io, extracted):
    extracted["result"] = Extraction(intent="purchase", party="Gupta Cement", amount=19000, mode="credit",
                                     lines=[Line(item="Cement", qty=50, unit="bag")])
    say("Gupta se 50 bag cement liya 19000 udhaar", io)
    say("confirm", io)
    assert stock(seeded_db, "Cement") == 62
    assert balance(seeded_db, "Gupta Cement") == -1900000


def test_short_answer_continues_the_question(seeded_db, io, extracted):
    extracted["result"] = Extraction(intent="sale", party="Sunita Devi", lines=[Line(item="Nails", qty=2)])
    question = texts(say("Sunita ko 2 kg nails becha", io))[-1]
    assert "Poora, aadha, ya udhaar" in question
    extracted["result"] = Extraction(intent="sale", party="Sunita Devi", mode="upi", lines=[Line(item="Nails", qty=2)],
                                     follows_up=True)
    reply = say("UPI se poora", io)
    assert extracted["texts"][-1] == "UPI se poora"
    assert (extracted["earlier"]["text"], extracted["earlier"]["question"]) == ("Sunita ko 2 kg nails becha", question)
    assert card(reply[-1])["card_kind"] == "review"
    # answered: the next message comes without the old question
    say("help", io)
    assert extracted["earlier"] is None


def test_new_request_after_question_is_not_glued_on(seeded_db, io, extracted):
    """A failed bill used to swallow every short message that followed it."""
    extracted["result"] = Extraction(intent="bill", party="Verma Constructions",
                                     lines=[Line(item="sariya", qty=100, unit="kg")])
    assert "sariya" in texts(say("verma ko bil bnao 100 kg sariya", io))[-1]
    extracted["result"] = Extraction(intent="payment_in", party="Sunita Devi", amount=500, mode="upi")
    reply = say("sunita se 500 aaye gpay pe", io)
    assert extracted["earlier"]["text"] == "verma ko bil bnao 100 kg sariya"   # ASI1 was shown the open question
    assert card(reply[-1])["card_kind"] == "review"                             # and read this as its own entry
    say("help", io)
    assert extracted["earlier"] is None


# GST bills

def rows_of(reply):
    return {r["label"]: r["value"] for r in card(reply)["card_payload"]["summary_rows"]}


VERMA_BILL = Extraction(intent="bill", party="Verma Constructions", lines=[Line(item="TMT Bar 12mm", qty=200, unit="kg")])


def fy_number(n):
    from core import clock, gst
    return gst.invoice_number(gst.financial_year(clock.today()), n)


def test_bill_draft_edit_and_issue(seeded_db, io, extracted):
    extracted["result"] = VERMA_BILL
    draft = say("Bill bana do Verma Constructions ke liye, 200 kg TMT 12mm", io)[-1]
    c = card(draft)
    assert c["card_kind"] == "detail" and c["card_payload"]["title"] == "Verma Constructions ka GST bill"
    rows = rows_of(draft)
    assert rows["Invoice no"] == f"{fy_number(1)} (confirm par)"
    assert rows["IGST"] == "₹2,074.58" and "CGST" not in rows          # Haryana customer
    assert rows["Bill total"] == "₹13,600"
    assert rows["Abhi mila"] == "₹0 (udhaar)" and rows["Verma Constructions ka total baaki"] == "₹49,600"
    assert rows["Stock: TMT Bar 12mm"] == "850 → 650 kg"
    first_request = c["card_payload"]["ctas"][0]["selection"]["request_id"]

    # edit: the draft is re-planned, nothing written
    extracted["result"] = Extraction(intent="bill", discount_pct=5)
    edited = say("5% discount", io)[-1]
    assert "bill for Verma Constructions: 200 kg TMT Bar 12mm" in extracted["shop"].draft
    rows = rows_of(edited)
    assert rows["Discount"] == "5%" and rows["Bill total"] == "₹12,920"
    assert seeded_db.execute("select count(*) as n from invoice").fetchone()["n"] == 0
    assert "purana card" in texts(say(json.dumps({"action": "confirm", "request_id": first_request}), io))[-1]

    sel = card(edited)["card_payload"]["ctas"][0]["selection"]
    done = say(json.dumps(sel), io)[-1]
    assert card(done) is None                                     # the draft card already showed the bill
    assert f"Bill {fy_number(1)} ban gaya." in texts([done])[0]
    assert f"Bill {fy_number(1)} ka PDF is computer pe save hua" in texts([done])[0]      # no Cloudinary in tests
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
    assert section["subtitle"] == "Total udhaar ₹74,900, 7 customers se."
    items = list_items(section)                    # one row per customer: name and Remind, then the details
    rows = [[*(el.get("value") or el.get("label") for el in it["children"][0]["children"]), it["children"][1]["value"]]
            for it in items]
    assert rows == [["Verma Constructions", "Remind", "₹36,000 baaki · 26 din se"],
                    ["Kailash Builders", "Remind", "₹17,000 baaki · 28 din se"],
                    ["Meena Interiors", "Remind", "₹7,600 baaki · 19 din se"],
                    ["Ramesh Kumar", "Remind", "₹5,400 baaki · 22 din se"],
                    ["Arif Electricals", "Remind", "₹3,500 baaki · 2 din se"],
                    ["Deepak Yadav", "Remind", "₹3,300 baaki · 4 din se"],
                    ["Sunita Devi", "Remind", "₹2,100 baaki · 9 din se"]]
    assert items[1]["children"][0]["children"][1]["action"]["selection"]["action"] == "remind"
    assert "Shree Traders" in section["children"][-1]["value"]                   # payables footer


def test_dues_top_debtor_answers_first(seeded_db, io, extracted):
    extracted["result"] = Extraction(intent="dues", top=1)
    section = card(say("Sabse zyada kiska udhar baaki hai?", io)[-1])["card_payload"]["root"]
    assert section["title"] == "Sabse zyada udhaar"
    assert section["subtitle"] == ("Sabse zyada udhaar Verma Constructions ka hai: ₹36,000 (26 din se). "
                                   "Total udhaar ₹74,900, 7 customers se.")
    assert [it["children"][1]["value"] for it in list_items(section)] == ["₹36,000 baaki · 26 din se"]
    assert "Shree Traders" not in json.dumps(section)                            # payables are a different question

    extracted["result"] = Extraction(intent="dues", top=2)
    section = card(say("top 2 udhaar", io)[-1])["card_payload"]["root"]
    assert section["title"] == "Top 2 udhaar"
    assert len(list_items(section)) == 2


def test_what_the_shop_owes_suppliers(seeded_db, io, extracted):
    extracted["result"] = Extraction(intent="dues", payable=True, top=1)
    section = card(say("mujhe sabse zyada kisko dena h", io)[-1])["card_payload"]["root"]
    assert section["title"] == "Aapko dena hai"
    assert section["subtitle"] == ("Sabse zyada Shree Traders ko dena hai: ₹22,000. "
                                   "Aapko 4 suppliers ko kul ₹51,600 dene hain.")
    assert "Verma" not in json.dumps(section)


def test_remind_button_to_whatsapp_link(seeded_db, io, extracted):
    extracted["result"] = Extraction(intent="dues")
    section = card(say("udhaar list", io)[-1])["card_payload"]["root"]
    ramesh_sel = list_items(section)[3]["children"][0]["children"][1]["action"]["selection"]
    draft = card(say(json.dumps(ramesh_sel), io)[-1])
    p = draft["card_payload"]
    assert draft["card_kind"] == "detail" and p["title"] == "Ramesh Kumar ko reminder"
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
    extracted["result"] = Extraction(intent="remind", party="Sunita Devi")
    p = card(say("Sunita ko payment reminder bhejo", io)[-1])["card_payload"]
    assert p["title"] == "Sunita Devi ko reminder"


def test_reminder_for_supplier_or_settled_customer(seeded_db, io, extracted):
    extracted["result"] = Extraction(intent="remind", party="Gupta Cement")
    assert texts(say("Gupta ko remind karo", io))[-1] == "Gupta Cement ka koi udhaar baaki nahi hai."
    extracted["result"] = Extraction(intent="payment_in", party="Sunita Devi", amount=2100, mode="cash")
    say("Sunita se 2100 mile", io)
    say("confirm", io)
    extracted["result"] = Extraction(intent="remind", party="Sunita Devi")
    assert texts(say("Sunita ko remind karo", io))[-1] == "Sunita Devi ka koi udhaar baaki nahi hai."


def test_credit_sale_sets_follow_up(seeded_db, io, extracted):
    from datetime import timedelta
    from core import clock
    extracted["result"] = RAMESH_SALE
    say("Ramesh ko 10 bag cement becha", io)
    done = texts(say("confirm", io))[-1]
    when = clock.today() + timedelta(days=7)
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

    extracted["result"] = Extraction(intent="remind", party="Verma Constructions")
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
