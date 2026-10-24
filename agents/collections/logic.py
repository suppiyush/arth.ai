"""Collections: who owes what, payment reminders, and keeping invoice status in step with receipts.

- query "dues": customers with a positive balance, aged by their oldest unpaid sale.
- plan "reminder": ASI1 writes the wording with placeholders; code fills amount, bill numbers and the
  UPI link, so no figure comes from the model. Commit records it and returns a wa.me share link.
- commit "after_money" (runs after Ledger/Billing for sales, bills and receipts): applies receipts to
  invoices, the same action's own bill first, then oldest open invoices, and sets a follow-up date
  when the customer still owes. Re-running it for an action replaces, never adds.
"""

import asyncio
import base64
import io
import re
from datetime import timedelta
from urllib.parse import quote, urlencode
from uuid import UUID

import qrcode

from core import cards, clock, db, llm, lookup
from core.lang import DEFAULT, of, pick, plural
from core.models import Committed, PlanResult, QueryResult, UndoDone
from core.money import inr, to_rupees

FOLLOW_UP_DAYS = 7
NOT_UNDONE = "not exists (select 1 from action a where a.id = {col} and a.status = 'undone')"


# Dues

def oldest_unpaid_days(party_id, due_paise: int) -> int | None:
    """Payments clear the oldest sales first, so the unpaid part is the newest sales adding up to the due."""
    sales = db.fetch_all(f"select amount_paise, txn_date from txn t where party_id = %s and kind = 'sale' "
                         f"and {NOT_UNDONE.format(col='t.action_id')} order by txn_date desc, id desc", (party_id,))
    remaining, oldest = due_paise, None
    for s in sales:
        oldest = s["txn_date"]
        remaining -= s["amount_paise"]
        if remaining <= 0:
            break
    return (clock.today() - oldest).days if oldest else None


def dues(business_id: str) -> list[dict]:
    rows = db.fetch_all("select pb.id, pb.name, pb.kind, pb.balance_paise, p.phone from party_balance pb "
                        "join party p on p.id = pb.id where pb.business_id = %s and pb.balance_paise <> 0 "
                        "order by pb.balance_paise desc", (business_id,))
    for r in rows:
        r["days"] = oldest_unpaid_days(r["id"], r["balance_paise"]) if r["kind"] == "customer" else None
    return rows


def query(request_id: str, business_id: str, intent: str, fields: dict) -> QueryResult:
    if intent != "dues":
        return QueryResult(request_id=request_id, ok=False, text=f"collections cannot answer {intent!r}")
    lang = of(fields)
    rows = dues(business_id)
    owed = [r for r in rows if r["kind"] == "customer" and r["balance_paise"] > 0]
    payable = [r for r in rows if r["kind"] == "supplier" and r["balance_paise"] < 0]
    if fields.get("payable"):
        return payables(request_id, sorted(payable, key=lambda r: r["balance_paise"]), fields.get("top"), lang)
    pay_line = ""
    if payable:
        owe, names = inr(-sum(r["balance_paise"] for r in payable)), ", ".join(r["name"] for r in payable)
        pay_line = pick(lang, f"Aapko suppliers ko dena hai: {owe} ({names}).", f"You owe suppliers {owe} ({names}).")
    if not owed:
        return QueryResult(request_id=request_id, ok=True, text=" ".join(filter(None, [
            pick(lang, "Kisi customer ka udhaar baaki nahi hai.", "No customer owes you money."), pay_line])))
    title = pick(lang, "Udhaar list", "Who owes you")
    if days := fields.get("older_than_days"):    # "15 din se purana udhaar": only dues at least that old
        owed = [r for r in owed if r["days"] is not None and r["days"] >= days]
        if not owed:
            return QueryResult(request_id=request_id, ok=True, text=pick(
                lang, f"{days} din se purana koi udhaar nahi hai.", f"Nobody has owed you for {days} days or more."))
        title, pay_line = pick(lang, f"{days}+ din purana udhaar", f"Owed for {days}+ days"), ""
    total = sum(r["balance_paise"] for r in owed)
    summary = pick(lang, f"Total udhaar {inr(total)}, {plural(len(owed), 'customer')} se.",
                   f"{plural(len(owed), 'customer')} owe you {inr(total)} in total.")
    if top := fields.get("top"):              # "sabse zyada kiska udhaar": answer first, then only the top rows
        first = owed[0]
        since = pick(lang, f" ({first['days']} din se)", f" (for {first['days']} days)") if first["days"] is not None else ""
        summary = pick(lang, f"Sabse zyada udhaar {first['name']} ka hai: {inr(first['balance_paise'])}{since}.",
                       f"{first['name']} owes you the most: {inr(first['balance_paise'])}{since}.") + f" {summary}"
        title = (pick(lang, "Sabse zyada udhaar", "Owes you the most") if top == 1
                 else pick(lang, f"Top {min(top, len(owed))} udhaar", f"Top {min(top, len(owed))} dues"))
        owed, pay_line = owed[:top], ""
    def since(d: int | None) -> str:
        if d is None:
            return "—"
        if d == 0:
            return pick(lang, "aaj se", "since today")
        return pick(lang, f"{d} din se", f"for {d} {'day' if d == 1 else 'days'}")

    card = cards.table(
        title, ["Customer", pick(lang, "Baaki", "Owes"), pick(lang, "Kab se", "For")],
        [[r["name"], inr(r["balance_paise"]), since(r["days"])] for r in owed],
        subtitle=summary,
        row_buttons=[cards.button("Remind", "remind", party_id=str(r["id"]), party=r["name"]) for r in owed],
        footer=[cards.text(pay_line, "muted")] if pay_line else None,
        detail=lambda r: pick(lang, f"{r[1]} baaki", f"{r[1]} due") + (f" · {r[2]}" if r[2] != "—" else ""),
    )
    return QueryResult(request_id=request_id, ok=True, card_kind=card.kind, card_payload=card.payload,
                       terminal=card.terminal, text=card.fallback + (f"\n{pay_line}" if pay_line else ""),
                       narration=summary)


def payables(request_id: str, rows: list[dict], top: int | None, lang: str) -> QueryResult:
    """What the shop owes suppliers, largest first ("mujhe kisko dena hai")."""
    if not rows:
        return QueryResult(request_id=request_id, ok=True,
                           text=pick(lang, "Aapko kisi supplier ko paise nahi dene hain.", "You don't owe any supplier."))
    total = inr(-sum(r["balance_paise"] for r in rows))
    summary = pick(lang, f"Aapko {plural(len(rows), 'supplier')} ko kul {total} dene hain.",
                   f"You owe {plural(len(rows), 'supplier')} {total} in total.")
    if top:
        most = inr(-rows[0]["balance_paise"])
        summary = pick(lang, f"Sabse zyada {rows[0]['name']} ko dena hai: {most}.",
                       f"You owe {rows[0]['name']} the most: {most}.") + f" {summary}"
        rows = rows[:top]
    card = cards.table(pick(lang, "Aapko dena hai", "You owe"), ["Supplier", pick(lang, "Dena hai", "Amount")],
                       [[r["name"], inr(-r["balance_paise"])] for r in rows], subtitle=summary,
                       detail=lambda r: pick(lang, f"{r[1]} dena hai", f"You owe {r[1]}"))
    return QueryResult(request_id=request_id, ok=True, card_kind=card.kind, card_payload=card.payload,
                       terminal=card.terminal, text=card.fallback, narration=summary)


# Reminders

def open_invoices(party_id) -> list[dict]:
    return db.fetch_all(f"""
        select i.id, i.number, i.total_paise,
               i.total_paise - coalesce((select sum(ip.amount_paise) from invoice_payment ip
                                          where ip.invoice_id = i.id and {NOT_UNDONE.format(col='ip.action_id')}), 0)
               as open_paise
        from invoice i where i.party_id = %s and i.status in ('issued', 'partly_paid')
        order by i.issued_at, i.number""", (party_id,))


def upi_link(vpa: str, shop: str, amount_paise: int, note: str) -> str:
    params = {"pa": vpa, "pn": shop, "am": f"{to_rupees(amount_paise):.2f}", "cu": "INR", "tn": note[:50]}
    return "upi://pay?" + urlencode(params, quote_via=quote)


def qr_data_uri(data: str) -> str:
    img = qrcode.make(data, box_size=4, border=2)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def wa_link(phone: str | None, message: str) -> str:
    digits = "".join(ch for ch in (phone or "") if ch.isdigit())
    return f"https://wa.me/{digits}?text={quote(message)}"


FALLBACK_TEMPLATE = ("Namaste {NAME} ji, {SHOP} se yaad dila rahe hain: aapka {AMOUNT} baaki hai{BILLS}. "
                     "Suvidha ho to yahan se pay kar dijiye: {LINK} Dhanyavaad!")
FALLBACK_TEMPLATE_EN = ("Hello {NAME}, a gentle reminder from {SHOP}: {AMOUNT} is due{BILLS}. "
                        "You can pay here: {LINK} Thank you!")


async def reminder_template(shop: str, name: str, lang: str = DEFAULT) -> str:
    """ASI1 writes the wording only; placeholders are filled by code. Falls back to a fixed template."""
    fallback = pick(lang, FALLBACK_TEMPLATE, FALLBACK_TEMPLATE_EN)
    try:
        text = await llm.explain(
            "Write one short, polite WhatsApp payment reminder (under 60 words, warm, not pushy) "
            "from a shop to a regular customer. Use these placeholders exactly, each once: {NAME}, {SHOP}, "
            "{AMOUNT}, {BILLS} (may be empty, put it right after the amount), {LINK}. "
            "Write no numbers, amounts or dates yourself. Reply with only the message.",
            {"shop": shop, "customer": name}, lang=lang)
    except Exception:
        return fallback
    if all(p in text for p in ("{NAME}", "{AMOUNT}", "{LINK}")) and not any(ch.isdigit() for ch in text):
        return text.strip().strip('"')
    return fallback


async def plan(request_id: str, business_id: str, intent: str, fields: dict) -> PlanResult:
    if intent != "reminder":
        return PlanResult(request_id=request_id, ok=False, error=f"collections cannot plan {intent!r}")

    lang = of(fields)

    def load():
        if fields.get("party_id"):
            row = db.fetch_one("select id, name, phone, kind from party where id = %s and business_id = %s",
                               (fields["party_id"], business_id))
            return row, None if row else pick(lang, "Ye customer nahi mila.", "I couldn't find that customer.")
        res = lookup.find_party(business_id, fields.get("party") or "", "customer", lang)
        if not res.row and not res.question:
            return None, pick(lang, f"'{fields.get('party')}' naam ka koi customer nahi mila.",
                              f"I couldn't find a customer called '{fields.get('party')}'.")
        return res.row, res.question

    party, question = await asyncio.to_thread(load)
    if question:
        return PlanResult(request_id=request_id, ok=True, question=question)
    due = await asyncio.to_thread(lookup.party_balance, party["id"])
    if due <= 0:
        return PlanResult(request_id=request_id, ok=True,
                          question=pick(lang, f"{party['name']} ka koi udhaar baaki nahi hai.", f"{party['name']} owes nothing."))
    biz = await asyncio.to_thread(db.fetch_one, "select name, upi_vpa from business where id = %s", (business_id,))
    bills = [i["number"] for i in await asyncio.to_thread(open_invoices, party["id"])]

    link = upi_link(biz["upi_vpa"], biz["name"], due, ", ".join(bills) or "Udhaar") if biz["upi_vpa"] else None
    template = await reminder_template(biz["name"], party["name"], lang)
    message = (template.replace("{NAME}", party["name"]).replace("{SHOP}", biz["name"])
               .replace("{AMOUNT}", inr(due)).replace("{BILLS}", f" (bill {', '.join(bills)})" if bills else "")
               # WhatsApp rarely makes upi:// clickable, so the plain UPI ID goes alongside it
               .replace("{LINK}", f"{link} (UPI ID: {biz['upi_vpa']})" if link else "(UPI ID not set)"))
    message = re.sub(r" +([.,!?])", r"\1", re.sub(r" {2,}", " ", message))   # an empty {BILLS} leaves gaps
    phone = f"+91 {party['phone'][2:7]} {party['phone'][7:]}" if party["phone"] and len(party["phone"]) == 12 \
        else party["phone"] or pick(lang, "phone nahi hai", "no phone")
    rows = [{"label": "To", "value": f"{party['name']} ({phone})"},
            {"label": pick(lang, "Baaki", "Due"), "value": inr(due)}]
    if bills:
        rows.append({"label": "Bills", "value": ", ".join(bills)})
    rows.append({"label": "Message", "value": message})
    return PlanResult(request_id=request_id, ok=True, summary_rows=rows, effects={
        "title": pick(lang, f"{party['name']} ko reminder", f"Reminder for {party['name']}"), "party_id": str(party["id"]), "party": party["name"],
        "amount_paise": due, "message": message, "upi_link": link, "wa_link": wa_link(party["phone"], message),
        "qr": qr_data_uri(link) if link else None})


# Commits

def _apply_receipts(cur, action_id: str) -> list[str]:
    """Allocate this action's receipts to invoices; returns 'number status' strings for the reply."""
    cur.execute("delete from invoice_payment where action_id = %s", (action_id,))
    touched, notes = {}, []      # dict keeps invoice order for the reply
    parties = cur.execute("select distinct party_id from txn where action_id = %s and party_id is not null",
                          (action_id,)).fetchall()
    for p in parties:
        party_id = p["party_id"]
        received = cur.execute("select coalesce(sum(amount_paise), 0) as s from txn where action_id = %s "
                               "and party_id = %s and kind = 'receipt'", (action_id, party_id)).fetchone()["s"]
        own = [r["invoice_id"] for r in cur.execute(
            "select invoice_id from txn where action_id = %s and party_id = %s and invoice_id is not null",
            (action_id, party_id)).fetchall()]
        candidates = cur.execute(f"""
            select i.id, i.number, i.total_paise - coalesce((select sum(ip.amount_paise) from invoice_payment ip
                     where ip.invoice_id = i.id and {NOT_UNDONE.format(col='ip.action_id')}), 0) as open_paise
            from invoice i where i.party_id = %s and i.status <> 'cancelled'
            order by (i.id = any(%s::uuid[])) desc, i.issued_at, i.number""", (party_id, own)).fetchall()
        for inv in candidates:
            touched[inv["id"]] = True
            if received <= 0 or inv["open_paise"] <= 0:
                continue
            pay = min(received, inv["open_paise"])
            cur.execute("insert into invoice_payment (action_id, invoice_id, amount_paise) values (%s, %s, %s)",
                        (action_id, inv["id"], pay))
            received -= pay
    for inv_id in touched:
        row = cur.execute(f"""
            update invoice i set status = case
                when paid >= i.total_paise then 'paid' when paid > 0 then 'partly_paid' else 'issued' end
            from (select coalesce(sum(amount_paise), 0) as paid from invoice_payment ip
                  where ip.invoice_id = %s and {NOT_UNDONE.format(col='ip.action_id')}) s
            where i.id = %s and i.status <> 'cancelled'
            returning i.number, i.status, (select count(*) from invoice_payment where action_id = %s
                                           and invoice_id = i.id) as hit""",
            (inv_id, inv_id, action_id)).fetchone()
        if row and row["hit"]:
            notes.append(f"{row['number']} {row['status'].replace('_', ' ')}")
    return notes


def _follow_up(cur, action_id: str, lang: str) -> list[str]:
    """After a credit sale or bill, schedule a follow-up for customers who still owe."""
    notes = []
    sold_to = cur.execute("select distinct t.party_id, p.name from txn t join party p on p.id = t.party_id "
                          "where t.action_id = %s and t.kind = 'sale' and p.kind = 'customer'", (action_id,)).fetchall()
    for p in sold_to:
        due = cur.execute("select balance_paise from party_balance where id = %s", (p["party_id"],)).fetchone()
        if not due or due["balance_paise"] <= 0:
            continue
        when = clock.today() + timedelta(days=FOLLOW_UP_DAYS)
        if not cur.execute("select 1 from reminder where action_id = %s and party_id = %s",
                           (action_id, p["party_id"])).fetchone():
            cur.execute("insert into reminder (action_id, party_id, amount_paise, follow_up) values (%s, %s, %s, %s)",
                        (action_id, p["party_id"], due["balance_paise"], when))
        notes.append(pick(lang, f"{p['name']} ko {when:%d %b} ko yaad dilaunga.",
                          f"I'll remind you about {p['name']} on {when:%d %b}."))
    return notes


def commit(request_id: str, action_id: str, business_id: str, effects: dict, lang: str = DEFAULT) -> Committed:
    action_id = str(UUID(action_id))
    with db.transaction() as cur:
        cur.execute("select pg_advisory_xact_lock(hashtext(%s))", ("collections:" + action_id,))
        if link := effects.get("link_receipts"):  # a bill made later for a sale: what was paid then counts towards it
            paid = cur.execute("select coalesce(sum(amount_paise), 0) as s from txn where action_id = %s and kind = 'receipt'",
                               (link["sale_action_id"],)).fetchone()["s"]
            if paid:
                cur.execute("insert into invoice_payment (action_id, invoice_id, amount_paise) values (%s, %s, %s) "
                            "on conflict do nothing", (link["sale_action_id"], link["invoice_id"], paid))
            return Committed(request_id=request_id, ok=True, message="")
        if effects.get("after_money"):
            bills = _apply_receipts(cur, action_id)
            notes = _follow_up(cur, action_id, lang)
            if bills:
                notes.insert(0, "Bill status: " + ", ".join(bills) + ".")
            return Committed(request_id=request_id, ok=True, message=" ".join(notes))
        # an approved reminder
        if not cur.execute("select 1 from reminder where action_id = %s", (action_id,)).fetchone():
            cur.execute("insert into reminder (action_id, party_id, amount_paise, message, upi_link, sent_at) "
                        "values (%s, %s, %s, %s, %s, now())",
                        (action_id, effects["party_id"], effects["amount_paise"], effects["message"],
                         effects["upi_link"]))
    lines = [pick(lang, f"WhatsApp pe bhejne ke liye kholiye: {effects['wa_link']}",
                  f"Open this to send it on WhatsApp: {effects['wa_link']}")]
    if effects["upi_link"]:
        lines.append(f"UPI link: {effects['upi_link']}")
    return Committed(request_id=request_id, ok=True, message="\n".join(lines))


def undo(action_id: str, lang: str = DEFAULT) -> UndoDone:
    """The action is already marked undone, so its invoice_payment rows no longer count: re-derive the
    status of the invoices they touched, and drop the action's reminders and follow-ups."""
    action_id = str(UUID(action_id))
    notes = []
    with db.transaction() as cur:
        cur.execute("select pg_advisory_xact_lock(hashtext(%s))", ("collections:" + action_id,))
        for r in cur.execute("select distinct invoice_id from invoice_payment where action_id = %s",
                             (action_id,)).fetchall():
            row = cur.execute(f"""
                update invoice i set status = case
                    when paid >= i.total_paise then 'paid' when paid > 0 then 'partly_paid' else 'issued' end
                from (select coalesce(sum(amount_paise), 0) as paid from invoice_payment ip
                      where ip.invoice_id = %s and {NOT_UNDONE.format(col='ip.action_id')}) s
                where i.id = %s and i.status <> 'cancelled' returning i.number, i.status""",
                (r["invoice_id"], r["invoice_id"])).fetchone()
            if row:
                notes.append(pick(lang, f"{row['number']} ab {row['status'].replace('_', ' ')}.",
                                  f"{row['number']} is now {row['status'].replace('_', ' ')}."))
        cur.execute("delete from reminder where action_id = %s", (action_id,))
    return UndoDone(action_id=action_id, ok=True, message=" ".join(notes))
