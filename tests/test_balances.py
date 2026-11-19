"""
Tests for Chunk 3: reading balances (get_balance, get_all_dues).

Undo is not built yet (Chunk 4), so tests that need an "undone" entry mark it
with a plain UPDATE through _undo(). Run with:   pytest -v
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


def _undo(entry_id):
    """Stand-in for undo_last: mark one entry as undone directly."""
    with db.connect() as conn:
        conn.execute("UPDATE entries SET status = 'undone' WHERE id = ?", (entry_id,))


def _count(table):
    with db.connect() as conn:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


# ---------- get_balance ----------

def test_balance_is_credits_minus_payments(shop):
    ledger.add_credit(shop, "Suresh", 650)
    ledger.add_credit(shop, "Suresh", 450)
    ledger.add_payment(shop, "Suresh", 200)
    assert ledger.get_balance(shop, "Suresh") == 900


def test_balance_matches_name_ignoring_case_and_spaces(shop):
    ledger.add_credit(shop, "Suresh", 500)
    assert ledger.get_balance(shop, "  SURESH ") == 500


def test_balance_works_for_kannada_names(shop):
    ledger.add_credit(shop, "ಸುರೇಶ್", 700)
    assert ledger.get_balance(shop, "ಸುರೇಶ್") == 700
    assert ledger.get_balance(shop, "ಸುರೇಶ್‍") == 700  # hidden joiner is ignored


def test_overpayment_gives_negative_balance(shop):
    ledger.add_credit(shop, "Ramesh", 100)
    ledger.add_payment(shop, "Ramesh", 150)
    assert ledger.get_balance(shop, "Ramesh") == -50


def test_undone_entries_do_not_count(shop):
    ledger.add_credit(shop, "Suresh", 650)
    wrong = ledger.add_credit(shop, "Suresh", 6500)   # mistake
    _undo(wrong)
    assert ledger.get_balance(shop, "Suresh") == 650


def test_customer_whose_entries_are_all_undone_owes_zero(shop):
    _undo(ledger.add_credit(shop, "Suresh", 650))
    assert ledger.get_balance(shop, "Suresh") == 0


def test_cash_sales_never_change_a_balance(shop):
    ledger.add_credit(shop, "Suresh", 300)
    ledger.add_cash_sale(shop, 1500)
    assert ledger.get_balance(shop, "Suresh") == 300


def test_balance_returns_a_plain_int(shop):
    ledger.add_credit(shop, "Suresh", 300)
    assert type(ledger.get_balance(shop, "Suresh")) is int
    assert type(ledger.get_balance(shop, "Nobody")) is int


def test_unknown_customer_owes_zero_and_is_not_created(shop):
    assert ledger.get_balance(shop, "Nobody") == 0
    assert _count("customers") == 0


def test_balance_in_another_shop_is_separate():
    shop_a = ledger.get_or_create_shop("911111111111")
    shop_b = ledger.get_or_create_shop("912222222222")
    ledger.add_credit(shop_a, "Suresh", 500)
    ledger.add_credit(shop_b, "Suresh", 80)
    assert ledger.get_balance(shop_a, "Suresh") == 500
    assert ledger.get_balance(shop_b, "Suresh") == 80


# ---------- get_all_dues ----------

def test_dues_biggest_first_and_only_positive(shop):
    ledger.add_credit(shop, "Kumar", 300)
    ledger.add_credit(shop, "Suresh", 900)
    ledger.add_credit(shop, "Ramesh", 200)
    ledger.add_payment(shop, "Ramesh", 200)       # settled -> not listed
    ledger.add_credit(shop, "Geeta", 50)
    ledger.add_payment(shop, "Geeta", 80)         # overpaid -> not listed
    assert ledger.get_all_dues(shop) == [("Suresh", 900), ("Kumar", 300)]


def test_dues_empty_shop(shop):
    assert ledger.get_all_dues(shop) == []


def test_dues_ignore_undone_entries(shop):
    ledger.add_credit(shop, "Suresh", 100)
    _undo(ledger.add_credit(shop, "Suresh", 9000))
    _undo(ledger.add_credit(shop, "Kumar", 400))   # Kumar now has nothing active
    assert ledger.get_all_dues(shop) == [("Suresh", 100)]


def test_dues_equal_amounts_are_sorted_by_name(shop):
    ledger.add_credit(shop, "Ramesh", 300)
    ledger.add_credit(shop, "Kumar", 300)
    ledger.add_credit(shop, "Suresh", 300)
    assert ledger.get_all_dues(shop) == [("Kumar", 300), ("Ramesh", 300), ("Suresh", 300)]


def test_dues_show_name_as_first_written(shop):
    ledger.add_credit(shop, "  Suresh ", 100)
    ledger.add_credit(shop, "SURESH", 50)
    assert ledger.get_all_dues(shop) == [("Suresh", 150)]


def test_dues_include_kannada_names(shop):
    ledger.add_credit(shop, "ಸುರೇಶ್", 900)
    ledger.add_credit(shop, "ರಮೇಶ್", 300)
    assert ledger.get_all_dues(shop) == [("ಸುರೇಶ್", 900), ("ರಮೇಶ್", 300)]


def test_dues_do_not_leak_between_shops():
    shop_a = ledger.get_or_create_shop("911111111111")
    shop_b = ledger.get_or_create_shop("912222222222")
    ledger.add_credit(shop_a, "Suresh", 500)
    ledger.add_credit(shop_b, "Kumar", 80)
    assert ledger.get_all_dues(shop_a) == [("Suresh", 500)]
    assert ledger.get_all_dues(shop_b) == [("Kumar", 80)]


def test_dues_agree_with_get_balance(shop):
    """The two functions must never disagree about the same customer."""
    ledger.add_credit(shop, "Suresh", 650)
    ledger.add_credit(shop, "Suresh", 450)
    ledger.add_payment(shop, "Suresh", 200)
    assert ledger.get_all_dues(shop) == [("Suresh", ledger.get_balance(shop, "Suresh"))]
