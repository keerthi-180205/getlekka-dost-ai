"""
matching.py - which customer did the owner mean?   (Chunk 8)

The owner says "ಸುರೇಶ್". The shop may have no such customer, exactly one, or
several people who could be him. Guessing wrong puts someone's money on the
wrong person's account, so when we are unsure we ASK instead of guessing.

The rules, in order (resolve_customer):

  1. A customer with exactly this name exists        -> "exact"  (use them, no question)
  2. No exact match, but some customers COULD be him -> "ask"    (which one, or a new person?)
  3. Nobody is even close                            -> "new"    (create a new customer)

"Could be him" (could_be_same_person) means one of:
  - the first word is the same    "Suresh"  vs  "Suresh milk"  vs  "Suresh tailor"
  - the spelling is very close    "ಸುರೇಶ"  vs  "ಸುರೇಶ್"  (the same Kannada name written
                                  with and without the final ್)

Why an exact match never asks: if the shop has "Suresh" and "Suresh tailor"
and the owner says just "Suresh", he means "Suresh". Asking every time would
make him stop using the app. The receipt always shows the old and new
balance, so a wrong pick is easy to notice, and Undo is one tap.

This file only LOOKS at customers. It never creates or changes anything.
"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher

from app import ledger

# How alike two names must be (0 to 1) to count as the same person's spelling.
# Measured on real names: spelling variants of one name scored 0.86 or more
# ("ಸುರೇಶ"/"ಸುರೇಶ್" 0.91, "ಗೀತಾ"/"ಗೀತ" 0.86), while DIFFERENT people scored 0.80 or
# less ("ರಮೇಶ್"/"ಮಹೇಶ್" 0.80, "ಸುರೇಶ್"/"ರಮೇಶ್" 0.73). The gap is narrow, so this
# number is something to re-check with the accuracy test (Chunk 11).
NEAR_MATCH_RATIO = 0.85

# At most this many existing names are offered as buttons. WhatsApp allows 3
# buttons: two existing names + "a new person".
MAX_CANDIDATES = 2


@dataclass(frozen=True)
class Resolution:
    """
    The answer to "which customer is this?"

    status      "exact", "new" or "ask" (see the top of this file)
    name        exact: the name as stored.   new / ask: the name as the owner said it
    candidates  only for "ask": existing names that could be the same person,
                most recently used first, at most MAX_CANDIDATES
    truncated   True if more candidates existed than we show. The owner can then
                say the full name instead of tapping a button.
    """
    status: str
    name: str
    candidates: tuple[str, ...] = ()
    truncated: bool = False


def tidy_name(name: str) -> str:
    """The spoken name with Unicode normalized and spaces collapsed (case kept)."""
    return " ".join(unicodedata.normalize("NFC", name).split())


def could_be_same_person(key_a: str, key_b: str) -> bool:
    """
    Do these two cleaned names (ledger.clean_name) possibly belong to one person?
    Same name, same first word, or a spelling that is almost the same.
    """
    if not key_a or not key_b:
        return False
    if key_a == key_b:
        return True
    if key_a.split()[0] == key_b.split()[0]:
        return True
    return SequenceMatcher(None, key_a, key_b).ratio() >= NEAR_MATCH_RATIO


def find_candidates(shop_id: int, spoken_name: str) -> list[str]:
    """
    Existing customers who could be the person the owner named, EXCLUDING an
    exact match, most recently used first. No limit (resolve_customer applies
    MAX_CANDIDATES; a balance question can show them all).
    """
    key = ledger.clean_name(spoken_name)
    return [
        customer["name"]
        for customer in ledger.list_customers(shop_id)
        if customer["name_key"] != key and could_be_same_person(key, customer["name_key"])
    ]


def resolve_customer(shop_id: int, spoken_name: str) -> Resolution:
    """
    Decide which customer the owner means. See the top of this file for the
    rules. Raises ValueError for an empty name.
    """
    key = ledger.clean_name(spoken_name)
    if not key:
        raise ValueError("Customer name cannot be empty")

    for customer in ledger.list_customers(shop_id):
        if customer["name_key"] == key:
            return Resolution("exact", customer["name"])

    spoken = tidy_name(spoken_name)
    candidates = find_candidates(shop_id, spoken_name)
    if not candidates:
        return Resolution("new", spoken)
    return Resolution(
        "ask", spoken,
        candidates=tuple(candidates[:MAX_CANDIDATES]),
        truncated=len(candidates) > MAX_CANDIDATES,
    )
