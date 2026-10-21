"""Supplier pricing rules, and the restock flow through respond(): quotes, one counter round,
carousel, choice, approval, then both payment legs. Suppliers run in-process from suppliers/*.yaml,
and the ledger (chain) is faked: transfers are recorded, verification answers as told."""

import asyncio
import json

import pytest

from agents.procurement import logic as procurement
from agents.supplier import logic as supplier
from core import chain, net
from core.llm import Extraction, Line
from core.models import RFQ, Counter, PlaceOrder
from uagents_core.contrib.protocols.payment import (CancelPayment, CommitPayment, CompletePayment, Funds,
                                                    RejectPayment, RequestPayment)

from agents.orchestrator import handler
from test_orchestrator import FakeStorage, balance, card, extracted, io, say, stock, texts  # noqa: F401

SUPPLIERS = {s.name: s for s in supplier.load_all(procurement.SUPPLIER_CONFIGS)}
GUPTA = SUPPLIERS["Gupta Cement"]


def rfq(item="Cement", amount=50, po="po-1"):
    return RFQ(po_id=po, item=item, qty=amount, need_by="2026-10-24")


# Supplier rules

def test_supplier_quotes_base_price_and_refuses_what_it_lacks():
    st = FakeStorage()
    q = supplier.on_rfq(GUPTA, st, "buyer", rfq())
    assert q.ok and q.unit_price_paise == 38500 and q.delivery_days == 3 and q.rating == 4.6
    assert not supplier.on_rfq(GUPTA, st, "buyer", rfq(item="Bricks")).ok          # doesn't stock it
    assert not supplier.on_rfq(SUPPLIERS["Balaji Build"], st, "buyer", rfq(amount=200)).ok   # only 150 bags


def test_counter_accepted_at_or_above_floor_once():
    st = FakeStorage()
    supplier.on_rfq(GUPTA, st, "buyer", rfq())
    assert supplier.on_counter(GUPTA, st, "buyer", Counter(po_id="po-1", target_unit_price_paise=37730)).unit_price_paise == 37730
    # one round only: a second, lower ask doesn't move it
    assert supplier.on_counter(GUPTA, st, "buyer", Counter(po_id="po-1", target_unit_price_paise=37200)).unit_price_paise == 37730


def test_counter_below_floor_holds_and_strangers_are_refused():
    st = FakeStorage()
    supplier.on_rfq(GUPTA, st, "buyer", rfq())
    assert supplier.on_counter(GUPTA, st, "buyer", Counter(po_id="po-1", target_unit_price_paise=37000)).unit_price_paise == 38500
    assert not supplier.on_counter(GUPTA, st, "someone-else", Counter(po_id="po-1", target_unit_price_paise=38000)).ok


# Restock through the orchestrator

class FakeSuppliers:
    """SupplierIO that runs supplier logic directly; addresses are agent1q<config agent name>."""

    def __init__(self):
        self.by_address = {f"agent1q{s.agent}": s for s in SUPPLIERS.values()}
        self.storage = {a: FakeStorage() for a in self.by_address}
        self.silent: set[str] = set()
        self.unverifiable: set[str] = set()             # suppliers whose on-chain check fails
        self.sent: list[tuple[str, str]] = []

    async def ask(self, address, msg, response_type, timeout):
        s = self.by_address[address]
        self.sent.append((s.name, type(msg).__name__))
        if s.name in self.silent:
            return None
        if isinstance(msg, PlaceOrder):
            return supplier.on_place_order(s, self.storage[address], "agent1qprocurement", msg, f"fetch1{s.agent}")
        handler = supplier.on_rfq if isinstance(msg, RFQ) else supplier.on_counter
        return handler(s, self.storage[address], "agent1qprocurement", msg)

    async def commit_payment(self, address, msg, timeout):
        s = self.by_address[address]
        self.sent.append((s.name, "CommitPayment"))
        verify = lambda *_: "transaction not found" if s.name in self.unverifiable else None  # noqa: E731
        return supplier.on_commit_payment(s, self.storage[address], "agent1qprocurement", msg, f"fetch1{s.agent}", verify)


class FakeChain:
    def __init__(self):
        self.transfers: list[tuple[str, str, int]] = []     # (from agent, to wallet, atestfet)
        self.leg1_ok = True

    def send(self, agent, to, amount):
        self.transfers.append((agent, to, amount))
        return f"TX{len(self.transfers)}"

    def verify_transfer(self, tx, sender, recipient, min_amount, wait=0):
        return None if self.leg1_ok and recipient == "fetch1orchestrator" else "transaction not found"


@pytest.fixture
def ledger(monkeypatch):
    fake = FakeChain()
    monkeypatch.setattr(chain, "address", lambda name: f"fetch1{name}")
    monkeypatch.setattr(chain, "send", fake.send)
    monkeypatch.setattr(chain, "verify_transfer", fake.verify_transfer)
    return fake


@pytest.fixture
def suppliers(io, monkeypatch, ledger):
    monkeypatch.setattr(net, "address", lambda name: f"agent1q{name}")
    io.suppliers = FakeSuppliers()
    return io.suppliers


def payload(reply):
    return card(reply)["card_payload"]


def po(conn):
    return conn.execute("select * from purchase_order order by created_at desc limit 1").fetchone()


def restock(io, extracted, amount=50, item="Cement"):
    extracted["result"] = Extraction(intent="restock", lines=[Line(item=item, qty=amount)])
    return say(f"{amount} bag {item} mangwa do", io)


def test_quotes_carousel_after_one_counter_round(seeded_db, io, suppliers, extracted):
    reply = restock(io, extracted)[-1]
    assert card(reply)["card_kind"] == "carousel"
    p = payload(reply)
    assert p["title"] == "Quotes for 50 bag Cement"
    assert [i["title"] for i in p["items"]] == ["Gupta Cement", "Shree Traders", "Balaji Build"]
    gupta, shree, balaji = p["items"]
    assert gupta["subtitle"] == "₹377.30/bag · 3 din · ★4.6"                      # 385 countered to 2% below
    assert gupta["secondary_text"] == "Total ₹18,865 · mol-bhav: ₹385 → ₹377.30"
    assert [b["label"] for b in gupta["badges"]] == ["Best value", "Top rated"]
    assert shree["secondary_text"] == "Total ₹19,600 · counter-offer nahi maana"   # floor 380 > 377.30
    assert [b["label"] for b in balaji["badges"]] == ["Fastest"]
    assert gupta["primary_cta"]["selection"]["supplier"] == "Gupta Cement"
    assert ("Balaji Build", "Counter") not in suppliers.sent                     # only the top two are countered
    row = po(seeded_db)
    assert row["status"] == "awaiting_approval" and len(row["quotes"]) == 3


def choose(io, reply, n=0):
    return say(json.dumps(payload(reply)["items"][n]["primary_cta"]["selection"]), io)


def test_choose_then_approve(seeded_db, io, suppliers, extracted):
    review = choose(io, restock(io, extracted)[-1])[-1]
    p = payload(review)
    assert p["title"] == "Restock: 50 bag Cement from Gupta Cement"
    rows = {r["label"]: r["value"] for r in p["summary_rows"]}
    assert rows["Rate"] == "₹377.30 (pehle ₹385)/bag"
    assert rows["Order total"] == "₹18,865" and rows["Arth fee (1%)"] == "₹188.65"
    assert rows["You pay"] == "₹19,053.65 = 1.9054 FET (testnet)"
    assert p["approve_cta"]["label"] == "Approve order"
    assert po(seeded_db)["status"] == "awaiting_approval"                       # nothing agreed before approval

    replies = say(json.dumps(p["approve_cta"]["selection"]), io)
    assert "Gupta Cement ke saath tay" in texts(replies)[0] and "Approve FET Payment" in texts(replies)[0]
    request = replies[-1]
    assert isinstance(request, RequestPayment)
    assert request.accepted_funds[0].amount == "1.905365" and request.accepted_funds[0].currency == "FET"
    assert request.recipient == "fetch1orchestrator" and request.reference == str(po(seeded_db)["id"])
    assert request.metadata["fet_network"] == "stable-testnet"
    row = po(seeded_db)
    assert row["status"] == "awaiting_user_payment" and row["unit_price_paise"] == 37730
    assert row["total_paise"] == 1886500 and row["arth_fee_paise"] == 18865 and row["action_id"] is not None
    assert seeded_db.execute("select stock_qty from item where name = 'Cement'").fetchone()["stock_qty"] == 12


def test_typed_supplier_name_and_haan_choose(seeded_db, io, suppliers, extracted):
    restock(io, extracted)
    assert payload(say("shree wala le lo", io)[-1])["title"] == "Restock: 50 bag Cement from Shree Traders"
    restock(io, extracted)
    assert payload(say("haan", io)[-1])["title"].endswith("from Gupta Cement")   # best value


def test_cancel_closes_the_order(seeded_db, io, suppliers, extracted):
    restock(io, extracted)
    assert "order cancel" in texts(say("cancel", io))[-1]
    assert po(seeded_db)["status"] == "cancelled"


def test_silent_and_unstocked_suppliers_are_noted(seeded_db, io, suppliers, extracted):
    suppliers.silent = {"Shree Traders"}
    p = payload(restock(io, extracted, amount=200)[-1])
    assert [i["title"] for i in p["items"]] == ["Gupta Cement"]
    assert "no reply: Shree Traders" in p["subtitle"] and "don't stock it: Balaji Build" in p["subtitle"]


def test_no_quotes_at_all(seeded_db, io, suppliers, extracted):
    suppliers.silent = set(SUPPLIERS)
    assert "kisi supplier ne jawab nahi diya" in texts(restock(io, extracted))[-1]
    assert po(seeded_db)["status"] == "failed"


def test_missing_quantity_is_suggested(seeded_db, io, suppliers, extracted):
    extracted["result"] = Extraction(intent="restock", lines=[Line(item="Cement")])
    reply = say("cement mangwa do", io)
    assert "28 bag maan liya" in texts(reply)[-1]                               # 2 x reorder 20 - stock 12
    assert payload(reply[-1])["title"] == "Quotes for 28 bag Cement"


def test_unknown_item_asks(seeded_db, io, suppliers, extracted):
    assert "nahi hai" in texts(restock(io, extracted, item="Marble"))[-1]
    assert seeded_db.execute("select count(*) as n from purchase_order").fetchone()["n"] == 0


def test_undo_cancels_unpaid_order(seeded_db, io, suppliers, extracted):
    restock(io, extracted)
    say("haan", io)
    say("confirm", io)
    extracted["result"] = Extraction(intent="undo")
    p = payload(say("undo", io)[-1])
    assert p["title"] == "Undo: Restock: 50 bag Cement from Gupta Cement?"
    done = texts(say("confirm", io))[-1]
    assert "Supplier order cancel ho gaya." in done
    assert po(seeded_db)["status"] == "cancelled"


# Payments: leg 1 (owner -> Arth) and leg 2 (Procurement -> supplier)

OWNER = "agent1qowner"


def approve(io, extracted, choice="haan", amount=50):
    restock(io, extracted, amount)
    say(choice, io)
    return say("confirm", io)[-1]                       # the RequestPayment


def commit_payment(io, request, tx="USERTX1", payer="fetch1owner"):
    msg = CommitPayment(funds=request.accepted_funds[0], recipient=request.recipient, transaction_id=tx,
                        reference=request.reference, metadata={"buyer_fet_wallet": payer} if payer else {})
    io.outbox.clear()
    asyncio.run(handler.on_commit_payment(OWNER, msg, io))
    return list(io.outbox)


def outbox_texts(outbox):
    return [c.text for _, m in outbox if hasattr(m, "content") for c in m.content if c.type == "text"]


def test_paid_restock_is_ordered_paid_and_posted(seeded_db, io, suppliers, extracted, ledger):
    request = approve(io, extracted)
    out = commit_payment(io, request)
    assert isinstance(out[0][1], CompletePayment) and out[0][1].transaction_id == "USERTX1"
    said = outbox_texts(out)
    assert said[0].startswith("✅ Payment mil gaya: 1.9054 FET")
    assert said[-1].startswith("📦 Order confirmed: 50 bag Cement from Gupta Cement @ ₹377.30 = ₹18,865.")
    assert "Delivery: " in said[-1] and "Khata updated." in said[-1]

    # leg 2: Procurement paid Gupta exactly the agreed total, once
    assert ledger.transfers == [("procurement", "fetch1supplier_gupta", 1886500000000000000)]
    assert ("Gupta Cement", "PlaceOrder") in suppliers.sent
    row = po(seeded_db)
    assert row["status"] == "confirmed" and row["user_tx"] == "USERTX1" and row["supplier_tx"] == "TX1"
    assert row["dispatch_eta"] is not None

    # posted under the restock's action: purchase on credit, bank payment, Arth fee as an expense
    txns = seeded_db.execute("select kind, amount_paise, mode, category, party_id from txn where action_id = %s "
                             "order by id", (row["action_id"],)).fetchall()
    assert [(t["kind"], t["amount_paise"], t["mode"]) for t in txns] == [
        ("purchase", 1886500, "credit"), ("payment", 1886500, "bank"), ("expense", 18865, "bank")]
    assert txns[2]["category"] == "arth fee" and txns[2]["party_id"] is None
    assert balance(seeded_db, "Gupta Cement") == 0 and stock(seeded_db, "Cement") == 62

    extracted["result"] = Extraction(intent="undo")
    assert "undo nahi kar sakte" in texts(say("undo", io))[-1]


def test_same_commit_twice_is_handled_once(seeded_db, io, suppliers, extracted, ledger):
    request = approve(io, extracted)
    commit_payment(io, request)
    again = commit_payment(io, request)
    assert [type(m).__name__ for _, m in again] == ["CompletePayment"]
    assert len(ledger.transfers) == 1


def test_unverified_payment_is_cancelled_and_order_waits(seeded_db, io, suppliers, extracted, ledger):
    request = approve(io, extracted)
    ledger.leg1_ok = False
    out = commit_payment(io, request)
    assert isinstance(out[0][1], CancelPayment) and out[0][1].reason == "transaction not found"
    assert "Payment verify nahi hua" in outbox_texts(out)[0]
    assert po(seeded_db)["status"] == "awaiting_user_payment" and ledger.transfers == []
    ledger.leg1_ok = True                                 # a good payment afterwards still goes through
    assert outbox_texts(commit_payment(io, request, tx="USERTX2"))[-1].startswith("📦 Order confirmed")


def test_missing_wallet_or_reused_tx_is_refused(seeded_db, io, suppliers, extracted, ledger):
    request = approve(io, extracted)
    assert "buyer_fet_wallet missing" in commit_payment(io, request, payer=None)[0][1].reason
    commit_payment(io, request)
    second = approve(io, extracted, amount=20)
    assert commit_payment(io, second)[0][1].reason == "transaction already used"


def test_supplier_cant_verify_so_next_best_supplier_is_used(seeded_db, io, suppliers, extracted, ledger):
    suppliers.unverifiable = {"Gupta Cement"}
    request = approve(io, extracted)
    said = outbox_texts(commit_payment(io, request))[-1]
    assert suppliers.sent.count(("Gupta Cement", "CommitPayment")) == 2         # retried once
    assert "50 bag Cement from Shree Traders @ ₹392 = ₹19,600" in said
    assert "Gupta Cement order poora nahi kar paya" in said
    assert [t[1] for t in ledger.transfers] == ["fetch1supplier_gupta", "fetch1supplier_shree"]
    row = po(seeded_db)
    assert row["status"] == "confirmed" and row["total_paise"] == 1960000
    assert balance(seeded_db, "Shree Traders") == -2200000                      # old debt only; this one is paid


def test_no_supplier_completes_so_owner_is_refunded(seeded_db, io, suppliers, extracted, ledger):
    suppliers.unverifiable = set(SUPPLIERS)
    request = approve(io, extracted)
    said = outbox_texts(commit_payment(io, request, payer="fetch1owner"))[-1]
    assert said.startswith("❌ Koi supplier order poora nahi kar paya") and "wapas bhej diye" in said
    assert ledger.transfers[-1] == ("procurement", "fetch1owner", 1905365000000000000)
    row = po(seeded_db)
    assert row["status"] == "failed" and row["refund_tx"] == f"TX{len(ledger.transfers)}"
    assert seeded_db.execute("select count(*) as n from txn where action_id = %s", (row["action_id"],)).fetchone()["n"] == 0
    assert stock(seeded_db, "Cement") == 12


def test_rejected_payment_cancels_the_order(seeded_db, io, suppliers, extracted, ledger):
    approve(io, extracted)
    io.outbox.clear()
    asyncio.run(handler.on_reject_payment(OWNER, RejectPayment(reason="not now"), io))
    assert "order bhi cancel" in outbox_texts(io.outbox)[0]
    assert po(seeded_db)["status"] == "cancelled" and ledger.transfers == []
    extracted["result"] = Extraction(intent="undo")
    assert "nothing to undo" in texts(say("undo", io))[-1]


def test_supplier_refuses_order_at_a_lower_price_and_unpaid_commit():
    st, s = FakeStorage(), SUPPLIERS["Gupta Cement"]
    supplier.on_rfq(s, st, "buyer", rfq())
    assert not supplier.on_place_order(s, st, "buyer", PlaceOrder(po_id="po-1", unit_price_paise=37000, qty=50), "w").ok
    req = supplier.on_place_order(s, st, "buyer", PlaceOrder(po_id="po-1", unit_price_paise=38500, qty=50), "w")
    assert req.accepted_funds[0].amount == "1.925" and req.recipient == "w"
    commit = CommitPayment(funds=Funds(amount="1.925", currency="FET"), recipient="w", transaction_id="T1",
                           reference="po-1", metadata={"buyer_fet_wallet": "fetch1buyer"})
    done, confirmed = supplier.on_commit_payment(s, st, "buyer", commit, "w", lambda *_: None)
    assert isinstance(done, CompletePayment) and confirmed.po_id == "po-1"
    again = commit.copy(update={"transaction_id": "T2"})
    assert supplier.on_commit_payment(s, st, "buyer", again, "w", lambda *_: None)[0].reason == "order already paid"
