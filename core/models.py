"""Typed messages between Arth agents.

Specialists speak plan-then-commit: Plan/PlanResult never write, Commit writes under one action_id,
Undo reverses it. Read-only requests use Query/QueryResult. The supplier negotiation messages and the
orchestrator -> procurement order payment are at the bottom; money itself moves over the Payment Protocol.
"""

from uagents import Model


class Plan(Model):              # orchestrator -> specialist: "what would you do?"
    request_id: str
    business_id: str
    intent: str                 # e.g. "sale", "bill", "stock_query"
    fields: dict                # validated extraction from ASI1


class PlanResult(Model):        # specialist -> orchestrator
    request_id: str
    ok: bool
    summary_rows: list[dict] = []   # [{"label": "Total", "value": "₹4,200"}] for the card
    effects: dict = {}              # exact rows to write, replayed on commit
    question: str | None = None     # G-3: ask instead of guessing
    error: str | None = None


class Commit(Model):            # orchestrator -> specialist after Confirm
    request_id: str
    action_id: str
    business_id: str
    effects: dict


class Committed(Model):
    request_id: str
    ok: bool
    message: str
    follow_ups: list[dict] = []     # e.g. {"type": "low_stock", "item_id": ...}


class Undo(Model):             # orchestrator -> each specialist that committed the action
    action_id: str


class UndoDone(Model):
    action_id: str
    ok: bool
    message: str = ""


class Query(Model):             # read-only requests (balances, stock, dues, reports)
    request_id: str
    business_id: str
    intent: str
    fields: dict


class QueryResult(Model):
    request_id: str
    ok: bool
    card_kind: str | None = None    # which card the orchestrator should render; None = text only
    card_payload: dict = {}
    terminal: bool = True           # informational card; False when it carries buttons
    text: str


# Procurement <-> supplier negotiation

class RFQ(Model):
    po_id: str
    item: str
    qty: float
    need_by: str


class Quote(Model):
    po_id: str
    supplier: str
    unit_price_paise: int
    delivery_days: int
    rating: float
    ok: bool


class Counter(Model):
    po_id: str
    target_unit_price_paise: int


class PlaceOrder(Model):
    po_id: str
    unit_price_paise: int
    qty: float


class OrderConfirmed(Model):
    po_id: str
    dispatch_eta: str


# Orchestrator <-> Procurement once the owner's payment (leg 1) is verified

class PayOrder(Model):          # place the approved order and pay the supplier (leg 2)
    request_id: str
    business_id: str
    po_id: str
    user_tx: str                # the owner's verified leg-1 transaction
    buyer_wallet: str           # where a refund goes if no supplier completes
    paid_atestfet: str          # what the owner paid (ints this large travel as strings)


class OrderResult(Model):
    request_id: str
    po_id: str
    ok: bool
    supplier: str | None = None
    supplier_id: str | None = None
    unit_price_paise: int = 0
    total_paise: int = 0
    supplier_tx: str | None = None
    dispatch_eta: str | None = None
    refund_tx: str | None = None
    message: str = ""
