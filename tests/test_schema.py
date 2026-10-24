import uuid

BIZ = "00000000-0000-4000-8000-000000000001"


def balances(conn) -> dict[str, int]:
    return {r["name"]: r["balance_paise"] for r in conn.execute("select name, balance_paise from party_balance")}


def test_seed_balances(seeded):
    assert balances(seeded) == {
        "Ramesh Kumar": 540000,
        "Sunita Devi": 210000,
        "Verma Constructions": 3600000,
        "Shree Traders": -2200000,
        "Gupta Cement": 0,
        "Balaji Build": 0,
        "Kailash Builders": 1700000,
        "Meena Interiors": 760000,
        "Deepak Yadav": 330000,
        "Arif Electricals": 350000,
        "Gopal Singh": 0,
        "Arora Homes": -500000,          # advance
        "Jaipur Paints & Hardware": -1380000,
        "Rajasthan Sand Suppliers": -900000,
        "Mahaveer Hardware": -680000,
    }


def test_shop_today_ignores_server_timezone(seeded):
    """Entries are dated in India whatever the server's timezone; UTC+14 and UTC-11 are never both on IST's date."""
    from core import clock
    for tz in ("Pacific/Kiritimati", "Pacific/Pago_Pago"):
        seeded.execute(f"set timezone to '{tz}'")
        assert seeded.execute("select shop_today() as d").fetchone()["d"] == clock.today(), tz


def test_seed_shape(seeded):
    counts = {t: seeded.execute(f"select count(*) as n from {t}").fetchone()["n"] for t in ("item", "party")}
    assert counts == {"item": 12, "party": 15}
    days = seeded.execute(
        "select count(distinct txn_date) as n from txn where kind = 'sale' and party_id is null").fetchone()["n"]
    assert days == 30


def test_seed_stock_moves_add_up(seeded):
    rows = seeded.execute("""
        select i.name, i.stock_qty, sum(m.qty) as moved,
               sum(m.qty) filter (where m.reason = 'opening') as opening
        from item i join stock_move m on m.item_id = i.id group by i.id""").fetchall()
    for r in rows:
        assert r["moved"] == r["stock_qty"], r["name"]
        assert r["opening"] >= 0, r["name"]


def test_cement_below_reorder(seeded):
    r = seeded.execute("select stock_qty, reorder_level from item where name = 'Cement'").fetchone()
    assert r["stock_qty"] < r["reorder_level"]


def _party(conn, name="Test Customer", kind="customer"):
    conn.execute("insert into business (id, owner_address, name) values (%s, 'owner', 'Test Shop') "
                 "on conflict do nothing", (BIZ,))
    return conn.execute("insert into party (business_id, kind, name) values (%s, %s, %s) returning id",
                        (BIZ, kind, name)).fetchone()["id"]


def _action(conn, status="committed"):
    aid = uuid.uuid4()
    conn.execute("insert into action (id, business_id, kind, status) values (%s, %s, 'sale', %s)",
                 (aid, BIZ, status))
    return aid


def _txn(conn, action_id, party_id, kind, paise, mode):
    conn.execute("insert into txn (business_id, action_id, party_id, kind, amount_paise, mode) "
                 "values (%s, %s, %s, %s, %s, %s)", (BIZ, action_id, party_id, kind, paise, mode))


def test_partial_payment_leaves_balance_due(empty):
    p = _party(empty)
    a = _action(empty)
    _txn(empty, a, p, "sale", 420000, "credit")
    _txn(empty, a, p, "receipt", 210000, "cash")
    assert balances(empty)["Test Customer"] == 210000


def test_supplier_signs(empty):
    s = _party(empty, "Test Supplier", "supplier")
    a = _action(empty)
    _txn(empty, a, s, "purchase", 500000, "credit")
    _txn(empty, a, s, "payment", 200000, "bank")
    assert balances(empty)["Test Supplier"] == -300000


def test_undone_action_is_excluded(empty):
    p = _party(empty)
    _txn(empty, _action(empty), p, "sale", 100000, "credit")
    _txn(empty, _action(empty, status="undone"), p, "sale", 999900, "credit")
    assert balances(empty)["Test Customer"] == 100000


def test_txn_without_action_survives_undo_filter(empty):
    # With the design's `action_id not in (...)` this row vanished as soon as any action was undone.
    p = _party(empty)
    _txn(empty, None, p, "sale", 50000, "credit")
    _action(empty, status="undone")
    assert balances(empty)["Test Customer"] == 50000


def test_opening_balance_counts(empty):
    p = _party(empty)
    empty.execute("update party set opening_balance_paise = 75000 where id = %s", (p,))
    assert balances(empty)["Test Customer"] == 75000
