"""
Tests for Chunk 2: recording money (add_credit, add_payment, add_cash_sale).

These read the database DIRECTLY, so they do not depend on get_balance
(which is Chunk 3). Run with:   pytest -v
"""
import pytest

from app import db, ledger


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    """Every test gets a new empty database."""
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test.db"))
    db.init_db()


@pytest.fixture
def shop():
    return ledger.get_or_create_shop("919876543210")


def _row(entry_id):
    """Read one entry back from the database."""
    with db.connect() as conn:
        return conn.execute("SELECT * FROM entries WHERE id = ?", (entry_id,)).fetchone()


def _count(table):
    with db.connect() as conn:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


# ---------- add_credit ----------

def test_credit_saves_the_right_fields(shop):
    entry_id = ledger.add_credit(shop, "Suresh", 650, due_date="2026-11-14", note="rice")
    row = _row(entry_id)
    assert row["kind"] == "credit"
    assert row["amount"] == 650
    assert row["due_date"] == "2026-11-14"
    assert row["note"] == "rice"
    assert row["status"] == "active"
    assert row["shop_id"] == shop


def test_credit_works_with_kannada_name(shop):
    entry_id = ledger.add_credit(shop, "ಸುರೇಶ್", 650)
    assert _row(entry_id)["customer_id"] == ledger.get_or_create_customer(shop, "ಸುರೇಶ್")


def test_two_credits_for_same_customer_share_one_customer(shop):
    ledger.add_credit(shop, "Suresh", 100)
    ledger.add_credit(shop, " SURESH ", 200)
    assert _count("customers") == 1
    assert _count("entries") == 2


@pytest.mark.parametrize("bad", [0, -50, 12.5, "650", None, True])
def test_bad_amount_is_rejected_and_nothing_is_saved(shop, bad):
    with pytest.raises(ValueError):
        ledger.add_credit(shop, "Suresh", bad)
    assert _count("entries") == 0
    assert _count("customers") == 0   # no stray customer left behind


@pytest.mark.parametrize("bad_date", ["friday", "2026-13-45", "14-11-2026", "", 20261114])
def test_bad_due_date_is_rejected_and_nothing_is_saved(shop, bad_date):
    with pytest.raises(ValueError):
        ledger.add_credit(shop, "Suresh", 650, due_date=bad_date)
    assert _count("entries") == 0
    assert _count("customers") == 0


def test_empty_customer_name_is_rejected(shop):
    with pytest.raises(ValueError):
        ledger.add_credit(shop, "   ", 650)
    assert _count("entries") == 0


# ---------- add_payment ----------

def test_payment_saves_the_right_fields(shop):
    entry_id = ledger.add_payment(shop, "Suresh", 200, note="UPI")
    row = _row(entry_id)
    assert row["kind"] == "payment"
    assert row["amount"] == 200
    assert row["due_date"] is None
    assert row["note"] == "UPI"


@pytest.mark.parametrize("bad", [0, -1, 1.5, "200", None, True])
def test_payment_bad_amount_is_rejected(shop, bad):
    with pytest.raises(ValueError):
        ledger.add_payment(shop, "Suresh", bad)
    assert _count("customers") == 0


def test_payment_may_exceed_what_is_owed(shop):
    """Allowed on purpose: the balance just goes below zero (Chunk 3 checks that)."""
    assert ledger.add_payment(shop, "Ramesh", 150) > 0


# ---------- add_cash_sale ----------

def test_cash_sale_has_no_customer(shop):
    entry_id = ledger.add_cash_sale(shop, 1500)
    row = _row(entry_id)
    assert row["kind"] == "sale"
    assert row["amount"] == 1500
    assert row["customer_id"] is None
    assert _count("customers") == 0


@pytest.mark.parametrize("bad", [0, -1, 1.5, "1500", None, True])
def test_cash_sale_bad_amount_is_rejected(shop, bad):
    with pytest.raises(ValueError):
        ledger.add_cash_sale(shop, bad)
    assert _count("entries") == 0


# ---------- ids and shop separation ----------

def test_entry_ids_are_integers_and_increase(shop):
    first = ledger.add_credit(shop, "Suresh", 100)
    second = ledger.add_payment(shop, "Suresh", 50)
    third = ledger.add_cash_sale(shop, 10)
    assert all(isinstance(i, int) for i in (first, second, third))
    assert first < second < third


def test_entry_belongs_to_the_shop_that_wrote_it():
    shop_a = ledger.get_or_create_shop("911111111111")
    shop_b = ledger.get_or_create_shop("912222222222")
    entry_id = ledger.add_credit(shop_a, "Suresh", 500)
    assert _row(entry_id)["shop_id"] == shop_a
    assert shop_a != shop_b
