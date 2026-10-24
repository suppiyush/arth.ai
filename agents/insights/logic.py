"""Insights: answers business questions ("Is mahine kitna profit hua?") from fixed SQL figures.

SQL computes every number: sales, purchases, expenses, gross profit (sales minus cost_paise of the
goods sold), top items, receivables and payables, low stock. ASI1 only words them (llm.explain);
without ASI1 a plain template is used. Up to two suggestions become buttons: remind the biggest
debtor, restock the lowest item.

Beyond the summary, ASI1 picks one of a fixed set of reports for the question (Extraction.report):
top_items, top_customers, expenses, compare, gst, purchases. Each is one function below that returns
a table and the figures it shows, already formatted, so the model has nothing to calculate. Any other
question ("custom") goes to ask.py, where ASI1 writes a checked, read-only SQL query.
"""

import asyncio
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from agents.insights import ask
from core import cards, clock, config, db, llm, lookup
from core.lang import of, pick, plural
from core.clock import PERIOD_LABEL, period_range  # noqa: F401  (period_range is part of this module's API)
from core.models import QueryResult
from core.money import inr, qty

LIVE = "not exists (select 1 from action a where a.id = t.action_id and a.status = 'undone')"
# Rankings need more than a few weeks of history to mean anything, so they default to the financial year.
DEFAULT_PERIOD = {"top_items": "this_year", "top_customers": "this_year", "purchases": "this_year"}


def figures(business_id: str, start: date, end: date) -> dict:
    totals = db.fetch_one(f"""select
        coalesce(sum(amount_paise) filter (where kind = 'sale'), 0) as sales,
        coalesce(sum(amount_paise) filter (where kind = 'purchase'), 0) as purchases,
        coalesce(sum(amount_paise) filter (where kind = 'expense'), 0) as expenses
        from txn t where business_id = %s and txn_date between %s and %s and {LIVE}""", (business_id, start, end))
    sold = sold_items(business_id, start, end)
    balances = db.fetch_all("select id, name, kind, balance_paise from party_balance where business_id = %s "
                            "and balance_paise <> 0 order by balance_paise desc", (business_id,))
    low = db.fetch_all("select name, unit, stock_qty, reorder_level from item where business_id = %s "
                       "and stock_qty < reorder_level order by stock_qty / nullif(reorder_level, 0)", (business_id,))
    cogs = int(sum(r["cost"] for r in sold))
    owed = [b for b in balances if b["kind"] == "customer" and b["balance_paise"] > 0]
    return {
        "sales": int(totals["sales"]), "purchases": int(totals["purchases"]), "expenses": int(totals["expenses"]),
        "cost_of_goods_sold": cogs, "gross_profit": int(totals["sales"]) - cogs,
        "net_profit": int(totals["sales"]) - cogs - int(totals["expenses"]),
        "top_items": [{"name": r["name"], "qty": qty(r["qty"], r["unit"])} for r in sold[:3]],
        "receivable": int(sum(b["balance_paise"] for b in owed)),          # the view sums to numeric
        "payable": -int(sum(b["balance_paise"] for b in balances if b["kind"] == "supplier" and b["balance_paise"] < 0)),
        "top_debtor": owed[0] if owed else None,
        "low_stock": low,
    }


def sold_items(business_id: str, start: date, end: date) -> list[dict]:
    """Items sold in the period: quantity, value at list price and cost, largest value first."""
    return db.fetch_all(f"""select i.name, i.unit, sum(-m.qty) as qty, sum(-m.qty * i.cost_paise) as cost,
        sum(-m.qty * i.price_paise) as value
        from stock_move m join item i on i.id = m.item_id
        where m.qty < 0 and i.business_id = %s and m.action_id in (
            select action_id from txn t where kind = 'sale' and business_id = %s and txn_date between %s and %s and {LIVE})
        group by i.name, i.unit order by value desc""", (business_id, business_id, start, end))


def plain_summary(label: str, f: dict, lang: str) -> str:
    lines = [pick(lang, f"{label} bikri {inr(f['sales'])}, kharcha {inr(f['expenses'])}, gross profit {inr(f['gross_profit'])} "
                        f"(net {inr(f['net_profit'])}).",
                  f"{label}: sales {inr(f['sales'])}, expenses {inr(f['expenses'])}, gross profit {inr(f['gross_profit'])} "
                  f"(net {inr(f['net_profit'])}).")]
    if f["top_items"]:
        best = ", ".join(f"{t['name']} ({t['qty']})" for t in f["top_items"])
        lines.append(pick(lang, f"Sabse zyada bika: {best}.", f"Best sellers: {best}."))
    lines.append(pick(lang, f"Logon se lena hai {inr(f['receivable'])}, suppliers ko dena hai {inr(f['payable'])}.",
                      f"Customers owe you {inr(f['receivable'])}; you owe suppliers {inr(f['payable'])}."))
    return "\n".join(lines)


async def summary(request_id: str, business_id: str, fields: dict) -> QueryResult:
    lang = of(fields)
    start, end, label = period_range(fields.get("period"), lang=lang)
    f = await asyncio.to_thread(figures, business_id, start, end)
    words = {k: (inr(v) if isinstance(v, int) else v) for k, v in f.items() if k not in ("top_debtor", "low_stock")}
    words["low_stock"] = [r["name"] for r in f["low_stock"]]
    words["period"] = label
    try:
        answer = await llm.explain(
            f"The owner asked: {fields.get('text') or 'how is business?'}\nAnswer it from these figures in two or "
            "three short sentences, then one short tip if one is useful.", words, lang=lang)
    except Exception:
        answer = plain_summary(label, f, lang)

    rows = [{"label": pick(lang, "Bikri", "Sales"), "value": inr(f["sales"])},
            {"label": "Gross profit", "value": inr(f["gross_profit"])},
            {"label": pick(lang, "Kharcha", "Expenses"), "value": inr(f["expenses"])},
            {"label": "Net profit", "value": inr(f["net_profit"])},
            {"label": pick(lang, "Lena hai / dena hai", "To receive / to pay"),
             "value": f"{inr(f['receivable'])} / {inr(f['payable'])}"}]
    ctas = []
    if d := f["top_debtor"]:
        ctas.append(cards.cta(f"Remind {d['name']} ({inr(d['balance_paise'])})", "remind",
                              party_id=str(d["id"]), party=d["name"]))
    if f["low_stock"]:
        item = f["low_stock"][0]["name"]
        ctas.append(cards.cta(f"Restock {item}", "example", text=f"{item} mangwa do"))
    title = pick(lang, f"{label} ka hisaab", f"{label}'s summary") + f" ({start:%d %b} – {end:%d %b})"
    card = cards.detail(title, rows, ctas)
    return QueryResult(request_id=request_id, ok=True, card_kind=card.kind, card_payload=card.payload,
                       terminal=not ctas, text=f"{answer}\n\n{card.fallback}", narration=answer)


# Reports

@dataclass
class Report:
    title: str
    headers: list[str]
    rows: list[list[str]]
    plain: str                                    # the answer when ASI1 is down, and the card's subtitle
    facts: dict = field(default_factory=dict)     # formatted figures ASI1 may word; never raw numbers to add up
    buttons: list[dict] = field(default_factory=list)


def _span(start: date, end: date) -> str:
    return f"{start:%d %b}" if start == end else f"{start:%d %b} – {end:%d %b}"


def _change(now: int, before: int) -> str:
    if not before:
        return "—"
    pct = (now - before) * 100 / abs(before)
    return f"{pct:+.0f}%"


def top_items(business_id: str, start: date, end: date, label: str, fields: dict) -> Report:
    lang = of(fields)
    sold = sold_items(business_id, start, end)
    names = db.fetch_all("select name from item where business_id = %s order by name", (business_id,))
    unsold = [r["name"] for r in names if r["name"] not in {s["name"] for s in sold}]
    rows = [[s["name"], qty(s["qty"], s["unit"]), inr(int(s["value"])), inr(int(s["value"] - s["cost"]))] for s in sold]
    if not sold:
        plain = pick(lang, f"{label} kuch nahi bika.", f"Nothing was sold ({label.lower()}).")
    else:
        top, by_margin = sold[0], max(sold, key=lambda s: s["value"] - s["cost"])
        top_text = f"{top['name']} ({qty(top['qty'], top['unit'])}, {inr(int(top['value']))})"
        margin_text = f"{by_margin['name']} ({inr(int(by_margin['value'] - by_margin['cost']))} margin)"
        plain = pick(lang, f"{label} sabse zyada bika: {top_text}. Sabse zyada kamai: {margin_text}.",
                     f"{label}, your best seller was {top_text}. The most profit came from {margin_text}.")
    if unsold:
        plain += pick(lang, f" Nahi bike: {', '.join(unsold)}.", f" Not sold: {', '.join(unsold)}.")
    return Report(pick(lang, f"Sabse zyada bikne wala maal: {label.lower()}", f"Best sellers: {label.lower()}"),
                  ["Item", pick(lang, "Bika", "Sold"), "Value", "Margin"], rows, plain,
                  {"best_by_value": rows[:3], "not_sold": unsold})


def top_customers(business_id: str, start: date, end: date, label: str, fields: dict) -> Report:
    lang = of(fields)
    sales = db.fetch_all(f"""select p.id, coalesce(p.name, 'Walk-in') as name, sum(t.amount_paise) as sales,
        count(distinct t.action_id) as entries
        from txn t left join party p on p.id = t.party_id
        where t.business_id = %s and t.kind = 'sale' and t.txn_date between %s and %s and {LIVE}
        group by p.id, p.name order by sales desc""", (business_id, start, end))
    if not sales:
        return Report("Customers", [], [], pick(lang, f"{label} koi bikri nahi hui.", f"No sales ({label.lower()})."))
    due = {r["id"]: r["balance_paise"] for r in db.fetch_all(
        "select id, balance_paise from party_balance where business_id = %s", (business_id,))}
    rows = [[r["name"], inr(int(r["sales"])), str(r["entries"]),
             inr(due[r["id"]]) if due.get(r["id"], 0) > 0 else "—"] for r in sales]
    named = [r for r in sales if r["id"]]
    if named:
        best = named[0]
        plain = pick(lang, f"{label} sabse bade customer {best['name']} hain: {inr(int(best['sales']))} ki kharid.",
                     f"{label}, your biggest customer is {best['name']}, who bought {inr(int(best['sales']))}.")
        if due.get(best["id"], 0) > 0:
            plain += pick(lang, f" Unka {inr(due[best['id']])} abhi baaki hai.", f" They still owe {inr(due[best['id']])}.")
    else:
        plain = pick(lang, f"{label} saari bikri walk-in customers ko hui.", f"{label}, all sales were to walk-in customers.")
    buttons = [cards.button(f"Remind {named[0]['name']}", "remind", party_id=str(named[0]["id"]), party=named[0]["name"])] \
        if named and due.get(named[0]["id"], 0) > 0 else []
    return Report(f"Top customers: {label.lower()}",
                  ["Customer", pick(lang, "Kharid", "Bought"), "Entries", pick(lang, "Baaki", "Owes")], rows, plain,
                  {"customers": rows[:5]}, buttons)


def expenses(business_id: str, start: date, end: date, label: str, fields: dict) -> Report:
    lang = of(fields)
    title = pick(lang, f"Kharcha: {label.lower()}", f"Expenses: {label.lower()}")
    rows = db.fetch_all(f"""select coalesce(nullif(t.category, ''), 'other') as category, sum(t.amount_paise) as total,
        count(*) as entries from txn t
        where t.business_id = %s and t.kind = 'expense' and t.txn_date between %s and %s and {LIVE}
        group by 1 order by total desc""", (business_id, start, end))
    if not rows:
        return Report(title, [], [], pick(lang, f"{label} koi kharcha nahi likha.", f"No expenses recorded ({label.lower()})."))
    total = sum(int(r["total"]) for r in rows)
    table = [[r["category"].title(), inr(int(r["total"])), str(r["entries"])] for r in rows]
    biggest = f"{rows[0]['category'].title()} ({inr(int(rows[0]['total']))})"
    plain = pick(lang, f"{label} kul kharcha {inr(total)}. Sabse bada: {biggest}.",
                 f"{label} you spent {inr(total)} in all. The biggest item was {biggest}.")
    return Report(title, [pick(lang, "Kis cheez pe", "On"), "Amount", "Entries"], table, plain,
                  {"total": inr(total), "by_category": table})


def compare(business_id: str, start: date, end: date, label: str, fields: dict) -> Report:
    lang = of(fields)
    p_start, p_end = clock.previous_range(start, end)
    now, before = figures(business_id, start, end), figures(business_id, p_start, p_end)
    metrics = [(pick(lang, "Bikri", "Sales"), "sales"), ("Gross profit", "gross_profit"),
               (pick(lang, "Kharcha", "Expenses"), "expenses"), ("Net profit", "net_profit"),
               (pick(lang, "Kharid", "Purchases"), "purchases")]
    rows = [[name, inr(now[k]), inr(before[k]), _change(now[k], before[k])] for name, k in metrics]
    change = _change(now["sales"], before["sales"])
    if lang == "en":
        trend = "went up" if now["sales"] > before["sales"] else "went down" if now["sales"] < before["sales"] else "stayed the same"
        plain = (f"Sales {trend}: {inr(now['sales'])} for {_span(start, end)} against {inr(before['sales'])} for "
                 f"{_span(p_start, p_end)} ({change}). Net profit {inr(now['net_profit'])} against {inr(before['net_profit'])}.")
    else:
        trend = "badhi" if now["sales"] > before["sales"] else "ghati" if now["sales"] < before["sales"] else "utni hi rahi"
        plain = (f"Bikri {trend}: {_span(start, end)} mein {inr(now['sales'])}, pichhle {_span(p_start, p_end)} mein "
                 f"{inr(before['sales'])} ({change}). Net profit {inr(now['net_profit'])}, pehle {inr(before['net_profit'])}.")
    return Report(pick(lang, f"{label} vs pehle", f"{label} vs before"),
                  ["", _span(start, end), _span(p_start, p_end), pick(lang, "Badlav", "Change")], rows, plain, {"rows": rows})


def _implied_gst(business_id: str, start: date, end: date) -> tuple[int, int, int]:
    """GST inside sales made without a bill: each sale's amount split over its items' GST rates by list
    value. -> (sales without a bill, estimated GST in them, how many could not be split)."""
    sales = db.fetch_all(f"""select t.action_id, t.amount_paise from txn t where t.business_id = %s and t.kind = 'sale'
        and t.invoice_id is null and t.txn_date between %s and %s and {LIVE}""", (business_id, start, end))
    moves = db.fetch_all("""select m.action_id, -m.qty * i.price_paise as value, i.gst_rate from stock_move m
        join item i on i.id = m.item_id where m.qty < 0 and m.action_id = any(%s)""",
                         ([s["action_id"] for s in sales],))
    by_action: dict = {}
    for m in moves:
        by_action.setdefault(m["action_id"], []).append(m)
    total = tax = unsplit = 0
    for s in sales:
        total += s["amount_paise"]
        ms = by_action.get(s["action_id"])
        value = sum(m["value"] for m in ms or [])
        if not value:
            unsplit += 1
            continue
        for m in ms:
            share, rate = Decimal(s["amount_paise"]) * m["value"] / value, m["gst_rate"]
            tax += round(share * rate / (100 + rate) if config.PRICES_INCLUDE_GST else share * rate / 100)
    return total, tax, unsplit


def gst(business_id: str, start: date, end: date, label: str, fields: dict) -> Report:
    lang = of(fields)
    inv = db.fetch_one("""select count(*) as n, coalesce(sum(taxable_paise), 0) as taxable,
        coalesce(sum(cgst_paise), 0) as cgst, coalesce(sum(sgst_paise), 0) as sgst, coalesce(sum(igst_paise), 0) as igst
        from invoice where business_id = %s and status not in ('cancelled', 'draft')
        and (issued_at at time zone 'Asia/Kolkata')::date between %s and %s""", (business_id, start, end))
    billed_tax = int(inv["cgst"] + inv["sgst"] + inv["igst"])
    unbilled, unbilled_tax, unsplit = _implied_gst(business_id, start, end)
    rows = [[plural(inv["n"], "GST bill"), inr(int(inv["taxable"])), inr(billed_tax)],
            [pick(lang, "Bina bill ki bikri (andaza)", "Sales without a bill (estimate)"), inr(unbilled), inr(unbilled_tax)],
            ["Total output GST", "", inr(billed_tax + unbilled_tax)]]
    split = f"CGST {inr(int(inv['cgst']))}, SGST {inr(int(inv['sgst']))}, IGST {inr(int(inv['igst']))}"
    plain = pick(lang,
                 f"{label} bikri pe GST lagbhag {inr(billed_tax + unbilled_tax)} hai: bills pe {inr(billed_tax)} ({split}) "
                 f"aur bina bill ki bikri pe andazan {inr(unbilled_tax)}. Kharid pe diya GST (input credit) Arth mein "
                 "record nahi hota, isliye asli bharna isse kam hoga. Return se pehle CA se milaa lijiye.",
                 f"{label}, GST on your sales is about {inr(billed_tax + unbilled_tax)}: {inr(billed_tax)} on bills "
                 f"({split}) and an estimated {inr(unbilled_tax)} on sales without a bill. GST you paid on purchases "
                 "(input credit) isn't tracked in Arth, so what you actually pay will be lower. Check with your CA before filing.")
    if unsplit:
        plain += pick(lang, f" {unsplit} entries mein item nahi tha, unka GST nahi joda.",
                      f" {plural(unsplit, 'entry', 'entries')} had no item, so their GST isn't included.")
    return Report(f"GST: {label.lower()} ({_span(start, end)})", ["", pick(lang, "Bikri", "Sales"), "GST"], rows, plain,
                  {"rows": rows, "note": "output GST only; input tax credit is not tracked"})


def purchases(business_id: str, start: date, end: date, label: str, fields: dict) -> Report:
    lang = of(fields)
    names = [l["item"] for l in fields.get("lines") or [] if l.get("item")]
    item = None
    if names:
        res = lookup.find_item(business_id, names[0], lang)
        if res.question:
            return Report(pick(lang, "Kharid", "Purchases"), [], [], res.question)
        item = res.row
    if not item:
        title = pick(lang, f"Kharid: {label.lower()}", f"Purchases: {label.lower()}")
        rows = db.fetch_all(f"""select p.name, sum(t.amount_paise) as total, count(distinct t.action_id) as entries,
            max(pb.balance_paise) as balance from txn t join party p on p.id = t.party_id
            join party_balance pb on pb.id = p.id
            where t.business_id = %s and t.kind = 'purchase' and t.txn_date between %s and %s and {LIVE}
            group by p.name order by total desc""", (business_id, start, end))
        if not rows:
            return Report(title, [], [], pick(lang, f"{label} koi kharid nahi likhi.", f"No purchases recorded ({label.lower()})."))
        table = [[r["name"], inr(int(r["total"])), str(r["entries"]),
                  inr(-r["balance"]) if r["balance"] < 0 else "—"] for r in rows]
        total, top = inr(sum(int(r["total"]) for r in rows)), f"{rows[0]['name']} ({inr(int(rows[0]['total']))})"
        plain = pick(lang, f"{label} kul kharid {total}, sabse zyada {top} se.",
                     f"{label} you bought {total} in all, the most from {top}.")
        return Report(title, ["Supplier", pick(lang, "Kharid", "Bought"), "Entries", pick(lang, "Dena hai", "You owe")],
                      table, plain, {"suppliers": table})
    # Rate paid per supplier for one item, from purchases that had only that item on them
    rows = db.fetch_all(f"""select p.name as supplier, t.amount_paise / m.qty as rate, t.txn_date
        from txn t join party p on p.id = t.party_id join stock_move m on m.action_id = t.action_id and m.qty > 0
        where t.business_id = %s and t.kind = 'purchase' and m.item_id = %s and t.txn_date between %s and %s and {LIVE}
        and (select count(*) from stock_move m2 where m2.action_id = t.action_id and m2.qty > 0) = 1
        order by t.txn_date desc""", (business_id, item["id"], start, end))
    restock = [cards.button(pick(lang, f"{item['name']} ke live quote lo", f"Get live quotes for {item['name']}"),
                            "example", text=f"{item['name']} mangwa do")]
    title = pick(lang, f"{item['name']}: kharid rate", f"{item['name']}: purchase rates")
    if not rows:
        return Report(title, [], [], pick(
            lang, f"{label} {item['name']} ki koi kharid record nahi hai. Supplier agents se aaj ke rate lene ke liye restock bolo.",
            f"No purchases of {item['name']} recorded ({label.lower()}). Ask me to restock it to get today's quotes "
            "from supplier agents."), buttons=restock)
    best: dict = {}
    for r in rows:
        if r["supplier"] not in best or r["rate"] < best[r["supplier"]]["rate"]:
            best[r["supplier"]] = r
    ranked = sorted(best.values(), key=lambda r: r["rate"])
    table = [[r["supplier"], f"{inr(int(r['rate']))}/{item['unit']}", f"{r['txn_date']:%d %b}"] for r in ranked]
    cheapest = f"{ranked[0]['supplier']} ({inr(int(ranked[0]['rate']))}/{item['unit']})"
    plain = pick(lang, f"{item['name']} sabse sasta {cheapest} se mila. Aaj ke rate ke liye supplier agents se quote le sakte hain.",
                 f"You got {item['name']} cheapest from {cheapest}. I can get today's quotes from supplier agents.")
    return Report(f"{title} ({label.lower()})", ["Supplier", "Rate", pick(lang, "Kab", "When")], table, plain,
                  {"rates": table}, restock)


REPORTS = {"top_items": top_items, "top_customers": top_customers, "expenses": expenses, "compare": compare,
           "gst": gst, "purchases": purchases}


async def report(request_id: str, business_id: str, name: str, fields: dict) -> QueryResult:
    lang = of(fields)
    start, end, label = period_range(fields.get("period"), DEFAULT_PERIOD.get(name, "this_month"), lang)
    rep = await asyncio.to_thread(REPORTS[name], business_id, start, end, label, fields)
    answer = rep.plain
    if rep.rows:
        try:
            answer = await llm.explain(
                f"The owner asked: {fields.get('text') or name}\nAnswer that question in two or three short sentences, "
                "using only this summary and table.",
                {"summary": rep.plain, "period": f"{label} ({_span(start, end)})", **rep.facts}, lang=lang)
        except Exception:
            pass
    if not rep.rows:
        return QueryResult(request_id=request_id, ok=True, text=answer)
    table = cards.table(rep.title, rep.headers, rep.rows, subtitle=rep.plain, footer=rep.buttons or None)
    return QueryResult(request_id=request_id, ok=True, card_kind=table.kind, card_payload=table.payload,
                       terminal=table.terminal, text=f"{answer}\n\n{table.fallback}", narration=answer)


async def query(request_id: str, business_id: str, intent: str, fields: dict) -> QueryResult:
    if intent != "question":
        return QueryResult(request_id=request_id, ok=False, text=f"insights cannot answer {intent!r}")
    name = fields.get("report") or "summary"
    if name == "custom":
        return await ask.answer(request_id, business_id, fields.get("text") or "", of(fields))
    if name in REPORTS:
        return await report(request_id, business_id, name, fields)
    return await summary(request_id, business_id, fields)
