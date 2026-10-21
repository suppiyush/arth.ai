"""Billing: GST invoices. Plan prices the lines and splits the tax (nothing is written, so the
orchestrator can re-plan the draft on edits like "5% discount"); Commit assigns the next number for
the financial year and writes invoice + invoice_line in one transaction.

The money side of a bill (sale txn, stock out) is planned and committed by Ledger and Inventory
under the same action_id; the orchestrator wires them together.
"""

from datetime import date
from uuid import UUID, uuid4

from core import db, gst, lookup
from core.llm import Extraction
from core.models import Committed, PlanResult, UndoDone
from core.money import inr, qty, to_paise


def _ask(request_id: str, question: str) -> PlanResult:
    return PlanResult(request_id=request_id, ok=True, question=question)


def next_number_preview(business_id: str, fy: str) -> str:
    row = db.fetch_one("select last_no from invoice_counter where business_id = %s and fy = %s", (business_id, fy))
    return gst.invoice_number(fy, (row["last_no"] if row else 0) + 1)


def plan(request_id: str, business_id: str, intent: str, fields: dict) -> PlanResult:
    if intent != "bill":
        return PlanResult(request_id=request_id, ok=False, error=f"billing cannot plan {intent!r}")
    ext = Extraction.model_validate(fields)
    if not ext.party:
        return _ask(request_id, "Bill kiske naam pe banana hai? (Who is the bill for?)")
    res = lookup.find_party(business_id, ext.party, "customer")
    if res.question:
        return _ask(request_id, res.question)
    party_row = res.row
    ref = ({"id": str(party_row["id"]), "name": party_row["name"]} if party_row
           else {"new": True, "name": ext.party.strip().title(), "kind": "customer"})

    if not ext.lines:
        return _ask(request_id, "Bill mein kya saamaan likhun? (Which items go on the bill?)")
    items = []
    for line in ext.lines:
        r = lookup.find_item(business_id, line.item)
        if r.question:
            return _ask(request_id, r.question)
        if line.qty is None:
            return _ask(request_id, f"Kitna {r.row['name']}? (How much {r.row['name']}?)")
        rate = to_paise(line.rate) if line.rate is not None else r.row["price_paise"]
        items.append((r.row, line.qty, rate))

    shop = db.fetch_one("select state_code from business where id = %s", (business_id,))
    party_state = party_row["state_code"] if party_row else None
    inv = gst.invoice_tax([(q, rate, it["gst_rate"]) for it, q, rate in items], shop["state_code"], party_state,
                          ext.discount_pct or 0)
    paid = ext.paid_paise(inv.total_paise) or 0
    status = "paid" if paid >= inv.total_paise else "partly_paid" if paid else "issued"
    fy = gst.financial_year(date.today())

    rows = [{"label": "Invoice no", "value": f"{next_number_preview(business_id, fy)} (on confirm)"},
            {"label": "Bill to", "value": ref["name"] + (" (new)" if ref.get("new") else "")}]
    for (it, q, rate), lt in zip(items, inv.lines):
        rows.append({"label": f"{qty(q, it['unit'])} {it['name']} @ {inr(rate)}",
                     "value": f"{inr(lt.total_paise)} (GST {qty(it['gst_rate'])}%)"})
    if ext.discount_pct:
        rows.append({"label": "Discount", "value": f"{ext.discount_pct:g}%"})
    rows.append({"label": "Taxable value", "value": inr(inv.taxable_paise)})
    if inv.inter_state:
        rows.append({"label": "IGST", "value": inr(inv.igst_paise)})
    else:
        rows += [{"label": "CGST", "value": inr(inv.cgst_paise)}, {"label": "SGST", "value": inr(inv.sgst_paise)}]
    if inv.round_off_paise:
        rows.append({"label": "Round off", "value": inr(inv.round_off_paise)})
    rows.append({"label": "Bill total", "value": inr(inv.total_paise)})

    effects = {
        "title": f"GST bill for {ref['name']}", "invoice_id": str(uuid4()), "party": ref, "fy": fy,
        "status": status, "discount_pct": ext.discount_pct or 0,
        "taxable_paise": inv.taxable_paise, "cgst_paise": inv.cgst_paise, "sgst_paise": inv.sgst_paise,
        "igst_paise": inv.igst_paise, "round_off_paise": inv.round_off_paise, "total_paise": inv.total_paise,
        "lines": [{"item_id": str(it["id"]), "item": it["name"], "unit": it["unit"], "qty": float(q),
                   "rate_paise": rate, "gst_rate": float(it["gst_rate"]),
                   "amount_paise": lt.taxable_paise, "tax_paise": lt.tax_paise}
                  for (it, q, rate), lt in zip(items, inv.lines)],
    }
    return PlanResult(request_id=request_id, ok=True, summary_rows=rows, effects=effects)


def commit(request_id: str, action_id: str, business_id: str, effects: dict) -> Committed:
    """Number and write the invoice. Re-applying the same invoice is a no-op returning its number."""
    action_id = str(UUID(action_id))
    invoice_id = effects["invoice_id"]
    ref = effects["party"]
    with db.transaction() as cur:
        cur.execute("select pg_advisory_xact_lock(hashtext(%s))", ("billing:" + invoice_id,))
        done = cur.execute("select number from invoice where id = %s", (invoice_id,)).fetchone()
        if not done:
            party_id = ref.get("id") or cur.execute(
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
    return Committed(request_id=request_id, ok=True, message=f"Bill {number} ban gaya.",
                     follow_ups=[{"type": "invoice", "invoice_id": invoice_id, "number": number}])


def undo(action_id: str) -> UndoDone:
    """Cancel the action's invoice. The number stays used, as GST rules expect for a cancelled bill."""
    rows = db.fetch_all("update invoice set status = 'cancelled' where action_id = %s returning number",
                        (str(UUID(action_id)),))
    return UndoDone(action_id=action_id, ok=True,
                    message=" ".join(f"Bill {r['number']} cancel ho gaya." for r in rows))
