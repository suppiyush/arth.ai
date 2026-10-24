"""Postgres access: one connection pool per process and a transaction helper.

    python -m core.db reset    drop everything, apply db/schema.sql and db/seed.sql
    python -m core.db init     apply schema and seed only if the database is empty (safe on every start)
    python -m core.db check    print row counts and party balances
"""

import sys
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Json  # noqa: F401  (re-exported for agents writing jsonb)
from psycopg_pool import ConnectionPool

from core import config

SCHEMA_SQL = config.ROOT / "db" / "schema.sql"
SEED_SQL = config.ROOT / "db" / "seed.sql"

_pool: ConnectionPool | None = None


def pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        # prepare_threshold=None: Supabase's pooler (pgbouncer) rejects server-side prepared statements.
        _pool = ConnectionPool(
            config.require("DATABASE_URL"), min_size=1, max_size=5, open=True,
            kwargs={"row_factory": dict_row, "prepare_threshold": None},
        )
    return _pool


@contextmanager
def transaction():
    """Yield a cursor inside one transaction; commits on success, rolls back on any exception."""
    with pool().connection() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                yield cur


def fetch_all(sql: str, params=None) -> list[dict]:
    with pool().connection() as conn:
        return conn.execute(sql, params).fetchall()


def fetch_one(sql: str, params=None) -> dict | None:
    with pool().connection() as conn:
        return conn.execute(sql, params).fetchone()


def seed_sql(business_id: str | None = None, owner: str = "demo-owner") -> str:
    """db/seed.sql for one shop. No business_id: the original demo shop. Another id: a copy of it with its own
    row ids (every seeded id is md5('arth-seed-...'), so a per-shop prefix keeps them apart) and dates
    relative to today."""
    sql = SEED_SQL.read_text(encoding="utf-8")
    if business_id and business_id != config.DEMO_BUSINESS_ID:
        sql = sql.replace(config.DEMO_BUSINESS_ID, business_id)
        sql = sql.replace("md5('arth-seed-", f"md5('arth-seed-{business_id}-")
    return sql.replace("'demo-owner'", "'" + owner.replace("'", "''") + "'")


def apply_schema(conn: psycopg.Connection, seed: bool = True) -> None:
    """Run schema.sql (and seed.sql) on a connection whose search_path points at an empty schema."""
    conn.execute(SCHEMA_SQL.read_text(encoding="utf-8"))
    if seed:
        conn.execute(seed_sql())


TABLES = ("catalog_change", "invoice_payment", "reminder", "purchase_order", "invoice_line", "invoice_counter", "invoice",
          "stock_move", "txn", "action", "item", "party", "business")
VIEWS = ("entries", "parties", "items", "bills", "bill_lines", "stock_moves", "party_balance")


def reset(url: str) -> None:
    """Drop only Arth's own objects, so Supabase's grants on the public schema survive."""
    with psycopg.connect(url, autocommit=False) as conn:
        conn.execute(f"drop view if exists {', '.join(VIEWS)}")
        conn.execute(f"drop table if exists {', '.join(TABLES)} cascade")
        apply_schema(conn)
        conn.commit()


def init(url: str) -> bool:
    """First start on a new database: create everything. A database that already has Arth's tables is left
    alone, so this can run before every start of the agents. -> True if it created the schema."""
    with psycopg.connect(url, autocommit=False) as conn:
        conn.execute("select pg_advisory_lock(hashtext('arth-init'))")
        if conn.execute("select to_regclass('business') is not null as ready").fetchone()[0]:
            return False
        apply_schema(conn)
        conn.commit()
        return True


def check(url: str) -> None:
    with psycopg.connect(url, row_factory=dict_row) as conn:
        for table in ("business", "party", "item", "action", "txn", "stock_move"):
            n = conn.execute(f"select count(*) as n from {table}").fetchone()["n"]
            print(f"{table:<12}{n:>6}")
        print()
        for row in conn.execute("select name, kind, balance_paise from party_balance order by kind, name"):
            print(f"{row['name']:<22}{row['kind']:<10}{row['balance_paise'] / 100:>12,.2f}")


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command == "reset":
        reset(config.require("DATABASE_URL"))
        print("schema and seed applied")
        check(config.DATABASE_URL)
    elif command == "init":
        created = init(config.require("DATABASE_URL"))
        with psycopg.connect(config.DATABASE_URL) as conn:
            shops, parties, entries = conn.execute("select (select count(*) from business), (select count(*) from party), "
                                                   "(select count(*) from txn)").fetchone()
        print(f"database {'created with the demo shop' if created else 'already set up; nothing changed'}: "
              f"{shops} shop(s), {parties} parties, {entries} entries")
    elif command == "check":
        check(config.require("DATABASE_URL"))
    else:
        print(__doc__)
        sys.exit(1)
