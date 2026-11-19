"""
replies.py - everything GetLekka SAYS to the owner, in Kannada.   (Chunk 9)

All the wording lives in this one file on purpose: you can read and fix every
sentence here without touching any logic. pipeline.py decides WHAT to say;
this file decides HOW it is said.

!! FOR REVIEW: this Kannada was written as natural shopkeeper talk, but a native
speaker (you) must read every sentence and change anything that sounds wrong,
too formal, or unclear. Words to check first:
    ಸಾಲ  = credit (goods taken, pay later)       ಪಾವತಿ = payment (money paid back)
    ಬಾಕಿ = balance still owed                    ನಗದು ಮಾರಾಟ = cash sale

What the pieces are:
    Button   one tap-able choice under a message (WhatsApp allows at most 3,
             and each title at most 20 characters)
    Reply    the text to send, plus its buttons (if any)

Button ids tell pipeline.py what a tap means:
    "undo:<batch id>"                cancel that voice note
    "q:<question id>:<option no.>"   an answer to a pending question
"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from datetime import date

BUTTON_TITLE_MAX = 20     # WhatsApp's limit for a reply button's title
BUTTONS_MAX = 3           # WhatsApp's limit for reply buttons under one message
BODY_WITH_BUTTONS_MAX = 1024   # WhatsApp's limit for the text above buttons
LIST_LIMIT = 20           # longest list of customers we print in one message


# ---------------------------------------------------------------------------
# Building blocks
# ---------------------------------------------------------------------------

def _shorten(text: str, limit: int) -> str:
    """
    Cut `text` to at most `limit` characters without breaking a Kannada letter.
    A Kannada letter is a base character plus signs that attach to it; cutting
    between them leaves a different, wrong letter. So we step back to a safe place.
    """
    if len(text) <= limit:
        return text
    cut = limit - 1                       # room for the "…"
    while cut > 0 and (
        unicodedata.category(text[cut]) in ("Mn", "Mc")   # next char is a sign for the one before
        or text[cut - 1] == "್"                      # last kept char is a dangling virama
        or text[cut] in "‌‍"
    ):
        cut -= 1
    return text[:cut] + "…"


@dataclass(frozen=True)
class Button:
    id: str
    title: str


@dataclass(frozen=True)
class Reply:
    text: str
    buttons: tuple[Button, ...] = ()


def button(button_id: str, title: str) -> Button:
    """A button whose title is guaranteed to fit WhatsApp's limit."""
    return Button(button_id, _shorten(title, BUTTON_TITLE_MAX))


def rupees(amount: int) -> str:
    """₹1,50,000: Indian digit grouping (last three digits, then pairs)."""
    digits = str(abs(amount))
    if len(digits) > 3:
        head, tail = digits[:-3], digits[-3:]
        pairs = []
        while len(head) > 2:
            pairs.insert(0, head[-2:])
            head = head[:-2]
        digits = ",".join([head, *pairs, tail])
    return f"{'-' if amount < 0 else ''}₹{digits}"


_MONTHS = ["ಜನವರಿ", "ಫೆಬ್ರವರಿ", "ಮಾರ್ಚ್", "ಏಪ್ರಿಲ್", "ಮೇ", "ಜೂನ್",
           "ಜುಲೈ", "ಆಗಸ್ಟ್", "ಸೆಪ್ಟೆಂಬರ್", "ಅಕ್ಟೋಬರ್", "ನವೆಂಬರ್", "ಡಿಸೆಂಬರ್"]


def kannada_date(iso_date: str) -> str:
    """'2026-11-27' -> '27 ನವೆಂಬರ್'"""
    day = date.fromisoformat(iso_date)
    return f"{day.day} {_MONTHS[day.month - 1]}"


KIND_WORD = {"credit": "ಸಾಲ", "payment": "ಪಾವತಿ"}


def owed(amount: int) -> str:
    """A balance in words: ₹450, or (if we owe THEM) '₹100 ವಾಪಸ್ ಕೊಡಬೇಕು'."""
    return rupees(amount) if amount >= 0 else f"{rupees(-amount)} ವಾಪಸ್ ಕೊಡಬೇಕು"


def describe_entry(kind: str, customer: str | None, amount: int) -> str:
    """'ಸುರೇಶ್ — ₹650 ಸಾಲ'  or  'ನಗದು ಮಾರಾಟ — ₹1,500'"""
    if kind == "sale":
        return f"ನಗದು ಮಾರಾಟ — {rupees(amount)}"
    return f"{customer} — {rupees(amount)} {KIND_WORD[kind]}"


# ---------------------------------------------------------------------------
# Fixed sentences
# ---------------------------------------------------------------------------

HELP_EXAMPLES = (
    "ಹೀಗೆ ಹೇಳಿ:\n"
    "• “ಸುರೇಶ್ ಗೆ 650 ಸಾಲ”\n"
    "• “ರಮೇಶ್ 200 ಕೊಟ್ರು”\n"
    "• “ಸುರೇಶ್ ಎಷ್ಟು ಬಾಕಿ ಇದೆ”\n"
    "• “ಯಾರ್ಯಾರು ಎಷ್ಟೆಷ್ಟು ಬಾಕಿ ಕೊಡಬೇಕು”"
)
UNCLEAR = f"ಕ್ಷಮಿಸಿ, ಅರ್ಥವಾಗಲಿಲ್ಲ 🙏\n{HELP_EXAMPLES}"
HEARD_NOTHING = "ಏನೂ ಕೇಳಿಸಲಿಲ್ಲ 🙏 ಮತ್ತೊಮ್ಮೆ ಹೇಳಿ."
TECH_PROBLEM = "ಕ್ಷಮಿಸಿ, ಏನೋ ತೊಂದರೆ ಆಯಿತು. ಸ್ವಲ್ಪ ಸಮಯದ ನಂತರ ಮತ್ತೆ ಪ್ರಯತ್ನಿಸಿ."
UNSUPPORTED = "ಈಗ ನನಗೆ ವಾಯ್ಸ್ ಮೆಸೇಜ್ ಮತ್ತು ಟೆಕ್ಸ್ಟ್ ಮಾತ್ರ ಅರ್ಥವಾಗುತ್ತದೆ."
ALREADY_ANSWERED = "ಈ ಪ್ರಶ್ನೆಗೆ ಈಗಾಗಲೇ ಉತ್ತರ ಕೊಟ್ಟಿದ್ದೀರಿ, ಅಥವಾ ಅದು ರದ್ದಾಗಿದೆ."
NOT_WRITTEN = "ಸರಿ, ಬರೆದಿಲ್ಲ."
LEFT_AS_IS = "ಸರಿ, ಹಾಗೆಯೇ ಇರಲಿ."
NOTHING_TO_UNDO = "ರದ್ದು ಮಾಡಲು ಏನೂ ಇಲ್ಲ."
NO_DUES = "ಯಾರಿಗೂ ಬಾಕಿ ಇಲ್ಲ 🎉"
NO_RECENT_ENTRY = "ಬೇರೆ ಹೆಸರಿಗೆ ಬದಲಿಸಲು ಇತ್ತೀಚಿನ ಬರಹ ಸಿಗಲಿಲ್ಲ."
NOTE_WAS_CANCELLED = "ಆ ಬರಹ ರದ್ದಾಗಿದೆ, ಆದ್ದರಿಂದ ಇದನ್ನು ಬರೆಯಲಿಲ್ಲ."
NAME_TAKEN_BY_SAME = "ಇದೇ ಹೆಸರು ಈಗಾಗಲೇ ಇದೆ. ಬೇರೆ ಹೆಸರು ಹೇಳಿ, ಉದಾ: “ಹಾಲು ಸುರೇಶ್”."
SAVED_HEADER = "✅ ಬರೆದುಕೊಂಡಿದ್ದೇನೆ"
UNDONE_HEADER = "↩️ ರದ್ದು ಮಾಡಿದ್ದೇನೆ:"
DUES_HEADER = "📒 ಬಾಕಿ ಪಟ್ಟಿ"
PRESS_BELOW = "ಕೆಳಗೆ ಒತ್ತಿ 👇"

UNDO_BUTTON = "↩️ ರದ್ದು ಮಾಡಿ"
NEW_CUSTOMER_BUTTON = "ಹೊಸ ಗ್ರಾಹಕ"
YES_BUTTON = "ಹೌದು, ಬರೆಯಿರಿ"
NO_BUTTON = "ಬೇಡ"

# What to tell the owner about each warning an entry can carry
WARNING_TEXT = {
    "large_amount": "ಇದು ದೊಡ್ಡ ಮೊತ್ತ.",
    "due_not_understood": "ಕೊಡುವ ದಿನ ಅರ್ಥವಾಗಲಿಲ್ಲ (ದಿನಾಂಕ ಇಲ್ಲದೆ ಬರೆಯುತ್ತೇನೆ).",
    "sale_has_customer": "ನಗದು ಮಾರಾಟಕ್ಕೆ ಹೆಸರು ಬರೆಯುವುದಿಲ್ಲ.",
}

# What to tell the owner about a part of the note we could not use
REJECT_TEXT = {
    "bad_amount": "ಮೊತ್ತ ಅರ್ಥವಾಗಲಿಲ್ಲ",
    "missing_customer": "ಹೆಸರು ಅರ್ಥವಾಗಲಿಲ್ಲ",
    "unknown_kind": "ಸಾಲವೋ ಪಾವತಿಯೋ ಗೊತ್ತಾಗಲಿಲ್ಲ",
    "not_an_object": "ಅರ್ಥವಾಗಲಿಲ್ಲ",
}


# ---------------------------------------------------------------------------
# Receipts and answers
# ---------------------------------------------------------------------------

def receipt(saved: list[dict], batch_id: int, extra_lines: list[str] | None = None) -> Reply:
    """
    "I wrote it down" with one line per saved entry and an Undo button.

    Each item of `saved`: {"kind", "customer", "amount", "due_date", "before", "after"}
    (before / after = the customer's balance around that entry; None for a cash sale).
    """
    lines = [SAVED_HEADER]
    for item in saved:
        line = f"• {describe_entry(item['kind'], item['customer'], item['amount'])}"
        if item["before"] is not None:
            line += f" (ಬಾಕಿ {owed(item['before'])} → {owed(item['after'])})"
        if item.get("due_date"):
            line += f", ಕೊಡುವ ದಿನ: {kannada_date(item['due_date'])}"
        lines.append(line)
    lines.extend(extra_lines or [])
    return Reply("\n".join(lines), (button(f"undo:{batch_id}", UNDO_BUTTON),))


def undone(entries: list[dict]) -> Reply:
    """What was cancelled. `entries` come from ledger.undo_* ({"kind", "amount", "customer"})."""
    if not entries:
        return Reply(NOTHING_TO_UNDO)
    lines = [UNDONE_HEADER]
    lines += [f"• {describe_entry(e['kind'], e['customer'], e['amount'])}" for e in entries]
    return Reply("\n".join(lines))


def balance(name: str, amount: int) -> str:
    if amount > 0:
        return f"{name}: ಬಾಕಿ {rupees(amount)}"
    if amount == 0:
        return f"{name}: ಬಾಕಿ ಇಲ್ಲ"
    return f"{name}: ನೀವು {rupees(-amount)} ವಾಪಸ್ ಕೊಡಬೇಕು"


def balances(items: list[tuple[str, int]]) -> Reply:
    """Several customers' balances, one per line (used when a name matches more than one person)."""
    return Reply("\n".join(balance(name, amount) for name, amount in items))


def no_such_customer(name: str) -> Reply:
    return Reply(f"“{name}” ಎಂಬ ಗ್ರಾಹಕರು ನನ್ನ ಪಟ್ಟಿಯಲ್ಲಿ ಇಲ್ಲ.")


def dues(items: list[tuple[str, int]]) -> Reply:
    """The 'who owes me' list, biggest first, with a total."""
    if not items:
        return Reply(NO_DUES)
    lines = [DUES_HEADER]
    for number, (name, amount) in enumerate(items[:LIST_LIMIT], start=1):
        lines.append(f"{number}. {name} — {rupees(amount)}")
    if len(items) > LIST_LIMIT:
        lines.append(f"… ಮತ್ತು ಇನ್ನೂ {len(items) - LIST_LIMIT} ಜನ")
    lines.append(f"ಒಟ್ಟು ಬಾಕಿ: {rupees(sum(amount for _, amount in items))} ({len(items)} ಜನ)")
    return Reply("\n".join(lines))


def not_understood(rejected: list[tuple[object, str]]) -> str:
    """
    The parts of a note we could not use. `rejected` is a list of
    (what the AI returned, reason code). Shown so the owner can say them again.
    """
    lines = ["ಈ ಭಾಗ ಅರ್ಥವಾಗಲಿಲ್ಲ, ಮತ್ತೊಮ್ಮೆ ಹೇಳಿ:"]
    for raw, reason in rejected:
        if isinstance(raw, dict):
            parts = [str(raw[key]) for key in ("customer", "amount") if raw.get(key) is not None]
            shown = _shorten(" ".join(parts), 40) if parts else "…"
        else:
            shown = "…"
        lines.append(f"• {shown} ({REJECT_TEXT.get(reason, REJECT_TEXT['not_an_object'])})")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Questions (each carries buttons; ids are "q:<question id>:<option number>")
# ---------------------------------------------------------------------------

def _option_buttons(question_id: int, titles: list[str]) -> tuple[Button, ...]:
    return tuple(button(f"q:{question_id}:{number}", title) for number, title in enumerate(titles, start=1))


def ask_which_customer(question_id: int, spoken: str, entries_text: str,
                       candidates: list[str], truncated: bool) -> Reply:
    """'Which Suresh?': the existing names as buttons, plus 'new customer'."""
    text = f"“{spoken}” ({entries_text}) — ಯಾರಿಗೆ ಬರೆಯಲಿ? ಕೆಳಗೆ ಒಂದನ್ನು ಒತ್ತಿ."
    if truncated:
        text += "\nಬೇರೆಯವರಾದರೆ ಪೂರ್ಣ ಹೆಸರು ಹೇಳಿ."
    return Reply(text, _option_buttons(question_id, [*candidates, NEW_CUSTOMER_BUTTON]))


def ask_confirm(question_id: int, entry_text: str, warnings: list[str]) -> Reply:
    """'Is this right?' for an entry that carries a warning (big amount, ...)."""
    reasons = [WARNING_TEXT[w] for w in warnings if w in WARNING_TEXT]
    text = "\n".join([entry_text, *reasons, "ಸರಿಯೇ?"])
    return Reply(text, _option_buttons(question_id, [YES_BUTTON, NO_BUTTON]))


def ask_kind(question_id: int, customer: str, amount: int) -> Reply:
    """'Credit or payment?' when the owner gave a name and an amount but no verb."""
    return Reply(f"{customer} — {rupees(amount)}: ಸಾಲವೋ, ಪಾವತಿಯೋ?",
                 _option_buttons(question_id, [KIND_WORD["credit"], KIND_WORD["payment"]]))


def ask_new_name(question_id: int, old_name: str, entries_text: str) -> Reply:
    """'What should I call this other person?' (the "different Suresh" repair)."""
    text = (f"ಸರಿ, ಇದು ಬೇರೆ “{old_name}” ({entries_text}). ಇವರನ್ನು ಏನೆಂದು ಬರೆಯಲಿ?\n"
            f"ಹೆಸರು ಹೇಳಿ ಅಥವಾ ಬರೆಯಿರಿ, ಉದಾ: “ಹಾಲು {old_name}”.")
    return Reply(text, _option_buttons(question_id, [NO_BUTTON]))


def moved(old_name: str, new_name: str, saved: list[dict], batch_id: int) -> Reply:
    """After a 'different person' fix: the entries now sit under the new name."""
    base = receipt(saved, batch_id,
                   extra_lines=[f"(“{old_name}” ಬದಲು “{new_name}” ಗೆ ಬರೆದಿದ್ದೇನೆ)"])
    return base
