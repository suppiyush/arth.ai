"""Inventory: plans and writes stock_move rows, keeping item.stock_qty in the same transaction."""

from decimal import Decimal
from uuid import UUID

from core import cards, db, lookup
from core.llm import Extraction
from core.models import Committed, PlanResult, QueryResult, UndoDone
from core.money import qty

DIRECTION = {"sale": -1, "purchase": 1}


def plan(request_id: str, business_id: str, intent: str, fields: dict) -> PlanResult:
    if intent not in DIRECTION:
        return PlanResult(request_id=request_id, ok=False, error=f"inventory cannot plan {intent!r}")
    ext = Extraction.model_validate(fields)
    moves, rows, oversell = [], [], False
    for line in ext.lines:
        res = lookup.find_item(business_id, line.item)
        if res.question:
            return PlanResult(request_id=request_id, ok=True, question=res.question)
        item = res.row
        if line.qty is None:
            return PlanResult(request_id=request_id, ok=True, question=f"Kitna {item['name']}? (How much {item['name']}?)")
        delta = Decimal(str(line.qty)) * DIRECTION[intent]
        before, after = item["stock_qty"], item["stock_qty"] + delta
        moves.append({"item_id": str(item["id"]), "item": item["name"], "qty": float(delta), "reason": intent})
        rows.append({"label": f"Stock: {item['name']}", "value": f"{qty(before)} → {qty(after, item['unit'])}"})
        if after < 0:
            oversell = True
            rows.append({"label": "⚠ Stock kam hai", "value": f"Sirf {qty(before, item['unit'])} {item['name']} hai"})
    return PlanResult(request_id=request_id, ok=True, summary_rows=rows, effects={"moves": moves, "oversell": oversell})


def commit(request_id: str, action_id: str, business_id: str, effects: dict) -> Committed:
    """Insert moves and update stock_qty; re-applying the same action_id is a no-op."""
    action_id = str(UUID(action_id))
    moves = effects.get("moves", [])
    follow_ups, left = [], []
    with db.transaction() as cur:
        cur.execute("select pg_advisory_xact_lock(hashtext(%s))", ("inventory:" + action_id,))
        if cur.execute("select 1 from stock_move where action_id = %s limit 1", (action_id,)).fetchone():
            return Committed(request_id=request_id, ok=True, message="Stock pehle se updated hai. (Already recorded.)")
        for m in moves:
            cur.execute("insert into stock_move (action_id, item_id, qty, reason) values (%s, %s, %s, %s)",
                        (action_id, m["item_id"], m["qty"], m["reason"]))
            item = cur.execute("update item set stock_qty = stock_qty + %s where id = %s and business_id = %s "
                               "returning id, name, unit, stock_qty, reorder_level",
                               (m["qty"], m["item_id"], business_id)).fetchone()
            left.append(f"{item['name']} {qty(item['stock_qty'], item['unit'])}")
            if m["qty"] < 0 and item["stock_qty"] <= item["reorder_level"]:
                follow_ups.append({"type": "low_stock", "item_id": str(item["id"]), "item": item["name"],
                                   "unit": item["unit"], "stock_qty": float(item["stock_qty"]),
                                   "reorder_level": float(item["reorder_level"])})
    message = f"Stock updated: {', '.join(left)} bacha." if left else "Stock unchanged."
    return Committed(request_id=request_id, ok=True, message=message, follow_ups=follow_ups)


def query(request_id: str, business_id: str, intent: str, fields: dict) -> QueryResult:
    if intent != "stock_query":
        return QueryResult(request_id=request_id, ok=False, text=f"inventory cannot answer {intent!r}")
    names = [l["item"] for l in fields.get("lines") or [] if l.get("item")]
    if names:
        items = []
        for name in names:
            res = lookup.find_item(business_id, name)
            if res.question:
                return QueryResult(request_id=request_id, ok=True, text=res.question)
            items.append(res.row)
    else:
        items = db.fetch_all("select * from item where business_id = %s "
                             "order by (stock_qty <= reorder_level) desc, name", (business_id,))
    low = [i for i in items if i["stock_qty"] <= i["reorder_level"]]
    if len(items) == 1:
        i = items[0]
        flag = f" ⚠ Kam hai (reorder level {qty(i['reorder_level'])})." if low else ""
        return QueryResult(request_id=request_id, ok=True,
                           text=f"{i['name']}: {qty(i['stock_qty'], i['unit'])} bacha hai.{flag}")
    table = cards.table(
        "Stock", ["Item", "Stock", "Reorder at"],
        [[("⚠ " if i in low else "") + i["name"], qty(i["stock_qty"], i["unit"]), qty(i["reorder_level"])]
         for i in items],
        subtitle=f"{len(low)} item kam hain: {', '.join(i['name'] for i in low)}" if low else "Sab stock theek hai.")
    return QueryResult(request_id=request_id, ok=True, card_kind=table.kind, card_payload=table.payload,
                       terminal=table.terminal, text=table.fallback)


def undo(action_id: str) -> UndoDone:
    """Write reversing moves for the action's stock movements (once) and restore stock_qty."""
    action_id = str(UUID(action_id))
    restored = []
    with db.transaction() as cur:
        cur.execute("select pg_advisory_xact_lock(hashtext(%s))", ("inventory:" + action_id,))
        if cur.execute("select 1 from stock_move where action_id = %s and reason = 'undo'", (action_id,)).fetchone():
            return UndoDone(action_id=action_id, ok=True, message="Stock pehle se wapas hai.")
        moves = cur.execute("select item_id, sum(qty) as qty from stock_move where action_id = %s group by item_id",
                            (action_id,)).fetchall()
        for m in moves:
            cur.execute("insert into stock_move (action_id, item_id, qty, reason) values (%s, %s, %s, 'undo')",
                        (action_id, m["item_id"], -m["qty"]))
            item = cur.execute("update item set stock_qty = stock_qty - %s where id = %s returning name, unit, stock_qty",
                               (m["qty"], m["item_id"])).fetchone()
            restored.append(f"{item['name']} {qty(item['stock_qty'], item['unit'])}")
    return UndoDone(action_id=action_id, ok=True,
                    message=f"Stock wapas: {', '.join(restored)}." if restored else "")
