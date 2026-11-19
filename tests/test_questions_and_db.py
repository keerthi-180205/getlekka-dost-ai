"""
Tests for Chunk 8, part 2: pending questions (app/questions.py), and the new
database pieces (pending_questions table, messages.buttons, get_message_id).

Run with:   pytest -v
"""
import json
import sqlite3
import threading

import pytest

from app import db, ledger, questions


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    """Every test gets a new empty database."""
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test.db"))
    db.init_db()


@pytest.fixture
def shop():
    return ledger.get_or_create_shop("919876543210")


# ---------- questions ----------

def test_create_and_read_back_a_question(shop):
    batch = ledger.create_batch(shop, "voice")
    payload = {"spoken": "ಸುರೇಶ್", "options": ["ಸುರೇಶ್ ಹಾಲು", "ಸುರೇಶ್"]}
    qid = questions.create_question(shop, batch, "choose_customer", payload)
    assert questions.get_question(shop, qid) == {
        "id": qid, "shop_id": shop, "batch_id": batch,
        "kind": "choose_customer", "payload": payload, "status": "open",
    }


def test_payload_is_stored_readably_in_kannada(shop):
    qid = questions.create_question(shop, None, "confirm_entry", {"name": "ಸುರೇಶ್"})
    with db.connect() as conn:
        raw = conn.execute("SELECT payload FROM pending_questions WHERE id = ?", (qid,)).fetchone()["payload"]
    assert "ಸುರೇಶ್" in raw and json.loads(raw) == {"name": "ಸುರೇಶ್"}


def test_a_question_does_not_need_a_batch(shop):
    qid = questions.create_question(shop, None, "new_name", {})
    assert questions.get_question(shop, qid)["batch_id"] is None


def test_unknown_question_kind_is_refused(shop):
    with pytest.raises(ValueError, match="Unknown question kind"):
        questions.create_question(shop, None, "chitchat", {})
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM pending_questions").fetchone()[0] == 0


def test_the_database_itself_refuses_bad_kinds_and_statuses(shop):
    with db.connect() as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO pending_questions (shop_id, kind, payload, created_at) "
                         "VALUES (?, 'bogus', '{}', 'x')", (shop,))
    with db.connect() as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO pending_questions (shop_id, kind, payload, status, created_at) "
                         "VALUES (?, 'confirm_entry', '{}', 'bogus', 'x')", (shop,))


def test_unknown_question_id_returns_none(shop):
    assert questions.get_question(shop, 999) is None


def test_a_shop_cannot_read_another_shops_question():
    shop_a = ledger.get_or_create_shop("911111111111")
    shop_b = ledger.get_or_create_shop("912222222222")
    qid = questions.create_question(shop_a, None, "confirm_entry", {})
    assert questions.get_question(shop_b, qid) is None
    assert not questions.claim_question(shop_b, qid)
    assert questions.get_question(shop_a, qid)["status"] == "open"


def test_open_questions_oldest_first_and_only_open(shop):
    a = questions.create_question(shop, None, "confirm_entry", {})
    b = questions.create_question(shop, None, "choose_kind", {})
    c = questions.create_question(shop, None, "confirm_entry", {})
    questions.claim_question(shop, b)
    assert [q["id"] for q in questions.open_questions(shop)] == [a, c]


def test_open_questions_can_filter_by_kind(shop):
    a = questions.create_question(shop, None, "confirm_entry", {})
    questions.create_question(shop, None, "choose_kind", {})
    assert [q["id"] for q in questions.open_questions(shop, ("confirm_entry",))] == [a]
    assert questions.open_questions(shop, ("new_name",)) == []


def test_latest_open_question_is_the_newest(shop):
    questions.create_question(shop, None, "confirm_entry", {})
    newest = questions.create_question(shop, None, "choose_kind", {})
    assert questions.latest_open_question(shop)["id"] == newest
    assert questions.latest_open_question(shop, ("confirm_entry",))["kind"] == "confirm_entry"


def test_latest_open_question_none_when_nothing_is_open(shop):
    assert questions.latest_open_question(shop) is None


def test_open_questions_are_per_shop():
    shop_a = ledger.get_or_create_shop("911111111111")
    shop_b = ledger.get_or_create_shop("912222222222")
    questions.create_question(shop_a, None, "confirm_entry", {})
    assert questions.open_questions(shop_b) == []


def test_claim_marks_answered_and_records_the_time(shop):
    qid = questions.create_question(shop, None, "confirm_entry", {})
    assert questions.claim_question(shop, qid) is True
    assert questions.get_question(shop, qid)["status"] == "answered"
    with db.connect() as conn:
        assert conn.execute("SELECT answered_at FROM pending_questions WHERE id = ?", (qid,)).fetchone()[0]


def test_claiming_twice_only_works_once(shop):
    qid = questions.create_question(shop, None, "confirm_entry", {})
    assert questions.claim_question(shop, qid) is True
    assert questions.claim_question(shop, qid) is False


def test_a_cancelled_question_cannot_be_claimed(shop):
    batch = ledger.create_batch(shop, "voice")
    ledger.add_credit(shop, "Suresh", 100, batch_id=batch)
    qid = questions.create_question(shop, batch, "confirm_entry", {})
    ledger.undo_batch(shop, batch)
    assert questions.claim_question(shop, qid) is False
    assert questions.get_question(shop, qid)["status"] == "cancelled"


def test_two_taps_at_the_same_moment_are_answered_once(shop):
    """The whole point of claim_question: a double tap must not save an entry twice."""
    qid = questions.create_question(shop, None, "confirm_entry", {})
    results = []

    def tap():
        results.append(questions.claim_question(shop, qid))

    threads = [threading.Thread(target=tap) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count(True) == 1


# ---------- database: messages.buttons and get_message_id ----------

def test_outgoing_message_saves_its_buttons():
    buttons = [{"id": "undo:3", "title": "ರದ್ದು ಮಾಡಿ"}]
    db.save_outgoing("sim.1", "919876543210", "text", "ಬರೆದಿದ್ದೇನೆ", buttons)
    with db.connect() as conn:
        row = conn.execute("SELECT body, buttons FROM messages WHERE wa_msg_id = 'sim.1'").fetchone()
    assert row["body"] == "ಬರೆದಿದ್ದೇನೆ"
    assert json.loads(row["buttons"]) == buttons
    assert "ರದ್ದು" in row["buttons"]           # readable, not escaped


def test_outgoing_message_without_buttons_still_works():
    db.save_outgoing("sim.2", "919876543210", "text", "hello")
    with db.connect() as conn:
        assert conn.execute("SELECT buttons FROM messages WHERE wa_msg_id = 'sim.2'").fetchone()["buttons"] is None


def test_get_message_id_finds_a_saved_message():
    db.save_incoming("wamid.abc", "919876543210", "text", "hi")
    message_id = db.get_message_id("wamid.abc")
    with db.connect() as conn:
        assert conn.execute("SELECT body FROM messages WHERE id = ?", (message_id,)).fetchone()["body"] == "hi"


def test_get_message_id_unknown_is_none():
    assert db.get_message_id("wamid.nope") is None


def test_a_batch_can_point_at_its_message(shop):
    db.save_incoming("wamid.abc", "919876543210", "audio", None)
    batch = ledger.create_batch(shop, "voice", message_id=db.get_message_id("wamid.abc"))
    assert batch > 0


# ---------- migration of the new columns / table on an old database ----------

def test_an_old_database_gets_the_newest_columns_and_table(tmp_path, monkeypatch):
    """A database from before Batch 3: batches without meta, messages without buttons, no questions table."""
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
        CREATE TABLE batches (id INTEGER PRIMARY KEY AUTOINCREMENT,
            shop_id INTEGER NOT NULL REFERENCES shops(id),
            source TEXT NOT NULL CHECK (source IN ('voice', 'text')),
            message_id INTEGER REFERENCES messages(id), transcript TEXT, audio_path TEXT,
            raw_extraction TEXT, status TEXT NOT NULL DEFAULT 'active'
            CHECK (status IN ('active', 'undone')), created_at TEXT NOT NULL);
        CREATE TABLE entries (id INTEGER PRIMARY KEY AUTOINCREMENT,
            shop_id INTEGER NOT NULL REFERENCES shops(id),
            customer_id INTEGER REFERENCES customers(id),
            batch_id INTEGER REFERENCES batches(id),
            kind TEXT NOT NULL CHECK (kind IN ('credit', 'payment', 'sale')),
            amount INTEGER NOT NULL CHECK (amount > 0), due_date TEXT, note TEXT,
            status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'undone')),
            created_at TEXT NOT NULL);
        INSERT INTO shops (owner_phone, created_at) VALUES ('919876543210', 'x');
        INSERT INTO messages (wa_msg_id, phone, direction, body, created_at) VALUES ('w1', '1', 'in', 'old', 'x');
        """
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(db, "DB_PATH", old_path)
    db.init_db()

    with db.connect() as c:
        tables = {r["name"] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        batch_cols = {r["name"] for r in c.execute("PRAGMA table_info(batches)")}
        message_cols = {r["name"] for r in c.execute("PRAGMA table_info(messages)")}
        assert c.execute("SELECT body FROM messages WHERE wa_msg_id = 'w1'").fetchone()["body"] == "old"
    assert "pending_questions" in tables
    assert "meta" in batch_cols and "buttons" in message_cols

    # and everything new works on it
    batch = ledger.create_batch(1, "voice", meta={"ok": True})
    assert questions.create_question(1, batch, "confirm_entry", {}) > 0
    db.save_outgoing("sim.1", "1", "text", "hi", [{"id": "a", "title": "b"}])
    db.init_db()      # running it again changes nothing and does not fail
