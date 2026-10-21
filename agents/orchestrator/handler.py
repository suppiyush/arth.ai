"""Orchestrator logic, kept apart from the uAgent wiring so it can be tested without a network.

respond() takes one incoming chat text and returns the reply messages. It reaches specialists only
through io.ask() / io.tell(), and keeps per-sender session state in io.storage:
    session:<sender> = {"pending": {...}, "asking": {...}, "history": [newest confirmed actions first]}
    payment:<po_id>  = an approved restock waiting for, or being paid by, its owner

Restock payments (Payment Protocol, the orchestrator is the seller towards the owner):
approve -> RequestPayment; on_commit_payment verifies the owner's transfer on-chain, sends
CompletePayment and hands the order to Procurement (PayOrder); on_order_result posts the purchase
and tells the owner.
"""

import asyncio
import re
import time
from typing import Protocol
from uuid import UUID, uuid4

from uagents import Model
from uagents_core.contrib.protocols.chat import ChatMessage
from uagents_core.contrib.protocols.payment import (CancelPayment, CommitPayment, CompletePayment, Funds,
                                                    RejectPayment, RequestPayment)

from core import cards, chain, config, db, llm
from core.fuzzy import best_match
from core.llm import Extraction, ExtractionError, Line, ShopContext
from core.models import (Commit, Committed, OrderResult, PayOrder, Plan, PlanResult, Query, QueryResult, Undo,
                         UndoDone)
from core.money import fet, inr, qty, to_paise

SEEN_KEY = "seen_msg_ids"
SEEN_LIMIT = 500
PENDING_TTL = 10 * 60               # a plan waiting for Confirm expires after 10 minutes
HISTORY_LIMIT = 10                  # confirmed actions remembered per owner for undo
PAYMENT_DEADLINE = 10 * 60          # seconds the owner has to approve the FET payment
ANSWER_MAX_WORDS = 6                # a reply this short to Arth's question continues the original message
# Read-only intents answered by one specialist's Query: intent -> (agent, fields from the extraction)
QUERY_AGENTS = {
    "stock_query": ("inventory", lambda e: {"lines": [l.model_dump() for l in e.lines]}),
    "statement": ("ledger", lambda e: {"party": e.party}),
    "daybook": ("ledger", lambda e: {"period": e.period}),
}
ALL_SPECIALISTS = ["ledger", "inventory", "billing", "collections", "procurement"]

# Which specialists plan and commit each write intent.
PLAN_AGENTS = {
    "sale": ["ledger", "inventory"],
    "purchase": ["ledger", "inventory"],
    "expense": ["ledger"],
    "payment_in": ["ledger"],
    "payment_out": ["ledger"],
}

# Agents that commit after the first round, because they read what it wrote (receipts, balances).
COMMIT_AFTER = {"sale": ["collections"], "bill": ["collections"], "payment_in": ["collections"]}

HELP_EXAMPLES = [
    "Ramesh ko 10 bag cement becha 4200 mein, aadha cash mila",
    "Bill bana do Verma Constructions ke liye, 200 kg TMT 12mm",
    "Kis kis ka udhaar baaki hai?",
    "Cement kitna bacha hai?",
    "Is mahine kitna profit hua?",
]

NOT_REACHABLE = "{agent} abhi jawab nahi de raha, thodi der mein phir try karein. ({agent} isn't responding, please try again shortly.)"


class Storage(Protocol):
    def get(self, key: str): ...
    def set(self, key: str, value) -> None: ...


class IO(Protocol):
    storage: Storage

    async def ask(self, agent: str, msg: Model, response_type: type[Model]) -> Model | None:
        """Send msg to a specialist and wait for its reply; None on timeout or delivery failure."""

    async def tell(self, agent: str, msg: Model) -> None:
        """Send msg to a specialist without waiting (its answer arrives as its own message)."""

    async def send(self, address: str, msg: Model) -> None:
        """Send msg to an outside address: the owner's ASI:One agent."""


# Business lookup and onboarding (G-10)

def ensure_business(sender: str) -> tuple[dict, bool]:
    """Return (business, is_new). A first-time sender claims the unclaimed demo shop if there is one,
    otherwise gets a fresh empty business."""
    with db.transaction() as cur:
        biz = cur.execute("select * from business where owner_address = %s", (sender,)).fetchone()
        if biz:
            return biz, False
        if config.CLAIM_DEMO_SHOP:
            biz = cur.execute("update business set owner_address = %s where id = %s and owner_address = 'demo-owner' "
                              "returning *", (sender, config.DEMO_BUSINESS_ID)).fetchone()
            if biz:
                return biz, True
        biz = cur.execute("insert into business (owner_address, name) values (%s, 'My Shop') returning *",
                          (sender,)).fetchone()
        return biz, True


def shop_context(business_id) -> ShopContext:
    items = db.fetch_all("select name from item where business_id = %s order by name", (business_id,))
    parties = db.fetch_all("select name from party where business_id = %s order by name", (business_id,))
    return ShopContext(items=[r["name"] for r in items], parties=[r["name"] for r in parties])


def mark_undone(business_id, action_id: str) -> None:
    with db.transaction() as cur:
        cur.execute("update action set status = 'undone' where id = %s and business_id = %s", (action_id, business_id))


def record_action(business_id, action_id: str, intent: str, fields: dict, source_msg_id: str) -> None:
    """The action row every committed row points at. Safe to repeat (retries reuse action_id)."""
    try:
        source = str(UUID(source_msg_id))
    except ValueError:
        source = None
    with db.transaction() as cur:
        cur.execute("insert into action (id, business_id, kind, payload, source_msg_id) values (%s, %s, %s, %s, %s) "
                    "on conflict do nothing",
                    (action_id, business_id, intent, db.Json(fields), source))


# Session and idempotency (G-7)

def seen(storage: Storage, msg_id: str) -> bool:
    ids = storage.get(SEEN_KEY) or []
    if msg_id in ids:
        return True
    storage.set(SEEN_KEY, (ids + [msg_id])[-SEEN_LIMIT:])
    return False


def session(storage: Storage, sender: str) -> dict:
    return storage.get(f"session:{sender}") or {}


def save_session(storage: Storage, sender: str, sess: dict) -> None:
    storage.set(f"session:{sender}", sess)


def live_pending(sess: dict) -> dict | None:
    p = sess.get("pending")
    return p if p and time.time() - p["created_at"] < PENDING_TTL else None


def set_asking(storage: Storage, sender: str, original: str | None) -> None:
    """Remember the message Arth just asked a question about (None clears it)."""
    sess = session(storage, sender)
    if original:
        sess["asking"] = {"text": original, "created_at": time.time()}
    else:
        sess.pop("asking", None)
    save_session(storage, sender, sess)


def with_context(storage: Storage, sender: str, body: str) -> str:
    """'UPI se poora' after 'Sunita ko 2 kg nails becha' -> both, so ASI1 sees the whole request."""
    asking = session(storage, sender).get("asking")
    if (asking and time.time() - asking["created_at"] < PENDING_TTL
            and len(body.split()) <= ANSWER_MAX_WORDS):
        return f"{asking['text']}. {body}"
    return body


# Replies

def help_card(shop_name: str) -> cards.Card:
    buttons = [cards.button(t, "example", text=t) for t in HELP_EXAMPLES]
    root = cards.section([
        cards.text("Tell me what happened in the shop, in English, Hindi or Hinglish. "
                   "I'll show you what I understood and write it down only after you confirm."),
        cards.heading("Try one", 3),
        cards.group(buttons, "column", gap=8),
    ], title=f"Arth for {shop_name}", subtitle="Khata, GST bills, stock, dues and restocking")
    fallback = "\n".join([f"Arth for {shop_name}: khata, GST bills, stock, dues and restocking.",
                          "Try one of these:", *(f"- {t}" for t in HELP_EXAMPLES)])
    return cards.custom(root, fallback)


def describe(ext: Extraction) -> str:
    """Human-readable echo of an extraction, used for intents whose specialist isn't built yet."""
    parts = [f"Intent: {ext.intent}"]
    if ext.party:
        parts.append(f"Party: {ext.party}")
    for line in ext.lines:
        amount = f"{qty(line.qty, line.unit)} " if line.qty is not None else ""
        rate = f" @ {inr(to_paise(line.rate))}" if line.rate is not None else ""
        parts.append(f"Item: {amount}{line.item}{rate}")
    if (total := ext.total_paise()) is not None:
        parts.append(f"Amount: {inr(total)}")
        if (paid := ext.paid_paise(total)) is not None:
            parts.append(f"Paid now: {inr(paid)}")
    if ext.mode:
        parts.append(f"Mode: {ext.mode}")
    if ext.discount_pct:
        parts.append(f"Discount: {ext.discount_pct:g}%")
    if ext.category:
        parts.append(f"Category: {ext.category}")
    if ext.period:
        parts.append(f"Period: {ext.period}")
    if ext.source == "fallback":
        parts.append("(understood offline, language service unavailable)")
    return "\n".join(parts)


def text(body: str) -> list[ChatMessage]:
    return [cards.text_message(body)]


# Plan -> review card

class PlanStop(Exception):
    """A specialist can't plan: unreachable, failed, or needs an answer from the user."""

    def __init__(self, reply: str, question: bool = False):
        super().__init__(reply)
        self.reply, self.question = reply, question


async def gather_plans(io: IO, request_id: str, business_id: str, intent: str, fields: dict,
                       agents: list[str]) -> dict[str, PlanResult]:
    plan = Plan(request_id=request_id, business_id=business_id, intent=intent, fields=fields)
    results: list[PlanResult | None] = await asyncio.gather(*(io.ask(a, plan, PlanResult) for a in agents))
    for agent, res in zip(agents, results):
        if res is None:
            raise PlanStop(NOT_REACHABLE.format(agent=f"Arth {agent.title()}"))
        if not res.ok:
            raise PlanStop(f"Ye entry nahi ban payi: {res.error} (I couldn't prepare this entry.)")
    for res in results:
        if res.question:
            raise PlanStop(res.question, question=True)
    return dict(zip(agents, results))


def save_pending(io: IO, sender: str, **pending) -> None:
    sess = session(io.storage, sender)
    sess["pending"] = {"action_id": str(uuid4()), "created_at": time.time(),
                       "after": COMMIT_AFTER.get(pending["intent"], []), **pending}
    save_session(io.storage, sender, sess)


def stop_reply(io: IO, sender: str, body: str, stop: PlanStop) -> list[ChatMessage]:
    if stop.question:
        set_asking(io.storage, sender, body)
    return text(stop.reply)


async def plan_write(io: IO, sender: str, msg_id: str, biz: dict, body: str,
                     ext: Extraction) -> list[ChatMessage]:
    request_id = str(uuid4())
    agents = PLAN_AGENTS[ext.intent]
    fields = ext.model_dump()
    try:
        by_agent = await gather_plans(io, request_id, str(biz["id"]), ext.intent, fields, agents)
    except PlanStop as stop:
        return stop_reply(io, sender, body, stop)

    rows = [r for res in by_agent.values() for r in res.summary_rows]
    title = by_agent[agents[0]].effects.get("title", ext.intent.title())
    oversell = any(res.effects.get("oversell") for res in by_agent.values())
    save_pending(io, sender, request_id=request_id, intent=ext.intent, fields=fields, business_id=str(biz["id"]),
                 msg_id=msg_id, title=title, rows=rows, effects={a: res.effects for a, res in by_agent.items()})
    card = cards.review(title, rows, request_id, approve_label="Sell anyway" if oversell else "Confirm")
    return [cards.card_message(card, "Check karke confirm kijiye:")]


# Bills: Billing prices and taxes the draft, then Ledger and Inventory plan the sale it implies

def merge_draft(fields: dict, edit: Extraction) -> Extraction:
    """Apply an edit ("5% discount", "make it 15 bags", "add 2 bags putty") to the draft's fields."""
    draft = Extraction.model_validate(fields)
    update = {k: getattr(edit, k) for k in ("party", "discount_pct", "mode", "paid", "paid_fraction")
              if getattr(edit, k) is not None}
    lines = [l.model_copy() for l in draft.lines]
    for new in edit.lines:
        same = next((l for l in lines if l.item.lower() == new.item.lower()), None)
        if same:
            same.qty = new.qty if new.qty is not None else same.qty
            same.rate = new.rate if new.rate is not None else same.rate
        else:
            lines.append(new)
    return draft.model_copy(update={**update, "lines": lines, "missing": []})


def is_draft_edit(pending: dict | None, ext: Extraction) -> bool:
    if not pending or pending["intent"] != "bill" or ext.intent != "bill":
        return False
    return not ext.party or best_match(ext.party, [pending["party_name"]]).name is not None


async def plan_bill(io: IO, sender: str, msg_id: str, biz: dict, body: str, ext: Extraction) -> list[ChatMessage]:
    request_id, business_id = str(uuid4()), str(biz["id"])
    fields = ext.model_dump()
    told_payment = ext.mode or ext.paid is not None or ext.paid_fraction is not None
    priced = ext if told_payment else ext.model_copy(update={"mode": "credit"})   # bills are on credit by default
    try:
        bill = (await gather_plans(io, request_id, business_id, "bill", priced.model_dump(), ["billing"]))["billing"]
        e = bill.effects
        sale = {**priced.model_dump(), "intent": "sale", "party": e["party"]["name"],
                "amount": e["total_paise"] / 100, "invoice_id": e["invoice_id"],
                "lines": [{"item": l["item"], "qty": l["qty"], "unit": l["unit"]} for l in e["lines"]]}
        by_agent = await gather_plans(io, request_id, business_id, "sale", sale, ["ledger", "inventory"])
    except PlanStop as stop:
        return stop_reply(io, sender, body, stop)

    rows = bill.summary_rows + by_agent["ledger"].summary_rows + by_agent["inventory"].summary_rows
    oversell = by_agent["inventory"].effects.get("oversell")
    draft = f"bill for {e['party']['name']}: " + ", ".join(f"{qty(l['qty'], l['unit'])} {l['item']}" for l in e["lines"])
    if e["discount_pct"]:
        draft += f", {e['discount_pct']:g}% discount"
    save_pending(io, sender, request_id=request_id, intent="bill", fields=fields, business_id=business_id,
                 msg_id=msg_id, party_name=e["party"]["name"], draft=draft, bill_rows=bill.summary_rows,
                 title=e["title"], rows=bill.summary_rows,
                 effects={"billing": e, "ledger": by_agent["ledger"].effects,
                          "inventory": by_agent["inventory"].effects})
    card = cards.detail(e["title"], rows, [
        cards.cta("Issue anyway" if oversell else "Confirm & issue", "confirm", request_id, primary=True),
        cards.cta("Cancel", "cancel", request_id),
    ])
    return [cards.card_message(card, 'Bill ka draft. Badlav ke liye likhiye, jaise "5% discount". '
                                     '(Draft bill. Type a change like "5% discount", or confirm.)')]


# Collections: dues list (read-only) and reminders (planned, approved, then shared via WhatsApp)

async def answer_query(io: IO, biz: dict, agent: str, intent: str, fields: dict) -> list[ChatMessage]:
    res: QueryResult | None = await io.ask(agent, Query(request_id=str(uuid4()), business_id=str(biz["id"]),
                                                        intent=intent, fields=fields), QueryResult)
    if res is None:
        return text(NOT_REACHABLE.format(agent=f"Arth {agent.title()}"))
    if not res.ok:
        return text(f"Ye jaankari nahi mil payi: {res.text} (I couldn't get that.)")
    if res.card_kind:
        return [cards.card_message(cards.Card(res.card_kind, res.card_payload, res.text, res.terminal))]
    return text(res.text)


async def plan_reminder(io: IO, sender: str, msg_id: str, biz: dict, who: dict) -> list[ChatMessage]:
    """who: {"party_id": ...} from a Remind button, or {"party": name} from typed text."""
    request_id = str(uuid4())
    try:
        res = (await gather_plans(io, request_id, str(biz["id"]), "reminder", who, ["collections"]))["collections"]
    except PlanStop as stop:
        return text(stop.reply)
    e = res.effects
    save_pending(io, sender, request_id=request_id, intent="reminder", fields=who, business_id=str(biz["id"]),
                 msg_id=msg_id, title=e["title"], rows=res.summary_rows[:2], effects={"collections": e})
    card = cards.detail(e["title"], res.summary_rows, [
        cards.cta("Approve & get WhatsApp link", "confirm", request_id, primary=True),
        cards.cta("Cancel", "cancel", request_id),
    ], hero_image=e.get("qr"))
    return [cards.card_message(card, "Reminder ka draft. Theek lage to approve kijiye:")]


# Restock: Procurement collects and negotiates quotes (carousel), the owner picks one, then approves

async def plan_restock(io: IO, sender: str, msg_id: str, biz: dict, body: str, ext: Extraction) -> list[ChatMessage]:
    request_id = str(uuid4())
    try:
        res = (await gather_plans(io, request_id, str(biz["id"]), "restock", ext.model_dump(), ["procurement"]))["procurement"]
    except PlanStop as stop:
        return stop_reply(io, sender, body, stop)
    e = res.effects
    save_pending(io, sender, request_id=request_id, intent="restock_quotes", fields=ext.model_dump(),
                 business_id=str(biz["id"]), msg_id=msg_id, po_id=e["po_id"], suppliers=e["suppliers"], effects={})
    narration = f"{len(e['suppliers'])} supplier agents se quote liye aur ek round mol-bhav kiya. Kaunsa lein?"
    if e.get("suggested"):
        narration = (f"Quantity nahi batayi, to {qty(e['qty'], e['unit'])} maan liya (reorder level ka do guna). "
                     + narration)
    return [cards.card_message(cards.Card("carousel", e["card"], e["fallback"]), narration)]


def chosen_supplier(pending: dict | None, body: str) -> str | None:
    """'Gupta wala' or 'shree traders' while quotes are on screen -> that supplier."""
    if not pending or pending["intent"] != "restock_quotes":
        return None
    words = set(re.sub(r"[^\w\s]", " ", body.lower()).split())
    hits = [s for s in pending["suppliers"] if s.lower().split()[0] in words or s.lower() == body.strip().lower()]
    return hits[0] if len(hits) == 1 else None


async def plan_choice(io: IO, sender: str, pending: dict, supplier: str) -> list[ChatMessage]:
    request_id = str(uuid4())
    try:
        res = (await gather_plans(io, request_id, pending["business_id"], "restock_choice",
                                  {"po_id": pending["po_id"], "supplier": supplier}, ["procurement"]))["procurement"]
    except PlanStop as stop:
        return text(stop.reply)
    e = res.effects
    save_pending(io, sender, request_id=request_id, intent="restock", fields=pending["fields"],
                 business_id=pending["business_id"], msg_id=pending["msg_id"], po_id=e["po_id"],
                 suppliers=pending["suppliers"], title=e["title"], rows=res.summary_rows, effects={"procurement": e})
    card = cards.review(e["title"], res.summary_rows, request_id, approve_label="Approve order")
    return [cards.card_message(card, "Order check kijiye. Approve karne ke baad payment hoga:")]


async def cancel_po(io: IO, pending: dict) -> None:
    """Tell Procurement the owner backed out, so the PO doesn't sit open. Best effort."""
    await io.ask("procurement", Commit(request_id=pending["request_id"], action_id=pending["action_id"],
                                       business_id=pending["business_id"],
                                       effects={"cancel": True, "po_id": pending["po_id"]}), Committed)


# Undo: confirm first, then mark the action undone and let each specialist reverse its part

def ago(ts: float) -> str:
    minutes = int((time.time() - ts) // 60)
    if minutes < 1:
        return "abhi abhi"
    if minutes < 60:
        return f"{minutes} min pehle"
    return f"{minutes // 60} ghante pehle"


def plan_undo(io: IO, sender: str, msg_id: str, biz: dict) -> list[ChatMessage]:
    history = session(io.storage, sender).get("history") or []
    if not history:
        return text("Undo karne ke liye kuch nahi hai. (There's nothing to undo.)")
    entry = history[0]
    payment = io.storage.get(f"payment:{entry['po_id']}") if entry.get("po_id") else None
    if entry.get("locked") or (payment and payment["status"] != "awaiting"):
        return text(f"{entry['title']}: payment ho chuka hai, ise undo nahi kar sakte. Supplier se baat kijiye. "
                    "(This order has been paid for, so it can't be undone here.)")
    request_id = str(uuid4())
    save_pending(io, sender, request_id=request_id, intent="undo", fields={}, business_id=str(biz["id"]),
                 msg_id=msg_id, effects={}, undo=entry)
    rows = entry["rows"][:6] + [{"label": "Recorded", "value": ago(entry["at"])}]
    card = cards.review(f"Undo: {entry['title']}?", rows, request_id, approve_label="Undo", reject_label="Keep it")
    return [cards.card_message(card, "Ye entry hata dun? (Remove this entry?)")]


async def do_undo(io: IO, sender: str, sess: dict, pending: dict) -> list[ChatMessage]:
    entry = pending["undo"]
    await asyncio.to_thread(mark_undone, pending["business_id"], entry["action_id"])
    agents = entry["agents"]
    results: list[UndoDone | None] = await asyncio.gather(*(
        io.ask(a, Undo(action_id=entry["action_id"]), UndoDone) for a in agents))
    failed = [a for a, r in zip(agents, results) if r is None or not r.ok]
    if failed:
        names = ", ".join(f"Arth {a.title()}" for a in failed)
        return text(f"{names} ne undo confirm nahi kiya. 'confirm' dobara bhejiye. "
                    f"({names} didn't confirm the undo. Send 'confirm' again to retry.)")
    sess.pop("pending", None)
    sess["history"] = [h for h in sess.get("history", []) if h["action_id"] != entry["action_id"]]
    save_session(io.storage, sender, sess)
    if entry.get("po_id") and io.storage.get(f"payment:{entry['po_id']}"):
        set_payment(io.storage, entry["po_id"], status="cancelled")
    lines = [f"↩ Undo ho gaya: {entry['title']}.", *(r.message for r in results if r.message)]
    return text("\n".join(lines))


# Restock payments

def drop_history(storage: Storage, owner: str, action_id: str) -> None:
    sess = session(storage, owner)
    sess["history"] = [h for h in sess.get("history", []) if h["action_id"] != action_id]
    save_session(storage, owner, sess)


def set_payment(storage: Storage, po_id: str, **changes) -> dict:
    record = {**(storage.get(f"payment:{po_id}") or {}), **changes}
    storage.set(f"payment:{po_id}", record)
    return record


def request_payment(io: IO, sender: str, pending: dict, messages: list[str]) -> list[Model]:
    """Approved restock -> RequestPayment for order total + Arth fee, paid to the orchestrator's wallet."""
    e = pending["effects"]["procurement"]
    atestfet = int(e["pay_atestfet"])
    set_payment(io.storage, e["po_id"], status="awaiting", owner=sender, business_id=pending["business_id"],
                action_id=pending["action_id"], atestfet=str(atestfet), title=e["title"], supplier=e["supplier"],
                item=e["item"], qty=e["qty"], unit=e["unit"], fee_paise=e["fee_paise"], created_at=time.time())
    request = RequestPayment(
        accepted_funds=[Funds(amount=chain.fet_amount(atestfet), currency="FET", payment_method="fet_direct")],
        recipient=chain.address("orchestrator"), deadline_seconds=PAYMENT_DEADLINE, reference=e["po_id"],
        description=f"{e['title']}: {inr(e['total_paise'])} + Arth fee {inr(e['fee_paise'])}",
        metadata={"fet_network": chain.NETWORK_NAME, "po_id": e["po_id"]})
    lines = [*messages, f"Ab {inr(e['pay_paise'])} = {fet(atestfet)} ka payment approve kijiye (Approve FET Payment). "
                        "Payment milte hi supplier ko order aur payment chala jayega.",
             "(Approve the FET payment; the order is placed with the supplier as soon as it arrives.)"]
    return [cards.text_message("\n".join(lines)), request]


def awaiting_for(storage: Storage, sender: str, reference: str | None) -> tuple[str, dict] | None:
    """The payment record a CommitPayment is for: by its reference, else the owner's latest approved restock."""
    if reference and (rec := storage.get(f"payment:{reference}")) and rec.get("owner") == sender:
        return reference, rec
    for h in session(storage, sender).get("history", []):
        rec = h.get("po_id") and storage.get(f"payment:{h['po_id']}")
        if rec and rec["status"] == "awaiting":
            return h["po_id"], rec
    return None


async def on_commit_payment(sender: str, msg: CommitPayment, io: IO) -> None:
    found = awaiting_for(io.storage, sender, msg.reference)
    tx = msg.transaction_id
    if found and found[1].get("user_tx") == tx:          # the same commit again: already handled
        await io.send(sender, CompletePayment(transaction_id=tx))
        return
    if not found or found[1]["status"] != "awaiting":
        await io.send(sender, CancelPayment(transaction_id=tx, reason="no restock payment is pending"))
        return
    po_id, rec = found
    payer = (msg.metadata or {}).get("buyer_fet_wallet") or (msg.metadata or {}).get("buyer_fet_address")
    used = io.storage.get("used_txs") or []
    reason = ("transaction already used" if tx in used else
              "buyer_fet_wallet missing from payment metadata" if not payer else
              "only FET payments are accepted" if msg.funds.currency != "FET" else
              await asyncio.to_thread(chain.verify_transfer, tx, payer, chain.address("orchestrator"),
                                      int(rec["atestfet"])))
    if reason:
        await io.send(sender, CancelPayment(transaction_id=tx, reason=reason))
        await io.send(sender, cards.text_message(f"Payment verify nahi hua: {reason}. Order abhi ruka hai. "
                                                 f"(Payment couldn't be verified, so the order is on hold.)"))
        return
    io.storage.set("used_txs", (used + [tx])[-SEEN_LIMIT:])
    set_payment(io.storage, po_id, status="paying_supplier", user_tx=tx, payer=payer)
    await io.send(sender, CompletePayment(transaction_id=tx))
    await io.send(sender, cards.text_message(
        f"✅ Payment mil gaya: {fet(int(rec['atestfet']))} ({chain.explorer(tx)}).\n"
        f"{rec['supplier']} ko order aur payment bhej raha hoon… (Paying the supplier now.)"))
    await io.tell("procurement", PayOrder(request_id=str(uuid4()), business_id=rec["business_id"], po_id=po_id,
                                          user_tx=tx, buyer_wallet=payer, paid_atestfet=rec["atestfet"]))


async def on_reject_payment(sender: str, msg: RejectPayment, io: IO) -> None:
    found = awaiting_for(io.storage, sender, None)
    if not found:
        return
    po_id, rec = found
    set_payment(io.storage, po_id, status="cancelled")
    drop_history(io.storage, sender, rec["action_id"])
    await io.ask("procurement", Commit(request_id=str(uuid4()), action_id=rec["action_id"], business_id=rec["business_id"],
                                       effects={"cancel": True, "po_id": po_id}), Committed)
    await asyncio.to_thread(mark_undone, rec["business_id"], rec["action_id"])
    await io.send(sender, cards.text_message("Payment cancel hua, to order bhi cancel kar diya. "
                                             "(Payment declined, so the order is cancelled.)"))


async def post_purchase(io: IO, rec: dict, res: OrderResult) -> list[str]:
    """The confirmed order into the khata and stock, under the restock's own action."""
    total = res.total_paise / 100
    fields = Extraction(intent="purchase", party=res.supplier, amount=total, paid=total, mode="bank",
                        lines=[Line(item=rec["item"], qty=rec["qty"], unit=rec["unit"],
                                    rate=res.unit_price_paise / 100)]).model_dump()
    request_id = str(uuid4())
    by_agent = await gather_plans(io, request_id, rec["business_id"], "purchase", fields, ["ledger", "inventory"])
    ledger = by_agent["ledger"].effects
    for t in ledger["txns"]:
        if t["kind"] == "payment":
            t["note"] = f"FET tx {res.supplier_tx[:12]}…"
    ledger["txns"].append({"kind": "expense", "amount_paise": rec["fee_paise"], "mode": "bank", "no_party": True,
                           "category": "arth fee", "note": f"Arth restock fee, FET tx {rec['user_tx'][:12]}…"})
    effects = {"ledger": ledger, "inventory": by_agent["inventory"].effects}
    results = await asyncio.gather(*(io.ask(a, Commit(request_id=request_id, action_id=rec["action_id"],
                                                      business_id=rec["business_id"], effects=e), Committed)
                                     for a, e in effects.items()))
    failed = [a for a, r in zip(effects, results) if r is None or not r.ok]
    if failed:
        raise PlanStop(f"Arth {', '.join(failed)} ne entry save nahi ki")
    return [r.message for r in results if r.message]


async def on_order_result(res: OrderResult, io: IO) -> None:
    rec = io.storage.get(f"payment:{res.po_id}")
    if not rec or rec["status"] in ("confirmed", "failed"):
        return
    owner = rec["owner"]
    if not res.ok:
        set_payment(io.storage, res.po_id, status="failed")
        drop_history(io.storage, owner, rec["action_id"])
        await asyncio.to_thread(mark_undone, rec["business_id"], rec["action_id"])
        refund = (f"Aapke {fet(int(rec['atestfet']))} wapas bhej diye: {chain.explorer(res.refund_tx)}" if res.refund_tx else
                  "Refund abhi nahi ho paya; hum ise theek karenge.")
        await io.send(owner, cards.text_message(
            f"❌ Koi supplier order poora nahi kar paya ({res.message}). {refund}\n"
            "(No supplier completed the order; your payment has been refunded.)"))
        return
    set_payment(io.storage, res.po_id, status="confirmed", supplier_tx=res.supplier_tx)
    lines = [f"📦 Order confirmed: {qty(rec['qty'], rec['unit'])} {rec['item']} from {res.supplier} "
             f"@ {inr(res.unit_price_paise)} = {inr(res.total_paise)}.",
             f"Supplier ko payment: {chain.explorer(res.supplier_tx)}"]
    if res.dispatch_eta:
        lines.append(f"Delivery: {res.dispatch_eta} tak.")
    if res.supplier != rec["supplier"]:
        lines.append(f"({rec['supplier']} order poora nahi kar paya, isliye agla best supplier liya: {res.message})")
    try:
        lines += await post_purchase(io, rec, res)
    except PlanStop as stop:
        lines.append(f"⚠ Order ho gaya, par khata update nahi hua: {stop.reply}")
    sess = session(io.storage, owner)
    for h in sess.get("history", []):
        if h["action_id"] == rec["action_id"]:
            h["locked"] = True
            h["agents"] = sorted(set(h["agents"]) | {"ledger", "inventory"})
    save_session(io.storage, owner, sess)
    await io.send(owner, cards.text_message("\n".join(lines)))


def invoice_card(pending: dict, number: str) -> cards.Card:
    rows = [{"label": "Invoice no", "value": number} if r["label"] == "Invoice no" else r for r in pending["bill_rows"]]
    card = cards.detail(f"Invoice {number}", rows, [])
    card.terminal = True
    return card


# Confirm / cancel

async def handle_selection(io: IO, sender: str, sel: dict) -> list[ChatMessage]:
    sess = session(io.storage, sender)
    pending = live_pending(sess)
    if not pending:
        return text("Abhi confirm karne ke liye kuch nahi hai. (Nothing is waiting for confirmation.)")
    if sel.get("request_id") and sel["request_id"] != pending["request_id"]:
        return text("Ye purana card hai, sabse naya wala use kijiye. (That card is out of date; please use the latest one.)")
    if sel["action"] == "cancel":
        sess.pop("pending", None)
        save_session(io.storage, sender, sess)
        if pending.get("po_id") and pending["intent"] != "undo":
            await cancel_po(io, pending)
            return text("Theek hai, order cancel kar diya. (Cancelled, no order was placed.)")
        if pending["intent"] == "undo":
            return text("Theek hai, entry rehne di. (Kept it, nothing was undone.)")
        return text("Theek hai, cancel kar diya. Kuch nahi likha. (Cancelled, nothing was recorded.)")
    if pending["intent"] == "undo":
        return await do_undo(io, sender, sess, pending)
    if pending["intent"] == "restock_quotes":      # "haan" at the quotes = the best-value supplier
        return await plan_choice(io, sender, pending, sel.get("supplier") or pending["suppliers"][0])

    await asyncio.to_thread(record_action, pending["business_id"], pending["action_id"], pending["intent"],
                            pending["fields"], pending["msg_id"])
    results: list[Committed] = []
    for agents in (list(pending["effects"]), pending.get("after", [])):
        round_results: list[Committed | None] = await asyncio.gather(*(
            io.ask(a, Commit(request_id=pending["request_id"], action_id=pending["action_id"],
                             business_id=pending["business_id"],
                             effects=pending["effects"].get(a) or {"after_money": True}), Committed)
            for a in agents))
        failed = [a for a, r in zip(agents, round_results) if r is None or not r.ok]
        if failed:
            # Pending is kept: commits are idempotent by action_id, so "confirm" again is a safe retry.
            names = ", ".join(f"Arth {a.title()}" for a in failed)
            return text(f"{names} ne save confirm nahi kiya. 'confirm' dobara bhejiye, kuch double nahi hoga. "
                        f"({names} didn't confirm the save. Send 'confirm' again to retry; "
                        "nothing will be counted twice.)")
        results += round_results

    sess.pop("pending", None)
    entry = {"action_id": pending["action_id"], "title": pending.get("title", pending["intent"].title()),
             "rows": (pending.get("rows") or [])[:6], "at": time.time(), "po_id": pending.get("po_id"),
             "agents": [a for a in ALL_SPECIALISTS if a in pending["effects"] or a in pending.get("after", [])]}
    sess["history"] = ([entry] + sess.get("history", []))[:HISTORY_LIMIT]
    save_session(io.storage, sender, sess)
    if pending["intent"] == "restock":
        return request_payment(io, sender, pending, [r.message for r in results if r.message])
    lines = ["✅ Ho gaya!", *(r.message for r in results if r.message)]
    invoice = None
    for f in (f for r in results for f in r.follow_ups):
        if f["type"] == "low_stock":
            lines.append(f"⚠ {f['item']} kam hai: sirf {qty(f['stock_qty'], f['unit'])} bacha "
                         f"(reorder level {qty(f['reorder_level'])}).")
        elif f["type"] == "invoice":
            invoice = f
    if invoice:
        return [cards.card_message(invoice_card(pending, invoice["number"]), "\n".join(lines))]
    return text("\n".join(lines))


# Entry point

async def respond(sender: str, msg_id: str, body: str, io: IO) -> list[ChatMessage]:
    if seen(io.storage, msg_id):
        return []
    body = body.strip()
    if not body:
        return []
    try:
        biz, is_new = await asyncio.to_thread(ensure_business, sender)
    except Exception:
        return text("Abhi records khul nahi rahe, ek minute mein phir try karein. "
                    "(I can't reach the shop records right now, please try again in a minute.)")
    replies = text(f"Namaste! Arth is set up for {biz['name']}.") if is_new else []

    sel = cards.parse_selection(body)
    if sel and sel.get("action") == "example" and sel.get("text"):
        body, sel = sel["text"], None
    if sel and sel.get("action") in ("confirm", "cancel"):
        return replies + await handle_selection(io, sender, sel)
    if sel and sel.get("action") == "choose_supplier":
        return replies + await handle_selection(io, sender, {**sel, "action": "confirm"})
    if sel and sel.get("action") == "remind" and (sel.get("party_id") or sel.get("party")):
        return replies + await plan_reminder(io, sender, msg_id, biz,
                                             {"party_id": sel.get("party_id"), "party": sel.get("party")})

    body = with_context(io.storage, sender, body)
    set_asking(io.storage, sender, None)
    pending = live_pending(session(io.storage, sender))
    if supplier := chosen_supplier(pending, body):
        return replies + await plan_choice(io, sender, pending, supplier)
    try:
        shop = await asyncio.to_thread(shop_context, biz["id"])
        shop.draft = pending.get("draft") if pending else None
        ext = await llm.extract(body, shop)
    except ExtractionError as err:
        return replies + text(err.question)
    except Exception:
        return replies + text("Kuch gadbad ho gayi, phir se bhejiye. (Something went wrong, please send that again.)")

    if is_draft_edit(pending, ext):
        return replies + await plan_bill(io, sender, msg_id, biz, body, merge_draft(pending["fields"], ext))
    if ext.intent == "help":
        return replies + [cards.card_message(help_card(biz["name"]))]
    if ext.missing:
        set_asking(io.storage, sender, body)
        return replies + text(llm.question_for(ext.missing))
    if ext.intent in QUERY_AGENTS:
        agent, fields_of = QUERY_AGENTS[ext.intent]
        return replies + await answer_query(io, biz, agent, ext.intent, fields_of(ext))
    if ext.intent == "question":
        return replies + await answer_query(io, biz, "insights", "question", {"period": ext.period, "text": body})
    if ext.intent == "undo":
        if pending and pending["intent"] == "undo":           # "undo" typed at the undo card means yes
            return replies + await handle_selection(io, sender, {"action": "confirm"})
        return replies + plan_undo(io, sender, msg_id, biz)
    if ext.intent == "dues":
        if ext.party:
            return replies + await plan_reminder(io, sender, msg_id, biz, {"party": ext.party})
        return replies + await answer_query(io, biz, "collections", "dues", {})
    if ext.intent == "bill":
        return replies + await plan_bill(io, sender, msg_id, biz, body, ext)
    if ext.intent == "restock":
        return replies + await plan_restock(io, sender, msg_id, biz, body, ext)
    if ext.intent in PLAN_AGENTS:
        return replies + await plan_write(io, sender, msg_id, biz, body, ext)
    return replies + text(f"Samajh gaya:\n{describe(ext)}\n\nThis one is coming in a later build step.")
