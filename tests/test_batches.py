"""
Tests for Batch 1 (Chunk 5): batches, linking entries to a voice note, and
the owner's whole-note Undo (undo_last_batch).

Run with:   pytest -v
"""
import sqlite3
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


def _entry_status(entry_id):
    with db.connect() as conn:
        return conn.execute("SELECT status FROM entries WHERE id = ?", (entry_id,)).fetchone()["status"]


def _batch_row(batch_id):
    with db.connect() as conn:
        return conn.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()


def _voice_note(shop):
    """A typical 3-entry voice note. Returns (batch_id, [entry ids])."""
    batch = ledger.create_batch(shop, "voice", transcript="Suresh 650, Ramesh 200 paid, cash 1500")
    ids = [
        ledger.add_credit(shop, "Suresh", 650, batch_id=batch),
        ledger.add_payment(shop, "Ramesh", 200, batch_id=batch),
        ledger.add_cash_sale(shop, 1500, batch_id=batch),
    ]
    return batch, ids


# ---------- database: new table, new column, migration ----------

def test_init_db_creates_batches_and_batch_id_column():
    with db.connect() as conn:
        tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        entry_columns = {r["name"] for r in conn.execute("PRAGMA table_info(entries)")}
    assert "batches" in tables
    assert "batch_id" in entry_columns


def test_init_db_can_run_many_times_safely(shop):
    ledger.add_credit(shop, "Suresh", 650)
    db.init_db()
    db.init_db()
    assert ledger.get_balance(shop, "Suresh") == 650


def test_old_database_is_upgraded_without_losing_data(tmp_path, monkeypatch):
    """A database made BEFORE batches existed must gain the column and keep its rows."""
    old_path = str(tmp_path / "old.db")
    conn = sqlite3.connect(old_path)
    conn.executescript(
        """
        CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, wa_msg_id TEXT UNIQUE,
            phone TEXT NOT NULL, direction TEXT NOT NULL, msg_type TEXT, body TEXT,
            created_at TEXT NOT NULL);
        CREATE TABLE shops (id INTEGER PRIMARY KEY AUTOINCREMENT, owner_phone TEXT NOT NULL UNIQUE,
            shop_name TEXT, upi_id TEXT, language TEXT, created_at TEXT NOT NULL);
        CREATE TABLE customers (id INTEGER PRIMARY KEY AUTOINCREMENT,
            shop_id INTEGER NOT NULL REFERENCES shops(id), name TEXT NOT NULL,
            name_key TEXT NOT NULL, phone TEXT, created_at TEXT NOT NULL,
            UNIQUE (shop_id, name_key));
        CREATE TABLE entries (id INTEGER PRIMARY KEY AUTOINCREMENT,
            shop_id INTEGER NOT NULL REFERENCES shops(id),
            customer_id INTEGER REFERENCES customers(id),
            kind TEXT NOT NULL CHECK (kind IN ('credit', 'payment', 'sale')),
            amount INTEGER NOT NULL CHECK (amount > 0), due_date TEXT, note TEXT,
            status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'undone')),
            created_at TEXT NOT NULL);
        INSERT INTO shops (owner_phone, created_at) VALUES ('919876543210', 'x');
        INSERT INTO customers (shop_id, name, name_key, created_at) VALUES (1, 'Suresh', 'suresh', 'x');
        INSERT INTO entries (shop_id, customer_id, kind, amount, created_at)
            VALUES (1, 1, 'credit', 650, 'x');
        """
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(db, "DB_PATH", old_path)
    db.init_db()   # the migration runs here

    assert ledger.get_balance(1, "Suresh") == 650            # old data intact
    with db.connect() as c:
        row = c.execute("SELECT batch_id FROM entries WHERE id = 1").fetchone()
    assert row["batch_id"] is None                            # old rows have no batch
    batch = ledger.create_batch(1, "voice")                   # and new features work
    ledger.add_credit(1, "Suresh", 100, batch_id=batch)
    assert ledger.get_balance(1, "Suresh") == 750
    assert ledger.undo_last_batch(1)["batch_id"] == batch
    assert ledger.undo_last_batch(1)["batch_id"] is None      # old single entry, undone alone
    assert ledger.get_balance(1, "Suresh") == 0


# ---------- create_batch ----------

def test_create_batch_saves_its_fields(shop):
    batch = ledger.create_batch(
        shop, "voice", transcript="ಸುರೇಶ್ ಗೆ 650", audio_path="audio/1.ogg", raw_extraction='{"x": 1}',
    )
    row = _batch_row(batch)
    assert isinstance(batch, int)
    assert row["shop_id"] == shop
    assert row["source"] == "voice"
    assert row["transcript"] == "ಸುರೇಶ್ ಗೆ 650"
    assert row["audio_path"] == "audio/1.ogg"
    assert row["raw_extraction"] == '{"x": 1}'
    assert row["status"] == "active"


@pytest.mark.parametrize("bad", ["", "audio", None, "VOICE"])
def test_create_batch_rejects_unknown_source(shop, bad):
    with pytest.raises(ValueError):
        ledger.create_batch(shop, bad)


# ---------- adding entries to a batch ----------

def test_entries_get_the_batch_id(shop):
    batch, ids = _voice_note(shop)
    with db.connect() as conn:
        rows = conn.execute("SELECT batch_id FROM entries ORDER BY id").fetchall()
    assert [r["batch_id"] for r in rows] == [batch, batch, batch]


def test_entries_without_a_batch_still_work(shop):
    entry_id = ledger.add_credit(shop, "Suresh", 650)
    with db.connect() as conn:
        assert conn.execute("SELECT batch_id FROM entries WHERE id = ?", (entry_id,)).fetchone()["batch_id"] is None


def test_unknown_batch_is_rejected_and_nothing_is_saved(shop):
    with pytest.raises(ValueError):
        ledger.add_credit(shop, "Suresh", 650, batch_id=999)
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0] == 0   # no stray customer


def test_another_shops_batch_is_rejected():
    shop_a = ledger.get_or_create_shop("911111111111")
    shop_b = ledger.get_or_create_shop("912222222222")
    batch_a = ledger.create_batch(shop_a, "voice")
    for add in (
        lambda: ledger.add_credit(shop_b, "Suresh", 100, batch_id=batch_a),
        lambda: ledger.add_payment(shop_b, "Suresh", 100, batch_id=batch_a),
        lambda: ledger.add_cash_sale(shop_b, 100, batch_id=batch_a),
    ):
        with pytest.raises(ValueError):
            add()


def test_cannot_add_to_a_batch_that_was_undone(shop):
    batch, _ = _voice_note(shop)
    ledger.undo_last_batch(shop)
    with pytest.raises(ValueError):
        ledger.add_credit(shop, "Suresh", 100, batch_id=batch)


def test_bad_amount_in_a_batch_is_still_rejected(shop):
    batch = ledger.create_batch(shop, "voice")
    with pytest.raises(ValueError):
        ledger.add_credit(shop, "Suresh", 0, batch_id=batch)


# ---------- undo_last_batch ----------

def test_undo_cancels_every_entry_of_the_voice_note(shop):
    batch, ids = _voice_note(shop)
    result = ledger.undo_last_batch(shop)

    assert result["batch_id"] == batch
    assert [e["id"] for e in result["entries"]] == ids
    assert [e["kind"] for e in result["entries"]] == ["credit", "payment", "sale"]
    assert [e["customer"] for e in result["entries"]] == ["Suresh", "Ramesh", None]
    assert all(_entry_status(i) == "undone" for i in ids)
    assert _batch_row(batch)["status"] == "undone"


def test_undo_batch_puts_balances_back(shop):
    ledger.add_credit(shop, "Suresh", 100)                 # before the voice note
    _voice_note(shop)
    assert ledger.get_balance(shop, "Suresh") == 750
    ledger.undo_last_batch(shop)
    assert ledger.get_balance(shop, "Suresh") == 100
    assert ledger.get_balance(shop, "Ramesh") == 0         # his -200 payment is gone too


def test_only_the_newest_batch_is_undone(shop):
    first, first_ids = _voice_note(shop)
    second, second_ids = _voice_note(shop)
    result = ledger.undo_last_batch(shop)
    assert result["batch_id"] == second
    assert all(_entry_status(i) == "active" for i in first_ids)
    assert _batch_row(first)["status"] == "active"


def test_undo_twice_cancels_two_batches_then_nothing(shop):
    first, _ = _voice_note(shop)
    second, _ = _voice_note(shop)
    assert ledger.undo_last_batch(shop)["batch_id"] == second
    assert ledger.undo_last_batch(shop)["batch_id"] == first
    assert ledger.undo_last_batch(shop) is None


def test_single_entry_without_batch_is_undone_alone(shop):
    a = ledger.add_credit(shop, "Suresh", 100)
    b = ledger.add_credit(shop, "Suresh", 200)
    result = ledger.undo_last_batch(shop)
    assert result["batch_id"] is None
    assert [e["id"] for e in result["entries"]] == [b]
    assert _entry_status(a) == "active"


def test_mixed_history_is_undone_newest_first(shop):
    batch, ids = _voice_note(shop)
    single = ledger.add_credit(shop, "Kumar", 300)         # typed after the voice note
    assert ledger.undo_last_batch(shop)["entries"][0]["id"] == single
    assert ledger.undo_last_batch(shop)["batch_id"] == batch


def test_nothing_to_undo_returns_none(shop):
    assert ledger.undo_last_batch(shop) is None


def test_batch_with_no_entries_does_not_break_undo(shop):
    """E.g. the AI found nothing in a note. It must not block undoing real entries."""
    entry = ledger.add_credit(shop, "Suresh", 100)
    ledger.create_batch(shop, "voice")                     # empty batch, newer than the entry
    assert ledger.undo_last_batch(shop)["entries"][0]["id"] == entry


def test_undo_batch_leaves_other_shops_alone():
    shop_a = ledger.get_or_create_shop("911111111111")
    shop_b = ledger.get_or_create_shop("912222222222")
    batch_a, ids_a = _voice_note(shop_a)
    batch_b, ids_b = _voice_note(shop_b)                   # newer ids, other shop
    assert ledger.undo_last_batch(shop_a)["batch_id"] == batch_a
    assert all(_entry_status(i) == "active" for i in ids_b)
    assert _batch_row(batch_b)["status"] == "active"


def test_undo_batch_after_some_entries_were_cancelled_one_by_one(shop):
    """undo_last (single) cancels the newest entry; undo_last_batch then
    cancels only the entries of that batch that are still active."""
    batch, ids = _voice_note(shop)
    ledger.undo_last(shop)                                 # cancels the cash sale only
    result = ledger.undo_last_batch(shop)
    assert result["batch_id"] == batch
    assert [e["id"] for e in result["entries"]] == ids[:2]
    assert all(_entry_status(i) == "undone" for i in ids)


def test_undo_last_ignores_batches_and_stays_single_entry(shop):
    batch, ids = _voice_note(shop)
    result = ledger.undo_last(shop)
    assert result["id"] == ids[-1]
    assert _entry_status(ids[0]) == "active"
    assert _batch_row(batch)["status"] == "active"


def test_kannada_names_come_back_from_undo(shop):
    batch = ledger.create_batch(shop, "voice")
    ledger.add_credit(shop, "ಸುರೇಶ್", 650, batch_id=batch)
    assert ledger.undo_last_batch(shop)["entries"][0]["customer"] == "ಸುರೇಶ್"


def test_two_undos_at_the_same_moment_cancel_two_different_batches(shop):
    """The write lock must stop both threads from cancelling the same batch."""
    batches = [_voice_note(shop)[0] for _ in range(4)]
    results = []

    def run():
        results.append(ledger.undo_last_batch(shop))

    threads = [threading.Thread(target=run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert {r["batch_id"] for r in results} == {batches[3], batches[2]}
    assert _batch_row(batches[0])["status"] == "active"
    assert _batch_row(batches[1])["status"] == "active"
