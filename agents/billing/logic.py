"""Billing: GST invoices. Plan prices the lines and splits the tax (nothing is written, so the
orchestrator can re-plan the draft on edits like "5% discount"); Commit assigns the next number for
the financial year and writes invoice + invoice_line in one transaction.

The money side of a bill (sale txn, stock out) is planned and committed by Ledger and Inventory
under the same action_id; the orchestrator wires them together.

Plan "bill_from_sale" makes a bill for a sale already in the khata (a quick sale made without "bill
banao"), from what was actually sold and charged.

Query "invoices" shows bills already made: one bill in full with its PDF link (by number, the latest, or the
only match), or a list filtered by customer or walk-in, period and unpaid. Query "invoice_pdf" sends PDF
links: the PDF is drawn (pdf.py), kept locally and, when Cloudinary is configured, uploaded once, with the
link remembered in invoice.pdf_url.
"""

from decimal import Decimal
from uuid import UUID, uuid4

from agents.billing import pdf
from core import cards, clock, config, db, gst, lookup, storage
from core.lang import DEFAULT, of, pick
from core.llm import Extraction
from core.models import Committed, PlanResult, QueryResult, UndoDone
from core.money import inr, qty, to_paise

LIST_ROWS = 15
STATUS_LABEL_EN = {"issued": "Unpaid", "partly_paid": "Part paid", "paid": "Paid", "cancelled": "Cancelled",
                   "draft": "Draft"}
STATUS_LABEL = {"issued": "Baaki", "partly_paid": "Thoda baaki", "paid": "Paid", "cancelled": "Cancelled",
                "draft": "Draft"}
OPEN_PAISE = """i.total_paise - coalesce((select sum(ip.amount_paise) from invoice_payment ip
    where ip.invoice_id = i.id and not exists (select 1 from action a where a.id = ip.action_id
    and a.status = 'undone')), 0)"""


def _ask(request_id: str, question: str) -> PlanResult:
    return PlanResult(request_id=request_id, ok=True, question=question)


def next_number_preview(business_id: str, fy: str) -> str:
    row = db.fetch_one("select last_no from invoice_counter where business_id = %s and fy = %s", (business_id, fy))
    return gst.invoice_number(fy, (row["last_no"] if row else 0) + 1)


def draft(business_id: str, bill_to: str, items: list[tuple], inv, discount_pct, lang: str) -> tuple[list[dict], str]:
    """The card rows of a bill about to be issued, and its financial year. items: (item row, qty, rate)."""
    fy = gst.financial_year(clock.today())
    rows = [{"label": "Invoice no", "value": f"{next_number_preview(business_id, fy)}"
                                             + pick(lang, " (confirm par)", " (on confirm)")},
            {"label": "Bill to", "value": bill_to}]
    for (it, q, rate), lt in zip(items, inv.lines):
        rows.append({"label": f"{qty(q, it['unit'])} {it['name']} @ {inr(rate)}",
                     "value": f"{inr(lt.total_paise)} (GST {qty(it['gst_rate'])}%)"})
    if discount_pct:
        rows.append({"label": "Discount", "value": f"{discount_pct:g}%"})
    rows.append({"label": "Taxable value", "value": inr(inv.taxable_paise)})
    if inv.inter_state:
        rows.append({"label": "IGST", "value": inr(inv.igst_paise)})
    else:
        rows += [{"label": "CGST", "value": inr(inv.cgst_paise)}, {"label": "SGST", "value": inr(inv.sgst_paise)}]
    if inv.round_off_paise:
        rows.append({"label": "Round off", "value": inr(inv.round_off_paise)})
    rows.append({"label": "Bill total", "value": inr(inv.total_paise)})
    return rows, fy


def invoice_effects(title: str, ref: dict | None, fy: str, status: str, discount_pct, items: list[tuple], inv) -> dict:
    """What commit() writes: the invoice and its lines."""
    return {
        "title": title, "invoice_id": str(uuid4()), "party": ref, "fy": fy, "status": status,
        "discount_pct": discount_pct or 0,
        "taxable_paise": inv.taxable_paise, "cgst_paise": inv.cgst_paise, "sgst_paise": inv.sgst_paise,
        "igst_paise": inv.igst_paise, "round_off_paise": inv.round_off_paise, "total_paise": inv.total_paise,
        "lines": [{"item_id": str(it["id"]), "item": it["name"], "unit": it["unit"], "qty": float(q),
                   "rate_paise": rate, "gst_rate": float(it["gst_rate"]),
                   "amount_paise": lt.taxable_paise, "tax_paise": lt.tax_paise}
                  for (it, q, rate), lt in zip(items, inv.lines)],
    }


def plan(request_id: str, business_id: str, intent: str, fields: dict) -> PlanResult:
    if intent == "bill_from_sale":
        return plan_from_sale(request_id, business_id, fields)
    if intent != "bill":
        return PlanResult(request_id=request_id, ok=False, error=f"billing cannot plan {intent!r}")
    ext, lang = Extraction.model_validate(fields), of(fields)
    if not ext.party:
        return _ask(request_id, pick(lang, "Bill kiske naam pe banana hai?", "Who is the bill for?"))
    res = lookup.find_party(business_id, ext.party, "customer", lang)
    if res.question:
        return _ask(request_id, res.question)
    party_row = res.row
    ref = ({"id": str(party_row["id"]), "name": party_row["name"]} if party_row
           else {"new": True, "name": ext.party.strip().title(), "kind": "customer"})

    if not ext.lines:
        return _ask(request_id, pick(lang, "Bill mein kya saamaan likhun?", "Which items go on the bill?"))
    items = []
    for line in ext.lines:
        r = lookup.find_item(business_id, line.item, lang)
        if r.question:
            return _ask(request_id, r.question)
        if line.qty is None:
            return _ask(request_id, pick(lang, f"{r.row['name']} kitna?", f"How much {r.row['name']}?"))
        rate = to_paise(line.rate) if line.rate is not None else r.row["price_paise"]
        items.append((r.row, line.qty, rate))

    shop = db.fetch_one("select state_code from business where id = %s", (business_id,))
    party_state = party_row["state_code"] if party_row else None
    inv = gst.invoice_tax([(q, rate, it["gst_rate"]) for it, q, rate in items], shop["state_code"], party_state,
                          ext.discount_pct or 0)
    paid = ext.paid_paise(inv.total_paise) or 0
    status = "paid" if paid >= inv.total_paise else "partly_paid" if paid else "issued"
    rows, fy = draft(business_id, ref["name"] + (pick(lang, " (naya)", " (new)") if ref.get("new") else ""),
                     items, inv, ext.discount_pct, lang)
    title = pick(lang, f"{ref['name']} ka GST bill", f"GST bill for {ref['name']}")
    return PlanResult(request_id=request_id, ok=True, summary_rows=rows,
                      effects=invoice_effects(title, ref, fy, status, ext.discount_pct, items, inv))


# A bill for a sale already in the khata (a quick sale made without "bill banao")

def plan_from_sale(request_id: str, business_id: str, fields: dict) -> PlanResult:
    """The latest sale without a bill for the customer (or walk-in) and period asked for, as a draft GST bill.
    Items and quantities come from the sale's stock moves, and what was charged is spread over them by list
    value, so the bill totals what the customer actually paid. Commit writes only the invoice; Ledger links
    the sale to it and Collections counts what was paid at the time (the orchestrator wires both)."""
    lang = of(fields)
    party, problem = _party(business_id, fields, lang)
    if problem:
        return _ask(request_id, problem)
    start, end, when = clock.period_range(fields.get("period"), "today", lang)
    sql = ("select t.id, t.action_id, t.amount_paise, t.txn_date, a.created_at from txn t join action a on a.id = t.action_id "
           "where t.business_id = %s and t.kind = 'sale' and t.invoice_id is null and a.status <> 'undone' "
           "and t.txn_date between %s and %s")
    params: list = [business_id, start, end]
    if party:
        sql, params = sql + " and t.party_id = %s", params + [party["id"]]
    elif fields.get("walk_in"):
        sql += " and t.party_id is null"
    sales = db.fetch_all(sql + " order by a.created_at desc", params)
    who = party["name"] if party else "walk-in"
    if not sales:
        return _ask(request_id, pick(lang, f"{when} {who} ki koi bina bill wali sale nahi mili.",
                                     f"I couldn't find a sale without a bill for {who} ({when.lower()})."))
    sale = sales[0]
    moves = db.fetch_all("select it.*, -m.qty as qty from stock_move m join item it on it.id = m.item_id "
                         "where m.action_id = %s and m.qty < 0 order by it.name", (sale["action_id"],))
    if not moves:
        return _ask(request_id, pick(lang, "Is sale mein saamaan likha nahi hai, isliye isse bill nahi ban sakta.",
                                     "That sale has no items recorded, so I can't make a bill from it."))
    total = sale["amount_paise"]
    values = [m["qty"] * m["price_paise"] for m in moves]
    items, left = [], total
    for n, (m, value) in enumerate(zip(moves, values)):
        amount = left if n == len(moves) - 1 else round(Decimal(total) * value / sum(values))
        left -= amount
        items.append((m, m["qty"], round(Decimal(amount) / m["qty"])))

    shop = db.fetch_one("select state_code from business where id = %s", (business_id,))
    inv = gst.invoice_tax([(q, rate, it["gst_rate"]) for it, q, rate in items], shop["state_code"],
                          party["state_code"] if party else None)
    paid = int(db.fetch_one("select coalesce(sum(amount_paise), 0) as s from txn where action_id = %s and kind = 'receipt'",
                            (sale["action_id"],))["s"]) if party else total            # a walk-in sale is paid at the counter
    status = "paid" if paid >= inv.total_paise else "partly_paid" if paid else "issued"
    rows, fy = draft(business_id, party["name"] if party else "Walk-in", items, inv, 0, lang)
    rows.insert(2, {"label": pick(lang, "Sale", "Sale"), "value": f"{sale['created_at']:%d %b, %I:%M %p} · {inr(total)}"})
    if len(sales) > 1:
        rows.append({"label": "⚠", "value": pick(lang, f"{when} {who} ki {len(sales)} bina bill wali sales hain; "
                                                       "sabse nayi wali li hai",
                                                 f"{who} has {len(sales)} sales without a bill ({when.lower()}); "
                                                 "this is the latest")})
    ref = {"id": str(party["id"]), "name": party["name"]} if party else None
    title = pick(lang, f"{who} ki sale ka GST bill", f"GST bill for {who}'s sale")
    effects = invoice_effects(title, ref, fy, status, 0, items, inv)
    effects.update(sale_action_id=str(sale["action_id"]), paid_paise=paid)
    return PlanResult(request_id=request_id, ok=True, summary_rows=rows, effects=effects)


def commit(request_id: str, action_id: str, business_id: str, effects: dict, lang: str = DEFAULT) -> Committed:
    """Number and write the invoice. Re-applying the same invoice is a no-op returning its number."""
    action_id = str(UUID(action_id))
    invoice_id = effects["invoice_id"]
    ref = effects["party"]
    with db.transaction() as cur:
        cur.execute("select pg_advisory_xact_lock(hashtext(%s))", ("billing:" + invoice_id,))
        done = cur.execute("select number from invoice where id = %s", (invoice_id,)).fetchone()
        if not done:
            party_id = None if ref is None else ref.get("id") or cur.execute(     # None: a walk-in sale's bill
                "insert into party (business_id, kind, name) values (%s, %s, %s) "
                "on conflict (business_id, name) do update set name = excluded.name returning id",
                (business_id, ref["kind"], ref["name"])).fetchone()["id"]
            n = cur.execute("insert into invoice_counter (business_id, fy, last_no) values (%s, %s, 1) "
                            "on conflict (business_id, fy) do update set last_no = invoice_counter.last_no + 1 "
                            "returning last_no", (business_id, effects["fy"])).fetchone()["last_no"]
            number = gst.invoice_number(effects["fy"], n)
            cur.execute("insert into invoice (id, business_id, action_id, number, party_id, status, taxable_paise, "
                        "cgst_paise, sgst_paise, igst_paise, round_off_paise, total_paise, discount_pct, issued_at) "
                        "values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())",
                        (invoice_id, business_id, action_id, number, party_id, effects["status"],
                         effects["taxable_paise"], effects["cgst_paise"], effects["sgst_paise"], effects["igst_paise"],
                         effects["round_off_paise"], effects["total_paise"], effects["discount_pct"]))
            for l in effects["lines"]:
                cur.execute("insert into invoice_line (invoice_id, item_id, qty, rate_paise, gst_rate, amount_paise, "
                            "tax_paise) values (%s, %s, %s, %s, %s, %s, %s)",
                            (invoice_id, l["item_id"], l["qty"], l["rate_paise"], l["gst_rate"], l["amount_paise"],
                             l["tax_paise"]))
        else:
            number = done["number"]
    return Committed(request_id=request_id, ok=True, message=pick(lang, f"Bill {number} ban gaya.", f"Bill {number} issued."),
                     follow_ups=[{"type": "invoice", "invoice_id": invoice_id, "number": number}])


# Bills already made: showing them, and their PDFs

def status_label(status: str, lang: str) -> str:
    return (STATUS_LABEL_EN if lang == "en" else STATUS_LABEL).get(status, status)


def _party(business_id: str, fields: dict, lang: str) -> tuple[dict | None, str | None]:
    """The customer a request names -> (row, problem). Walk-in, or no name, is (None, None)."""
    if not fields.get("party") or fields.get("walk_in"):
        return None, None
    res = lookup.find_party(business_id, fields["party"], lang=lang)
    if res.question or not res.row:
        return None, res.question or pick(lang, f"'{fields['party']}' naam ka koi khata nahi mila.",
                                          f"I couldn't find '{fields['party']}'.")
    return res.row, None


def _bills(business_id: str, fields: dict, party: dict | None, lang: str) -> tuple[list[dict], str]:
    """Bills for the customer (or walk-in), period and unpaid filter asked for, newest first, and a title."""
    sql = (f"select i.*, p.name as party, {OPEN_PAISE} as open_paise from invoice i left join party p on p.id = i.party_id "
           "where i.business_id = %s and i.status <> 'draft'")
    params: list = [business_id]
    label = pick(lang, "Sab bills", "All bills")
    if party:
        sql, params = sql + " and i.party_id = %s", params + [party["id"]]
        label = pick(lang, f"{party['name']} ke bills", f"Bills for {party['name']}")
    elif fields.get("walk_in"):
        sql += " and i.party_id is null"
        label = pick(lang, "Walk-in bills", "Walk-in bills")
    if fields.get("period"):
        start, end, when = clock.period_range(fields["period"], lang=lang)
        sql += " and (i.issued_at at time zone 'Asia/Kolkata')::date between %s and %s"
        params += [start, end]
        label += f", {when.lower()}"
    if fields.get("unpaid"):
        sql += " and i.status in ('issued', 'partly_paid')"
        label += pick(lang, " (baaki wale)", " (unpaid)")
    return db.fetch_all(sql + " order by i.issued_at desc, i.number desc", params), label


def pdf_line(business_id: str, inv: dict, lang: str) -> str:
    """The PDF link line for one bill. The PDF is drawn and uploaded the first time, then the link is reused;
    an old public URL (which free Cloudinary accounts block) is replaced by a signed one."""
    url = inv["pdf_url"] if config.CLOUDINARY_URL else None
    if not (url and "/download?" in url):
        lines = db.fetch_all("select l.*, it.name, it.unit, it.hsn from invoice_line l join item it on it.id = l.item_id "
                             "where l.invoice_id = %s order by it.name", (inv["id"],))
        shop = db.fetch_one("select * from business where id = %s", (business_id,))
        party = db.fetch_one("select * from party where id = %s", (inv["party_id"],)) if inv["party_id"] else None
        stored = storage.save_pdf(pdf.render(inv, lines, shop, party), business_id, inv["number"])
        if not stored.url:                   # no Cloudinary: the PDF exists only on this computer
            return pick(lang, f"Bill {inv['number']} ka PDF is computer pe save hua: {stored.path}. ASI:One mein link "
                              "ke liye .env mein CLOUDINARY_URL set kijiye.",
                        f"The PDF of bill {inv['number']} is saved on this computer: {stored.path}. Set CLOUDINARY_URL "
                        "in .env to get a link in ASI:One.")
        url = stored.url
        with db.transaction() as cur:
            cur.execute("update invoice set pdf_url = %s where id = %s", (url, inv["id"]))
    return pick(lang, f"📄 Bill {inv['number']} ka PDF: {url}", f"📄 PDF of bill {inv['number']}: {url}")


def invoice_detail(request_id: str, business_id: str, inv: dict, lang: str) -> QueryResult:
    """One bill in full, with its PDF link above the card."""
    lines = db.fetch_all("select l.*, it.name, it.unit from invoice_line l join item it on it.id = l.item_id "
                         "where l.invoice_id = %s", (inv["id"],))
    open_paise = db.fetch_one(f"select {OPEN_PAISE} as open from invoice i where i.id = %s", (inv["id"],))["open"]
    rows = [{"label": "Bill to", "value": inv["party"] or "Walk-in"},
            {"label": "Date", "value": f"{inv['issued_at']:%d %b %Y}" if inv["issued_at"] else "—"}]
    rows += [{"label": f"{qty(l['qty'], l['unit'])} {l['name']} @ {inr(l['rate_paise'])}",
              "value": f"{inr(l['amount_paise'] + l['tax_paise'])} (GST {qty(l['gst_rate'])}%)"} for l in lines]
    if inv["discount_pct"]:
        rows.append({"label": "Discount", "value": f"{qty(inv['discount_pct'])}%"})
    rows.append({"label": "Taxable value", "value": inr(inv["taxable_paise"])})
    if inv["igst_paise"]:
        rows.append({"label": "IGST", "value": inr(inv["igst_paise"])})
    else:
        rows += [{"label": "CGST", "value": inr(inv["cgst_paise"])}, {"label": "SGST", "value": inr(inv["sgst_paise"])}]
    rows += [{"label": "Bill total", "value": inr(inv["total_paise"])},
             {"label": "Status", "value": status_label(inv["status"], lang)
              + (pick(lang, f" ({inr(open_paise)} baaki)", f" ({inr(open_paise)} due)")
                 if inv["status"] in ("issued", "partly_paid") else "")}]
    card = cards.detail(f"Invoice {inv['number']}", rows, [])
    card.terminal = True
    try:
        link = pdf_line(business_id, inv, lang)
    except Exception:                        # the bill still shows; the owner can ask for the PDF again
        link = ""
    return QueryResult(request_id=request_id, ok=True, card_kind=card.kind, card_payload=card.payload, terminal=True,
                       text="\n\n".join(filter(None, [link, card.fallback])), narration=link)


def invoices(request_id: str, business_id: str, fields: dict) -> QueryResult:
    """Bills already made: one in full (by number, the latest, or the only match), or a list. missing=True when
    nothing matches, so the orchestrator can offer to make a bill from the sale instead."""
    lang = of(fields)
    party, problem = _party(business_id, fields, lang)
    if problem:
        return QueryResult(request_id=request_id, ok=True, text=problem)
    if fields.get("invoice") or (fields.get("top") == 1 and not fields.get("period")):
        res = lookup.find_invoice(business_id, fields.get("invoice"), party and party["id"], lang)
        if res.question:
            return QueryResult(request_id=request_id, ok=True, text=res.question, missing=not fields.get("invoice"))
        return invoice_detail(request_id, business_id, res.row, lang)

    rows, label = _bills(business_id, fields, party, lang)
    if not rows:
        return QueryResult(request_id=request_id, ok=True, missing=True,
                           text=pick(lang, f"{label}: koi bill nahi mila.", f"{label}: no bills found."))
    if len(rows) == 1 and (party or fields.get("walk_in") or fields.get("period")):
        return invoice_detail(request_id, business_id, rows[0], lang)
    live = [r for r in rows if r["status"] != "cancelled"]
    total, due = inr(sum(r["total_paise"] for r in live)), inr(sum(max(r["open_paise"], 0) for r in live))
    summary = pick(lang, f"{len(live)} bill, total {total}, baaki {due}.",
                   f"{len(live)} {'bill' if len(live) == 1 else 'bills'}, total {total}, {due} unpaid.")
    shown = rows[:fields.get("top") or LIST_ROWS]
    if len(shown) < len(rows):
        summary += pick(lang, f" Naye {len(shown)} dikha raha hoon.", f" Showing the latest {len(shown)}.")
    table = cards.table(label, ["Bill", "Date", "Customer", "Total", "Status"],
                        [[r["number"], f"{r['issued_at']:%d %b}" if r["issued_at"] else "—", r["party"] or "Walk-in",
                          inr(r["total_paise"]), status_label(r["status"], lang)] for r in shown],
                        subtitle=summary, detail=lambda r: f"{r[2]} · {r[3]} · {r[4]} · {r[1]}",
                        footer=[cards.text(pick(lang, 'PDF ke liye likhiye, jaise "bill 1 ka PDF bhejo".',
                                                'For a PDF, say "send the PDF of bill 1".'), "muted")])
    return QueryResult(request_id=request_id, ok=True, card_kind=table.kind, card_payload=table.payload,
                       terminal=True, text=table.fallback)


def invoice_pdf(request_id: str, business_id: str, fields: dict) -> QueryResult:
    """PDF links: one bill (by id, number, the latest, or the customer's latest), or every bill of a period.
    missing=True when nothing matches."""
    lang = of(fields)
    if fields.get("invoice_id"):
        inv = db.fetch_one("select i.*, p.name as party from invoice i left join party p on p.id = i.party_id "
                           "where i.id = %s and i.business_id = %s", (fields["invoice_id"], business_id))
        if not inv:
            return QueryResult(request_id=request_id, ok=True, text=pick(lang, "Ye bill nahi mila.", "I couldn't find that bill."))
        invs = [inv]
    else:
        party, problem = _party(business_id, fields, lang)
        if problem:
            return QueryResult(request_id=request_id, ok=True, text=problem)
        if fields.get("invoice") or not fields.get("period"):
            res = lookup.find_invoice(business_id, fields.get("invoice"), party and party["id"], lang)
            if res.question:
                return QueryResult(request_id=request_id, ok=True, text=res.question, missing=not fields.get("invoice"))
            invs = [res.row]
        else:
            invs, label = _bills(business_id, fields, party, lang)
            if not invs:
                return QueryResult(request_id=request_id, ok=True, missing=True,
                                   text=pick(lang, f"{label}: koi bill nahi mila.", f"{label}: no bills found."))
            invs = invs[:LIST_ROWS]
    try:
        lines = [pdf_line(business_id, inv, lang) for inv in invs]
    except Exception as err:
        return QueryResult(request_id=request_id, ok=False, text=f"PDF: {type(err).__name__}: {err}")
    return QueryResult(request_id=request_id, ok=True, text="\n".join(lines))


def query(request_id: str, business_id: str, intent: str, fields: dict) -> QueryResult:
    if intent == "invoice_pdf":
        return invoice_pdf(request_id, business_id, fields)
    if intent != "invoices":
        return QueryResult(request_id=request_id, ok=False, text=f"billing cannot answer {intent!r}")
    return invoices(request_id, business_id, fields)


def undo(action_id: str, lang: str = DEFAULT) -> UndoDone:
    """Cancel the action's invoice. The number stays used, as GST rules expect for a cancelled bill."""
    # pdf_url is cleared so the next PDF of this bill is drawn again, marked CANCELLED
    rows = db.fetch_all("update invoice set status = 'cancelled', pdf_url = null where action_id = %s returning number",
                        (str(UUID(action_id)),))
    return UndoDone(action_id=action_id, ok=True,
                    message=" ".join(pick(lang, f"Bill {r['number']} cancel ho gaya.", f"Bill {r['number']} cancelled.")
                                     for r in rows))
