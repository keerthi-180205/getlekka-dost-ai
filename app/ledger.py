"""
ledger.py - the heart of GetLekka: the credit book itself.

It records credit, payments and cash sales, calculates balances, and undoes
entries. Every other file asks this one to do anything with money.

Golden rules:
  1. Only THIS file does money math. The AI never calculates balances.
  2. Amounts are whole rupees (int). Never float.
  3. Never delete rows. Undo only changes status to 'undone'.
  4. Balances are calculated from `entries` every time, never stored.
  5. Every query is limited to one shop (shop_id), so shops never see each other's data.

Sections: shops and customers, recording money, reading balances, undo,
then helpers for reading customers and batches.

Run the tests:   python -m pytest -q
"""
import json
import unicodedata
from datetime import date

from app import db

# Invisible characters that Kannada (and other Indic) text often carries.
# They change how a word is stored but not how it looks, so we drop them
# before comparing names. (ZWSP, ZWNJ, ZWJ, BOM)
_INVISIBLE_CHARS = dict.fromkeys(map(ord, "\u200b\u200c\u200d\ufeff"))


# ---------------------------------------------------------------------------
# Helper (already written for you)
# ---------------------------------------------------------------------------

def clean_name(name: str) -> str:
    """
    Turn a name into a matching key, so 'Suresh', ' suresh ', 'SURESH'
    all count as the same person.

    Kannada-safe (GetLekka decision, Nov 2026): the same Kannada word can be
    stored as different byte sequences that look identical on screen. So we
    also (1) normalize Unicode to NFC and (2) remove invisible joiner
    characters. Speech-to-text output is a common source of these.

    Limit: this does NOT merge genuinely different spellings, such as
    'ಸುರೇಶ' and 'ಸುರೇಶ್' (with and without the final virama). If those
    duplicates turn out to be common, we add fuzzy matching later.
    """
    name = unicodedata.normalize("NFC", name).translate(_INVISIBLE_CHARS)
    return " ".join(name.strip().lower().split())


def _check_amount(amount) -> None:
    """Stop bad amounts early (already written for you)."""
    if not isinstance(amount, int) or isinstance(amount, bool) or amount <= 0:
        raise ValueError(f"Amount must be a positive whole number of rupees, got {amount!r}")


# ---------------------------------------------------------------------------
# 1. Shops and customers
# ---------------------------------------------------------------------------

def get_or_create_shop(owner_phone: str) -> int:
    """
    Return the id of the shop for this phone number.
    If no shop has this number yet, create one and return the new id.

    Example:
        get_or_create_shop("919876543210")  -> 1   (created)
        get_or_create_shop("919876543210")  -> 1   (same shop again)
    """
    # Why INSERT OR IGNORE first, then SELECT (instead of SELECT, then INSERT):
    # two messages from a brand-new owner can arrive at the same moment. With
    # SELECT-then-INSERT both could see "no shop" and the second INSERT would
    # crash on the UNIQUE rule. INSERT OR IGNORE quietly does nothing if the
    # shop already exists, so the SELECT after it always finds exactly one row.
    with db.connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO shops (owner_phone, created_at) VALUES (?, ?)",
            (owner_phone, db._now()),
        )
        row = conn.execute(
            "SELECT id FROM shops WHERE owner_phone = ?", (owner_phone,)
        ).fetchone()
        return row["id"]


def get_or_create_customer(shop_id: int, name: str) -> int:
    """
    Return the id of this customer in this shop. Create them if new.
    Matching uses clean_name(), so 'Suresh' and ' suresh ' are one person.
    Keep the name as first written in the `name` column.

    Raise ValueError if the name is empty after cleaning.
    """
    key = clean_name(name)
    if not key:
        raise ValueError("Customer name cannot be empty")

    # Same race-safe pattern as get_or_create_shop. UNIQUE (shop_id, name_key)
    # in the table guarantees one customer per cleaned name per shop.
    with db.connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO customers (shop_id, name, name_key, created_at) "
            "VALUES (?, ?, ?, ?)",
            (shop_id, name.strip(), key, db._now()),
        )
        row = conn.execute(
            "SELECT id FROM customers WHERE shop_id = ? AND name_key = ?",
            (shop_id, key),
        ).fetchone()
        return row["id"]


# ---------------------------------------------------------------------------
# 2. Recording money
# ---------------------------------------------------------------------------

def _check_due_date(due_date: str | None) -> None:
    """
    Accept None, or a real calendar date written 'YYYY-MM-DD'.

    Why strict: the AI will later hand us dates it worked out from words like
    "Friday". Code (not the AI) turns those into real dates, and this check
    makes sure nothing like "friday" or "2026-13-45" ever reaches the database.
    """
    if due_date is None:
        return
    try:
        date.fromisoformat(due_date)
    except (TypeError, ValueError):
        raise ValueError(f"Due date must be a real date like 2026-11-14, got {due_date!r}")


def create_batch(shop_id: int, source: str, message_id: int | None = None,
                 transcript: str | None = None, audio_path: str | None = None,
                 raw_extraction: str | None = None, meta: dict | None = None) -> int:
    """
    Open a new batch: ONE owner submission, such as one voice note that may
    contain several entries ("Suresh 650, Ramesh paid 200, ...").
    Return the new batch id, then pass it as `batch_id=` to add_credit,
    add_payment or add_cash_sale for each entry in that submission.

    Why batches exist:
      - One Undo can cancel everything the voice note created (undo_last_batch).
      - We keep the transcript, the saved audio and the AI's raw answer as
        proof, so a disputed entry can always be traced back to what was said.

    source          'voice' or 'text'
    message_id      id of the row in the `messages` table, if there is one
    transcript      what speech-to-text heard
    audio_path      where the voice note file is saved
    raw_extraction  the AI's raw answer, kept for audit
    meta            a small dictionary saved as JSON: which engines ran, how long
                    they took, what we understood. Used by usage.py and for debugging.
    """
    if source not in ("voice", "text"):
        raise ValueError(f"source must be 'voice' or 'text', got {source!r}")
    with db.connect() as conn:
        cursor = conn.execute(
            "INSERT INTO batches (shop_id, source, message_id, transcript, audio_path, "
            "raw_extraction, meta, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (shop_id, source, message_id, transcript, audio_path, raw_extraction,
             json.dumps(meta, ensure_ascii=False) if meta else None, db._now()),
        )
        return cursor.lastrowid


def _check_batch(shop_id: int, batch_id: int | None) -> None:
    """
    If an entry is being added to a batch, make sure that is allowed: the batch
    must exist, belong to THIS shop, and still be active (adding money to a
    batch that was already undone makes no sense). None means "no batch" and
    is always fine.
    """
    if batch_id is None:
        return
    with db.connect() as conn:
        row = conn.execute(
            "SELECT status FROM batches WHERE id = ? AND shop_id = ?",
            (batch_id, shop_id),
        ).fetchone()
    if row is None:
        raise ValueError(f"Batch {batch_id!r} does not exist in this shop")
    if row["status"] != "active":
        raise ValueError(f"Batch {batch_id!r} was undone; cannot add entries to it")


def _insert_entry(shop_id: int, customer_id: int | None, kind: str, amount: int,
                  due_date: str | None, note: str | None,
                  batch_id: int | None = None) -> int:
    """
    The ONE place that writes a row into `entries` (credit, payment and sale
    all go through here). Callers must validate the inputs first.
    Returns the new entry's id. New entries start as status='active'.
    """
    with db.connect() as conn:
        cursor = conn.execute(
            "INSERT INTO entries (shop_id, customer_id, batch_id, kind, amount, due_date, "
            "note, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (shop_id, customer_id, batch_id, kind, amount, due_date, note, db._now()),
        )
        return cursor.lastrowid


def add_credit(shop_id: int, customer_name: str, amount: int,
               due_date: str | None = None, note: str | None = None,
               batch_id: int | None = None) -> int:
    """
    Customer took goods and will pay later (udhaar).
    Save an entry with kind='credit'. Return the new entry id.
    Pass `batch_id` (from create_batch) to group it with the other entries of
    the same voice note.

    Example: add_credit(1, "Suresh", 650, due_date="2026-11-14")
    """
    # Order matters: validate everything BEFORE touching the database, so a
    # rejected request never leaves a half-created customer behind.
    _check_amount(amount)
    _check_due_date(due_date)
    _check_batch(shop_id, batch_id)
    customer_id = get_or_create_customer(shop_id, customer_name)
    return _insert_entry(shop_id, customer_id, "credit", amount, due_date, note, batch_id)


def add_payment(shop_id: int, customer_name: str, amount: int,
                note: str | None = None, batch_id: int | None = None) -> int:
    """
    Customer paid back money. Save an entry with kind='payment'.
    Return the new entry id. `batch_id` groups it with its voice note.
    (A customer may pay more than they owe - that's allowed; their
     balance just goes below zero, meaning the shop owes them.)
    """
    # Same as add_credit, but kind='payment' and no due_date (a payment
    # is money coming IN, so there is nothing to be "due").
    _check_amount(amount)
    _check_batch(shop_id, batch_id)
    customer_id = get_or_create_customer(shop_id, customer_name)
    return _insert_entry(shop_id, customer_id, "payment", amount, None, note, batch_id)


def add_cash_sale(shop_id: int, amount: int, note: str | None = None,
                  batch_id: int | None = None) -> int:
    """
    A normal cash sale, no customer. kind='sale', customer_id stays empty (None).
    Return the new entry id. `batch_id` groups it with its voice note.
    """
    _check_amount(amount)
    _check_batch(shop_id, batch_id)
    return _insert_entry(shop_id, None, "sale", amount, None, note, batch_id)


# ---------------------------------------------------------------------------
# 3. Reading balances
# ---------------------------------------------------------------------------

# THE balance rule, written once (golden rule 4: balances are calculated, never
# stored). Credits count as plus, payments as minus, cash sales as nothing.
# Used inside a SELECT by both get_balance and get_all_dues. Callers must add
# "status = 'active'" so undone entries are ignored.
_BALANCE_SUM = (
    "SUM(CASE WHEN kind = 'credit'  THEN amount "
    "         WHEN kind = 'payment' THEN -amount "
    "         ELSE 0 END)"
)

def get_balance(shop_id: int, customer_name: str) -> int:
    """
    How much this customer owes right now:
        (sum of ACTIVE credits) - (sum of ACTIVE payments)
    Undone entries do not count.
    A customer who doesn't exist yet owes 0 (and must NOT be created here).

    Example: credit 650, credit 450, payment 200  -> 900
    """
    key = clean_name(customer_name)
    with db.connect() as conn:
        # Look the customer up only. Never create one here: asking for a
        # balance must not change the book.
        customer = conn.execute(
            "SELECT id FROM customers WHERE shop_id = ? AND name_key = ?",
            (shop_id, key),
        ).fetchone()
        if customer is None:
            return 0

        # shop_id is checked on the entries too, as a second lock so one
        # shop can never read another shop's money.
        row = conn.execute(
            f"SELECT {_BALANCE_SUM} FROM entries "
            "WHERE shop_id = ? AND customer_id = ? AND status = 'active'",
            (shop_id, customer["id"]),
        ).fetchone()
        return row[0] or 0  # SUM gives None when there are no rows


def get_all_dues(shop_id: int) -> list[tuple[str, int]]:
    """
    Everyone who owes money in this shop, biggest amount first.
    Only customers whose balance is MORE than 0.

    Example: [("Suresh", 900), ("Kumar", 300)]
    """
    # One query for the whole shop instead of one query per customer:
    #   GROUP BY c.id   -> one result row per customer
    #   HAVING balance  -> keep only customers who owe money (> 0). HAVING is
    #                      WHERE for grouped results, applied after the SUM.
    #   ORDER BY        -> biggest first; equal amounts sorted by name so the
    #                      order is always the same (predictable for the owner).
    # Customers with no active entries have no rows to group, so they never appear.
    with db.connect() as conn:
        rows = conn.execute(
            f"SELECT c.name AS name, {_BALANCE_SUM} AS balance "
            "FROM customers c JOIN entries e ON e.customer_id = c.id "
            "WHERE c.shop_id = ? AND e.shop_id = ? AND e.status = 'active' "
            "GROUP BY c.id "
            "HAVING balance > 0 "
            "ORDER BY balance DESC, c.name",
            (shop_id, shop_id),
        ).fetchall()
    return [(row["name"], row["balance"]) for row in rows]


# ---------------------------------------------------------------------------
# 4. Undo
# ---------------------------------------------------------------------------

def _newest_active_entry(conn, shop_id: int):
    """
    The newest entry of THIS shop that is still active, or None.
    Shared by undo_last and undo_last_batch so the rule lives in one place.

    Ordering by id (not by time) is safe because ids only ever go up.
    LEFT JOIN keeps cash sales: they have no customer, so `customer` is None.
    """
    return conn.execute(
        "SELECT e.id, e.kind, e.amount, e.batch_id, c.name AS customer "
        "FROM entries e LEFT JOIN customers c ON c.id = e.customer_id "
        "WHERE e.shop_id = ? AND e.status = 'active' "
        "ORDER BY e.id DESC LIMIT 1",
        (shop_id,),
    ).fetchone()


def _entry_summary(row) -> dict:
    """The small dict that undo functions hand back for one cancelled entry."""
    return {
        "id": row["id"],
        "kind": row["kind"],
        "amount": row["amount"],
        "customer": row["customer"],
    }


def undo_last(shop_id: int) -> dict | None:
    """
    Cancel this shop's most recent ACTIVE entry: set its status to 'undone'.
    Never DELETE it - we keep history.

    This is the SINGLE-ENTRY tool. It ignores batches. The owner-facing Undo
    (the button under a receipt) is undo_last_batch, which cancels a whole
    voice note at once.

    Return what was undone, like:
        {"id": 7, "kind": "credit", "amount": 650, "customer": "Suresh"}
    ("customer" is None for a cash sale.)
    Return None if there is nothing to undo.
    """
    with db.connect() as conn:
        # BEGIN IMMEDIATE takes the database's write lock NOW, before we look.
        # Without it, two undo requests at the same moment could both find the
        # same "latest" entry. With it, the second request waits, then finds
        # the NEXT entry. (The `with` block commits when it ends.)
        conn.execute("BEGIN IMMEDIATE")

        row = _newest_active_entry(conn, shop_id)
        if row is None:
            return None  # nothing left to undo

        # Strike it out, never delete it (golden rule 3). shop_id is repeated
        # here as a second lock against touching another shop's entry.
        conn.execute(
            "UPDATE entries SET status = 'undone' WHERE id = ? AND shop_id = ?",
            (row["id"], shop_id),
        )
        return _entry_summary(row)


def _cancel_batch(conn, shop_id: int, batch_id: int) -> list[dict]:
    """
    Cancel everything in one batch, inside the caller's transaction:
      - every still-active entry becomes 'undone' (returned, oldest first)
      - the batch itself becomes 'undone'
      - its still-open questions are cancelled (nothing to ask about a note
        that no longer exists)
    Shared by undo_last_batch and undo_batch so the rule lives in one place.
    """
    # Only the entries still active: if some were already cancelled one by one,
    # they stay as they are and are not reported.
    rows = conn.execute(
        "SELECT e.id, e.kind, e.amount, c.name AS customer "
        "FROM entries e LEFT JOIN customers c ON c.id = e.customer_id "
        "WHERE e.shop_id = ? AND e.batch_id = ? AND e.status = 'active' "
        "ORDER BY e.id",
        (shop_id, batch_id),
    ).fetchall()
    conn.executemany(
        "UPDATE entries SET status = 'undone' WHERE id = ? AND shop_id = ?",
        [(row["id"], shop_id) for row in rows],
    )
    conn.execute(
        "UPDATE batches SET status = 'undone' WHERE id = ? AND shop_id = ?",
        (batch_id, shop_id),
    )
    conn.execute(
        "UPDATE pending_questions SET status = 'cancelled' "
        "WHERE batch_id = ? AND shop_id = ? AND status = 'open'",
        (batch_id, shop_id),
    )
    return [_entry_summary(row) for row in rows]


def undo_last_batch(shop_id: int) -> dict | None:
    """
    The owner's spoken "undo". Cancel the shop's most recent submission as a whole.

    Look at the newest ACTIVE entry:
      - it belongs to a batch (a voice note)  -> cancel EVERY active entry of
        that batch, mark the batch 'undone', and cancel its open questions
      - it has no batch (a single typed entry) -> cancel just that entry
    Nothing is ever deleted; entries and batches are only marked 'undone'.

    Return None if there is nothing to undo, otherwise:
        {"batch_id": 3,                      # None when it was a single entry
         "entries": [{"id": 7, "kind": "credit", "amount": 650, "customer": "Suresh"},
                     {"id": 8, "kind": "payment", "amount": 200, "customer": "Ramesh"}]}
    """
    with db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")   # same write lock as undo_last: see there

        newest = _newest_active_entry(conn, shop_id)
        if newest is None:
            return None

        batch_id = newest["batch_id"]
        if batch_id is None:
            conn.execute(
                "UPDATE entries SET status = 'undone' WHERE id = ? AND shop_id = ?",
                (newest["id"], shop_id),
            )
            return {"batch_id": None, "entries": [_entry_summary(newest)]}
        return {"batch_id": batch_id, "entries": _cancel_batch(conn, shop_id, batch_id)}


def undo_batch(shop_id: int, batch_id: int) -> dict | None:
    """
    Cancel ONE specific voice note: what the Undo button under a receipt does.
    (undo_last_batch cancels "the newest"; a button tapped hours later must
    cancel the note it was printed under, not whatever is newest by then.)

    Return None if that batch does not exist in this shop or was already
    undone (so tapping Undo twice does nothing the second time). Otherwise
    the same shape as undo_last_batch. "entries" may be empty if its entries
    had all been cancelled one by one already.
    """
    with db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT status FROM batches WHERE id = ? AND shop_id = ?", (batch_id, shop_id)
        ).fetchone()
        if row is None or row["status"] != "active":
            return None
        return {"batch_id": batch_id, "entries": _cancel_batch(conn, shop_id, batch_id)}


def undo_entries(shop_id: int, entry_ids: list[int]) -> list[dict]:
    """
    Cancel exactly these entries (used to MOVE an entry to a different
    customer: cancel it here, add it again there). Entries that are not
    this shop's, or are already undone, are skipped silently.
    Returns what was cancelled, oldest first, in the undo_last format.
    The batch is not touched; it stays active.
    """
    if not entry_ids:
        return []
    marks = ",".join("?" * len(entry_ids))
    with db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            "SELECT e.id, e.kind, e.amount, c.name AS customer "
            "FROM entries e LEFT JOIN customers c ON c.id = e.customer_id "
            f"WHERE e.shop_id = ? AND e.status = 'active' AND e.id IN ({marks}) "
            "ORDER BY e.id",
            (shop_id, *entry_ids),
        ).fetchall()
        conn.executemany(
            "UPDATE entries SET status = 'undone' WHERE id = ? AND shop_id = ?",
            [(row["id"], shop_id) for row in rows],
        )
        return [_entry_summary(row) for row in rows]


# ---------------------------------------------------------------------------
# 5. Reading customers and batches (used by matching.py and pipeline.py)
# ---------------------------------------------------------------------------

def list_customers(shop_id: int) -> list[dict]:
    """
    Every customer of this shop, most recently used first:
        [{"name": "Suresh", "name_key": "suresh"}, ...]
    "Recently used" = the newest entry (even a cancelled one) with that customer;
    customers with no entries come last. When we must ask the owner "which
    Suresh?", the recent ones are the likeliest.
    """
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT c.name, c.name_key FROM customers c "
            "LEFT JOIN entries e ON e.customer_id = c.id "
            "WHERE c.shop_id = ? GROUP BY c.id "
            "ORDER BY COALESCE(MAX(e.id), 0) DESC, c.name",
            (shop_id,),
        ).fetchall()
    return [{"name": row["name"], "name_key": row["name_key"]} for row in rows]


def get_batch_entries(shop_id: int, batch_id: int, only_active: bool = True) -> list[dict]:
    """
    The entries of one batch, oldest first, with everything needed to show or
    re-create them:
        {"id", "kind", "amount", "customer", "due_date", "note", "status"}
    """
    status_filter = "AND e.status = 'active' " if only_active else ""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT e.id, e.kind, e.amount, c.name AS customer, e.due_date, e.note, e.status "
            "FROM entries e LEFT JOIN customers c ON c.id = e.customer_id "
            f"WHERE e.shop_id = ? AND e.batch_id = ? {status_filter}"
            "ORDER BY e.id",
            (shop_id, batch_id),
        ).fetchall()
    return [dict(row) for row in rows]


def is_batch_active(shop_id: int, batch_id: int | None) -> bool:
    """True if this batch exists in this shop and has not been undone."""
    if batch_id is None:
        return False
    with db.connect() as conn:
        row = conn.execute(
            "SELECT status FROM batches WHERE id = ? AND shop_id = ?", (batch_id, shop_id)
        ).fetchone()
    return row is not None and row["status"] == "active"


def last_active_batch(shop_id: int) -> int | None:
    """The batch of the shop's newest active entry, or None (no entries / entry has no batch)."""
    with db.connect() as conn:
        row = _newest_active_entry(conn, shop_id)
    return row["batch_id"] if row else None
