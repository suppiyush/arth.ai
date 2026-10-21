"""A supplier's pricing and order rules, kept apart from the uAgent wiring so they can be tested directly.

Quote at the configured base price if the item is stocked in the quantity asked. One counter-offer
per order: accepted if the target is at or above the floor, otherwise the quote stands.
PlaceOrder at the agreed price is answered with a Payment Protocol RequestPayment (this agent is the
seller); a CommitPayment is verified on-chain before CompletePayment and OrderConfirmed.
Orders live in the agent's storage under po:<po_id>, keyed to the buyer who asked.
"""

from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Callable

import yaml
from uagents_core.contrib.protocols.payment import (CancelPayment, CommitPayment, CompletePayment, Funds,
                                                    RequestPayment)

from core import chain
from core.fuzzy import best_match
from core.models import RFQ, Counter, OrderConfirmed, PlaceOrder, Quote
from core.money import paise_to_atestfet, to_paise

PAYMENT_DEADLINE = 300      # seconds the buyer has to pay


@dataclass
class Supplier:
    agent: str                  # key into core.net.PORTS and SEED_<AGENT>
    name: str
    delivery_days: int
    rating: float
    items: dict[str, dict]      # name -> {"base": paise, "floor": paise, "stock": qty}


def load(path: str | Path) -> Supplier:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    items = {name: {"base": to_paise(v["base"]), "floor": to_paise(v["floor"]), "stock": float(v["stock"])}
             for name, v in raw["items"].items()}
    return Supplier(raw["agent"], raw["name"], int(raw["delivery_days"]), float(raw["rating"]), items)


def load_all(folder: str | Path) -> list[Supplier]:
    return [load(p) for p in sorted(Path(folder).glob("*.yaml"))]


def _quote(s: Supplier, po_id: str, price: int = 0, ok: bool = True) -> Quote:
    return Quote(po_id=po_id, supplier=s.name, unit_price_paise=price, delivery_days=s.delivery_days,
                 rating=s.rating, ok=ok)


def on_rfq(s: Supplier, storage, buyer: str, rfq: RFQ) -> Quote:
    name = best_match(rfq.item, list(s.items)).name
    if not name or rfq.qty <= 0 or rfq.qty > s.items[name]["stock"]:
        return _quote(s, rfq.po_id, ok=False)
    price = s.items[name]["base"]
    storage.set(f"po:{rfq.po_id}", {"buyer": buyer, "item": name, "qty": rfq.qty,
                                    "unit_price_paise": price, "countered": False})
    return _quote(s, rfq.po_id, price)


def on_counter(s: Supplier, storage, buyer: str, msg: Counter) -> Quote:
    open_quote = storage.get(f"po:{msg.po_id}")
    if not open_quote or open_quote["buyer"] != buyer:
        return _quote(s, msg.po_id, ok=False)
    price = open_quote["unit_price_paise"]
    if not open_quote["countered"] and msg.target_unit_price_paise >= s.items[open_quote["item"]]["floor"]:
        price = min(price, msg.target_unit_price_paise)
    storage.set(f"po:{msg.po_id}", {**open_quote, "unit_price_paise": price, "countered": True})
    return _quote(s, msg.po_id, price)


# Orders and payment

Verify = Callable[[str, str, str, int], str | None]     # (tx, from, to, min atestfet) -> reason or None


def on_place_order(s: Supplier, storage, buyer: str, msg: PlaceOrder, wallet: str) -> RequestPayment | Quote:
    """Ask for payment if the order matches this buyer's open quote; otherwise refuse with Quote(ok=False)."""
    order = storage.get(f"po:{msg.po_id}")
    if (not order or order["buyer"] != buyer or msg.unit_price_paise < order["unit_price_paise"]
            or msg.qty != order["qty"] or msg.qty > s.items[order["item"]]["stock"]):
        return _quote(s, msg.po_id, ok=False)
    if order.get("status") != "paid":
        total = round(order["unit_price_paise"] * msg.qty)
        order = {**order, "status": "awaiting_payment", "total_paise": total,
                 "amount_atestfet": str(paise_to_atestfet(total))}
        storage.set(f"po:{msg.po_id}", order)
    return RequestPayment(
        accepted_funds=[Funds(amount=chain.fet_amount(int(order["amount_atestfet"])), currency="FET",
                              payment_method="fet_direct")],
        recipient=wallet, deadline_seconds=PAYMENT_DEADLINE, reference=msg.po_id,
        description=f"{msg.qty:g} {order['item']} from {s.name}",
        metadata={"fet_network": chain.NETWORK_NAME, "po_id": msg.po_id})


def on_commit_payment(s: Supplier, storage, buyer: str, msg: CommitPayment, wallet: str,
                      verify: Verify | None = None) -> tuple[CompletePayment | CancelPayment, OrderConfirmed | None]:
    po_id = msg.reference or ""
    order = storage.get(f"po:{po_id}")
    if not order or order["buyer"] != buyer or order.get("status") not in ("awaiting_payment", "paid"):
        return CancelPayment(transaction_id=msg.transaction_id, reason="no order awaiting payment"), None
    if order.get("status") == "paid":            # a retried commit for an order already paid
        if order.get("tx") == msg.transaction_id:
            return CompletePayment(transaction_id=msg.transaction_id), _confirmed(s, po_id)
        return CancelPayment(transaction_id=msg.transaction_id, reason="order already paid"), None
    used = storage.get("used_txs") or []
    payer = (msg.metadata or {}).get("buyer_fet_wallet") or (msg.metadata or {}).get("buyer_fet_address")
    reason = ("transaction already used" if msg.transaction_id in used else
              "buyer_fet_wallet missing" if not payer else
              (verify or chain.verify_transfer)(msg.transaction_id, payer, wallet, int(order["amount_atestfet"])))
    if reason:
        return CancelPayment(transaction_id=msg.transaction_id, reason=reason), None
    storage.set("used_txs", (used + [msg.transaction_id])[-500:])
    storage.set(f"po:{po_id}", {**order, "status": "paid", "tx": msg.transaction_id})
    return CompletePayment(transaction_id=msg.transaction_id), _confirmed(s, po_id)


def _confirmed(s: Supplier, po_id: str) -> OrderConfirmed:
    return OrderConfirmed(po_id=po_id, dispatch_eta=(date.today() + timedelta(days=s.delivery_days)).isoformat())

