"""Read-only name resolution shared by agents: spoken names -> party and item rows.

Agents read through here; each agent still writes only its own tables.
"""

from dataclasses import dataclass

from core import db
from core.fuzzy import best_match


@dataclass
class Resolved:
    row: dict | None = None
    question: str | None = None       # set when the user has to choose or correct


def find_party(business_id: str, name: str, kind: str | None = None) -> Resolved:
    """Match among parties of `kind` first, then all parties. No match -> row None, no question
    (the caller offers to create the party)."""
    rows = db.fetch_all("select id, name, kind, phone, state_code from party where business_id = %s", (business_id,))
    pools = [[r for r in rows if r["kind"] == kind], rows] if kind else [rows]
    for pool in pools:
        m = best_match(name, [r["name"] for r in pool])
        if m.ambiguous:
            return Resolved(question=f"{' ya '.join(m.candidates)}? Kaun sa? (Which one did you mean?)")
        if m.name:
            return Resolved(next(r for r in pool if r["name"] == m.name))
    return Resolved()


def find_item(business_id: str, name: str) -> Resolved:
    rows = db.fetch_all("select id, name, unit, hsn, gst_rate, cost_paise, price_paise, stock_qty, reorder_level "
                        "from item where business_id = %s", (business_id,))
    m = best_match(name, [r["name"] for r in rows])
    if m.ambiguous:
        return Resolved(question=f"{' ya '.join(m.candidates)}? Kaun sa? (Which one did you mean?)")
    if m.name:
        return Resolved(next(r for r in rows if r["name"] == m.name))
    return Resolved(question=f"'{name}' mere stock list mein nahi hai. (I don't have '{name}' in your item list.)")


def party_balance(party_id) -> int:
    row = db.fetch_one("select balance_paise from party_balance where id = %s", (party_id,))
    return int(row["balance_paise"]) if row else 0
