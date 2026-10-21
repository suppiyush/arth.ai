"""Each test gets a throwaway Postgres schema with schema.sql (and seed.sql) applied.

Set TEST_DATABASE_URL to any Postgres you can create schemas in; never point it at the demo data.
"""

import uuid

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from core import config, db


def _schema_conn(seed: bool):
    if not config.TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL is not set")
    name = f"arth_test_{uuid.uuid4().hex[:12]}"
    conn = psycopg.connect(config.TEST_DATABASE_URL, row_factory=dict_row, autocommit=True)
    conn.execute(f"create schema {name}")
    conn.execute(f"set search_path to {name}")
    conn.schema_name = name
    try:
        db.apply_schema(conn, seed=seed)
        yield conn
    finally:
        conn.execute(f"drop schema {name} cascade")
        conn.close()


@pytest.fixture
def seeded():
    yield from _schema_conn(seed=True)


@pytest.fixture
def empty():
    yield from _schema_conn(seed=False)


@pytest.fixture
def seeded_db(seeded):
    """Point core.db's pool at the seeded throwaway schema; yields a direct connection for asserts."""
    test_pool = ConnectionPool(config.TEST_DATABASE_URL, min_size=1, max_size=3, open=True, kwargs={
        "row_factory": dict_row, "prepare_threshold": None, "options": f"-csearch_path={seeded.schema_name}"})
    saved, db._pool = db._pool, test_pool
    try:
        yield seeded
    finally:
        db._pool = saved
        test_pool.close()
