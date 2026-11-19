"""
db.py - the database: one SQLite file (dukaandost.db) with six tables.

  messages   every WhatsApp message in and out (skip duplicates, count costs)
  shops      one row per shop owner
  customers  the people each shop gives credit to
  batches    one row per owner submission (e.g. one voice note). Keeps the
             transcript and audio path as proof, and groups the entries that
             one voice note created so a single Undo can cancel them all
  entries    every money event: credit, payment or cash sale
  pending_questions  things the bot asked the owner and is waiting on
             ("which Suresh?", "is ₹65,000 right?"). Open until answered.

Balances are NEVER stored. They are always calculated from `entries`,
so they can never get out of sync.
"""
import json
import sqlite3
from datetime import datetime, timezone

DB_PATH = "dukaandost.db"  # the whole database is this one file (tests change this)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect() -> sqlite3.Connection:
    """Open the database.
    - row_factory = sqlite3.Row lets you read columns by name: row["amount"]
    - foreign_keys = ON makes SQLite reject an entry for a shop that doesn't exist
    Use it like:   with db.connect() as conn: conn.execute(...)
    (the `with` block saves your changes automatically when it ends)
    """
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    """Create all tables if they don't exist yet. Safe to run many times."""
    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                wa_msg_id   TEXT UNIQUE,          -- WhatsApp's own ID for the message
                phone       TEXT NOT NULL,        -- the shop owner's number
                direction   TEXT NOT NULL,        -- 'in' or 'out'
                msg_type    TEXT,                 -- 'text', 'audio', ...
                body        TEXT,                 -- the words (if any)
                buttons     TEXT,                 -- JSON list of reply buttons we sent (if any)
                created_at  TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS shops (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_phone  TEXT NOT NULL UNIQUE, -- e.g. '919876543210'
                shop_name    TEXT,
                upi_id       TEXT,
                language     TEXT,
                created_at   TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS customers (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                shop_id     INTEGER NOT NULL REFERENCES shops(id),
                name        TEXT NOT NULL,         -- as first written: 'Suresh'
                name_key    TEXT NOT NULL,         -- cleaned for matching: 'suresh'
                phone       TEXT,                  -- optional
                created_at  TEXT NOT NULL,
                UNIQUE (shop_id, name_key)         -- one 'suresh' per shop
            );

            CREATE TABLE IF NOT EXISTS batches (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                shop_id         INTEGER NOT NULL REFERENCES shops(id),
                source          TEXT NOT NULL CHECK (source IN ('voice', 'text')),
                message_id      INTEGER REFERENCES messages(id), -- the WhatsApp message, if any
                transcript      TEXT,                -- what speech-to-text heard
                audio_path      TEXT,                -- the saved voice note (our proof)
                raw_extraction  TEXT,                -- the AI's raw answer, kept for audit
                meta            TEXT,                -- JSON: which engines ran, how long, what we understood
                status          TEXT NOT NULL DEFAULT 'active'
                                CHECK (status IN ('active', 'undone')),
                created_at      TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS entries (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                shop_id      INTEGER NOT NULL REFERENCES shops(id),
                customer_id  INTEGER REFERENCES customers(id),  -- empty for a cash sale
                batch_id     INTEGER REFERENCES batches(id),    -- empty if not from a batch
                kind         TEXT NOT NULL CHECK (kind IN ('credit', 'payment', 'sale')),
                amount       INTEGER NOT NULL CHECK (amount > 0), -- whole rupees
                due_date     TEXT,                  -- 'YYYY-MM-DD' or empty
                note         TEXT,
                status       TEXT NOT NULL DEFAULT 'active'
                             CHECK (status IN ('active', 'undone')),
                created_at   TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS pending_questions (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                shop_id      INTEGER NOT NULL REFERENCES shops(id),
                batch_id     INTEGER REFERENCES batches(id),   -- the voice note it came from
                kind         TEXT NOT NULL CHECK (kind IN
                             ('choose_customer', 'confirm_entry', 'choose_kind', 'new_name')),
                payload      TEXT NOT NULL,         -- JSON: what to do once it is answered
                status       TEXT NOT NULL DEFAULT 'open'
                             CHECK (status IN ('open', 'answered', 'cancelled')),
                created_at   TEXT NOT NULL,
                answered_at  TEXT
            );
            """
        )
        _migrate(conn)


def _add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    """ALTER TABLE ... ADD COLUMN, but only when the column is not there yet."""
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _migrate(conn: sqlite3.Connection) -> None:
    """
    Bring an OLDER database up to date. Runs every time init_db runs and only
    ADDS things - it never deletes or changes existing data.

    Why it is needed: CREATE TABLE IF NOT EXISTS does nothing when the table
    already exists, so a database made before a column existed would never
    get it. We check for each newer column and add it if it is missing.
    (Whole new tables need no help: CREATE TABLE IF NOT EXISTS makes them.)
    """
    _add_column_if_missing(conn, "entries", "batch_id", "INTEGER REFERENCES batches(id)")
    _add_column_if_missing(conn, "batches", "meta", "TEXT")
    _add_column_if_missing(conn, "messages", "buttons", "TEXT")

    # Created after the columns are guaranteed to exist. Speeds up "all entries
    # of this batch" (whole-note Undo) and "open questions of this shop".
    conn.execute("CREATE INDEX IF NOT EXISTS idx_entries_batch ON entries(batch_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pending_shop ON pending_questions(shop_id, status)")


# ---------- messages ----------

def save_incoming(wa_msg_id: str, phone: str, msg_type: str, body: str | None) -> bool:
    """
    Save an incoming message.
    Returns True if it is NEW, False if we already had it (a duplicate retry).
    """
    with connect() as conn:
        cursor = conn.execute(
            "INSERT OR IGNORE INTO messages (wa_msg_id, phone, direction, msg_type, body, created_at) "
            "VALUES (?, ?, 'in', ?, ?, ?)",
            (wa_msg_id, phone, msg_type, body, _now()),
        )
        # rowcount is 1 if a row was inserted, 0 if the ID already existed
        return cursor.rowcount == 1


def save_outgoing(wa_msg_id: str | None, phone: str, msg_type: str, body: str,
                  buttons: list[dict] | None = None) -> None:
    """
    Save a message we sent, so we can count messages (= cost) later.
    `buttons` (a list like [{"id": "undo:3", "title": "..."}]) is saved as JSON
    so simulate.py can show the buttons in practice mode.
    """
    with connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO messages (wa_msg_id, phone, direction, msg_type, body, buttons, created_at) "
            "VALUES (?, ?, 'out', ?, ?, ?, ?)",
            (wa_msg_id, phone, msg_type, body, json.dumps(buttons, ensure_ascii=False) if buttons else None, _now()),
        )


def get_message_id(wa_msg_id: str) -> int | None:
    """The database id of a saved message (used to link a batch to its WhatsApp message)."""
    with connect() as conn:
        row = conn.execute("SELECT id FROM messages WHERE wa_msg_id = ?", (wa_msg_id,)).fetchone()
    return row["id"] if row else None
