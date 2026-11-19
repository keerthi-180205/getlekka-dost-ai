"""
Extra tests for Chunk 4: undo_last.

The original undo tests live in tests/test_ledger.py. These cover the edge
cases around them. Run with:   pytest -v
"""
import threading

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


def _status(entry_id):
    with db.connect() as conn:
        return conn.execute("SELECT status FROM entries WHERE id = ?", (entry_id,)).fetchone()["status"]


def test_undo_returns_id_kind_amount_and_customer(shop):
    entry_id = ledger.add_credit(shop, "Suresh", 650)
    assert ledger.undo_last(shop) == {
        "id": entry_id, "kind": "credit", "amount": 650, "customer": "Suresh",
    }


def test_undo_a_payment_raises_the_balance_back(shop):
    ledger.add_credit(shop, "Suresh", 650)
    ledger.add_payment(shop, "Suresh", 200)
    undone = ledger.undo_last(shop)
    assert undone["kind"] == "payment"
    assert ledger.get_balance(shop, "Suresh") == 650


def test_undo_works_newest_first_across_customers_and_sales(shop):
    first = ledger.add_credit(shop, "Suresh", 100)
    second = ledger.add_cash_sale(shop, 500)
    third = ledger.add_credit(shop, "Kumar", 300)
    assert ledger.undo_last(shop)["id"] == third
    assert ledger.undo_last(shop)["id"] == second
    assert ledger.undo_last(shop)["id"] == first
    assert ledger.undo_last(shop) is None


def test_undo_never_picks_an_already_undone_entry(shop):
    ledger.add_credit(shop, "Suresh", 100)
    ledger.add_credit(shop, "Suresh", 200)
    a = ledger.undo_last(shop)
    b = ledger.undo_last(shop)
    assert a["id"] != b["id"]


def test_undo_keeps_every_row_and_marks_status(shop):
    ids = [ledger.add_credit(shop, "Suresh", n) for n in (100, 200, 300)]
    ledger.undo_last(shop)
    assert [_status(i) for i in ids] == ["active", "active", "undone"]


def test_undo_gives_the_customer_name_as_first_written(shop):
    ledger.add_credit(shop, "  ಸುರೇಶ್ ", 650)
    ledger.add_credit(shop, "ಸುರೇಶ್", 100)
    assert ledger.undo_last(shop)["customer"] == "ಸುರೇಶ್"


def test_undo_in_one_shop_leaves_the_other_shop_alone():
    shop_a = ledger.get_or_create_shop("911111111111")
    shop_b = ledger.get_or_create_shop("912222222222")
    ledger.add_credit(shop_a, "Suresh", 500)
    entry_b = ledger.add_credit(shop_b, "Kumar", 80)   # newer id, but another shop
    assert ledger.undo_last(shop_a)["customer"] == "Suresh"
    assert _status(entry_b) == "active"
    assert ledger.get_balance(shop_b, "Kumar") == 80


def test_two_undos_at_the_same_moment_cancel_two_different_entries(shop):
    """The race the write lock prevents: both threads must not undo the same entry."""
    for n in (100, 200, 300, 400):
        ledger.add_credit(shop, "Suresh", n)

    results = []

    def run():
        results.append(ledger.undo_last(shop))

    threads = [threading.Thread(target=run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    undone_ids = {r["id"] for r in results}
    assert len(undone_ids) == 2                       # two different entries
    assert ledger.get_balance(shop, "Suresh") == 300  # 100 + 200 remain
