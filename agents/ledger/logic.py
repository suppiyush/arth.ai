"""Ledger (khata): plans and writes txn rows. Never writes on Plan.

Posting rule (see db/schema.sql): a sale or purchase with a party posts mode 'credit' plus a separate
receipt/payment row for money that changed hands; a walk-in sale is one row with its payment mode.
Undo needs no write here: every ledger query skips rows whose action is undone. The one exception is
set_party (add a customer or supplier, or change their phone), which records the old values in
catalog_change so undo can put them back.
"""

import re
from datetime import date, timedelta
from uuid import UUID, uuid4

from core import cards, clock, db, lookup
from core.fuzzy import best_match
from core.lang import DEFAULT, of, pick
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


def not_found(name: str, lang: str) -> str:
    return pick(lang, f"'{name}' naam ka koi khata nahi mila.", f"I couldn't find '{name}' among your customers and suppliers.")


def _party_ref(business_id: str, name: str, kind: str, allow_new: bool, lang: str):
    """-> (ref, row, question). ref is {"id", "name"} or {"new": True, "name", "kind"}."""
    res = lookup.find_party(business_id, name, kind, lang)
    if res.question:
        return None, None, res.question
    if res.row:
        return {"id": str(res.row["id"]), "name": res.row["name"]}, res.row, None
    if not allow_new:
        return None, None, not_found(name, lang)
    return {"new": True, "name": name.strip().title(), "kind": kind}, None, None


def how_much(item: str, lang: str) -> str:
    return pick(lang, f"{item} kitna?", f"How much {item}?")


def what_amount(lang: str) -> str:
    return pick(lang, "Kitne rupaye?", "What amount?")


def _priced_total(business_id: str, ext: Extraction, price_field: str, lang: str):
    """-> (total_paise, note, question). The spoken amount wins; otherwise catalogue price x qty."""
    parts, catalogue_total = [], 0
    for line in ext.lines:
        res = lookup.find_item(business_id, line.item, lang)
        if res.question:
            return None, None, res.question
        item = res.row
        if line.qty is None:
            return None, None, how_much(item["name"], lang)
        parts.append(f"{qty(line.qty, item['unit'])} {item['name']}")
        rate = to_paise(line.rate) if line.rate is not None else item[price_field]
        catalogue_total += round(line.qty * rate)
    total = ext.total_paise()
    if total is None:
        total = catalogue_total if ext.lines else None
    if not total:
        return None, None, what_amount(lang)
    return total, ", ".join(parts) or None, None


def owes_line(name: str, balance: int, kind: str, lang: str) -> str:
    """One party's balance as a sentence: they owe the shop, the shop owes them, an advance, or settled.
    (Positive balance = they owe the shop.) A customer below zero has paid in advance; so has the shop,
    to a supplier above zero. Never a negative amount."""
    if balance == 0:
        return pick(lang, f"{name} ka hisaab barabar hai.", f"{name} is settled.")
    if kind == "customer":
        return (pick(lang, f"{name} ka baaki: {inr(balance)}", f"{name} owes you {inr(balance)}") if balance > 0 else
                pick(lang, f"{name} ka advance: {inr(-balance)}", f"{name} has paid {inr(-balance)} in advance"))
    return (pick(lang, f"Aapko {name} ko dena hai: {inr(-balance)}", f"You owe {name} {inr(-balance)}") if balance < 0 else
            pick(lang, f"{name} ko advance diya hua: {inr(balance)}", f"You've paid {name} {inr(balance)} in advance"))


def balance_value(balance: int, kind: str, lang: str) -> str:
    """A balance for a card row headed "Baaki" (customer) or "Aapko dena hai" (supplier): the amount, or
    "₹X advance" when it has gone the other way."""
    owed = balance if kind == "customer" else -balance
    return inr(owed) if owed >= 0 else f"{inr(-owed)} advance"


def recent_same(party_id, kind: str, amount_paise: int) -> int | None:
    """Minutes since the same entry (party, kind, amount) was last recorded, if within 10 minutes: a likely repeat."""
    row = db.fetch_one("select extract(epoch from now() - a.created_at) / 60 as mins from txn t "
                       "join action a on a.id = t.action_id where t.party_id = %s and t.kind = %s and t.amount_paise = %s "
                       "and a.status <> 'undone' and a.created_at > now() - interval '10 minutes' "
                       "order by a.created_at desc limit 1", (party_id, kind, amount_paise))
    return int(row["mins"]) if row else None


def _trade(request_id: str, business_id: str, ext: Extraction, kind: str, invoice_id: str | None, lang: str) -> PlanResult:
    """sale or purchase. With invoice_id (a GST bill) the sale row points at the invoice and the card
    rows cover only payment and balance, since Billing already shows the items and total."""
    is_sale = kind == "sale"
    party_kind, money_kind = ("customer", "receipt") if is_sale else ("supplier", "payment")
    who = "Customer" if is_sale else "Supplier"

    ref = row = None
    if ext.party:
        ref, row, q = _party_ref(business_id, ext.party, party_kind, True, lang)
        if q:
            return _ask(request_id, q)
    total, note, q = _priced_total(business_id, ext, "price_paise" if is_sale else "cost_paise", lang)
    if q:
        return _ask(request_id, q)

    paid = ext.paid_paise(total)
    mode = _money_mode(ext)
    rows = []
    if ref is None:
        if paid is not None and paid < total:
            return _ask(request_id, pick(lang, "Baaki paisa kiske khate mein likhun?",
                                         "Whose account should the unpaid part go to?"))
        txns = [{"kind": kind, "amount_paise": total, "mode": mode, "note": note}]
        rows += [_row(who, "Walk-in" if is_sale else pick(lang, "Nakad kharid", "Cash purchase"))]
        if note:
            rows.append(_row("Items", note))
        rows += [_row("Total", inr(total)), _row(pick(lang, "Payment", "Paid"), f"{inr(total)} {MODE_LABEL[mode]}")]
        title = (pick(lang, "Walk-in bikri", "Walk-in sale") if is_sale else pick(lang, "Nakad kharid", "Cash purchase"))
    else:
        if paid is None:
            return _ask(request_id, pick(lang, f"{ref['name']} ne kitna payment kiya? Poora, aadha, ya udhaar?",
                                         f"How much did {ref['name']} pay now: in full, half, or on credit?")
                        if is_sale else
                        pick(lang, f"{ref['name']} ko kitna payment kiya? Poora, aadha, ya udhaar?",
                             f"How much did you pay {ref['name']} now: in full, half, or on credit?"))
        txns = [{"kind": kind, "amount_paise": total, "mode": "credit", "note": note, "invoice_id": invoice_id}]
        if paid:
            txns.append({"kind": money_kind, "amount_paise": paid, "mode": mode, "note": None})
        due = total - paid
        before = lookup.party_balance(row["id"]) if row else 0
        after = before + due if is_sale else before - due
        if not invoice_id:
            rows.append(_row(who, ref["name"] + pick(lang, " (naya)", " (new)") if ref.get("new") else ref["name"]))
            if note:
                rows.append(_row("Items", note))
            rows.append(_row("Total", inr(total)))
        paid_label = pick(lang, "Abhi mila", "Paid now") if is_sale else pick(lang, "Abhi diya", "Paid now")
        rows += [_row(paid_label, f"{inr(paid)} {MODE_LABEL[mode]}" if paid else pick(lang, "₹0 (udhaar)", "₹0 (on credit)")),
                 _row(pick(lang, "Is entry ka baaki", "Due on this entry"), inr(due))]
        if is_sale:
            rows.append(_row(pick(lang, f"{ref['name']} ka total baaki", f"{ref['name']} owes in total"),
                             balance_value(after, "customer", lang)))
        else:
            rows.append(_row(pick(lang, f"Aapko {ref['name']} ko dena hai", f"You owe {ref['name']}"),
                             balance_value(after, "supplier", lang)))
        title = (pick(lang, f"{ref['name']} ko bikri", f"Sale to {ref['name']}") if is_sale
                 else pick(lang, f"{ref['name']} se kharid", f"Purchase from {ref['name']}"))
    return PlanResult(request_id=request_id, ok=True, summary_rows=rows,
                      effects={"title": title, "party": ref, "txns": txns})


def _expense(request_id: str, business_id: str, ext: Extraction, lang: str) -> PlanResult:
    total = ext.total_paise()
    if not total:
        return _ask(request_id, what_amount(lang))
    mode = _money_mode(ext)
    category = (ext.category or "other").lower()
    rows = [_row("Category", category.title()), _row("Amount", inr(total)),
            _row(pick(lang, "Kaise diya", "Paid by"), MODE_LABEL[mode])]
    return PlanResult(request_id=request_id, ok=True, summary_rows=rows, effects={
        "title": pick(lang, "Kharcha", "Expense"), "party": None,
        "txns": [{"kind": "expense", "amount_paise": total, "mode": mode, "category": category, "note": None}]})


def _payment(request_id: str, business_id: str, ext: Extraction, incoming: bool, lang: str) -> PlanResult:
    ref, row, q = _party_ref(business_id, ext.party or "", "customer" if incoming else "supplier", False, lang)
    if q:
        return _ask(request_id, q)
    total = ext.total_paise()
    if not total:
        return _ask(request_id, what_amount(lang))
    mode = _money_mode(ext)
    name, kind = ref["name"], row["kind"]
    before = lookup.party_balance(row["id"])
    after = before - total if incoming else before + total
    rows = [_row("From" if incoming else "To", name), _row("Amount", inr(total)), _row("Mode", MODE_LABEL[mode]),
            _row(pick(lang, "Baaki", "They owe") if kind == "customer" else pick(lang, "Aapko dena hai", "You owe"),
                 f"{balance_value(before, kind, lang)} → {balance_value(after, kind, lang)}")]
    # Warn, don't block: an advance is real, but paying more than is owed is often a typo or a repeat.
    owed = max(before if incoming else -before, 0)
    if total > owed:
        extra = inr(total - owed)
        rows.append(_row(pick(lang, "⚠ Baaki se zyada", "⚠ More than owed"),
                         pick(lang, f"{name} ka sirf {inr(owed)} baaki tha; {extra} advance ban jayega",
                              f"{name} only owed {inr(owed)}; {extra} becomes an advance") if incoming else
                         pick(lang, f"Aapko {name} ko sirf {inr(owed)} dena tha; {extra} advance ho jayega",
                              f"You only owed {name} {inr(owed)}; {extra} becomes an advance")))
    txn_kind = "receipt" if incoming else "payment"
    if (mins := recent_same(row["id"], txn_kind, total)) is not None:
        rows.append(_row(pick(lang, "⚠ Shayad dobara", "⚠ Possible repeat"),
                         pick(lang, f"Yahi payment {mins} min pehle bhi likha gaya tha" if mins else
                              "Yahi payment abhi abhi likha gaya tha",
                              f"The same payment was recorded {mins} min ago" if mins else
                              "The same payment was recorded just now")))
    title = (pick(lang, f"{name} se payment", f"Payment from {name}") if incoming
             else pick(lang, f"{name} ko payment", f"Payment to {name}"))
    return PlanResult(request_id=request_id, ok=True, summary_rows=rows, effects={
        "title": title, "party": ref, "warn": any(r["label"].startswith("⚠") for r in rows),
        "txns": [{"kind": txn_kind, "amount_paise": total, "mode": mode, "note": None}]})


def phone_number(spoken: str | None) -> str | None:
    """"98290 12345" or "+91-9829012345" -> "919829012345", the form wa.me links need."""
    digits = re.sub(r"\D", "", spoken or "")
    if len(digits) == 10:
        return "91" + digits
    return digits if len(digits) == 12 and digits.startswith("91") else None


def show_phone(phone: str) -> str:
    """"919829012345" -> "+91 98290 12345"."""
    return f"+91 {phone[2:7]} {phone[7:]}" if len(phone) == 12 and phone.startswith("91") else phone


def _set_party(request_id: str, business_id: str, ext: Extraction, lang: str) -> PlanResult:
    parties = db.fetch_all("select id, name, kind, phone from party where business_id = %s", (business_id,))
    m = best_match(ext.party or "", [r["name"] for r in parties])
    if m.ambiguous:
        return _ask(request_id, lookup.which(m.candidates, lang))
    phone = phone_number(ext.phone)
    if ext.phone and not phone:
        return _ask(request_id, pick(lang, f"'{ext.phone}' sahi mobile number nahi lag raha. 10 digit ka number bataiye.",
                                     f"'{ext.phone}' doesn't look like a mobile number. Please give the 10 digits."))
    if m.name:
        row = next(r for r in parties if r["name"] == m.name)
        changes = {k: v for k, v in (("phone", phone), ("kind", ext.party_kind)) if v and v != row[k]}
        if not changes:
            saved = f" (phone {show_phone(row['phone'])})" if row["phone"] else ""
            return _ask(request_id, pick(lang, f"{row['name']} pehle se {row['kind']} hai{saved}. Kya badalna hai?",
                                         f"{row['name']} is already saved as a {row['kind']}{saved}. What should change?"))
        show = lambda k, v: (show_phone(v) if k == "phone" else v) if v else "—"
        rows = [_row("Party", row["name"])] + [_row(k.title(), f"{show(k, row[k])} → {show(k, v)}")
                                                for k, v in changes.items()]
        return PlanResult(request_id=request_id, ok=True, summary_rows=rows, effects={
            "title": pick(lang, f"{row['name']} badlein", f"Update {row['name']}"), "party_id": str(row["id"]),
            "changes": changes, "before": {k: row[k] for k in changes}})
    kind = ext.party_kind or "customer"
    new = {"id": str(uuid4()), "name": " ".join(ext.party.split()).title(), "kind": kind, "phone": phone}
    label = pick(lang, f"Naya {kind}", f"New {kind}")
    rows = [_row(label, new["name"]), _row("Phone", show_phone(phone) if phone else "—")]
    return PlanResult(request_id=request_id, ok=True, summary_rows=rows,
                      effects={"title": f"{label}: {new['name']}", "new_party": new})


def _save_party(cur, business_id: str, action_id: str, effects: dict, lang: str) -> str:
    if new := effects.get("new_party"):
        cur.execute("insert into party (id, business_id, kind, name, phone) values (%s, %s, %s, %s, %s)",
                    (new["id"], business_id, new["kind"], new["name"], new["phone"]))
        cur.execute("insert into catalog_change (action_id, entity, entity_id, before) values (%s, 'party', %s, null)",
                    (action_id, new["id"]))
        return pick(lang, f"{new['name']} ka khata khul gaya.", f"Added {new['name']}.")
    cur.execute("insert into catalog_change (action_id, entity, entity_id, before) values (%s, 'party', %s, %s)",
                (action_id, effects["party_id"], db.Json(effects["before"])))
    sets = ", ".join(f"{k} = %s" for k in effects["changes"])
    row = cur.execute(f"update party set {sets} where id = %s and business_id = %s returning name",
                      (*effects["changes"].values(), effects["party_id"], business_id)).fetchone()
    return pick(lang, f"{row['name']} update ho gaya.", f"Updated {row['name']}.")


def plan(request_id: str, business_id: str, intent: str, fields: dict) -> PlanResult:
    ext, lang = Extraction.model_validate(fields), of(fields)
    if intent == "set_party":
        return _set_party(request_id, business_id, ext, lang)
    if intent in ("sale", "purchase"):
        return _trade(request_id, business_id, ext, intent, fields.get("invoice_id"), lang)
    if intent == "expense":
        return _expense(request_id, business_id, ext, lang)
    if intent in ("payment_in", "payment_out"):
        return _payment(request_id, business_id, ext, intent == "payment_in", lang)
    return PlanResult(request_id=request_id, ok=False, error=f"ledger cannot plan {intent!r}")


def commit(request_id: str, action_id: str, business_id: str, effects: dict, lang: str = DEFAULT) -> Committed:
    """Write the planned txn rows under action_id. Re-applying the same action_id is a no-op."""
    action_id = str(UUID(action_id))
    ref = effects.get("party")
    with db.transaction() as cur:
        cur.execute("select pg_advisory_xact_lock(hashtext(%s))", ("ledger:" + action_id,))
        if (cur.execute("select 1 from txn where action_id = %s limit 1", (action_id,)).fetchone()
                or cur.execute("select 1 from catalog_change where action_id = %s and entity = 'party'",
                               (action_id,)).fetchone()):
            return Committed(request_id=request_id, ok=True,
                             message=pick(lang, "Khata pehle se update hai.", "Already recorded."))
        if "new_party" in effects or "changes" in effects:
            return Committed(request_id=request_id, ok=True,
                             message=_save_party(cur, business_id, action_id, effects, lang))
        if link := effects.get("link_invoice"):   # a bill made later for this sale: point the sale row at it
            cur.execute("update txn set invoice_id = %s where action_id = %s and kind = 'sale' and invoice_id is null",
                        (link["invoice_id"], link["sale_action_id"]))
            return Committed(request_id=request_id, ok=True, message="")
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
    message = pick(lang, "Khata update ho gaya.", "Khata updated.")
    if party_id:
        bal = lookup.party_balance(party_id)
        kind = ref.get("kind") or db.fetch_one("select kind from party where id = %s", (party_id,))["kind"]
        if bal > 0 and kind == "customer":
            message += pick(lang, f" {ref['name']} ka total baaki: {inr(bal)}.", f" {ref['name']} now owes {inr(bal)} in total.")
        elif bal < 0 and kind == "supplier":
            message += pick(lang, f" Aapko {ref['name']} ko {inr(-bal)} dena hai.", f" You now owe {ref['name']} {inr(-bal)}.")
        elif bal == 0:
            message += pick(lang, f" {ref['name']} ka hisaab barabar.", f" {ref['name']} is settled.")
        else:                                 # an advance, either way round
            message += " " + owes_line(ref["name"], bal, kind, lang) + "."
    return Committed(request_id=request_id, ok=True, message=message)


# Queries

LIVE = "not exists (select 1 from action a where a.id = t.action_id and a.status = 'undone')"
KIND_LABEL = {"sale": "Sale", "purchase": "Purchase", "expense": "Expense", "receipt": "Received",
              "payment": "Paid", "credit_note": "Credit note"}
KIND_LABEL_HI = {"sale": "Bikri", "purchase": "Kharid", "expense": "Kharcha", "receipt": "Mila",
                 "payment": "Diya", "credit_note": "Credit note"}
CASH_SIGN = "case when t.kind in ('sale', 'receipt') then 1 else -1 end"
STATEMENT_ROWS = 10
DAYBOOK_ROWS = 25


def _entry(t: dict, lang: str) -> str:
    what = (KIND_LABEL if lang == "en" else KIND_LABEL_HI)[t["kind"]]
    detail = t["note"] or t["category"] or ""
    mode = f" ({MODE_LABEL[t['mode']]})" if t["mode"] in MODE_LABEL else ""
    return f"{what}{mode}" + (f": {detail}" if detail else "")


def statement(request_id: str, business_id: str, party_name: str, lang: str) -> QueryResult:
    res = lookup.find_party(business_id, party_name, lang=lang)
    if res.question or not res.row:
        return QueryResult(request_id=request_id, ok=True, text=res.question or not_found(party_name, lang))
    party = res.row
    phone = f" · Phone {show_phone(party['phone'])}" if party["phone"] else ""
    rows = db.fetch_all(f"select t.* from txn t where t.party_id = %s and {LIVE} "
                        f"order by t.txn_date desc, t.id desc limit %s", (party["id"], STATEMENT_ROWS))
    bal = lookup.party_balance(party["id"])
    status = owes_line(party["name"], bal, party["kind"], lang)
    if not rows:
        return QueryResult(request_id=request_id, ok=True,
                           text=pick(lang, f"{party['name']} ki koi entry nahi hai.", f"No entries for {party['name']} yet.")
                           + f" {status}{phone}")
    sign = {"sale": "+", "payment": "+", "receipt": "−", "purchase": "−", "credit_note": "−"}
    last = pick(lang, f" · pichhli {len(rows)} entries", f" · last {len(rows)} entries")
    table = cards.table(
        f"Khata: {party['name']}", ["Entry", "Date", "Amount"],
        [[_entry(t, lang), f"{t['txn_date']:%d %b}", f"{sign.get(t['kind'], '')}{inr(t['amount_paise'])}"] for t in rows],
        subtitle=status + phone + (last if len(rows) == STATEMENT_ROWS else ""),
        footer=[cards.button("Remind", "remind", party_id=str(party["id"]), party=party["name"])]
        if party["kind"] == "customer" and bal > 0 else None,
        detail=lambda r: f"{r[2]} · {r[1]}")
    return QueryResult(request_id=request_id, ok=True, card_kind=table.kind, card_payload=table.payload,
                       terminal=table.terminal, text=table.fallback)


def day_of(period: str | None) -> date:
    today = clock.today()
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


def daybook(request_id: str, business_id: str, period: str | None, lang: str) -> QueryResult:
    day = day_of(period)
    rows = db.fetch_all(f"select t.*, p.name as party from txn t left join party p on p.id = t.party_id "
                        f"where t.business_id = %s and t.txn_date = %s and {LIVE} order by t.id",
                        (business_id, day))
    totals = {k: sum(t["amount_paise"] for t in rows if t["kind"] == k) for k in KIND_LABEL}
    cash = cash_in_hand(business_id)
    when = pick(lang, "Aaj", "Today") if day == clock.today() else f"{day:%d %b}"
    summary = pick(lang, f"Bikri {inr(totals['sale'])} · Aaya {inr(totals['receipt'])} · "
                         f"Kharcha {inr(totals['expense'])} · Galle mein cash {inr(cash)}",
                   f"Sales {inr(totals['sale'])} · Received {inr(totals['receipt'])} · "
                   f"Expenses {inr(totals['expense'])} · Cash in hand {inr(cash)}")
    if not rows:
        return QueryResult(request_id=request_id, ok=True, text=pick(
            lang, f"{when} koi entry nahi hai. Galle mein cash: {inr(cash)}.",
            f"No entries for {when.lower() if when == 'Today' else when}. Cash in hand: {inr(cash)}."))
    shown = rows[-DAYBOOK_ROWS:]
    table = cards.table(
        f"Day book: {when}", ["Entry", "Party", "Amount"],
        [[_entry(t, lang), t["party"] or ("Walk-in" if t["kind"] == "sale" else "—"), inr(t["amount_paise"])]
         for t in shown],
        subtitle=summary + (pick(lang, f" · {len(rows)} mein se pichhli {len(shown)}", f" · last {len(shown)} of {len(rows)}")
                            if len(rows) > len(shown) else ""),
        detail=lambda r: r[2] + (f" · {r[1]}" if r[1] != "—" else ""))
    return QueryResult(request_id=request_id, ok=True, card_kind=table.kind, card_payload=table.payload,
                       terminal=table.terminal, text=table.fallback)


def query(request_id: str, business_id: str, intent: str, fields: dict) -> QueryResult:
    if intent == "statement":
        return statement(request_id, business_id, fields.get("party") or "", of(fields))
    if intent == "daybook":
        return daybook(request_id, business_id, fields.get("period"), of(fields))
    return QueryResult(request_id=request_id, ok=False, text=f"ledger cannot answer {intent!r}")


def _undo_parties(action_id: str, lang: str) -> list[str]:
    """Put back parties this action edited; remove ones it added unless they already have entries."""
    notes = []
    with db.transaction() as cur:
        cur.execute("select pg_advisory_xact_lock(hashtext(%s))", ("ledger:" + action_id,))
        for c in cur.execute("delete from catalog_change where action_id = %s and entity = 'party' "
                             "returning entity_id, before", (action_id,)).fetchall():
            if c["before"]:
                sets = ", ".join(f"{k} = %s" for k in c["before"])
                row = cur.execute(f"update party set {sets} where id = %s returning name",
                                  (*c["before"].values(), c["entity_id"])).fetchone()
                notes.append(pick(lang, f"{row['name']} pehle jaisa.", f"{row['name']} is back as it was."))
            elif cur.execute("select 1 from txn where party_id = %s union all select 1 from invoice where party_id = %s "
                             "union all select 1 from purchase_order where supplier_id = %s limit 1",
                             (c["entity_id"],) * 3).fetchone():
                notes.append(pick(lang, "Naya khata rakha, kyunki uski entries ho chuki hain.",
                                  "Kept the new party, because it already has entries."))
            else:
                cur.execute("delete from reminder where party_id = %s", (c["entity_id"],))
                row = cur.execute("delete from party where id = %s returning name", (c["entity_id"],)).fetchone()
                notes.append(pick(lang, f"{row['name']} ka khata hataya.", f"Removed {row['name']}."))
    return notes


def undo(action_id: str, lang: str = DEFAULT) -> UndoDone:
    """Nothing to write for entries: the action is already marked undone and every balance and query skips it.
    Two exceptions: parties this action added or edited (put back), and an earlier sale this action made a bill
    for (unlinked, so it counts as a sale without a bill again)."""
    action_id = str(UUID(action_id))
    if notes := _undo_parties(action_id, lang):
        return UndoDone(action_id=action_id, ok=True, message=" ".join(notes))
    with db.transaction() as cur:
        unlinked = cur.execute("update txn set invoice_id = null where action_id <> %s and invoice_id in "
                               "(select id from invoice where action_id = %s) returning id", (action_id, action_id)).fetchall()
    if unlinked:
        return UndoDone(action_id=action_id, ok=True,
                        message=pick(lang, "Sale wapas bina bill ki ho gayi.", "The sale is back to having no bill."))
    parties = db.fetch_all("select distinct p.id, p.name, p.kind from txn t join party p on p.id = t.party_id "
                           "where t.action_id = %s", (action_id,))
    notes = [pick(lang, "Khata se hata diya.", "Removed from the khata.")]
    for p in parties:
        bal = lookup.party_balance(p["id"])
        notes.append(pick(lang, "Ab ", "Now: ") + owes_line(p["name"], bal, p["kind"], lang) + ".")
    return UndoDone(action_id=action_id, ok=True, message=" ".join(notes))
