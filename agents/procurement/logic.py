"""Procurement: restocking through supplier agents.

- plan "restock": open a purchase_order, send RFQ to every linked supplier agent, collect quotes for
  QUOTE_WAIT seconds, then one counter round: the two cheapest get Counter(best price - 2%). Returns a
  carousel comparing the quotes. The PO is Procurement's working record, so planning writes it.
- plan "restock_choice": price the chosen quote with Arth's fee, in rupees and FET, for the review card.
- commit: the approved order (status awaiting_user_payment), or {"cancel": True} when the owner backs out.
- pay_order (after the orchestrator verified the owner's payment, leg 1): PlaceOrder to the chosen
  supplier, pay its RequestPayment from Procurement's wallet, CommitPayment, wait for OrderConfirmed
  (leg 2). A commit the supplier can't verify is retried once; then the next-best quote is tried at its
  own price. If no supplier completes, the owner is refunded and the order marked failed.
- undo: cancels an order that hasn't been paid for.

Supplier agents are reached through a SupplierIO, so tests can run the suppliers' logic in-process.
"""

import asyncio
import math
from datetime import timedelta
from typing import Protocol

from uagents import Model
from uagents_core.contrib.protocols.payment import CancelPayment, CommitPayment, CompletePayment, RequestPayment

from agents.supplier import logic as supplier_logic
from core import cards, chain, clock, config, db, lookup, net
from core.fuzzy import best_match
from core.lang import DEFAULT, of, pick
from core.models import RFQ, Committed, Counter, OrderConfirmed, OrderResult, PlaceOrder, PlanResult, Quote, UndoDone
from core.money import fet, inr, paise_to_atestfet, percent_of, qty as fmt_qty

QUOTE_WAIT = 6              # seconds to wait for quotes; a silent supplier is skipped
COUNTER_WAIT = 4
COUNTER_PCT = 2             # ask the top two for this much below the best quote
COUNTER_ROUND_SIZE = 2
NEED_BY_DAYS = 3
ORDER_WAIT = 10             # PlaceOrder -> RequestPayment
COMMIT_WAIT = 60            # CommitPayment -> CompletePayment; the supplier waits for the chain
COMMIT_TRIES = 2            # one retry of the same transaction before moving to the next supplier
SUPPLIER_CONFIGS = config.ROOT / "suppliers"


class SupplierIO(Protocol):
    async def ask(self, address: str, msg: Model, response_type: type[Model] | set[type[Model]],
                  timeout: float) -> Model | None:
        """Send msg to a supplier agent and wait for its reply; None on timeout or delivery failure."""

    async def commit_payment(self, address: str, msg: CommitPayment, timeout: float
                             ) -> tuple[CompletePayment | CancelPayment | None, OrderConfirmed | None]:
        """Send CommitPayment; return the supplier's answer and, after CompletePayment, its OrderConfirmed."""


# Discovery: seeded supplier parties get the address of the matching supplier agent

def link_suppliers(business_id: str) -> None:
    for s in supplier_logic.load_all(SUPPLIER_CONFIGS):
        try:
            addr = net.address(s.agent)
        except RuntimeError:                # no seed on this machine: that supplier runs elsewhere
            continue
        with db.transaction() as cur:
            cur.execute("update party set agent_address = %s where business_id = %s and kind = 'supplier' "
                        "and name = %s and agent_address is distinct from %s", (addr, business_id, s.name, addr))


def linked_suppliers(business_id: str) -> list[dict]:
    return db.fetch_all("select id, name, agent_address from party where business_id = %s and kind = 'supplier' "
                        "and agent_address is not null order by name", (business_id,))


# Quotes and the counter round

async def collect(suppliers: SupplierIO, parties: list[dict], msgs: list[Model], wait: float) -> list[Quote | None]:
    return await asyncio.gather(*(suppliers.ask(p["agent_address"], m, Quote, wait) for p, m in zip(parties, msgs)))


def rank_key(q: dict):
    return q["unit_price_paise"], q["delivery_days"], -q["rating"]


async def negotiate(suppliers: SupplierIO, po_id: str, item: str, amount: float,
                    parties: list[dict]) -> tuple[list[dict], list[str], list[str]]:
    """Returns (quotes cheapest first, names that didn't reply, names that don't stock it)."""
    rfq = RFQ(po_id=po_id, item=item, qty=amount, need_by=(clock.today() + timedelta(days=NEED_BY_DAYS)).isoformat())
    replies = await collect(suppliers, parties, [rfq] * len(parties), QUOTE_WAIT)
    quotes, silent, unstocked = [], [], []
    for p, q in zip(parties, replies):
        if q is None:
            silent.append(p["name"])
        elif not q.ok:
            unstocked.append(p["name"])
        else:
            quotes.append({"party_id": str(p["id"]), "supplier": p["name"], "address": p["agent_address"],
                           "first_price_paise": q.unit_price_paise, "unit_price_paise": q.unit_price_paise,
                           "delivery_days": q.delivery_days, "rating": q.rating, "countered": False})
    quotes.sort(key=rank_key)
    if not quotes:
        return quotes, silent, unstocked

    target = percent_of(quotes[0]["unit_price_paise"], 100 - COUNTER_PCT)
    top = quotes[:COUNTER_ROUND_SIZE]
    counters = await collect(suppliers, [{"agent_address": q["address"]} for q in top],
                             [Counter(po_id=po_id, target_unit_price_paise=target)] * len(top), COUNTER_WAIT)
    for q, c in zip(top, counters):
        q["countered"] = True
        if c is not None and c.ok and c.unit_price_paise < q["unit_price_paise"]:
            q["unit_price_paise"] = c.unit_price_paise
    quotes.sort(key=rank_key)
    return quotes, silent, unstocked


def order_total(unit_price_paise: int, amount: float) -> int:
    return round(unit_price_paise * amount)


def suggested_qty(item: dict) -> float:
    """Enough to reach twice the reorder level, at least one reorder level's worth."""
    level, stock = float(item["reorder_level"]), float(item["stock_qty"])
    return float(max(math.ceil(2 * level - stock), math.ceil(level), 1))


def quotes_card(request_id: str, item: dict, amount: float, quotes: list[dict], silent: list[str],
                unstocked: list[str], lang: str = DEFAULT) -> cards.Card:
    unit = item["unit"]
    fastest = min(q["delivery_days"] for q in quotes)
    top_rating = max(q["rating"] for q in quotes)
    entries = []
    for n, q in enumerate(quotes):
        badges = []
        if n == 0:
            badges.append(cards.badge_("Best value", "success"))
        if q["delivery_days"] == fastest and len(quotes) > 1:
            badges.append(cards.badge_("Fastest", "info"))
        if q["rating"] == top_rating and len(quotes) > 1:
            badges.append(cards.badge_("Top rated", "info"))
        secondary = f"Total {inr(order_total(q['unit_price_paise'], amount))}"
        if q["unit_price_paise"] < q["first_price_paise"]:
            secondary += pick(lang, " · mol-bhav: ", " · negotiated: ") + f"{inr(q['first_price_paise'])} → {inr(q['unit_price_paise'])}"
        elif q["countered"]:
            secondary += pick(lang, " · counter-offer nahi maana", " · declined our counter-offer")
        days = pick(lang, f"{q['delivery_days']} din", f"{q['delivery_days']} {'day' if q['delivery_days'] == 1 else 'days'}")
        entries.append(cards.carousel_item(
            q["supplier"], q["supplier"],
            f"{inr(q['unit_price_paise'])}/{unit} · {days} · ★{q['rating']:g}",
            cards.cta("Choose", "choose_supplier", request_id, primary=n == 0, supplier=q["supplier"]),
            secondary_text=secondary, badges=badges))
    notes = [pick(lang, f"{len(quotes)} quote, ek round mol-bhav", f"{len(quotes)} quotes, 1 round of counter-offers"),
             pick(lang, f"Arth fee {config.ARTH_FEE_PCT:g}% approve karne par", f"Arth fee {config.ARTH_FEE_PCT:g}% at approval")]
    if silent:
        notes.append(pick(lang, f"jawab nahi aaya: {', '.join(silent)}", f"no reply: {', '.join(silent)}"))
    if unstocked:
        notes.append(pick(lang, f"inke paas nahi hai: {', '.join(unstocked)}", f"don't stock it: {', '.join(unstocked)}"))
    title = pick(lang, f"{fmt_qty(amount, unit)} {item['name']} ke quote", f"Quotes for {fmt_qty(amount, unit)} {item['name']}")
    return cards.carousel(title, entries, " · ".join(notes))


def create_po(business_id: str, item_id, amount: float) -> str:
    with db.transaction() as cur:
        row = cur.execute("insert into purchase_order (business_id, item_id, qty) values (%s, %s, %s) returning id",
                          (business_id, item_id, amount)).fetchone()
    return str(row["id"])


def set_po(po_id: str, **cols) -> None:
    sets = ", ".join(f"{k} = %s" for k in cols)
    with db.transaction() as cur:
        cur.execute(f"update purchase_order set {sets} where id = %s",
                    [db.Json(v) if isinstance(v, (list, dict)) else v for v in cols.values()] + [po_id])


async def plan_restock(request_id: str, business_id: str, fields: dict, suppliers: SupplierIO) -> PlanResult:
    lines, lang = fields.get("lines") or [], of(fields)
    if not lines:
        return PlanResult(request_id=request_id, ok=True,
                          question=pick(lang, "Kaunsa saamaan mangwana hai?", "Which item should I restock?"))
    found = await asyncio.to_thread(lookup.find_item, business_id, lines[0]["item"], lang)
    if found.question:
        return PlanResult(request_id=request_id, ok=True, question=found.question)
    item = found.row
    amount = float(lines[0]["qty"]) if lines[0].get("qty") else suggested_qty(item)
    await asyncio.to_thread(link_suppliers, business_id)
    parties = await asyncio.to_thread(linked_suppliers, business_id)
    if not parties:
        return PlanResult(request_id=request_id, ok=False,
                          error=pick(lang, "koi supplier agent juda nahi hai", "no supplier agents are linked"))

    po_id = await asyncio.to_thread(create_po, business_id, item["id"], amount)
    quotes, silent, unstocked = await negotiate(suppliers, po_id, item["name"], amount, parties)
    if not quotes:
        await asyncio.to_thread(set_po, po_id, status="failed", quotes=[])
        why = (pick(lang, "kisi supplier ne jawab nahi diya", "no supplier replied") if not unstocked
               else pick(lang, f"{', '.join(unstocked)} ke paas nahi hai", f"{', '.join(unstocked)} don't stock it"))
        return PlanResult(request_id=request_id, ok=False,
                          error=pick(lang, f"{item['name']} ka quote nahi mila: {why}", f"no quote for {item['name']}: {why}"))
    await asyncio.to_thread(set_po, po_id, status="awaiting_approval", quotes=quotes)

    card = quotes_card(request_id, item, amount, quotes, silent, unstocked, lang)
    rows = [{"label": "Item", "value": f"{fmt_qty(amount, item['unit'])} {item['name']}"},
            {"label": "Best quote", "value": f"{quotes[0]['supplier']} @ {inr(quotes[0]['unit_price_paise'])}"}]
    if not lines[0].get("qty"):
        rows.append({"label": "Quantity", "value": f"suggested: stock {fmt_qty(item['stock_qty'], item['unit'])}, "
                                                   f"reorder level {fmt_qty(item['reorder_level'])}"})
    return PlanResult(request_id=request_id, ok=True, summary_rows=rows, effects={
        "po_id": po_id, "item": item["name"], "qty": amount, "unit": item["unit"],
        "suppliers": [q["supplier"] for q in quotes], "card": card.payload, "fallback": card.fallback,
        "suggested": not lines[0].get("qty")})


def load_po(po_id: str, business_id: str) -> dict | None:
    return db.fetch_one("select po.*, i.name as item_name, i.unit from purchase_order po join item i on i.id = po.item_id "
                        "where po.id = %s and po.business_id = %s", (po_id, business_id))


def plan_choice(request_id: str, business_id: str, fields: dict) -> PlanResult:
    lang = of(fields)
    po = load_po(fields["po_id"], business_id)
    if not po or po["status"] != "awaiting_approval":
        return PlanResult(request_id=request_id, ok=False,
                          error=pick(lang, "ye order ab khula nahi hai", "this order is no longer open"))
    quotes = po["quotes"] or []
    name = best_match(fields.get("supplier") or "", [q["supplier"] for q in quotes]).name
    if not name:
        return PlanResult(request_id=request_id, ok=True,
                          question=pick(lang, f"Kaunsa supplier? {' / '.join(q['supplier'] for q in quotes)}",
                                        f"Which supplier: {' / '.join(q['supplier'] for q in quotes)}?"))
    q = next(q for q in quotes if q["supplier"] == name)
    amount = float(po["qty"])
    total = order_total(q["unit_price_paise"], amount)
    fee = percent_of(total, config.ARTH_FEE_PCT)
    pay = total + fee
    atestfet = paise_to_atestfet(pay)
    rate = inr(q["unit_price_paise"])
    if q["unit_price_paise"] < q["first_price_paise"]:
        rate += pick(lang, f" (pehle {inr(q['first_price_paise'])})", f" (was {inr(q['first_price_paise'])})")
    title = pick(lang, f"Restock: {name} se {fmt_qty(amount, po['unit'])} {po['item_name']}",
                 f"Restock: {fmt_qty(amount, po['unit'])} {po['item_name']} from {name}")
    rows = [
        {"label": "Supplier", "value": f"{name} · ★{q['rating']:g}"},
        {"label": "Item", "value": f"{fmt_qty(amount, po['unit'])} {po['item_name']}"},
        {"label": "Rate", "value": f"{rate}/{po['unit']}"},
        {"label": "Order total", "value": inr(total)},
        {"label": f"Arth fee ({config.ARTH_FEE_PCT:g}%)", "value": inr(fee)},
        {"label": pick(lang, "Aap denge", "You pay"), "value": f"{inr(pay)} = {fet(atestfet)} (testnet)"},
        {"label": "Delivery", "value": pick(lang, f"{q['delivery_days']} din mein",
                                            f"in {q['delivery_days']} {'day' if q['delivery_days'] == 1 else 'days'}")},
    ]
    return PlanResult(request_id=request_id, ok=True, summary_rows=rows, effects={
        "po_id": str(po["id"]), "supplier_id": q["party_id"], "supplier": name, "unit_price_paise": q["unit_price_paise"],
        "total_paise": total, "fee_paise": fee, "pay_paise": pay, "pay_atestfet": str(atestfet),
        "delivery_days": q["delivery_days"], "title": title,
        "item": po["item_name"], "qty": amount, "unit": po["unit"]})


async def plan(request_id: str, business_id: str, intent: str, fields: dict, suppliers: SupplierIO) -> PlanResult:
    if intent == "restock":
        return await plan_restock(request_id, business_id, fields, suppliers)
    if intent == "restock_choice":
        return await asyncio.to_thread(plan_choice, request_id, business_id, fields)
    return PlanResult(request_id=request_id, ok=False, error=f"procurement cannot plan {intent!r}")


def commit(request_id: str, action_id: str, business_id: str, effects: dict, lang: str = DEFAULT) -> Committed:
    with db.transaction() as cur:
        if effects.get("cancel"):
            cur.execute("update purchase_order set status = 'cancelled' where id = %s and business_id = %s "
                        "and status in ('quoting', 'awaiting_approval', 'awaiting_user_payment')",
                        (effects["po_id"], business_id))
            return Committed(request_id=request_id, ok=True, message="")
        po = cur.execute("select status, action_id from purchase_order where id = %s and business_id = %s for update",
                         (effects["po_id"], business_id)).fetchone()
        if not po:
            return Committed(request_id=request_id, ok=False, message="order not found")
        if po["status"] != "awaiting_approval" and str(po["action_id"]) != action_id:
            return Committed(request_id=request_id, ok=False, message=f"order is {po['status']}")
        cur.execute("update purchase_order set action_id = %s, supplier_id = %s, unit_price_paise = %s, total_paise = %s, "
                    "arth_fee_paise = %s, status = 'awaiting_user_payment' where id = %s",
                    (action_id, effects["supplier_id"], effects["unit_price_paise"], effects["total_paise"],
                     effects["fee_paise"], effects["po_id"]))
    return Committed(request_id=request_id, ok=True, message=pick(
        lang, f"Order {effects['supplier']} ke saath tay: {inr(effects['unit_price_paise'])} rate, "
              f"total {inr(effects['total_paise'])}.",
        f"Order agreed with {effects['supplier']}: {inr(effects['unit_price_paise'])} rate, "
        f"total {inr(effects['total_paise'])}."))


def undo(action_id: str, lang: str = DEFAULT) -> UndoDone:
    with db.transaction() as cur:
        po = cur.execute("select status from purchase_order where action_id = %s", (action_id,)).fetchone()
        if not po:
            return UndoDone(action_id=action_id, ok=True)
        if po["status"] in ("awaiting_user_payment", "awaiting_approval", "cancelled"):
            cur.execute("update purchase_order set status = 'cancelled' where action_id = %s", (action_id,))
            return UndoDone(action_id=action_id, ok=True,
                            message=pick(lang, "Supplier order cancel ho gaya.", "Supplier order cancelled."))
    return UndoDone(action_id=action_id, ok=False,
                    message=f"Order is already {po['status']}; paid orders can't be undone here.")


# Leg 2: pay the supplier

def claim_po(po_id: str, business_id: str, user_tx: str) -> dict | None:
    """Move an approved order to paying_supplier with the owner's tx. Returns the row, or None when the
    order isn't waiting for this payment. A repeat with the same tx returns the row as it stands."""
    with db.transaction() as cur:
        po = cur.execute("select po.*, i.name as item_name, i.unit from purchase_order po join item i on i.id = po.item_id "
                         "where po.id = %s and po.business_id = %s for update", (po_id, business_id)).fetchone()
        if not po:
            return None
        if po["user_tx"] == user_tx:
            return po
        if po["status"] != "awaiting_user_payment" or po["user_tx"]:
            return None
        cur.execute("update purchase_order set status = 'paying_supplier', user_tx = %s where id = %s", (user_tx, po_id))
        return {**po, "status": "paying_supplier", "user_tx": user_tx}


def candidates(po: dict) -> list[dict]:
    """The owner's choice first, then the other quotes cheapest first."""
    quotes = sorted(po["quotes"] or [], key=rank_key)
    return sorted(quotes, key=lambda q: q["party_id"] != str(po["supplier_id"]))


def result_of(request_id: str, po: dict, message: str = "") -> OrderResult:
    supplier = next((q for q in po["quotes"] or [] if q["party_id"] == str(po["supplier_id"])), None)
    return OrderResult(request_id=request_id, po_id=str(po["id"]), ok=po["status"] == "confirmed",
                       supplier=supplier and supplier["supplier"], supplier_id=supplier and supplier["party_id"],
                       unit_price_paise=po["unit_price_paise"] or 0, total_paise=po["total_paise"] or 0,
                       supplier_tx=po["supplier_tx"], refund_tx=po["refund_tx"],
                       dispatch_eta=po["dispatch_eta"] and po["dispatch_eta"].isoformat(), message=message)


async def pay_supplier(suppliers: SupplierIO, po: dict, q: dict) -> tuple[str | None, OrderConfirmed | None, str]:
    """-> (supplier tx, confirmation, note). tx is set whenever money left our wallet."""
    amount = float(po["qty"])
    req = await suppliers.ask(q["address"], PlaceOrder(po_id=str(po["id"]), unit_price_paise=q["unit_price_paise"],
                                                       qty=amount), {RequestPayment, Quote}, ORDER_WAIT)
    if not isinstance(req, RequestPayment):
        return None, None, f"{q['supplier']}: order not accepted"
    funds = next((f for f in req.accepted_funds if f.currency == "FET" and f.payment_method == "fet_direct"), None)
    expected = paise_to_atestfet(order_total(q["unit_price_paise"], amount))
    if funds is None or chain.to_atestfet(funds.amount) > expected:
        return None, None, f"{q['supplier']}: asked for more than the agreed price"
    try:
        tx = await asyncio.to_thread(chain.send, "procurement", req.recipient, chain.to_atestfet(funds.amount))
    except Exception as err:
        return None, None, f"{q['supplier']}: transfer failed ({type(err).__name__})"
    await asyncio.to_thread(set_po, str(po["id"]), supplier_tx=tx)
    commit = CommitPayment(funds=funds, recipient=req.recipient, transaction_id=tx, reference=str(po["id"]),
                           description=req.description, metadata={"buyer_fet_wallet": chain.address("procurement")})
    note = f"{q['supplier']}: payment not confirmed"
    for _ in range(COMMIT_TRIES):
        done, confirmed = await suppliers.commit_payment(q["address"], commit, COMMIT_WAIT)
        if isinstance(done, CompletePayment):
            return tx, confirmed, ""
        if isinstance(done, CancelPayment) and done.reason:
            note = f"{q['supplier']}: {done.reason}"
    return tx, None, note


async def pay_order(request_id: str, business_id: str, po_id: str, user_tx: str, buyer_wallet: str,
                    paid_atestfet: int, suppliers: SupplierIO) -> OrderResult:
    po = await asyncio.to_thread(claim_po, po_id, business_id, user_tx)
    if po is None:
        return OrderResult(request_id=request_id, po_id=po_id, ok=False, message="order is not waiting for this payment")
    if po["status"] != "paying_supplier":                  # already finished: report, don't pay again
        return result_of(request_id, po)

    notes = []
    for q in candidates(po):
        tx, confirmed, note = await pay_supplier(suppliers, po, q)
        if tx and confirmed:
            total = order_total(q["unit_price_paise"], float(po["qty"]))
            await asyncio.to_thread(set_po, po_id, supplier_id=q["party_id"], unit_price_paise=q["unit_price_paise"],
                                    total_paise=total, supplier_tx=tx, dispatch_eta=confirmed.dispatch_eta,
                                    status="confirmed")
            po = await asyncio.to_thread(load_po, po_id, business_id)
            return result_of(request_id, po, "; ".join(notes))
        notes.append(note)

    refund = None
    try:
        refund = await asyncio.to_thread(chain.send, "procurement", buyer_wallet, paid_atestfet)
    except Exception as err:
        notes.append(f"refund failed ({type(err).__name__})")
    await asyncio.to_thread(set_po, po_id, status="failed", refund_tx=refund)
    po = await asyncio.to_thread(load_po, po_id, business_id)
    return result_of(request_id, po, "; ".join(notes))
