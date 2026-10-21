"""Ledger (khata): plans and writes txn rows. Never writes on Plan.

Posting rule (see db/schema.sql): a sale or purchase with a party posts mode 'credit' plus a separate
receipt/payment row for money that changed hands; a walk-in sale is one row with its payment mode.
Undo needs no write here: every ledger query skips rows whose action is undone.
"""

from datetime import date, timedelta
from uuid import UUID

from core import cards, db, lookup
from core.llm import Extraction
from core.models import Committed, PlanResult, QueryResult, UndoDone
from core.money import inr, qty, to_paise

MONEY_MODES = ("cash", "upi", "bank")
MODE_LABEL = {"cash": "cash", "upi": "UPI", "bank": "bank"}


def _ask(request_id: str, question: str) -> PlanResult:
    return PlanResult(request_id=request_id, ok=True, question=question)


def _row(label: str, value: str) -> dict:
    return {"label": label, "value": value}


def _money_mode(ext: Extraction) -> str:
    return ext.mode if ext.mode in MONEY_MODES else "cash"


def _party_ref(business_id: str, name: str, kind: str, allow_new: bool):
    """-> (ref, row, question). ref is {"id", "name"} or {"new": True, "name", "kind"}."""
    res = lookup.find_party(business_id, name, kind)
    if res.question:
        return None, None, res.question
    if res.row:
        return {"id": str(res.row["id"]), "name": res.row["name"]}, res.row, None
    if not allow_new:
        return None, None, f"'{name}' naam ka koi khata nahi mila. (I couldn't find '{name}' in your parties.)"
    return {"new": True, "name": name.strip().title(), "kind": kind}, None, None


def _priced_total(business_id: str, ext: Extraction, price_field: str):
    """-> (total_paise, note, question). The spoken amount wins; otherwise catalogue price x qty."""
    parts, catalogue_total = [], 0
    for line in ext.lines:
        res = lookup.find_item(business_id, line.item)
        if res.question:
            return None, None, res.question
        item = res.row
        if line.qty is None:
            return None, None, f"Kitna {item['name']}? (How much {item['name']}?)"
        parts.append(f"{qty(line.qty, item['unit'])} {item['name']}")
        rate = to_paise(line.rate) if line.rate is not None else item[price_field]
        catalogue_total += round(line.qty * rate)
    total = ext.total_paise()
    if total is None:
        total = catalogue_total if ext.lines else None
    if not total:
        return None, None, "Kitne rupaye? (What amount?)"
    return total, ", ".join(parts) or None, None


def _trade(request_id: str, business_id: str, ext: Extraction, kind: str, invoice_id: str | None = None) -> PlanResult:
    """sale or purchase. With invoice_id (a GST bill) the sale row points at the invoice and the card
    rows cover only payment and balance, since Billing already shows the items and total."""
    is_sale = kind == "sale"
    party_kind, money_kind = ("customer", "receipt") if is_sale else ("supplier", "payment")
    who = "Customer" if is_sale else "Supplier"

    ref = row = None
    if ext.party:
        ref, row, q = _party_ref(business_id, ext.party, party_kind, allow_new=True)
        if q:
            return _ask(request_id, q)
    total, note, q = _priced_total(business_id, ext, "price_paise" if is_sale else "cost_paise")
    if q:
        return _ask(request_id, q)

    paid = ext.paid_paise(total)
    mode = _money_mode(ext)
    rows = []
    if ref is None:
        if paid is not None and paid < total:
            return _ask(request_id, "Baaki paisa kiske khate mein likhun? (Whose account should the balance go to?)")
        txns = [{"kind": kind, "amount_paise": total, "mode": mode, "note": note}]
        rows += [_row(who, "Walk-in" if is_sale else "Cash purchase")]
        if note:
            rows.append(_row("Items", note))
        rows += [_row("Total", inr(total)), _row("Paid", f"{inr(total)} {MODE_LABEL[mode]}")]
        title = "Walk-in sale" if is_sale else "Cash purchase"
    else:
        if paid is None:
            return _ask(request_id, f"{ref['name']} ka kitna payment hua? Poora, aadha, ya udhaar? "
                                    "(How much was paid now: full, half, or on credit?)")
        txns = [{"kind": kind, "amount_paise": total, "mode": "credit", "note": note, "invoice_id": invoice_id}]
        if paid:
            txns.append({"kind": money_kind, "amount_paise": paid, "mode": mode, "note": None})
        due = total - paid
        before = lookup.party_balance(row["id"]) if row else 0
        after = before + due if is_sale else before - due
        if not invoice_id:
            rows.append(_row(who, ref["name"] + (" (new)" if ref.get("new") else "")))
            if note:
                rows.append(_row("Items", note))
            rows.append(_row("Total", inr(total)))
        rows += [_row("Paid now", f"{inr(paid)} {MODE_LABEL[mode]}" if paid else "₹0 (udhaar)"),
                 _row("Baaki (this entry)", inr(due))]
        if is_sale:
            rows.append(_row(f"{ref['name']} ka total baaki", inr(after)))
        else:
            rows.append(_row(f"Aapko {ref['name']} ko dena hai", inr(-after)))
        title = f"Sale to {ref['name']}" if is_sale else f"Purchase from {ref['name']}"
    return PlanResult(request_id=request_id, ok=True, summary_rows=rows,
                      effects={"title": title, "party": ref, "txns": txns})


def _expense(request_id: str, business_id: str, ext: Extraction) -> PlanResult:
    total = ext.total_paise()
    if not total:
        return _ask(request_id, "Kitne rupaye? (What amount?)")
    mode = _money_mode(ext)
    category = (ext.category or "other").lower()
    rows = [_row("Category", category.title()), _row("Amount", inr(total)), _row("Paid by", MODE_LABEL[mode])]
    return PlanResult(request_id=request_id, ok=True, summary_rows=rows, effects={
        "title": "Expense", "party": None,
        "txns": [{"kind": "expense", "amount_paise": total, "mode": mode, "category": category, "note": None}]})


def _payment(request_id: str, business_id: str, ext: Extraction, incoming: bool) -> PlanResult:
    ref, row, q = _party_ref(business_id, ext.party or "", "customer" if incoming else "supplier", allow_new=False)
    if q:
        return _ask(request_id, q)
    total = ext.total_paise()
    if not total:
        return _ask(request_id, "Kitne rupaye? (What amount?)")
    mode = _money_mode(ext)
    before = lookup.party_balance(row["id"])
    after = before - total if incoming else before + total
    rows = [_row("From" if incoming else "To", ref["name"]), _row("Amount", inr(total)),
            _row("Mode", MODE_LABEL[mode])]
    if row["kind"] == "customer":
        rows.append(_row("Baaki", f"{inr(before)} → {inr(after)}"))
    else:
        rows.append(_row("Aapko dena hai", f"{inr(-before)} → {inr(-after)}"))
    title = f"Payment from {ref['name']}" if incoming else f"Payment to {ref['name']}"
    return PlanResult(request_id=request_id, ok=True, summary_rows=rows, effects={
        "title": title, "party": ref,
        "txns": [{"kind": "receipt" if incoming else "payment", "amount_paise": total, "mode": mode, "note": None}]})


def plan(request_id: str, business_id: str, intent: str, fields: dict) -> PlanResult:
    ext = Extraction.model_validate(fields)
    if intent in ("sale", "purchase"):
        return _trade(request_id, business_id, ext, intent, fields.get("invoice_id"))
    if intent == "expense":
        return _expense(request_id, business_id, ext)
    if intent in ("payment_in", "payment_out"):
        return _payment(request_id, business_id, ext, incoming=intent == "payment_in")
    return PlanResult(request_id=request_id, ok=False, error=f"ledger cannot plan {intent!r}")


def commit(request_id: str, action_id: str, business_id: str, effects: dict) -> Committed:
    """Write the planned txn rows under action_id. Re-applying the same action_id is a no-op."""
    action_id = str(UUID(action_id))
    ref = effects.get("party")
    with db.transaction() as cur:
        cur.execute("select pg_advisory_xact_lock(hashtext(%s))", ("ledger:" + action_id,))
        if cur.execute("select 1 from txn where action_id = %s limit 1", (action_id,)).fetchone():
            return Committed(request_id=request_id, ok=True, message="Khata pehle se updated hai. (Already recorded.)")
        party_id = None
        if ref:
            party_id = ref.get("id") or cur.execute(
                "insert into party (business_id, kind, name) values (%s, %s, %s) "
                "on conflict (business_id, name) do update set name = excluded.name returning id",
                (business_id, ref["kind"], ref["name"])).fetchone()["id"]
        for t in effects["txns"]:
            cur.execute("insert into txn (business_id, action_id, party_id, kind, amount_paise, mode, category, note, "
                        "invoice_id) values (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                        (business_id, action_id, None if t.get("no_party") else party_id, t["kind"],
                         t["amount_paise"], t["mode"],
                         t.get("category"), t.get("note"), t.get("invoice_id")))
    message = "Khata updated."
    if party_id:
        bal = lookup.party_balance(party_id)
        if bal > 0:
            message += f" {ref['name']} ka total baaki: {inr(bal)}."
        elif bal < 0:
            message += f" Aapko {ref['name']} ko {inr(-bal)} dena hai."
        else:
            message += f" {ref['name']} ka hisaab barabar."
    return Committed(request_id=request_id, ok=True, message=message)


# Queries

LIVE = "not exists (select 1 from action a where a.id = t.action_id and a.status = 'undone')"
KIND_LABEL = {"sale": "Sale", "purchase": "Purchase", "expense": "Expense", "receipt": "Received",
              "payment": "Paid", "credit_note": "Credit note"}
CASH_SIGN = "case when t.kind in ('sale', 'receipt') then 1 else -1 end"
STATEMENT_ROWS = 10
DAYBOOK_ROWS = 25


def _entry(t: dict) -> str:
    what = KIND_LABEL[t["kind"]]
    detail = t["note"] or t["category"] or ""
    mode = f" ({MODE_LABEL[t['mode']]})" if t["mode"] in MODE_LABEL else ""
    return f"{what}{mode}" + (f": {detail}" if detail else "")


def statement(request_id: str, business_id: str, party_name: str) -> QueryResult:
    res = lookup.find_party(business_id, party_name)
    if res.question or not res.row:
        return QueryResult(request_id=request_id, ok=True,
                           text=res.question or f"'{party_name}' naam ka koi khata nahi mila. (I couldn't find '{party_name}'.)")
    party = res.row
    rows = db.fetch_all(f"select t.* from txn t where t.party_id = %s and {LIVE} "
                        f"order by t.txn_date desc, t.id desc limit %s", (party["id"], STATEMENT_ROWS))
    bal = lookup.party_balance(party["id"])
    if party["kind"] == "customer":
        status = f"{party['name']} ka baaki: {inr(bal)}" if bal > 0 else f"{party['name']} ka hisaab barabar." \
            if bal == 0 else f"{party['name']} ka advance: {inr(-bal)}"
    else:
        status = f"Aapko {party['name']} ko dena hai: {inr(-bal)}" if bal < 0 else f"{party['name']} ka hisaab barabar." \
            if bal == 0 else f"{party['name']} se lena hai: {inr(bal)}"
    if not rows:
        return QueryResult(request_id=request_id, ok=True, text=f"{party['name']} ki koi entry nahi hai. {status}")
    sign = {"sale": "+", "payment": "+", "receipt": "−", "purchase": "−", "credit_note": "−"}
    table = cards.table(
        f"Khata: {party['name']}", ["Date", "Entry", "Amount"],
        [[f"{t['txn_date']:%d %b}", _entry(t), f"{sign.get(t['kind'], '')}{inr(t['amount_paise'])}"] for t in rows],
        subtitle=status + (f" · last {len(rows)} entries" if len(rows) == STATEMENT_ROWS else ""),
        footer=[cards.button("Remind", "remind", party_id=str(party["id"]), party=party["name"])]
        if party["kind"] == "customer" and bal > 0 else None)
    return QueryResult(request_id=request_id, ok=True, card_kind=table.kind, card_payload=table.payload,
                       terminal=table.terminal, text=table.fallback)


def day_of(period: str | None) -> date:
    today = date.today()
    if period == "yesterday":
        return today - timedelta(days=1)
    try:
        return date.fromisoformat(period) if period and len(period) == 10 else today
    except ValueError:
        return today


def cash_in_hand(business_id: str) -> int:
    row = db.fetch_one(f"select coalesce(sum({CASH_SIGN} * t.amount_paise), 0) as cash from txn t "
                       f"where t.business_id = %s and t.mode = 'cash' and t.kind <> 'credit_note' and {LIVE}",
                       (business_id,))
    return int(row["cash"])


def daybook(request_id: str, business_id: str, period: str | None) -> QueryResult:
    day = day_of(period)
    rows = db.fetch_all(f"select t.*, p.name as party from txn t left join party p on p.id = t.party_id "
                        f"where t.business_id = %s and t.txn_date = %s and {LIVE} order by t.id",
                        (business_id, day))
    totals = {k: sum(t["amount_paise"] for t in rows if t["kind"] == k) for k in KIND_LABEL}
    cash = cash_in_hand(business_id)
    when = "Aaj" if day == date.today() else f"{day:%d %b}"
    summary = (f"Sale {inr(totals['sale'])} · Aaya {inr(totals['receipt'])} · "
               f"Kharcha {inr(totals['expense'])} · Galle mein cash {inr(cash)}")
    if not rows:
        return QueryResult(request_id=request_id, ok=True, text=f"{when} koi entry nahi hai. Galle mein cash: {inr(cash)}.")
    shown = rows[-DAYBOOK_ROWS:]
    table = cards.table(
        f"Day book: {when}", ["Entry", "Party", "Amount"],
        [[_entry(t), t["party"] or "Walk-in", inr(t["amount_paise"])] for t in shown],
        subtitle=summary + (f" · last {len(shown)} of {len(rows)}" if len(rows) > len(shown) else ""))
    return QueryResult(request_id=request_id, ok=True, card_kind=table.kind, card_payload=table.payload,
                       terminal=table.terminal, text=table.fallback)


def query(request_id: str, business_id: str, intent: str, fields: dict) -> QueryResult:
    if intent == "statement":
        return statement(request_id, business_id, fields.get("party") or "")
    if intent == "daybook":
        return daybook(request_id, business_id, fields.get("period"))
    return QueryResult(request_id=request_id, ok=False, text=f"ledger cannot answer {intent!r}")


def undo(action_id: str) -> UndoDone:
    """Nothing to write: the action is already marked undone and every balance and query skips it."""
    parties = db.fetch_all("select distinct p.id, p.name, p.kind from txn t join party p on p.id = t.party_id "
                           "where t.action_id = %s", (action_id,))
    notes = ["Khata se hata diya."]
    for p in parties:
        bal = lookup.party_balance(p["id"])
        notes.append(f"{p['name']} ka baaki ab {inr(bal)}." if p["kind"] == "customer"
                     else f"Aapko {p['name']} ko ab {inr(max(-bal, 0))} dena hai.")
    return UndoDone(action_id=action_id, ok=True, message=" ".join(notes))
