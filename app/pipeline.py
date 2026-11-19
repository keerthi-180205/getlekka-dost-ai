"""
pipeline.py - the brain of GetLekka: from what the owner said to what we do.   (Chunk 9)

main.py hands over ONE thing the owner sent (a voice note, a text, or a button
tap). This file decides what to do and returns the replies to send. It knows
nothing about WhatsApp or the internet, so it is easy to test and read.

    voice note --speech.transcribe()--> text --extraction.extract()--> checked entries
                                                                          |
        +---------------------------------------------------------------+
        v
    for each entry:  which customer?  (matching.py)
        exact match, or a new name  --> save it now              (ledger.py)
        looks like someone else too --> ASK "which one?"         (questions.py)
        big amount / odd detail      --> ASK "is this right?"
        no credit/payment word       --> ASK "credit or payment?"
    then reply with a receipt (old balance -> new balance) and an Undo button

THE RULES THIS FILE FOLLOWS
  1. We never guess where money goes. If we are unsure, we ask and save nothing
     for that entry until the owner answers.
  2. Clear entries of a voice note are saved at once. Only the unsure ones wait.
  3. All money math is ledger.py's. This file only calls it.
  4. A failure never loses what the owner said: the transcript, audio path and
     the AI's raw answer are saved on a "batch" even when we could not understand it.
  5. Every reply is built by replies.py (Kannada), never written here.

Entry points (each returns a list of Reply to send, in order):
    handle_text(shop_id, text, ...)
    handle_voice(shop_id, audio_path, ...)
    handle_button(shop_id, button_id, ...)
"""
from __future__ import annotations

import dataclasses
import re
from datetime import date

from app import extraction, ledger, matching, questions, replies, speech
from app.extraction import ExtractedEntry, ExtractionError
from app.replies import Reply
from app.speech import SpeechError

# A message that is just one of these, while a "what shall I call him?" question
# is open, means "never mind".
CANCEL_WORDS = {"ಬೇಡ", "ಬೇಡಾ", "cancel", "no"}

MAX_NAME_CHARS = 40   # a longer message is not a name; treat it as a normal note
MAX_NAME_WORDS = 5

# How many options each kind of question has (checked before an answer is accepted).
# choose_customer is missing on purpose: its number of options varies per question.
OPTION_COUNT = {"confirm_entry": 2, "choose_kind": 2, "new_name": 1}

# Questions a bare "1" / "2" / "3" can answer. Not new_name: its answer is a name.
NUMBER_ANSWERABLE = ("choose_customer", "confirm_entry", "choose_kind")


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

def handle_text(shop_id: int, text: str, *, message_id: int | None = None,
                today: date | None = None) -> list[Reply]:
    """The owner typed a message."""
    return _handle_utterance(shop_id, text, source="text", message_id=message_id,
                             audio_path=None, stt=None, today=today)


def handle_voice(shop_id: int, audio_path: str, *, message_id: int | None = None,
                 today: date | None = None) -> list[Reply]:
    """The owner sent a voice note, already saved at `audio_path`."""
    try:
        transcript = speech.transcribe(audio_path)
    except SpeechError as err:
        print(f"[speech error] {err}")
        _log_failure(shop_id, "voice", message_id, audio_path, None, {"error": str(err)})
        return [Reply(replies.TECH_PROBLEM)]

    stt = {"engine": transcript.engine, "language": transcript.language,
           "seconds": transcript.elapsed_seconds}
    if transcript.is_empty:
        _log_failure(shop_id, "voice", message_id, audio_path, "", {"stt": stt, "error": "nothing heard"})
        return [Reply(replies.HEARD_NOTHING)]
    return _handle_utterance(shop_id, transcript.text, source="voice", message_id=message_id,
                             audio_path=audio_path, stt=stt, today=today)


def handle_button(shop_id: int, button_id: str, *, today: date | None = None) -> list[Reply]:
    """
    The owner tapped a button. Ids are made in replies.py:
        "undo:<batch id>"               cancel that voice note
        "q:<question id>:<option no.>"  an answer to a question
    """
    parts = (button_id or "").split(":")
    try:                    # only the number reading is guarded; real bugs below must not be hidden
        if len(parts) == 2 and parts[0] == "undo":
            batch_id = int(parts[1])
        elif len(parts) == 3 and parts[0] == "q":
            question_id, option = int(parts[1]), int(parts[2])
        else:
            return [Reply(replies.UNCLEAR)]
    except ValueError:      # the id was not made by us
        return [Reply(replies.UNCLEAR)]

    if parts[0] == "undo":
        result = ledger.undo_batch(shop_id, batch_id)
        return [replies.undone(result["entries"]) if result else Reply(replies.NOTHING_TO_UNDO)]

    question = questions.get_question(shop_id, question_id)
    if question is None or question["status"] != "open":
        return [Reply(replies.ALREADY_ANSWERED)]
    return _answer_question(shop_id, question, option, today)


# ---------------------------------------------------------------------------
# One thing the owner said (text, or what a voice note was turned into)
# ---------------------------------------------------------------------------

def _handle_utterance(shop_id, text, *, source, message_id, audio_path, stt, today) -> list[Reply]:
    text = (text or "").strip()
    if not text:
        return [Reply(replies.HEARD_NOTHING)]

    # A bare number answers the newest open multiple-choice question ("1" = first button).
    if text.isdigit() and 1 <= int(text) <= 9:
        question = questions.latest_open_question(shop_id, NUMBER_ANSWERABLE)
        if question is not None:
            return _answer_question(shop_id, question, int(text), today)

    # While "what shall I call him?" is open, a short message is the name.
    name_question = questions.latest_open_question(shop_id, ("new_name",))
    if name_question is not None and _looks_like_a_name(text):
        return _answer_new_name(shop_id, name_question, text, today)

    return _process(shop_id, text, source, message_id, audio_path, stt, today)


def _looks_like_a_name(text: str) -> bool:
    """Short, no digits: 'ಹಾಲು ಸುರೇಶ್' is a name; a whole sentence with an amount is not."""
    return (len(text) <= MAX_NAME_CHARS
            and len(text.split()) <= MAX_NAME_WORDS
            and not any(ch.isdigit() for ch in text))


def _process(shop_id, text, source, message_id, audio_path, stt, today) -> list[Reply]:
    """Understand a normal note and act on it."""
    try:
        result = extraction.extract(text, today=today)
    except ExtractionError as err:
        print(f"[extraction error] {err}")
        _log_failure(shop_id, source, message_id, audio_path, text,
                     {"stt": stt, "error": str(err), "llm_engine": err.engine}, raw=err.raw)
        return [Reply(replies.TECH_PROBLEM)]

    # The batch is this voice note. It stores the proof (transcript, audio, the AI's
    # raw answer) and groups the entries so one Undo can cancel them all.
    meta = {"stt": stt, "llm": {"engine": result.engine, "seconds": result.elapsed_seconds},
            "intent": result.intent, "needs_attention": result.needs_attention}
    batch_id = ledger.create_batch(shop_id, source, message_id, transcript=text,
                                   audio_path=audio_path, raw_extraction=result.raw_text, meta=meta)

    if result.intent == "entries":
        return _record_entries(shop_id, batch_id, result, today)
    if result.intent == "balance":
        return _answer_balance(shop_id, result.customer)
    if result.intent == "dues":
        return [replies.dues(ledger.get_all_dues(shop_id))]
    if result.intent == "undo":
        undone = ledger.undo_last_batch(shop_id)
        return [replies.undone(undone["entries"]) if undone else Reply(replies.NOTHING_TO_UNDO)]
    if result.intent == "different_person":
        return _ask_new_name(shop_id, result.customer)
    return [Reply(replies.UNCLEAR)]


def _log_failure(shop_id, source, message_id, audio_path, transcript, meta, raw=None) -> None:
    """Keep a record of a note we could not use, so nothing the owner said is lost."""
    ledger.create_batch(shop_id, source, message_id, transcript=transcript,
                        audio_path=audio_path, raw_extraction=raw, meta=meta)


# ---------------------------------------------------------------------------
# Recording entries: save what is clear, ask about what is not
# ---------------------------------------------------------------------------

def _entry_to_dict(entry: ExtractedEntry) -> dict:
    data = dataclasses.asdict(entry)
    data["warnings"] = list(entry.warnings)      # JSON has lists, not tuples
    return data


def _entry_from_dict(data: dict) -> ExtractedEntry:
    return ExtractedEntry(**{**data, "warnings": tuple(data.get("warnings", ()))})


def _entries_text(entries: list[ExtractedEntry]) -> str:
    """'₹650 ಸಾಲ, ₹100 ಪಾವತಿ' (used inside questions)."""
    return ", ".join(f"{replies.rupees(e.amount)} {replies.KIND_WORD[e.kind]}" for e in entries)


def _save(shop_id: int, batch_id: int, entry: ExtractedEntry) -> dict:
    """Write one entry into the ledger. Returns what the receipt needs."""
    if entry.kind == "sale":
        ledger.add_cash_sale(shop_id, entry.amount, note=entry.note, batch_id=batch_id)
        before = after = None
    else:
        before = ledger.get_balance(shop_id, entry.customer)
        if entry.kind == "credit":
            ledger.add_credit(shop_id, entry.customer, entry.amount, due_date=entry.due_date,
                              note=entry.note, batch_id=batch_id)
        else:
            ledger.add_payment(shop_id, entry.customer, entry.amount, note=entry.note, batch_id=batch_id)
        after = ledger.get_balance(shop_id, entry.customer)
    return {"kind": entry.kind, "customer": entry.customer, "amount": entry.amount,
            "due_date": entry.due_date, "before": before, "after": after}


def _route_entries(shop_id, batch_id, entries, *, skip_matching=False) -> tuple[list[dict], list[Reply]]:
    """
    Decide, for each entry, between SAVE NOW and ASK. Returns
    (what was saved, the questions to send).

    skip_matching=True is used after the owner has already told us which
    customer he meant, so we do not ask about the name a second time.

    IMPORTANT: every name is matched against the book AS IT WAS BEFORE THIS
    NOTE, and only then do we save anything. If we saved as we went, an owner
    who says "Suresh milk 100, Suresh tailor 200" in one breath would be asked
    "which Suresh?" about the second name, because the first was just saved.
    Naming two people in one note shows he knows they are different.
    """
    # Pass 1: look up every name (nothing is saved yet)
    looked_up = []
    for entry in entries:
        resolution = None
        if entry.kind != "sale" and not skip_matching:
            resolution = matching.resolve_customer(shop_id, entry.customer)
        looked_up.append((entry, resolution))

    # Pass 2: save the clear ones, collect the unsure ones
    saved: list[dict] = []
    asked: list[Reply] = []
    unsure_names: dict[str, dict] = {}     # one question per spoken name, even for several entries

    for entry, resolution in looked_up:
        if resolution is not None:
            if resolution.status == "ask":
                group = unsure_names.setdefault(
                    ledger.clean_name(entry.customer), {"resolution": resolution, "entries": []})
                group["entries"].append(entry)
                continue
            entry = dataclasses.replace(entry, customer=resolution.name)   # the stored / tidied name

        if entry.warnings:
            asked.append(_ask_confirm(shop_id, batch_id, entry))
        else:
            saved.append(_save(shop_id, batch_id, entry))

    for group in unsure_names.values():
        asked.append(_ask_which_customer(shop_id, batch_id, group["resolution"], group["entries"]))
    return saved, asked


def _record_entries(shop_id, batch_id, result, today) -> list[Reply]:
    """A voice note full of entries: save the clear ones, ask about the rest."""
    saved, asked = _route_entries(shop_id, batch_id, list(result.entries))

    not_understood: list[tuple[object, str]] = []
    for rejected in result.rejected:
        question = _ask_kind(shop_id, batch_id, rejected, today) if rejected.reason == "unknown_kind" else None
        if question is not None:
            asked.append(question)
        else:
            not_understood.append((rejected.raw, rejected.reason))

    out: list[Reply] = []
    if saved:
        out.append(replies.receipt(saved, batch_id))
    if not_understood:
        out.append(Reply(replies.not_understood(not_understood)))
    out.extend(asked)
    return out or [Reply(replies.UNCLEAR)]


# ---------------------------------------------------------------------------
# Asking
# ---------------------------------------------------------------------------

def _ask_confirm(shop_id, batch_id, entry: ExtractedEntry) -> Reply:
    """An entry with a warning (big amount, ...) waits for a yes."""
    qid = questions.create_question(shop_id, batch_id, "confirm_entry", {"entry": _entry_to_dict(entry)})
    return replies.ask_confirm(qid, replies.describe_entry(entry.kind, entry.customer, entry.amount),
                               list(entry.warnings))


def _ask_which_customer(shop_id, batch_id, resolution, entries) -> Reply:
    """'Which Suresh?' The options are the existing names, then the spoken name as a new person."""
    options = [*resolution.candidates, resolution.name]
    qid = questions.create_question(shop_id, batch_id, "choose_customer", {
        "spoken": resolution.name,
        "options": options,
        "entries": [_entry_to_dict(e) for e in entries],
    })
    return replies.ask_which_customer(qid, resolution.name, _entries_text(entries),
                                      list(resolution.candidates), resolution.truncated)


def _ask_kind(shop_id, batch_id, rejected, today) -> Reply | None:
    """
    A name and an amount but no 'credit' or 'payment' word. If the rest of the
    entry is sound, ask which it is. Returns None if it is not worth asking
    (the amount or name was also bad).
    """
    if not isinstance(rejected.raw, dict):
        return None
    probe = extraction.check_entry({**rejected.raw, "kind": "credit"}, today)
    if not isinstance(probe, ExtractedEntry):
        return None
    qid = questions.create_question(shop_id, batch_id, "choose_kind", {"raw": rejected.raw})
    return replies.ask_kind(qid, probe.customer, probe.amount)


def _ask_new_name(shop_id, spoken_name) -> list[Reply]:
    """The owner says the last entry was for a DIFFERENT person with this name."""
    batch_id = ledger.last_active_batch(shop_id)
    if batch_id is None:
        return [Reply(replies.NO_RECENT_ENTRY)]
    key = ledger.clean_name(spoken_name)
    rows = [e for e in ledger.get_batch_entries(shop_id, batch_id)
            if e["customer"] and e["kind"] in ("credit", "payment")
            and matching.could_be_same_person(key, ledger.clean_name(e["customer"]))]
    if not rows:
        return [Reply(replies.NO_RECENT_ENTRY)]

    qid = questions.create_question(shop_id, batch_id, "new_name", {
        "old_name": rows[0]["customer"], "entry_ids": [e["id"] for e in rows]})
    entries_text = ", ".join(f"{replies.rupees(e['amount'])} {replies.KIND_WORD[e['kind']]}" for e in rows)
    return [replies.ask_new_name(qid, rows[0]["customer"], entries_text)]


# ---------------------------------------------------------------------------
# Answering
# ---------------------------------------------------------------------------

def _answer_question(shop_id, question, option, today) -> list[Reply]:
    """The owner picked option number `option` (1, 2, 3) of an open question."""
    payload = question["payload"]
    option_count = len(payload["options"]) if question["kind"] == "choose_customer" else OPTION_COUNT[question["kind"]]
    if not 1 <= option <= option_count:
        return [Reply(replies.UNCLEAR)]

    batch_id = question["batch_id"]
    if not ledger.is_batch_active(shop_id, batch_id):
        return [Reply(replies.NOTE_WAS_CANCELLED)]
    # Only ONE caller wins this; a double tap does nothing the second time.
    if not questions.claim_question(shop_id, question["id"]):
        return [Reply(replies.ALREADY_ANSWERED)]

    kind = question["kind"]
    if kind == "choose_customer":
        chosen = payload["options"][option - 1]
        entries = [dataclasses.replace(_entry_from_dict(d), customer=chosen) for d in payload["entries"]]
        return _finish(shop_id, batch_id, entries, skip_matching=True)

    if kind == "confirm_entry":
        if option == 2:
            return [Reply(replies.NOT_WRITTEN)]
        entry = dataclasses.replace(_entry_from_dict(payload["entry"]), warnings=())   # the owner confirmed
        return _finish(shop_id, batch_id, [entry], skip_matching=True)

    if kind == "choose_kind":
        chosen_kind = ("credit", "payment")[option - 1]
        entry = extraction.check_entry({**payload["raw"], "kind": chosen_kind}, today)
        if not isinstance(entry, ExtractedEntry):
            return [Reply(replies.not_understood([(payload["raw"], entry.reason)]))]
        return _finish(shop_id, batch_id, [entry])

    return [Reply(replies.LEFT_AS_IS)]          # "new_name": option 1 is "never mind"


def _finish(shop_id, batch_id, entries, *, skip_matching=False) -> list[Reply]:
    """Save the entries an answer settled (some may still need a question of their own)."""
    saved, asked = _route_entries(shop_id, batch_id, entries, skip_matching=skip_matching)
    return ([replies.receipt(saved, batch_id)] if saved else []) + asked


def _answer_new_name(shop_id, question, text, today) -> list[Reply]:
    """The owner told us what to call the 'different' person (or said never mind)."""
    if text.strip().lower() in CANCEL_WORDS:
        questions.claim_question(shop_id, question["id"])
        return [Reply(replies.LEFT_AS_IS)]

    payload, batch_id = question["payload"], question["batch_id"]
    new_name = matching.tidy_name(text)
    if ledger.clean_name(new_name) == ledger.clean_name(payload["old_name"]):
        return [Reply(replies.NAME_TAKEN_BY_SAME)]            # the question stays open
    if not ledger.is_batch_active(shop_id, batch_id):
        return [Reply(replies.NOTE_WAS_CANCELLED)]
    if not questions.claim_question(shop_id, question["id"]):
        return [Reply(replies.ALREADY_ANSWERED)]

    # Move = cancel the entries under the old name, then write them again under the
    # new one. Nothing is deleted (golden rule), the old rows stay as 'undone'.
    wanted = set(payload["entry_ids"])
    rows = [e for e in ledger.get_batch_entries(shop_id, batch_id) if e["id"] in wanted]
    if not rows:
        return [Reply(replies.NO_RECENT_ENTRY)]
    ledger.undo_entries(shop_id, [e["id"] for e in rows])
    saved = [_save(shop_id, batch_id, ExtractedEntry(e["kind"], new_name, e["amount"], e["due_date"], e["note"]))
             for e in rows]
    return [replies.moved(payload["old_name"], new_name, saved, batch_id)]


# ---------------------------------------------------------------------------
# Questions the owner asks about the book
# ---------------------------------------------------------------------------

def _answer_balance(shop_id: int, spoken_name: str) -> list[Reply]:
    """'How much does Suresh owe?' If the name fits several people, show all of them."""
    resolution = matching.resolve_customer(shop_id, spoken_name)
    if resolution.status == "exact":
        return [Reply(replies.balance(resolution.name, ledger.get_balance(shop_id, resolution.name)))]
    if resolution.status == "ask":
        names = matching.find_candidates(shop_id, spoken_name)
        return [replies.balances([(name, ledger.get_balance(shop_id, name)) for name in names])]
    return [replies.no_such_customer(resolution.name)]
