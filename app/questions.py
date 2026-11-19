"""
questions.py - things the bot asked the owner and is waiting for an answer to.  (Chunk 8)

Examples of a pending question:
  "Which Suresh?"            kind "choose_customer"
  "₹65,000 for Suresh. Right?"   kind "confirm_entry"
  "Credit or payment?"       kind "choose_kind"
  "What should I call him?"  kind "new_name"

How it works with the rest:
  - While a question is open, the entry it is about is NOT in the ledger yet.
    (Clear entries from the same voice note are saved at once; only the unsure
    ones wait.)
  - The question's `payload` (a dictionary, saved as JSON) holds everything
    needed to finish the job once the owner answers.
  - Undoing the voice note cancels its open questions (see ledger._cancel_batch).

This file is only the storage: create, read, and mark answered. WHAT to do
with an answer is decided in pipeline.py.
"""
from __future__ import annotations

import json

from app import db

KINDS = ("choose_customer", "confirm_entry", "choose_kind", "new_name")


def _row_to_dict(row) -> dict:
    return {
        "id": row["id"],
        "shop_id": row["shop_id"],
        "batch_id": row["batch_id"],
        "kind": row["kind"],
        "payload": json.loads(row["payload"]),
        "status": row["status"],
    }


def create_question(shop_id: int, batch_id: int | None, kind: str, payload: dict) -> int:
    """Open a new question and return its id. `payload` must be JSON-friendly."""
    if kind not in KINDS:
        raise ValueError(f"Unknown question kind {kind!r}. Choose one of: {', '.join(KINDS)}")
    with db.connect() as conn:
        cursor = conn.execute(
            "INSERT INTO pending_questions (shop_id, batch_id, kind, payload, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (shop_id, batch_id, kind, json.dumps(payload, ensure_ascii=False), db._now()),
        )
        return cursor.lastrowid


def get_question(shop_id: int, question_id: int) -> dict | None:
    """
    One question as {"id", "shop_id", "batch_id", "kind", "payload", "status"},
    or None. The shop is part of the lookup so a shop can never read or answer
    another shop's question.
    """
    with db.connect() as conn:
        row = conn.execute(
            "SELECT * FROM pending_questions WHERE id = ? AND shop_id = ?",
            (question_id, shop_id),
        ).fetchone()
    return _row_to_dict(row) if row else None


def open_questions(shop_id: int, kinds: tuple[str, ...] | None = None) -> list[dict]:
    """The shop's open questions, oldest first. Optionally only certain kinds."""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM pending_questions WHERE shop_id = ? AND status = 'open' ORDER BY id",
            (shop_id,),
        ).fetchall()
    questions = [_row_to_dict(row) for row in rows]
    return [q for q in questions if kinds is None or q["kind"] in kinds]


def latest_open_question(shop_id: int, kinds: tuple[str, ...] | None = None) -> dict | None:
    """The NEWEST open question (the one the owner is most likely answering), or None."""
    questions = open_questions(shop_id, kinds)
    return questions[-1] if questions else None


def claim_question(shop_id: int, question_id: int) -> bool:
    """
    Mark an open question as answered. Returns True only for the caller that
    actually changed it from 'open'. If the owner taps a button twice, or two
    taps arrive at once, exactly ONE caller gets True and acts on the answer;
    the others get False and do nothing. That is what stops an entry from
    being saved twice.
    """
    with db.connect() as conn:
        cursor = conn.execute(
            "UPDATE pending_questions SET status = 'answered', answered_at = ? "
            "WHERE id = ? AND shop_id = ? AND status = 'open'",
            (db._now(), question_id, shop_id),
        )
        return cursor.rowcount == 1
