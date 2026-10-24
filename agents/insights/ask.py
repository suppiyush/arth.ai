"""Questions no fixed report answers ("is mahine ka average bill kitna hai?"), answered with SQL that ASI1 writes.

ASI1 sees only the descriptions of six read-only views (bottom of db/schema.sql) and replies
{"sql": ..., "answer": "... {column} ..."}. Nothing it writes runs unchecked:

1. sqlglot parses the SQL: exactly one SELECT, reading only those views (or its own WITH names),
   calling only known SQL functions or a short list of Postgres ones. The SQL that runs is sqlglot's
   rendering of what was checked, so the check and Postgres never read different statements.
2. It runs as arth_reader, a role that can read those views and nothing else, in a read-only
   transaction with a statement timeout. arth.business_id is set for the transaction, so the views
   show this shop only.
3. The answer's {placeholders} are filled by code from the first result row, so every figure the
   owner reads came out of Postgres, never out of the model.

A rejected or failing query gets one repair round with the error; then Arth says it couldn't answer.
"""

import asyncio
import re
from datetime import date
from decimal import Decimal

import sqlglot
from psycopg.rows import tuple_row
from sqlglot import exp

from core import cards, clock, db, llm
from core.lang import DEFAULT, name as lang_name, pick
from core.models import QueryResult
from core.money import inr, qty, to_paise

VIEWS = {"entries", "parties", "items", "bills", "bill_lines", "stock_moves"}
# Postgres functions sqlglot doesn't model itself (it parses them as "anonymous"). Anything else unknown,
# e.g. set_config, current_setting, pg_sleep, pg_read_file, dblink, is refused.
EXTRA_FUNCTIONS = {"to_char", "date_part", "age", "make_date", "initcap", "split_part", "btrim", "ceil", "floor",
                   "trunc", "mode", "justify_days", "to_date"}
FORBIDDEN = tuple(getattr(exp, name) for name in (
    "Insert", "Update", "Delete", "Merge", "Create", "Drop", "Alter", "Command", "Set", "Into", "Copy", "Use",
    "Transaction", "Commit", "Rollback", "Lock", "Dot", "Placeholder", "Parameter") if hasattr(exp, name))
MAX_ROWS = 20
TIMEOUT_MS = 3000

PROMPT = """You write one PostgreSQL SELECT that answers an Indian shop owner's question from these read-only views.
Every view holds only this shop's data. Money columns are in rupees.

entries(date, kind, amount, mode, category, note, party, party_kind, bill_number)
  The khata. kind: sale | purchase | expense | receipt (money in from a customer) | payment (money out to a
  supplier) | credit_note. One sale row is one sale, which the owner calls a "bill" whether or not it has a GST
  invoice. A sale or purchase with a named party has mode 'credit', and the money that changed hands is a
  separate receipt or payment row with mode cash/upi/bank. A walk-in sale has party null and its real mode.
  category is set on expenses: rent, salary, electricity, transport, ...
parties(name, kind, phone, state_code, balance)
  kind customer | supplier. balance > 0: they owe the shop. balance < 0: the shop owes them.
  A customer with balance < 0 has paid an advance; that is not money the owner "owes" or "has to pay".
  So questions about whom the owner owes or must pay use kind = 'supplier' and balance < 0, and
  questions about who owes the owner use kind = 'customer' and balance > 0.
items(name, unit, hsn, gst_rate, cost, price, stock, reorder_level)
  price is the selling price including GST; cost is what the shop pays.
bills(number, date, party, status, discount_pct, taxable, cgst, sgst, igst, total, paid)
  GST invoices the shop issued to its customers. status issued | partly_paid | paid | cancelled. Leave out
  cancelled unless asked. Purchases from suppliers are never here: they are entries with kind 'purchase'
  (amount, party = the supplier) and stock_moves with reason 'purchase' (item, qty, party = the supplier).
bill_lines(bill_number, date, bill_status, item, unit, qty, rate, gst_rate, taxable, tax)
stock_moves(date, item, unit, qty, reason, party)
  qty < 0 went out, qty > 0 came in. reason sale | purchase | adjust | opening. party is the customer or
  supplier of that sale or purchase (null for walk-in). Use this for quantities of an item a party bought:
  it covers every sale, with or without a GST bill.

Today is {today} ({weekday}). This month started {month_start}; the financial year started {fy_start}.
Weeks start on Monday. Write dates as literals (date '2026-10-01'); do not use current_date or now().
Shop items: {items}
Shop parties: {parties}

Reply with ONE JSON object and nothing else:
{{"sql": "...", "answer": "one short sentence with {{column}} placeholders"}}
or, when these views cannot answer the question: {{"cannot": "one short sentence saying why"}}

Rules:
- One SELECT (WITH is fine) over the views above only. No other tables, schemas or system functions.
- Do all arithmetic in SQL. Round money to 2 decimals and name money columns with the suffix _rs.
- Name every output column with AS. Put the most relevant row first; at most 20 rows.
- For "most", "least", "best" or "highest" questions, return the top few rows rather than LIMIT 1, so a tie
  is visible in the table.
- The answer sentence may use placeholders only for columns of the FIRST row, and writes no numbers itself.
  A _rs placeholder already shows the ₹ sign, so don't add ₹, Rs or "rupaye" around it.
- Write "answer" and "cannot" in {language}. Short, natural and grammatical; no emojis.

Example. "is mahine ka average bill kitna hai"
{{"sql": "select round(avg(amount), 2) as avg_bill_rs, count(*) as bills from entries where kind = 'sale' and date >= date '{month_start}'", "answer": "Is mahine {{bills}} bikri hui, average bill {{avg_bill_rs}} raha."}}
"""


class Rejected(Exception):
    """The SQL is not allowed to run; the message goes back to ASI1 for one repair."""


def check(sql: str) -> str:
    """-> the SQL to run (sqlglot's rendering of the checked statement), or Rejected."""
    try:
        statements = [s for s in sqlglot.parse(sql, read="postgres") if s is not None]
    except sqlglot.errors.ParseError as err:
        raise Rejected(f"could not parse the SQL: {str(err)[:200]}")
    if len(statements) != 1:
        raise Rejected("send exactly one statement")
    tree = statements[0]
    if not isinstance(tree, exp.Query):
        raise Rejected("only a SELECT query is allowed")
    if bad := next(tree.find_all(*FORBIDDEN), None):
        raise Rejected(f"not allowed in a read-only answer: {type(bad).__name__}")
    ctes = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    for table in tree.find_all(exp.Table):
        if isinstance(table.this, exp.Func):              # FROM generate_series(...): a function, checked below
            continue
        if table.db or table.catalog:
            raise Rejected(f"use the views directly, without a schema: {table.sql()}")
        if table.name.lower() not in VIEWS | ctes:
            raise Rejected(f"unknown table {table.name!r}; use only {', '.join(sorted(VIEWS))}")
    for func in tree.find_all(exp.Func):
        if isinstance(func, exp.Anonymous) and func.name.lower() not in EXTRA_FUNCTIONS:
            raise Rejected(f"function {func.name}() is not allowed")
    return tree.sql(dialect="postgres")


def run(business_id: str, sql: str) -> tuple[list[str], list[tuple]]:
    """Run checked SQL as arth_reader, read-only, for this shop only."""
    with db.pool().connection() as conn:
        with conn.transaction():
            conn.execute("set transaction read only")
            conn.execute(f"set local statement_timeout = {TIMEOUT_MS}")
            conn.execute("select set_config('arth.business_id', %s, true)", (business_id,))
            conn.execute("set local role arth_reader")
            with conn.cursor(row_factory=tuple_row) as cur:
                cur.execute(sql)
                columns = [d.name for d in cur.description or []]
                rows = cur.fetchmany(MAX_ROWS)
    return columns, rows


def show(column: str, value) -> str:
    if value is None:
        return "—"
    if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        if column.endswith("_rs"):
            return inr(to_paise(value))
        return qty(round(Decimal(str(value)), 2))
    if isinstance(value, date):
        return f"{value:%d %b %Y}"
    return str(value).strip()


def heading(column: str) -> str:
    return (column[:-3] + " (₹)" if column.endswith("_rs") else column).replace("_", " ").capitalize()


def fill(template: str, columns: list[str], row: tuple, question: str = "") -> str | None:
    """The answer sentence with the first row's values; None if it names a column that isn't there or
    writes a number of its own (every figure must come from the query; numbers the owner said may be
    repeated, as in "5000 se zyada udhaar wale")."""
    values = {c: show(c, v) for c, v in zip(columns, row)}
    names = re.findall(r"\{(\w+)\}", template)
    said = set(re.findall(r"\d+", question))
    if not names or any(n not in values for n in names):
        return None
    if any(n not in said for n in re.findall(r"\d+", re.sub(r"\{\w+\}", "", template))):
        return None
    template = re.sub(r"(?:₹|\bRs\.?)\s*(\{\w+_rs\})", r"\1", template)            # "₹{x_rs}" -> "{x_rs}"
    template = re.sub(r"(\{\w+_rs\})\s*(?:rupaye|rupees|rupee|rs\b\.?)", r"\1", template, flags=re.I)
    return re.sub(r"\{(\w+)\}", lambda m: values[m.group(1)], template)


def prompt(business_id: str, lang: str) -> str:
    today = clock.today()
    names = db.fetch_one("""select (select string_agg(name, ', ' order by name) from item where business_id = %s) as items,
        (select string_agg(name, ', ' order by name) from party where business_id = %s) as parties""",
                         (business_id, business_id))
    return PROMPT.format(language=lang_name(lang), today=today.isoformat(), weekday=f"{today:%A}",
                         month_start=today.replace(day=1).isoformat(),
                         fy_start=clock.fy_start(today).isoformat(), items=names["items"] or "(none)",
                         parties=names["parties"] or "(none)")


async def answer(request_id: str, business_id: str, question: str, lang: str = DEFAULT) -> QueryResult:
    messages = [{"role": "system", "content": await asyncio.to_thread(prompt, business_id, lang)},
                {"role": "user", "content": question}]
    problem = None
    for attempt in range(2):
        if problem:
            messages += [{"role": "user", "content": f"That did not work: {problem}. Reply with the corrected JSON object."}]
        try:
            reply, raw = await llm.chat_json(messages)
        except ValueError as err:
            problem = f"the reply was not one JSON object ({err})"
            continue
        except Exception:
            return QueryResult(request_id=request_id, ok=True, text=pick(
                lang, "Is sawaal ke liye language service chahiye, jo abhi nahi mil rahi. Thodi der mein phir puchhiye.",
                "I need my language service for this question and can't reach it right now. Please try again shortly."))
        messages.append({"role": "assistant", "content": raw})
        if reply.get("cannot"):
            return QueryResult(request_id=request_id, ok=True, text=str(reply["cannot"]))
        try:
            sql = check(str(reply.get("sql") or ""))
            columns, rows = await asyncio.to_thread(run, business_id, sql)
        except Rejected as err:
            problem = str(err)
            continue
        except Exception as err:                             # Postgres error: bad column, timeout, permission
            problem = f"Postgres said: {str(err).splitlines()[0][:300]}"
            continue
        llm.log.info(f"answered with SQL: {sql}")
        return result(request_id, question, str(reply.get("answer") or ""), columns, rows, lang)
    llm.log.warning(f"could not answer {question!r}: {problem}")
    return QueryResult(request_id=request_id, ok=True, text=pick(
        lang, "Maaf kijiye, iska jawab abhi nahi nikal paya. Thoda alag tarah se puchh kar dekhiye.",
        "Sorry, I couldn't work that out. Try asking it another way."))


def result(request_id: str, question: str, template: str, columns: list[str], rows: list[tuple],
           lang: str = DEFAULT) -> QueryResult:
    if not rows:
        return QueryResult(request_id=request_id, ok=True,
                           text=pick(lang, "Is sawaal ke liye koi entry nahi mili.", "No matching entries."))
    sentence = fill(template, columns, rows[0], question)
    if len(rows) == 1 and len(columns) <= 3:
        text = sentence or "; ".join(f"{heading(c)}: {show(c, v)}" for c, v in zip(columns, rows[0]))
        return QueryResult(request_id=request_id, ok=True, text=text)
    table = cards.table(question.strip().rstrip("?")[:60] or pick(lang, "Jawab", "Answer"), [heading(c) for c in columns],
                        [[show(c, v) for c, v in zip(columns, r)] for r in rows], subtitle=sentence)
    return QueryResult(request_id=request_id, ok=True, card_kind=table.kind, card_payload=table.payload,
                       terminal=table.terminal, text=table.fallback, narration=sentence or "")

