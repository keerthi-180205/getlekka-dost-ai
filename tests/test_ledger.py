"""
Tests for ledger.py  - run with:   pytest -v

Each test uses a brand-new empty database, so tests never affect each other
or your real dukaandost.db.

Don't change this file to make a test pass - change ledger.py.
"""
import pytest

from app import db, ledger


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    """Before every test: point the app at a new, empty database file."""
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test.db"))
    db.init_db()


@pytest.fixture
def shop():
    return ledger.get_or_create_shop("919876543210")


# ---------- 1. Shops and customers ----------

def test_same_phone_gives_same_shop():
    first = ledger.get_or_create_shop("919876543210")
    second = ledger.get_or_create_shop("919876543210")
    assert first == second


def test_different_phones_give_different_shops():
    assert ledger.get_or_create_shop("911111111111") != ledger.get_or_create_shop("912222222222")


def test_customer_names_match_ignoring_case_and_spaces(shop):
    a = ledger.get_or_create_customer(shop, "Suresh")
    b = ledger.get_or_create_customer(shop, "  SURESH ")
    assert a == b


def test_same_name_in_two_shops_is_two_customers():
    shop_a = ledger.get_or_create_shop("911111111111")
    shop_b = ledger.get_or_create_shop("912222222222")
    assert ledger.get_or_create_customer(shop_a, "Suresh") != ledger.get_or_create_customer(shop_b, "Suresh")


def test_empty_customer_name_is_rejected(shop):
    with pytest.raises(ValueError):
        ledger.get_or_create_customer(shop, "   ")


# ---------- 2. Recording money ----------

def test_credit_increases_balance(shop):
    ledger.add_credit(shop, "Suresh", 650)
    assert ledger.get_balance(shop, "Suresh") == 650


def test_credit_and_payment(shop):
    ledger.add_credit(shop, "Suresh", 650)
    ledger.add_credit(shop, "suresh", 450)
    ledger.add_payment(shop, "Suresh", 200)
    assert ledger.get_balance(shop, "Suresh") == 900


def test_overpayment_goes_below_zero(shop):
    ledger.add_credit(shop, "Ramesh", 100)
    ledger.add_payment(shop, "Ramesh", 150)
    assert ledger.get_balance(shop, "Ramesh") == -50


@pytest.mark.parametrize("bad", [0, -50, 12.5, "650", None, True])
def test_bad_amounts_are_rejected(shop, bad):
    with pytest.raises(ValueError):
        ledger.add_credit(shop, "Suresh", bad)


def test_cash_sale_does_not_change_any_customer_balance(shop):
    ledger.add_credit(shop, "Suresh", 300)
    ledger.add_cash_sale(shop, 1500)
    assert ledger.get_balance(shop, "Suresh") == 300


def test_entries_return_their_id(shop):
    first = ledger.add_credit(shop, "Suresh", 100)
    second = ledger.add_payment(shop, "Suresh", 50)
    assert isinstance(first, int) and isinstance(second, int)
    assert second > first


def test_due_date_is_saved(shop):
    entry_id = ledger.add_credit(shop, "Suresh", 650, due_date="2026-11-14")
    with db.connect() as conn:
        row = conn.execute("SELECT due_date FROM entries WHERE id = ?", (entry_id,)).fetchone()
    assert row["due_date"] == "2026-11-14"


# ---------- 3. Reading balances ----------

def test_unknown_customer_owes_zero_and_is_not_created(shop):
    assert ledger.get_balance(shop, "Nobody") == 0
    with db.connect() as conn:
        count = conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0]
    assert count == 0


def test_shops_do_not_see_each_others_money():
    shop_a = ledger.get_or_create_shop("911111111111")
    shop_b = ledger.get_or_create_shop("912222222222")
    ledger.add_credit(shop_a, "Suresh", 500)
    assert ledger.get_balance(shop_b, "Suresh") == 0


def test_all_dues_sorted_and_only_positive(shop):
    ledger.add_credit(shop, "Kumar", 300)
    ledger.add_credit(shop, "Suresh", 900)
    ledger.add_credit(shop, "Ramesh", 200)
    ledger.add_payment(shop, "Ramesh", 200)   # Ramesh is settled -> not listed
    assert ledger.get_all_dues(shop) == [("Suresh", 900), ("Kumar", 300)]


def test_all_dues_empty_shop(shop):
    assert ledger.get_all_dues(shop) == []


# ---------- 4. Undo ----------

def test_undo_cancels_last_entry(shop):
    ledger.add_credit(shop, "Suresh", 650)
    ledger.add_credit(shop, "Suresh", 615)       # oops, wrong amount
    undone = ledger.undo_last(shop)
    assert undone["amount"] == 615
    assert undone["kind"] == "credit"
    assert undone["customer"] == "Suresh"
    assert ledger.get_balance(shop, "Suresh") == 650


def test_undo_twice_cancels_two_entries(shop):
    ledger.add_credit(shop, "Suresh", 100)
    ledger.add_credit(shop, "Suresh", 200)
    ledger.add_credit(shop, "Suresh", 300)
    ledger.undo_last(shop)
    ledger.undo_last(shop)
    assert ledger.get_balance(shop, "Suresh") == 100


def test_undo_keeps_the_row(shop):
    entry_id = ledger.add_credit(shop, "Suresh", 650)
    ledger.undo_last(shop)
    with db.connect() as conn:
        row = conn.execute("SELECT status FROM entries WHERE id = ?", (entry_id,)).fetchone()
    assert row is not None, "Undo must not DELETE the entry"
    assert row["status"] == "undone"


def test_undo_with_nothing_returns_none(shop):
    assert ledger.undo_last(shop) is None


def test_undo_cash_sale_has_no_customer(shop):
    ledger.add_cash_sale(shop, 1500)
    assert ledger.undo_last(shop)["customer"] is None


def test_undo_only_touches_own_shop():
    shop_a = ledger.get_or_create_shop("911111111111")
    shop_b = ledger.get_or_create_shop("912222222222")
    ledger.add_credit(shop_a, "Suresh", 500)
    assert ledger.undo_last(shop_b) is None
    assert ledger.get_balance(shop_a, "Suresh") == 500
