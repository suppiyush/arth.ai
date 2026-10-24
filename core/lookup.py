"""Read-only name resolution shared by agents: spoken names -> party and item rows.

Agents read through here; each agent still writes only its own tables.
"""

import re
from dataclasses import dataclass

from core import db
from core.fuzzy import best_match
from core.lang import DEFAULT, pick


@dataclass
class Resolved:
    row: dict | None = None
    question: str | None = None       # set when the user has to choose or correct


def which(candidates: list[str], lang: str) -> str:
    return pick(lang, f"{' ya '.join(candidates)}? Kaun sa?", f"Did you mean {' or '.join(candidates)}?")


def find_party(business_id: str, name: str, kind: str | None = None, lang: str = DEFAULT) -> Resolved:
    """Match among parties of `kind` first, then all parties. No match -> row None, no question
    (the caller offers to create the party)."""
    rows = db.fetch_all("select id, name, kind, phone, state_code from party where business_id = %s", (business_id,))
    pools = [[r for r in rows if r["kind"] == kind], rows] if kind else [rows]
    for pool in pools:
        m = best_match(name, [r["name"] for r in pool])
        if m.ambiguous:
            return Resolved(question=which(m.candidates, lang))
        if m.name:
            return Resolved(next(r for r in pool if r["name"] == m.name))
    return Resolved()


def find_item(business_id: str, name: str, lang: str = DEFAULT) -> Resolved:
    rows = db.fetch_all("select id, name, unit, hsn, gst_rate, cost_paise, price_paise, stock_qty, reorder_level "
                        "from item where business_id = %s", (business_id,))
    m = best_match(name, [r["name"] for r in rows], every_word=True)
    if m.ambiguous:
        return Resolved(question=which(m.candidates, lang))
    if m.name:
        return Resolved(next(r for r in rows if r["name"] == m.name))
    missing = pick(lang, f"'{name}' aapki item list mein nahi hai.", f"'{name}' isn't in your item list.")
    if len(m.near) == 1:
        return Resolved(question=f"{missing} " + pick(lang, f"{m.near[0]} chahiye tha?", f"Did you mean {m.near[0]}?"))
    if m.near:
        return Resolved(question=f"{missing} {which(m.near, lang)}")
    return Resolved(question=missing)


def find_invoice(business_id: str, spoken: str | None, party_id=None, lang: str = DEFAULT) -> Resolved:
    """"ARTH/26-27/0007", "0007" or "7" -> that bill (this financial year's first); no number -> the
    party's latest bill, or the shop's latest. Cancelled bills are found too, so callers can say so."""
    sql = ("select i.*, p.name as party from invoice i left join party p on p.id = i.party_id "
           "where i.business_id = %s")
    params: list = [business_id]
    if party_id:
        sql, params = sql + " and i.party_id = %s", params + [party_id]
    rows = db.fetch_all(sql + " order by i.issued_at desc, i.number desc", params)
    if spoken and (digits := re.findall(r"\d+", spoken)):
        exact = [r for r in rows if r["number"].lower() == spoken.strip().lower()]
        rows = exact or [r for r in rows if re.search(rf"/0*{int(digits[-1])}$", r["number"])]
        if not rows:
            return Resolved(question=pick(lang, f"Bill '{spoken}' nahi mila.", f"I couldn't find bill '{spoken}'."))
    if not rows:
        return Resolved(question=pick(lang, "Abhi tak koi bill nahi bana.", "No bills have been made yet."))
    return Resolved(rows[0])


def party_balance(party_id) -> int:
    row = db.fetch_one("select balance_paise from party_balance where id = %s", (party_id,))
    return int(row["balance_paise"]) if row else 0
