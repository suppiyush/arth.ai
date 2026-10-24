"""Inventory: plans and writes stock_move rows, keeping item.stock_qty in the same transaction.

Also owns the item list: set_item adds an item or edits its price, cost, GST rate, unit or reorder
level, recording the old values in catalog_change so undo can put them back. stock_adjust moves stock
without money (damaged, found, a physical count).
"""

from decimal import Decimal
from uuid import UUID, uuid4

from core import cards, config, db, lookup
from core.fuzzy import best_match
from core.lang import DEFAULT, of, pick
from core.llm import Extraction
from core.models import Committed, PlanResult, QueryResult, UndoDone
from core.money import inr, qty, to_paise

DIRECTION = {"sale": -1, "purchase": 1}
ITEM_FIELDS = {"price_paise": "Selling price", "cost_paise": "Cost", "gst_rate": "GST", "unit": "Unit",
               "reorder_level": "Reorder level"}
ITEM_FIELDS_HI = {"price_paise": "Bechne ka rate", "cost_paise": "Kharid rate", "gst_rate": "GST", "unit": "Unit",
                  "reorder_level": "Reorder level"}
DEFAULT_GST = 18


def _ask(request_id: str, question: str) -> PlanResult:
    return PlanResult(request_id=request_id, ok=True, question=question)


def how_much(item: str, lang: str) -> str:
    return pick(lang, f"{item} kitna?", f"How much {item}?")


def _show(field: str, value) -> str:
    if value is None:
        return "—"
    if field.endswith("_paise"):
        return inr(int(value))
    return f"{qty(value)}%" if field == "gst_rate" else qty(value) if field == "reorder_level" else str(value)


def plan(request_id: str, business_id: str, intent: str, fields: dict) -> PlanResult:
    ext, lang = Extraction.model_validate(fields), of(fields)
    if intent == "stock_adjust":
        return adjust(request_id, business_id, ext, lang)
    if intent == "set_item":
        return set_item(request_id, business_id, ext, lang)
    if intent not in DIRECTION:
        return PlanResult(request_id=request_id, ok=False, error=f"inventory cannot plan {intent!r}")
    moves, rows, oversell = [], [], False
    for line in ext.lines:
        res = lookup.find_item(business_id, line.item, lang)
        if res.question:
            return PlanResult(request_id=request_id, ok=True, question=res.question)
        item = res.row
        if line.qty is None:
            return PlanResult(request_id=request_id, ok=True, question=how_much(item["name"], lang))
        delta = Decimal(str(line.qty)) * DIRECTION[intent]
        before, after = item["stock_qty"], item["stock_qty"] + delta
        moves.append({"item_id": str(item["id"]), "item": item["name"], "qty": float(delta), "reason": intent})
        rows.append({"label": f"Stock: {item['name']}", "value": f"{qty(before)} → {qty(after, item['unit'])}"})
        if after < 0:
            oversell = True
            rows.append({"label": pick(lang, "⚠ Stock kam hai", "⚠ Not enough stock"),
                         "value": pick(lang, f"Sirf {qty(before, item['unit'])} {item['name']} hai",
                                       f"Only {qty(before, item['unit'])} {item['name']} in stock")})
    return PlanResult(request_id=request_id, ok=True, summary_rows=rows, effects={"moves": moves, "oversell": oversell})


def adjust(request_id: str, business_id: str, ext: Extraction, lang: str) -> PlanResult:
    """Damaged / lost (out), found (in) or a physical count (set): stock moves with no money."""
    moves, rows = [], []
    for line in ext.lines:
        res = lookup.find_item(business_id, line.item, lang)
        if res.question:
            return _ask(request_id, res.question)
        item = res.row
        if line.qty is None:
            return _ask(request_id, how_much(item["name"], lang))
        q = Decimal(str(line.qty))
        delta = q - item["stock_qty"] if ext.adjust == "set" else q if ext.adjust == "in" else -q
        before, after = item["stock_qty"], item["stock_qty"] + delta
        if after < 0:
            return _ask(request_id, pick(lang, f"Stock mein sirf {qty(before, item['unit'])} {item['name']} hai.",
                                         f"There are only {qty(before, item['unit'])} {item['name']} in stock."))
        if delta == 0:
            return _ask(request_id, pick(lang, f"{item['name']} pehle se {qty(before, item['unit'])} hai, kuch badalna nahi.",
                                         f"{item['name']} is already at {qty(before, item['unit'])}; nothing to change."))
        moves.append({"item_id": str(item["id"]), "item": item["name"], "qty": float(delta), "reason": "adjust"})
        value = inr(abs(round(delta * item["cost_paise"])))
        rows += [{"label": f"Stock: {item['name']}", "value": f"{qty(before)} → {qty(after, item['unit'])}"},
                 {"label": pick(lang, "Kharid rate pe value", "Value at cost"), "value": ("−" if delta < 0 else "+") + value}]
    reasons = {"in": pick(lang, "mila / wapas aaya", "found / returned"), "set": pick(lang, "stock ginti", "stock count")}
    reason = ext.category or reasons.get(ext.adjust or "", pick(lang, "kharab / gum", "damaged / lost"))
    rows.insert(0, {"label": pick(lang, "Wajah", "Reason"), "value": reason})
    return PlanResult(request_id=request_id, ok=True, summary_rows=rows,
                      effects={"title": pick(lang, "Stock badlav", "Stock adjustment"), "moves": moves, "oversell": False})


def set_item(request_id: str, business_id: str, ext: Extraction, lang: str) -> PlanResult:
    """Edit an item, or add it when no item of that name exists."""
    line = ext.lines[0]
    items = db.fetch_all("select * from item where business_id = %s", (business_id,))
    m = best_match(line.item, [i["name"] for i in items], every_word=True)
    if m.ambiguous:
        return _ask(request_id, lookup.which(m.candidates, lang))
    labels = ITEM_FIELDS if lang == "en" else ITEM_FIELDS_HI
    changes = {k: v for k, v in (("price_paise", None if line.rate is None else to_paise(line.rate)),
                                 ("cost_paise", None if ext.cost is None else to_paise(ext.cost)),
                                 ("gst_rate", ext.gst_rate), ("reorder_level", ext.reorder_level),
                                 ("unit", line.unit)) if v is not None}
    if m.name:
        item = next(i for i in items if i["name"] == m.name)
        changes = {k: v for k, v in changes.items() if v != item[k]}
        if not changes:
            return _ask(request_id, pick(lang, f"{item['name']} ka kya badalna hai: rate, cost, GST ya reorder level?",
                                         f"What should change for {item['name']}: price, cost, GST or reorder level?"))
        before = {k: item[k] for k in changes}
        rows = [{"label": "Item", "value": item["name"]}] + [
            {"label": labels[k], "value": f"{_show(k, before[k])} → {_show(k, v)}"} for k, v in changes.items()]
        return PlanResult(request_id=request_id, ok=True, summary_rows=rows, effects={
            "title": pick(lang, f"{item['name']} badlein", f"Update {item['name']}"), "item_id": str(item["id"]),
            "changes": {k: float(v) if isinstance(v, Decimal) else v for k, v in changes.items()},
            "before": {k: float(v) if isinstance(v, Decimal) else v for k, v in before.items()}})

    name = " ".join(line.item.split())
    if "price_paise" not in changes:
        return _ask(request_id, pick(lang, f"'{name}' naya item hai. Kitne mein bechenge?",
                                     f"'{name}' is a new item. What's its selling price?"))
    if "cost_paise" not in changes:
        return _ask(request_id, pick(lang, f"{name} kitne ka aata hai (kharid rate)? Profit sahi dikhane ke liye chahiye.",
                                     f"What does {name} cost you? I need it to work out profit."))
    new = {"id": str(uuid4()), "name": name, "unit": changes.get("unit", "piece"),
           "gst_rate": changes.get("gst_rate", DEFAULT_GST), "price_paise": changes["price_paise"],
           "cost_paise": changes["cost_paise"], "reorder_level": changes.get("reorder_level", 0)}
    opening = line.qty or 0
    gst_note = " incl. GST" if config.PRICES_INCLUDE_GST else ""
    new_label = pick(lang, "Naya item", "New item")
    rows = [{"label": new_label, "value": f"{name} (per {new['unit']})"},
            {"label": labels["price_paise"], "value": inr(new["price_paise"]) + gst_note},
            {"label": labels["cost_paise"], "value": inr(new["cost_paise"])},
            {"label": "GST", "value": f"{qty(new['gst_rate'])}%" + ("" if "gst_rate" in changes else " (default)")},
            {"label": pick(lang, "Shuru ka stock", "Opening stock"), "value": qty(opening, new["unit"])},
            {"label": "Reorder level", "value": qty(new["reorder_level"])}]
    moves = [{"item_id": new["id"], "item": name, "qty": float(opening), "reason": "opening"}] if opening else []
    return PlanResult(request_id=request_id, ok=True, summary_rows=rows,
                      effects={"title": f"{new_label}: {name}", "new_item": new, "moves": moves})


def _save_item(cur, business_id: str, action_id: str, effects: dict, lang: str) -> str | None:
    """set_item's write: the new item, or the edited columns. Returns the reply line."""
    if new := effects.get("new_item"):
        cur.execute("insert into item (id, business_id, name, unit, gst_rate, cost_paise, price_paise, reorder_level) "
                    "values (%s, %s, %s, %s, %s, %s, %s, %s)",
                    (new["id"], business_id, new["name"], new["unit"], new["gst_rate"], new["cost_paise"],
                     new["price_paise"], new["reorder_level"]))
        cur.execute("insert into catalog_change (action_id, entity, entity_id, before) values (%s, 'item', %s, null)",
                    (action_id, new["id"]))
        return pick(lang, f"{new['name']} item list mein jud gaya.", f"Added {new['name']} to your items.")
    if changes := effects.get("changes"):
        cur.execute("insert into catalog_change (action_id, entity, entity_id, before) values (%s, 'item', %s, %s)",
                    (action_id, effects["item_id"], db.Json(effects["before"])))
        sets = ", ".join(f"{k} = %s" for k in changes)
        row = cur.execute(f"update item set {sets} where id = %s and business_id = %s returning name",
                          (*changes.values(), effects["item_id"], business_id)).fetchone()
        done = ", ".join(f"{(ITEM_FIELDS if lang == 'en' else ITEM_FIELDS_HI)[k]} {_show(k, v)}" for k, v in changes.items())
        return pick(lang, f"{row['name']} update ho gaya: {done}.", f"Updated {row['name']}: {done}.")
    return None


def commit(request_id: str, action_id: str, business_id: str, effects: dict, lang: str = DEFAULT) -> Committed:
    """Insert moves and update stock_qty (and set_item's item row); re-applying the same action_id is a no-op."""
    action_id = str(UUID(action_id))
    moves = effects.get("moves", [])
    follow_ups, left = [], []
    with db.transaction() as cur:
        cur.execute("select pg_advisory_xact_lock(hashtext(%s))", ("inventory:" + action_id,))
        if (cur.execute("select 1 from stock_move where action_id = %s limit 1", (action_id,)).fetchone()
                or cur.execute("select 1 from catalog_change where action_id = %s and entity = 'item'",
                               (action_id,)).fetchone()):
            return Committed(request_id=request_id, ok=True, message=pick(lang, "Stock pehle se update hai.", "Already recorded."))
        saved = _save_item(cur, business_id, action_id, effects, lang)
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
    message = (pick(lang, f"Stock update: {', '.join(left)} bacha.", f"Stock now: {', '.join(left)}.") if left
               else pick(lang, "Stock nahi badla.", "Stock unchanged."))
    if saved:
        message = saved + (f" {message}" if left else "")
    return Committed(request_id=request_id, ok=True, message=message, follow_ups=follow_ups)


def item_info(request_id: str, business_id: str, fields: dict) -> QueryResult:
    """Price, cost, margin and GST of named items; with none named, the price list and stock value."""
    names = [l["item"] for l in fields.get("lines") or [] if l.get("item")]
    lang = of(fields)
    gst_note = " (GST included)" if config.PRICES_INCLUDE_GST else " (+ GST)"
    if names:
        items = []
        for name in names:
            res = lookup.find_item(business_id, name, lang)
            if res.question:
                return QueryResult(request_id=request_id, ok=True, text=res.question)
            items.append(res.row)
        if len(items) == 1:
            i = items[0]
            margin = i["price_paise"] - i["cost_paise"]
            pct = f" ({margin * 100 / i['price_paise']:.0f}%)" if i["price_paise"] else ""
            hsn = f" (HSN {i['hsn']})" if i["hsn"] else ""
            return QueryResult(request_id=request_id, ok=True, text=pick(
                lang,
                f"{i['name']}: bechne ka rate {inr(i['price_paise'])}/{i['unit']}{gst_note}, "
                f"kharid {inr(i['cost_paise'])}, margin {inr(margin)}{pct}. GST {qty(i['gst_rate'])}%{hsn}. "
                f"Stock {qty(i['stock_qty'], i['unit'])}.",
                f"{i['name']}: selling price {inr(i['price_paise'])}/{i['unit']}{gst_note}, "
                f"cost {inr(i['cost_paise'])}, margin {inr(margin)}{pct}. GST {qty(i['gst_rate'])}%{hsn}. "
                f"In stock: {qty(i['stock_qty'], i['unit'])}."))
    else:
        items = db.fetch_all("select * from item where business_id = %s order by name", (business_id,))
        if not items:
            return QueryResult(request_id=request_id, ok=True, text=pick(lang, "Item list khaali hai.", "No items yet."))
    at_cost = sum(round(i["stock_qty"] * i["cost_paise"]) for i in items)
    at_price = sum(round(i["stock_qty"] * i["price_paise"]) for i in items)
    table = cards.table(
        "Rate list", ["Item", "Rate", "Cost", "GST", "Stock value"],
        [[i["name"], f"{inr(i['price_paise'])}/{i['unit']}", inr(i["cost_paise"]), f"{qty(i['gst_rate'])}%",
          inr(round(i["stock_qty"] * i["cost_paise"]))] for i in items],
        subtitle=pick(lang, f"Stock ki value: {inr(at_cost)} kharid rate pe, {inr(at_price)} bechne ke rate pe. Rates{gst_note}.",
                      f"Stock value: {inr(at_cost)} at cost, {inr(at_price)} at selling price. Rates{gst_note}."))
    return QueryResult(request_id=request_id, ok=True, card_kind=table.kind, card_payload=table.payload,
                       terminal=table.terminal, text=table.fallback)


def query(request_id: str, business_id: str, intent: str, fields: dict) -> QueryResult:
    if intent == "item_info":
        return item_info(request_id, business_id, fields)
    if intent != "stock_query":
        return QueryResult(request_id=request_id, ok=False, text=f"inventory cannot answer {intent!r}")
    names = [l["item"] for l in fields.get("lines") or [] if l.get("item")]
    lang = of(fields)
    if names:
        items = []
        for name in names:
            res = lookup.find_item(business_id, name, lang)
            if res.question:
                return QueryResult(request_id=request_id, ok=True, text=res.question)
            items.append(res.row)
    else:
        items = db.fetch_all("select * from item where business_id = %s "
                             "order by (stock_qty <= reorder_level) desc, name", (business_id,))
    low = [i for i in items if i["stock_qty"] <= i["reorder_level"]]
    if len(items) == 1:
        i = items[0]
        flag = pick(lang, f" ⚠ Kam hai (reorder level {qty(i['reorder_level'])}).",
                    f" ⚠ Running low (reorder level {qty(i['reorder_level'])}).") if low else ""
        return QueryResult(request_id=request_id, ok=True, text=pick(
            lang, f"{i['name']}: {qty(i['stock_qty'], i['unit'])} bacha hai.{flag}",
            f"{i['name']}: {qty(i['stock_qty'], i['unit'])} left.{flag}"))
    table = cards.table(
        "Stock", ["Item", "Stock", "Reorder at"],
        [[("⚠ " if i in low else "") + i["name"], qty(i["stock_qty"], i["unit"]), qty(i["reorder_level"])]
         for i in items],
        subtitle=(pick(lang, f"{len(low)} item kam hain: {', '.join(i['name'] for i in low)}",
                       f"{len(low)} running low: {', '.join(i['name'] for i in low)}") if low
                  else pick(lang, "Sab stock theek hai.", "All stock is fine.")),
        detail=lambda r: pick(lang, f"{r[1]} bacha · reorder level {r[2]}", f"{r[1]} left · reorder at {r[2]}"))
    return QueryResult(request_id=request_id, ok=True, card_kind=table.kind, card_payload=table.payload,
                       terminal=table.terminal, text=table.fallback)


def undo(action_id: str, lang: str = DEFAULT) -> UndoDone:
    """Write reversing moves for the action's stock movements (once) and restore stock_qty."""
    action_id = str(UUID(action_id))
    restored = []
    with db.transaction() as cur:
        cur.execute("select pg_advisory_xact_lock(hashtext(%s))", ("inventory:" + action_id,))
        if cur.execute("select 1 from stock_move where action_id = %s and reason = 'undo'", (action_id,)).fetchone():
            return UndoDone(action_id=action_id, ok=True, message=pick(lang, "Stock pehle se wapas hai.",
                                                                       "Stock was already put back."))
        items = _undo_items(cur, action_id, lang)
        moves = cur.execute("select item_id, sum(qty) as qty from stock_move where action_id = %s group by item_id",
                            (action_id,)).fetchall()
        for m in moves:
            cur.execute("insert into stock_move (action_id, item_id, qty, reason) values (%s, %s, %s, 'undo')",
                        (action_id, m["item_id"], -m["qty"]))
            item = cur.execute("update item set stock_qty = stock_qty - %s where id = %s returning name, unit, stock_qty",
                               (m["qty"], m["item_id"])).fetchone()
            restored.append(f"{item['name']} {qty(item['stock_qty'], item['unit'])}")
    back = pick(lang, f"Stock wapas: {', '.join(restored)}.", f"Stock back to: {', '.join(restored)}.")
    notes = ([back] if restored else []) + [f"{n}." for n in items]
    return UndoDone(action_id=action_id, ok=True, message=" ".join(notes))


def _undo_items(cur, action_id: str, lang: str) -> list[str]:
    """Put back items this action edited; remove items it added unless something else already uses them."""
    notes = []
    for c in cur.execute("delete from catalog_change where action_id = %s and entity = 'item' "
                         "returning entity_id, before", (action_id,)).fetchall():
        if c["before"]:
            sets = ", ".join(f"{k} = %s" for k in c["before"])
            row = cur.execute(f"update item set {sets} where id = %s returning name",
                              (*c["before"].values(), c["entity_id"])).fetchone()
            notes.append(pick(lang, f"{row['name']} pehle jaisa", f"{row['name']} is back as it was"))
            continue
        used = cur.execute("select 1 from stock_move where item_id = %s and action_id <> %s union all "
                           "select 1 from invoice_line where item_id = %s union all "
                           "select 1 from purchase_order where item_id = %s limit 1",
                           (c["entity_id"], action_id, c["entity_id"], c["entity_id"])).fetchone()
        if used:
            notes.append(pick(lang, "Naya item list mein rakha, kyunki uski entries ho chuki hain",
                              "Kept the new item, because it already has entries"))
            continue
        cur.execute("delete from stock_move where action_id = %s and item_id = %s", (action_id, c["entity_id"]))
        row = cur.execute("delete from item where id = %s returning name", (c["entity_id"],)).fetchone()
        notes.append(pick(lang, f"{row['name']} list se hataya", f"Removed {row['name']} from your items"))
    return notes
