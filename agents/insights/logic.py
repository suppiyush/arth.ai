"""Insights: answers business questions ("Is mahine kitna profit hua?") from fixed SQL figures.

SQL computes every number: sales, purchases, expenses, gross profit (sales minus cost_paise of the
goods sold), top items, receivables and payables, low stock. ASI1 only words them (llm.explain);
without ASI1 a plain template is used. Up to two suggestions become buttons: remind the biggest
debtor, restock the lowest item.
"""

import asyncio
from datetime import date, timedelta

from core import cards, db, llm
from core.models import QueryResult
from core.money import inr, qty

LIVE = "not exists (select 1 from action a where a.id = t.action_id and a.status = 'undone')"
PERIOD_LABEL = {"today": "Aaj", "yesterday": "Kal", "this_week": "Is hafte", "last_week": "Pichhle hafte",
                "this_month": "Is mahine", "last_month": "Pichhle mahine"}


def period_range(period: str | None) -> tuple[date, date, str]:
    """-> (from, to inclusive, label). Defaults to this month."""
    today = date.today()
    month_start = today.replace(day=1)
    if period == "today":
        return today, today, PERIOD_LABEL["today"]
    if period == "yesterday":
        d = today - timedelta(days=1)
        return d, d, PERIOD_LABEL["yesterday"]
    if period == "this_week":
        return today - timedelta(days=today.weekday()), today, PERIOD_LABEL["this_week"]
    if period == "last_week":
        end = today - timedelta(days=today.weekday() + 1)
        return end - timedelta(days=6), end, PERIOD_LABEL["last_week"]
    if period == "last_month":
        end = month_start - timedelta(days=1)
        return end.replace(day=1), end, PERIOD_LABEL["last_month"]
    if period and len(period) == 7:
        try:
            start = date.fromisoformat(period + "-01")
            end = (start.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
            return start, end, f"{start:%b %Y}"
        except ValueError:
            pass
    if period and len(period) == 10:
        try:
            d = date.fromisoformat(period)
            return d, d, f"{d:%d %b}"
        except ValueError:
            pass
    return month_start, today, PERIOD_LABEL["this_month"]


def figures(business_id: str, start: date, end: date) -> dict:
    totals = db.fetch_one(f"""select
        coalesce(sum(amount_paise) filter (where kind = 'sale'), 0) as sales,
        coalesce(sum(amount_paise) filter (where kind = 'purchase'), 0) as purchases,
        coalesce(sum(amount_paise) filter (where kind = 'expense'), 0) as expenses
        from txn t where business_id = %s and txn_date between %s and %s and {LIVE}""", (business_id, start, end))
    sold = db.fetch_all(f"""select i.name, i.unit, sum(-m.qty) as qty, sum(-m.qty * i.cost_paise) as cost,
        sum(-m.qty * i.price_paise) as value
        from stock_move m join item i on i.id = m.item_id
        where m.qty < 0 and i.business_id = %s and m.action_id in (
            select action_id from txn t where kind = 'sale' and business_id = %s and txn_date between %s and %s and {LIVE})
        group by i.name, i.unit order by value desc""", (business_id, business_id, start, end))
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
        "receivable": sum(b["balance_paise"] for b in owed),
        "payable": -sum(b["balance_paise"] for b in balances if b["kind"] == "supplier" and b["balance_paise"] < 0),
        "top_debtor": owed[0] if owed else None,
        "low_stock": low,
    }


def plain_summary(label: str, f: dict) -> str:
    lines = [f"{label} sale {inr(f['sales'])}, kharcha {inr(f['expenses'])}, gross profit {inr(f['gross_profit'])} "
             f"(net {inr(f['net_profit'])})."]
    if f["top_items"]:
        lines.append("Sabse zyada bika: " + ", ".join(f"{t['name']} ({t['qty']})" for t in f["top_items"]) + ".")
    lines.append(f"Logon se lena hai {inr(f['receivable'])}, suppliers ko dena hai {inr(f['payable'])}.")
    return "\n".join(lines)


async def query(request_id: str, business_id: str, intent: str, fields: dict) -> QueryResult:
    if intent != "question":
        return QueryResult(request_id=request_id, ok=False, text=f"insights cannot answer {intent!r}")
    start, end, label = period_range(fields.get("period"))
    f = await asyncio.to_thread(figures, business_id, start, end)
    words = {k: (inr(v) if isinstance(v, int) else v) for k, v in f.items() if k not in ("top_debtor", "low_stock")}
    words["low_stock"] = [r["name"] for r in f["low_stock"]]
    words["period"] = label
    try:
        summary = await llm.explain(
            f"The owner asked: {fields.get('text') or 'how is business?'}\nAnswer in at most three short lines "
            "using these figures, then one line of advice if useful.", words)
    except Exception:
        summary = plain_summary(label, f)

    rows = [{"label": "Sale", "value": inr(f["sales"])},
            {"label": "Gross profit", "value": inr(f["gross_profit"])},
            {"label": "Kharcha", "value": inr(f["expenses"])},
            {"label": "Net profit", "value": inr(f["net_profit"])},
            {"label": "Lena hai / dena hai", "value": f"{inr(f['receivable'])} / {inr(f['payable'])}"}]
    ctas = []
    if d := f["top_debtor"]:
        ctas.append(cards.cta(f"Remind {d['name']} ({inr(d['balance_paise'])})", "remind",
                              party_id=str(d["id"]), party=d["name"]))
    if f["low_stock"]:
        item = f["low_stock"][0]["name"]
        ctas.append(cards.cta(f"Restock {item}", "example", text=f"{item} mangwa do"))
    card = cards.detail(f"{label} ka hisaab ({start:%d %b} – {end:%d %b})", rows, ctas)
    return QueryResult(request_id=request_id, ok=True, card_kind=card.kind, card_payload=card.payload,
                       terminal=not ctas, text=f"{summary}\n\n{card.fallback}")
