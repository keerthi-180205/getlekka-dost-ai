"""
Tests for the Batch 3 ledger helpers: batch meta, undo_batch, undo_entries,
list_customers, get_batch_entries, last_active_batch, and "undo cancels the
note's open questions".

Run with:   pytest -v
"""
import json

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


def _batch_status(batch_id):
    with db.connect() as conn:
        return conn.execute("SELECT status FROM batches WHERE id = ?", (batch_id,)).fetchone()["status"]


def _open_question(shop_id, batch_id, status="open"):
    """Insert a pending question directly (questions.py has its own tests)."""
    with db.connect() as conn:
        cursor = conn.execute(
            "INSERT INTO pending_questions (shop_id, batch_id, kind, payload, status, created_at) "
            "VALUES (?, ?, 'confirm_entry', '{}', ?, 'x')",
            (shop_id, batch_id, status),
        )
        return cursor.lastrowid


def _question_status(question_id):
    with db.connect() as conn:
        return conn.execute("SELECT status FROM pending_questions WHERE id = ?", (question_id,)).fetchone()["status"]


def _note(shop, *amounts, customer="Suresh"):
    """A batch with one credit per amount. Returns (batch_id, [entry ids])."""
    batch = ledger.create_batch(shop, "voice")
    return batch, [ledger.add_credit(shop, customer, n, batch_id=batch) for n in amounts]


# ---------- batch meta ----------

def test_batch_meta_is_saved_as_json(shop):
    batch = ledger.create_batch(shop, "voice", meta={"stt": {"engine": "local"}, "intent": "ಸುರೇಶ್"})
    with db.connect() as conn:
        raw = conn.execute("SELECT meta FROM batches WHERE id = ?", (batch,)).fetchone()["meta"]
    assert json.loads(raw) == {"stt": {"engine": "local"}, "intent": "ಸುರೇಶ್"}
    assert "ಸುರೇಶ್" in raw      # stored readably, not as \\u escapes


def test_batch_without_meta_has_none(shop):
    batch = ledger.create_batch(shop, "voice")
    with db.connect() as conn:
        assert conn.execute("SELECT meta FROM batches WHERE id = ?", (batch,)).fetchone()["meta"] is None


# ---------- undo_batch (the Undo button) ----------

def test_undo_batch_cancels_that_batch_even_when_it_is_not_the_newest(shop):
    old_batch, old_ids = _note(shop, 100, 200)
    new_batch, new_ids = _note(shop, 300)
    result = ledger.undo_batch(shop, old_batch)
    assert result["batch_id"] == old_batch
    assert [e["id"] for e in result["entries"]] == old_ids
    assert all(_status(i) == "undone" for i in old_ids)
    assert _status(new_ids[0]) == "active"          # the newer note is untouched
    assert _batch_status(old_batch) == "undone"
    assert _batch_status(new_batch) == "active"


def test_undo_batch_twice_does_nothing_the_second_time(shop):
    batch, _ = _note(shop, 100)
    assert ledger.undo_batch(shop, batch) is not None
    assert ledger.undo_batch(shop, batch) is None


def test_undo_batch_unknown_id_returns_none(shop):
    assert ledger.undo_batch(shop, 999) is None


def test_undo_batch_of_another_shop_is_refused():
    shop_a = ledger.get_or_create_shop("911111111111")
    shop_b = ledger.get_or_create_shop("912222222222")
    batch_a, ids_a = _note(shop_a, 100)
    assert ledger.undo_batch(shop_b, batch_a) is None
    assert _status(ids_a[0]) == "active"
    assert _batch_status(batch_a) == "active"


def test_undo_batch_when_its_entries_were_already_cancelled(shop):
    batch, ids = _note(shop, 100)
    ledger.undo_entries(shop, ids)
    result = ledger.undo_batch(shop, batch)
    assert result == {"batch_id": batch, "entries": []}
    assert _batch_status(batch) == "undone"


def test_undo_batch_puts_balances_back(shop):
    ledger.add_credit(shop, "Suresh", 50)
    batch, _ = _note(shop, 100, 200)
    assert ledger.get_balance(shop, "Suresh") == 350
    ledger.undo_batch(shop, batch)
    assert ledger.get_balance(shop, "Suresh") == 50


# ---------- undo cancels the note's open questions ----------

def test_undo_batch_cancels_its_open_questions_only(shop):
    batch, _ = _note(shop, 100)
    other, _ = _note(shop, 200)
    mine = _open_question(shop, batch)
    answered = _open_question(shop, batch, status="answered")
    theirs = _open_question(shop, other)
    ledger.undo_batch(shop, batch)
    assert _question_status(mine) == "cancelled"
    assert _question_status(answered) == "answered"       # history is not rewritten
    assert _question_status(theirs) == "open"


def test_undo_last_batch_also_cancels_open_questions(shop):
    batch, _ = _note(shop, 100)
    question = _open_question(shop, batch)
    ledger.undo_last_batch(shop)
    assert _question_status(question) == "cancelled"


# ---------- undo_entries ----------

def test_undo_entries_cancels_only_the_given_entries(shop):
    batch, ids = _note(shop, 100, 200, 300)
    result = ledger.undo_entries(shop, [ids[0], ids[2]])
    assert [e["id"] for e in result] == [ids[0], ids[2]]
    assert [_status(i) for i in ids] == ["undone", "active", "undone"]
    assert _batch_status(batch) == "active"               # the batch itself stays open
    assert ledger.get_balance(shop, "Suresh") == 200


def test_undo_entries_returns_the_usual_summary(shop):
    _, ids = _note(shop, 650)
    assert ledger.undo_entries(shop, ids) == [
        {"id": ids[0], "kind": "credit", "amount": 650, "customer": "Suresh"}
    ]


def test_undo_entries_skips_already_undone_and_unknown_ids(shop):
    _, ids = _note(shop, 100, 200)
    ledger.undo_entries(shop, [ids[0]])
    result = ledger.undo_entries(shop, [ids[0], ids[1], 999])
    assert [e["id"] for e in result] == [ids[1]]


def test_undo_entries_with_nothing_given(shop):
    assert ledger.undo_entries(shop, []) == []


def test_undo_entries_never_touches_another_shop():
    shop_a = ledger.get_or_create_shop("911111111111")
    shop_b = ledger.get_or_create_shop("912222222222")
    _, ids_a = _note(shop_a, 100)
    assert ledger.undo_entries(shop_b, ids_a) == []
    assert _status(ids_a[0]) == "active"


# ---------- list_customers ----------

def test_list_customers_most_recently_used_first(shop):
    ledger.add_credit(shop, "Suresh", 100)
    ledger.add_credit(shop, "Ramesh", 100)
    ledger.add_credit(shop, "Kumar", 100)
    ledger.add_credit(shop, "Suresh", 50)           # Suresh used again: now the newest
    assert [c["name"] for c in ledger.list_customers(shop)] == ["Suresh", "Kumar", "Ramesh"]


def test_list_customers_gives_name_and_key(shop):
    ledger.add_credit(shop, "  Suresh   Gowda ", 100)
    assert ledger.list_customers(shop) == [{"name": "Suresh   Gowda", "name_key": "suresh gowda"}]


def test_list_customers_includes_customers_without_entries_last(shop):
    ledger.get_or_create_customer(shop, "Nobody")
    ledger.add_credit(shop, "Suresh", 100)
    assert [c["name"] for c in ledger.list_customers(shop)] == ["Suresh", "Nobody"]


def test_list_customers_is_per_shop():
    shop_a = ledger.get_or_create_shop("911111111111")
    shop_b = ledger.get_or_create_shop("912222222222")
    ledger.add_credit(shop_a, "Suresh", 100)
    ledger.add_credit(shop_b, "Kumar", 100)
    assert [c["name"] for c in ledger.list_customers(shop_a)] == ["Suresh"]


def test_list_customers_empty_shop(shop):
    assert ledger.list_customers(shop) == []


def test_list_customers_does_not_duplicate_customers_with_many_entries(shop):
    for _ in range(3):
        ledger.add_credit(shop, "Suresh", 10)
    assert len(ledger.list_customers(shop)) == 1


# ---------- get_batch_entries ----------

def test_get_batch_entries_gives_everything_needed_to_recreate_them(shop):
    batch = ledger.create_batch(shop, "voice")
    credit = ledger.add_credit(shop, "ಸುರೇಶ್", 650, due_date="2026-11-27", note="ಅಕ್ಕಿ", batch_id=batch)
    sale = ledger.add_cash_sale(shop, 1500, batch_id=batch)
    assert ledger.get_batch_entries(shop, batch) == [
        {"id": credit, "kind": "credit", "amount": 650, "customer": "ಸುರೇಶ್",
         "due_date": "2026-11-27", "note": "ಅಕ್ಕಿ", "status": "active"},
        {"id": sale, "kind": "sale", "amount": 1500, "customer": None,
         "due_date": None, "note": None, "status": "active"},
    ]


def test_get_batch_entries_active_only_or_all(shop):
    batch, ids = _note(shop, 100, 200)
    ledger.undo_entries(shop, [ids[0]])
    assert [e["id"] for e in ledger.get_batch_entries(shop, batch)] == [ids[1]]
    everything = ledger.get_batch_entries(shop, batch, only_active=False)
    assert [(e["id"], e["status"]) for e in everything] == [(ids[0], "undone"), (ids[1], "active")]


def test_get_batch_entries_ignores_other_batches_and_shops():
    shop_a = ledger.get_or_create_shop("911111111111")
    shop_b = ledger.get_or_create_shop("912222222222")
    batch_a, ids_a = _note(shop_a, 100)
    _note(shop_a, 999)
    assert [e["id"] for e in ledger.get_batch_entries(shop_a, batch_a)] == ids_a
    assert ledger.get_batch_entries(shop_b, batch_a) == []


# ---------- is_batch_active ----------

def test_is_batch_active_follows_undo(shop):
    batch, _ = _note(shop, 100)
    assert ledger.is_batch_active(shop, batch) is True
    ledger.undo_batch(shop, batch)
    assert ledger.is_batch_active(shop, batch) is False


def test_is_batch_active_false_for_unknown_none_and_other_shops():
    shop_a = ledger.get_or_create_shop("911111111111")
    shop_b = ledger.get_or_create_shop("912222222222")
    batch_a, _ = _note(shop_a, 100)
    assert ledger.is_batch_active(shop_a, 999) is False
    assert ledger.is_batch_active(shop_a, None) is False
    assert ledger.is_batch_active(shop_b, batch_a) is False


# ---------- last_active_batch ----------

def test_last_active_batch_follows_the_newest_active_entry(shop):
    first, _ = _note(shop, 100)
    second, _ = _note(shop, 200)
    assert ledger.last_active_batch(shop) == second
    ledger.undo_batch(shop, second)
    assert ledger.last_active_batch(shop) == first


def test_last_active_batch_none_when_nothing_or_no_batch(shop):
    assert ledger.last_active_batch(shop) is None
    ledger.add_credit(shop, "Suresh", 100)          # an entry with no batch
    assert ledger.last_active_batch(shop) is None
