"""Questions answered with ASI1-written SQL (agents/insights/ask.py): what the checker refuses, what the
database refuses even if the checker were bypassed, and the answer flow with ASI1's replies faked."""

import asyncio
import json
import uuid

import pytest

from test_orchestrator import card, extracted, io, say, texts  # noqa: F401

from agents.insights import ask
from core import llm
from core.llm import Extraction

BIZ = "00000000-0000-4000-8000-000000000001"


# The checker

@pytest.mark.parametrize("sql", [
    "insert into entries values (1)",
    "update items set price = 1",
    "delete from parties",
    "drop view entries",
    "select 1; drop table txn",
    "select * into stolen from entries",
    "select * from txn",                                          # base tables, not the views
    "select * from business",
    "select * from public.entries",                               # schema-qualified
    "select relname from pg_catalog.pg_class",
    "select * from information_schema.tables",
    "select set_config('arth.business_id', '00000000-0000-4000-8000-000000000002', true)",
    "select * from entries where current_setting('arth.business_id') is not null",
    "with x as (select pg_sleep(10)) select * from x",
    "select pg_read_file('/etc/passwd')",
    "select pg_catalog.set_config('a', 'b', false)",
    "copy entries to '/tmp/x'",
    "set role postgres",
    "select * from entries for update",
    "",
])
def test_checker_refuses(sql):
    with pytest.raises(ask.Rejected):
        ask.check(sql)


@pytest.mark.parametrize("sql", [
    "select round(avg(amount), 2) as avg_bill_rs from entries where kind = 'sale' and date >= date '2026-10-01'",
    "select to_char(date, 'Day') as day, sum(amount) as sales_rs from entries where kind = 'sale' group by 1 order by 2 desc",
    "with s as (select party, sum(amount) as t from entries where kind = 'sale' group by party) "
    "select party, t as total_rs from s order by t desc limit 3",
    "select g::date as day from generate_series(date '2026-10-01', date '2026-10-07', interval '1 day') g",
    "select percentile_cont(0.5) within group (order by amount) as median_rs from entries where kind = 'sale'",
    "select b.number, l.item from bills b join bill_lines l on l.bill_number = b.number",
])
def test_checker_accepts_ordinary_questions(sql):
    assert ask.check(sql).upper().startswith(("SELECT", "WITH"))


# The database, with the checker out of the way

def add_other_shop(conn):
    other = str(uuid.uuid4())
    conn.execute("insert into business (id, owner_address, name) values (%s, 'other-owner', 'Other Shop')", (other,))
    conn.execute("insert into txn (business_id, kind, amount_paise, mode) values (%s, 'sale', 99900000, 'cash')", (other,))
    return other


def test_views_show_only_the_asking_shop(seeded_db):
    other = add_other_shop(seeded_db)
    _, mine = ask.run(BIZ, "select max(amount) as m, count(*) as n from entries")
    _, theirs = ask.run(other, "select max(amount) as m, count(*) as n from entries")
    assert mine[0][0] < 999000 and theirs[0] == (999000, 1)


@pytest.mark.parametrize("sql", [
    "select count(*) from txn",                                    # no grant on base tables
    "insert into business (owner_address, name) values ('x', 'y')",   # read-only transaction
    "create table stolen as select * from entries",
])
def test_database_refuses_even_without_the_checker(seeded_db, sql):
    with pytest.raises(Exception) as err:
        ask.run(BIZ, sql)
    assert "permission denied" in str(err.value) or "read-only" in str(err.value)
    assert seeded_db.execute("select count(*) as n from business").fetchone()["n"] == 1


def test_slow_query_is_cut_off(seeded_db, monkeypatch):
    monkeypatch.setattr(ask, "TIMEOUT_MS", 200)
    with pytest.raises(Exception) as err:
        ask.run(BIZ, "select pg_sleep(2)")
    assert "statement timeout" in str(err.value)


def test_role_does_not_leak_to_the_next_query(seeded_db):
    from core import db
    ask.run(BIZ, "select 1 as one")
    assert db.fetch_one("select current_user as u")["u"] != "arth_reader"
    assert db.fetch_one("select count(*) as n from txn")["n"] > 0


# Answers

def fake_asi1(monkeypatch, *replies):
    queue, seen = list(replies), []

    async def chat_json(messages):
        seen.append(messages)
        r = queue.pop(0)
        if isinstance(r, Exception):
            raise r
        return r, json.dumps(r)

    monkeypatch.setattr(llm, "chat_json", chat_json)
    return seen


def test_average_bill_is_filled_from_the_query(seeded_db, monkeypatch):
    fake_asi1(monkeypatch, {
        "sql": "select round(avg(amount), 2) as avg_bill_rs, count(*) as bills from entries where kind = 'sale'",
        "answer": "Ab tak {bills} bikri, average bill {avg_bill_rs}."})
    res = asyncio.run(ask.answer("r1", BIZ, "average bill kitna hai"))
    expected = ask.run(BIZ, "select round(avg(amount), 2) as a, count(*) as n from entries where kind = 'sale'")[1][0]
    from core.money import inr, to_paise
    assert res.text == f"Ab tak {expected[1]} bikri, average bill {inr(to_paise(expected[0]))}."


def test_refused_sql_gets_one_repair_round(seeded_db, monkeypatch):
    seen = fake_asi1(monkeypatch,
                     {"sql": "select avg(amount_paise) as a from txn", "answer": "{a}"},
                     {"sql": "select count(*) as n from entries where kind = 'expense'", "answer": "{n} kharche likhe hain."})
    res = asyncio.run(ask.answer("r1", BIZ, "kitne kharche likhe"))
    assert res.text == "13 kharche likhe hain."
    repair = [m["content"] for m in seen[1] if m["content"].startswith("That did not work")]
    assert len(repair) == 1 and "unknown table 'txn'" in repair[0]


def test_two_failures_end_politely(seeded_db, monkeypatch):
    fake_asi1(monkeypatch, {"sql": "select nope from entries", "answer": "{nope}"},
              {"sql": "select also_nope from entries", "answer": "{also_nope}"})
    assert "couldn't work that out" in asyncio.run(ask.answer("r1", BIZ, "?")).text


def test_cannot_and_asi1_down(seeded_db, monkeypatch):
    fake_asi1(monkeypatch, {"cannot": "Mausam ka data mere paas nahi hai."}, RuntimeError("down"))
    assert asyncio.run(ask.answer("r1", BIZ, "kal baarish hogi?")).text == "Mausam ka data mere paas nahi hai."
    assert "language service" in asyncio.run(ask.answer("r1", BIZ, "average bill")).text


def test_a_number_written_by_the_model_is_not_trusted(seeded_db, monkeypatch):
    fake_asi1(monkeypatch, {"sql": "select count(*) as n from entries where kind = 'expense'",
                            "answer": "Aapke {n} kharche hain, yaani 5000 rupaye."})
    assert asyncio.run(ask.answer("r1", BIZ, "kitne kharche")).text == "N: 13"


def test_owner_numbers_may_be_repeated_and_double_rupee_signs_go():
    assert ask.fill("{n} grahak ka udhaar 5000 se zyada: ₹{t_rs} rupaye", ["n", "t_rs"], (2, 41400),
                    "kitne grahak ka udhaar 5000 se zyada?") == "2 grahak ka udhaar 5000 se zyada: ₹41,400"
    assert ask.fill("{n} grahak, 6000 se zyada", ["n"], (2,), "5000 se zyada?") is None


def test_quantities_bought_by_a_customer_include_khata_sales(seeded_db):
    _, rows = ask.run(BIZ, "select sum(-qty) as bags from stock_moves where party = 'Ramesh Kumar' and item = 'Cement'")
    assert rows == [(20,)]


def test_many_rows_become_a_table(seeded_db, monkeypatch):
    fake_asi1(monkeypatch, {
        "sql": "select mode, round(sum(amount), 2) as total_rs from entries where kind = 'sale' "
               "and party is null group by mode order by total_rs desc",
        "answer": "Sabse zyada bikri {mode} mein hui: {total_rs}."})
    res = asyncio.run(ask.answer("r1", BIZ, "cash vs UPI kitni bikri"))
    assert res.card_kind == "custom" and "Sabse zyada bikri cash mein hui: ₹" in res.text
    assert "Mode | Total (₹)" in res.text


def test_custom_question_reaches_ask_through_the_orchestrator(seeded_db, io, extracted, monkeypatch):
    fake_asi1(monkeypatch, {"sql": "select count(*) as n from parties where kind = 'supplier'",
                            "answer": "Aapke {n} supplier hain."})
    extracted["result"] = Extraction(intent="question", report="custom")
    assert texts(say("kitne supplier hain mere", io))[-1] == "Aapke 6 supplier hain."
