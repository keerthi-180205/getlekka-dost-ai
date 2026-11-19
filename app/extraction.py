"""
extraction.py - reads the owner's words and turns them into checked entries.  (Chunk 7)

The flow, in one picture:

    transcript (text from speech.py)
        |
        v   prompts.py builds the instructions + examples + the owner's words
    AI engine  ("local" = Ollama on this laptop, "hosted" = Google Gemini)
        |   answers with a small JSON text
        v
    interpret()  <-- OUR CODE checks every value
        |
        v
    Extraction(intent="entries", entries=[credit Suresh 650 due 2026-11-20, ...])

THE RULE OF THIS FILE: the AI only READS. It never adds numbers, never works
out dates and never touches the ledger. Everything it says is untrusted until
interpret() has checked it:
    - amounts must be whole rupees above 0
    - kinds must be credit / payment / sale
    - credit and payment need a customer
    - "Friday" is turned into a real date by OUR code (parse_due)
    - a suspiciously large amount is flagged for the owner to confirm

This file does NOT save anything. It has no database code. Saving the checked
entries into the ledger happens in the WhatsApp wiring step (Chunk 9).

What can come back (Extraction.intent):
    "entries"  the owner recorded credit / payments / cash sales
    "balance"  the owner asked how much ONE customer owes
    "dues"     the owner asked who owes how much
    "undo"     the owner wants the last thing cancelled
    "different_person"  the owner says the last entry was for ANOTHER person with
               the same name as an existing customer ("ಇದು ಬೇರೆ ಸುರೇಶ್")
    "unclear"  we could not tell. Ask the owner again instead of guessing.

Settings (all optional, read from .env - see .env.example):
    EXTRACTION_ENGINE        local | hosted             (default: local)
    OLLAMA_URL               where Ollama runs          (default: http://localhost:11434)
    OLLAMA_MODEL             which local model          (default: llama3.1)
    EXTRACTION_HOSTED_MODEL  hosted model name          (default: gemini-2.5-flash,
                             check Google's current model list)
    GEMINI_API_KEY           same key as speech.py (hosted engine only)
    SPEECH_LANGUAGE          kn | hi | en               (shared with speech.py)

Privacy note: the hosted engine sends the transcript (customer names and
amounts) to Google. See the note in speech.py.

Try it by hand:
    python -m app.extraction "ಸುರೇಶ್ ಗೆ 650 ಸಾಲ, ಶುಕ್ರವಾರ ಕೊಡ್ತಾರೆ"
    python -m app.extraction "ಸುರೇಶ್ ಎಷ್ಟು ಬಾಕಿ ಇದೆ" --engine hosted
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import re
import time
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import httpx
from dotenv import load_dotenv

from app import prompts

load_dotenv()  # read .env so the settings above are available


class ExtractionError(Exception):
    """
    Anything that goes wrong while reading the owner's words (clear message inside).

    raw     the AI's raw reply, when there was one (kept for the accuracy log)
    engine  which engine was used ("local" / "hosted"), when known
    """

    def __init__(self, message: str, raw: str | None = None, engine: str | None = None):
        super().__init__(message)
        self.raw = raw
        self.engine = engine


# India has no daylight saving, so a fixed +5:30 offset is always right and
# needs no extra package. "Friday" must mean the shop's Friday, not the server's.
INDIA_TIME = timezone(timedelta(hours=5, minutes=30))

KINDS = ("credit", "payment", "sale")
INTENTS = ("entries", "balance", "dues", "undo", "different_person", "unclear")

# Above this, an amount is probably a mis-hearing (650 heard as 65000).
# The entry is NOT dropped; it is flagged so the owner can confirm it.
MAX_REASONABLE_AMOUNT = 100_000

MAX_TRANSCRIPT_CHARS = 5000   # a voice note longer than this is almost certainly a mistake
MAX_NOTE_CHARS = 200
MAX_DUE_DAYS = 366            # a due date further away than a year is not believable

# Warning codes an accepted entry can carry. Chunk 9 turns these into questions.
WARN_LARGE_AMOUNT = "large_amount"
WARN_DUE_NOT_UNDERSTOOD = "due_not_understood"
WARN_SALE_HAS_CUSTOMER = "sale_has_customer"


# ---------------------------------------------------------------------------
# The result types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ExtractedEntry:
    """
    One checked entry, ready for the ledger (Chunk 9 passes it to add_credit /
    add_payment / add_cash_sale).

    kind       "credit", "payment" or "sale"
    customer   the name as spoken; None for a cash sale
    amount     whole rupees, above 0
    due_date   'YYYY-MM-DD' (worked out by our code) or None. Credit only.
    note       what was bought, a few words, or None
    warnings   codes such as "large_amount". Empty means nothing to double-check.
    """
    kind: str
    customer: str | None
    amount: int
    due_date: str | None = None
    note: str | None = None
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class Rejected:
    """Something the AI returned that failed our checks, and why. Never saved."""
    raw: object
    reason: str   # "unknown_kind", "bad_amount", "missing_customer" or "not_an_object"


@dataclass(frozen=True)
class Extraction:
    """
    Everything we understood from one voice note.

    intent           see the top of this file
    entries          the checked entries (only when intent == "entries")
    customer         the customer asked about (only when intent is "balance" or
                     "different_person")
    rejected         parts of the note we could NOT use. Ask the owner about them.
    engine           "local" or "hosted"
    elapsed_seconds  how long the AI took (for the cost and speed report)
    raw_text         the AI's raw reply, kept as an audit trail (batches.raw_extraction)
    """
    intent: str
    entries: tuple[ExtractedEntry, ...] = ()
    customer: str | None = None
    rejected: tuple[Rejected, ...] = ()
    engine: str = ""
    elapsed_seconds: float = 0.0
    raw_text: str = ""

    @property
    def needs_attention(self) -> bool:
        """
        True when we must NOT save silently: the note was unclear, part of it
        was rejected, or an entry carries a warning. The caller asks the owner.
        """
        return (
            self.intent == "unclear"
            or bool(self.rejected)
            or any(entry.warnings for entry in self.entries)
        )


# ---------------------------------------------------------------------------
# Checking values (this is where "the AI never decides" is enforced)
# ---------------------------------------------------------------------------

def today_in_india() -> date:
    """Today's date for the shop (India time), whatever timezone the server uses."""
    return datetime.now(INDIA_TIME).date()


_WEEKDAYS = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
             "friday": 4, "saturday": 5, "sunday": 6}


def parse_due(text, today: date) -> tuple[str | None, bool]:
    """
    Turn the AI's due phrase into a real date. Returns (date_or_None, understood).

    Accepted phrases: today, tomorrow, day_after_tomorrow, a weekday name
    ("friday"), "in N days", or an explicit 'YYYY-MM-DD'.

    A weekday means its NEXT occurrence, never today: if today is Friday,
    "friday" is a week away (an owner who says "he'll pay on Friday" on a
    Friday means next Friday; "today" has its own word).

    `understood` is False when the phrase was something else, or a date in the
    past or more than a year ahead. Then the date is dropped (None) and the
    caller can warn. No due date at all (None / "") counts as understood.
    """
    if text is None:
        return None, True
    if not isinstance(text, str):
        return None, False

    value = text.strip().lower().replace("_", " ")
    if value in ("", "null", "none"):
        return None, True

    if value == "today":
        days = 0
    elif value == "tomorrow":
        days = 1
    elif value == "day after tomorrow":
        days = 2
    elif value in _WEEKDAYS:
        # 1..7 days ahead: the next time that weekday comes round, after today
        days = (_WEEKDAYS[value] - today.weekday() - 1) % 7 + 1
    elif (match := re.fullmatch(r"in (\d{1,3}) days?", value)):
        days = int(match.group(1))
    elif re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        try:
            days = (date.fromisoformat(value) - today).days
        except ValueError:      # e.g. 2026-13-45
            return None, False
    else:
        return None, False

    if days < 0 or days > MAX_DUE_DAYS:
        return None, False
    return (today + timedelta(days=days)).isoformat(), True


def _parse_amount(value) -> int | None:
    """
    A positive whole number of rupees, or None if the value is not one.

    Accepts: 650, 650.0, "650", "1,500", "₹650" and Kannada digits like "೬೫೦".
    Rejects: 0, negatives, 650.5, True/False, None, words, lists.
    """
    if isinstance(value, bool):          # True is an int in Python; never a rupee amount
        return None
    if isinstance(value, int):
        number = value
    elif isinstance(value, float):
        if not value.is_integer():
            return None
        number = int(value)
    elif isinstance(value, str):
        cleaned = value.strip().replace(",", "").replace("₹", "").strip()
        try:
            number = int(cleaned)        # Python's int() also reads Kannada digits
        except ValueError:
            return None
    else:
        return None
    return number if number > 0 else None


def _clean_text(value) -> str | None:
    """A tidy string (Unicode-normalized, single spaces) or None if empty / not text."""
    if not isinstance(value, str):
        return None
    cleaned = " ".join(unicodedata.normalize("NFC", value).split())
    return cleaned or None


def _check_entry(item, today: date) -> ExtractedEntry | Rejected:
    """Check ONE entry from the AI. Returns the checked entry, or why it was rejected."""
    if not isinstance(item, dict):
        return Rejected(item, "not_an_object")

    kind = item.get("kind")
    kind = kind.strip().lower() if isinstance(kind, str) else None
    if kind not in KINDS:                # includes the AI's own "unknown"
        return Rejected(item, "unknown_kind")

    amount = _parse_amount(item.get("amount"))
    if amount is None:
        return Rejected(item, "bad_amount")

    customer = _clean_text(item.get("customer"))
    warnings: list[str] = []
    if kind == "sale":
        if customer is not None:         # a cash sale has no customer; do not guess one
            warnings.append(WARN_SALE_HAS_CUSTOMER)
            customer = None
    elif customer is None:
        return Rejected(item, "missing_customer")

    due_date = None
    if kind == "credit":                 # only a credit can be "due"
        due_date, understood = parse_due(item.get("due"), today)
        if not understood:
            warnings.append(WARN_DUE_NOT_UNDERSTOOD)

    if amount > MAX_REASONABLE_AMOUNT:
        warnings.append(WARN_LARGE_AMOUNT)

    note = _clean_text(item.get("note"))
    return ExtractedEntry(
        kind=kind,
        customer=customer,
        amount=amount,
        due_date=due_date,
        note=note[:MAX_NOTE_CHARS] if note else None,
        warnings=tuple(warnings),
    )


def check_entry(item, today: date) -> ExtractedEntry | Rejected:
    """
    Check ONE entry dictionary (the same shape the AI returns for an entry).
    Public so pipeline.py can re-check an entry after the owner answers
    "credit or payment?": it adds the missing kind and runs the same checks.
    """
    return _check_entry(item, today)


def _load_json(raw_text: str) -> dict:
    """
    Pull the JSON object out of the AI's reply. Models sometimes wrap it in
    ```json fences or add a sentence around it, so we take everything from the
    first { to the last }.
    """
    if not isinstance(raw_text, str) or not raw_text.strip():
        raise ExtractionError("The AI returned an empty answer.", raw=raw_text)
    start, end = raw_text.find("{"), raw_text.rfind("}")
    if start == -1 or end <= start:
        raise ExtractionError("The AI's answer contains no JSON object.", raw=raw_text)
    try:
        data = json.loads(raw_text[start:end + 1])
    except json.JSONDecodeError as err:
        raise ExtractionError(f"The AI's answer is not valid JSON: {err}", raw=raw_text) from err
    if not isinstance(data, dict):
        raise ExtractionError("The AI's answer is not a JSON object.", raw=raw_text)
    return data


def interpret(raw_text: str, today: date) -> Extraction:
    """
    Check the AI's raw reply and turn it into an Extraction. Raises
    ExtractionError only when the reply is not usable JSON at all; anything
    wrong INSIDE valid JSON is handled by rejecting that part or by returning
    intent "unclear".

    Contradictory replies are treated as unclear on purpose. For example,
    intent "balance" together with a list of entries: we do not guess which
    half the AI meant, we ask the owner again.
    """
    data = _load_json(raw_text)

    intent = data.get("intent")
    intent = intent.strip().lower() if isinstance(intent, str) else None
    raw_entries = data.get("entries") if isinstance(data.get("entries"), list) else []
    customer = _clean_text(data.get("customer"))

    unclear = Extraction(intent="unclear", raw_text=raw_text)

    if intent == "entries":
        accepted, rejected = [], []
        for item in raw_entries:
            checked = _check_entry(item, today)
            (rejected if isinstance(checked, Rejected) else accepted).append(checked)
        if not accepted and not rejected:    # said "entries" but listed nothing
            return unclear
        return Extraction(intent="entries", entries=tuple(accepted),
                          rejected=tuple(rejected), raw_text=raw_text)

    if intent in ("balance", "different_person"):    # both need a name and no entries
        if raw_entries or customer is None:
            return unclear
        return Extraction(intent=intent, customer=customer, raw_text=raw_text)

    if intent in ("dues", "undo"):
        if raw_entries:
            return unclear
        return Extraction(intent=intent, raw_text=raw_text)

    return unclear                           # "unclear", missing or unknown intent


# ---------------------------------------------------------------------------
# Engines: each one sends the prompt to an AI and returns its raw reply
# ---------------------------------------------------------------------------

class ExtractionEngine:
    """The shape every engine follows: a `name`, and complete(prompt) -> raw reply text."""

    name = "base"

    def complete(self, prompt: str) -> str:
        raise NotImplementedError


class OllamaEngine(ExtractionEngine):
    """
    Talks to Ollama, which runs an AI model on this computer. Free and private.

    Needs Ollama running (`ollama serve`, or the Ollama app) and the model
    downloaded (`ollama pull llama3.1`). The first request after a while is
    slow because the model must be loaded into memory.

    temperature 0 = always give the most likely answer (not creative), which is
    what we want for reading. format "json" makes Ollama return valid JSON.
    """

    name = "local"

    def __init__(self, url: str | None = None, model: str | None = None,
                 timeout: float = 300.0, post=None):
        self.url = (url or os.getenv("OLLAMA_URL", "http://localhost:11434")).rstrip("/")
        self.model = model or os.getenv("OLLAMA_MODEL", "llama3.1")
        self.timeout = timeout
        self._post = post or httpx.post   # tests pass a fake here

    def complete(self, prompt: str) -> str:
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "format": "json",
            "options": {"temperature": 0},
        }
        try:
            response = self._post(f"{self.url}/api/chat", json=payload, timeout=self.timeout)
        except httpx.ConnectError as err:
            raise ExtractionError(
                f"Cannot reach Ollama at {self.url}. Is it running? Start it with: ollama serve"
            ) from err
        except httpx.TimeoutException as err:
            raise ExtractionError(
                f"Ollama did not answer within {self.timeout:.0f} seconds. "
                f"The model may be too big for this computer."
            ) from err

        if response.status_code != 200:
            detail = self._error_detail(response)
            hint = f" Download it with: ollama pull {self.model}" if response.status_code == 404 else ""
            raise ExtractionError(f"Ollama returned an error ({response.status_code}): {detail}.{hint}")
        try:
            return response.json()["message"]["content"]
        except (ValueError, KeyError, TypeError) as err:
            raise ExtractionError("Ollama sent a reply in an unexpected format.") from err

    @staticmethod
    def _error_detail(response) -> str:
        """Ollama explains errors as {"error": "..."}; fall back to a generic text."""
        try:
            return str(response.json().get("error", "no details"))
        except (ValueError, AttributeError):
            return "no details"


class HostedEngine(ExtractionEngine):
    """
    Sends the prompt to Google's Gemini model. Usually better at Kannada than a
    small local model, but it needs GEMINI_API_KEY and the transcript leaves
    this computer (see the privacy note at the top of this file).
    """

    name = "hosted"

    def __init__(self, api_key: str | None = None, model: str | None = None, client=None):
        self.api_key = api_key if api_key is not None else os.getenv("GEMINI_API_KEY", "")
        self.model = model or os.getenv("EXTRACTION_HOSTED_MODEL", "gemini-2.5-flash")
        self._client = client   # tests pass a fake client here

    def _get_client(self):
        if self._client is None:
            if not self.api_key:
                raise ExtractionError(
                    "The hosted engine needs GEMINI_API_KEY in your .env file "
                    "(create the key yourself; never paste it in a chat)."
                )
            try:
                from google import genai
            except ImportError as err:
                raise ExtractionError(
                    "The hosted engine needs google-genai. Install it with: "
                    "pip install google-genai"
                ) from err
            self._client = genai.Client(api_key=self.api_key)
        return self._client

    def complete(self, prompt: str) -> str:
        client = self._get_client()
        response = client.models.generate_content(
            model=self.model,
            contents=prompt,
            config={"response_mime_type": "application/json", "temperature": 0},
        )
        return response.text or ""   # text can be None when the service returns nothing


_ENGINE_CLASSES = {"local": OllamaEngine, "hosted": HostedEngine}

# Engines are kept after first use (one connection setup, one client).
_engines: dict[str, ExtractionEngine] = {}


def get_engine(name: str | None = None) -> ExtractionEngine:
    """
    Return the engine called `name` ("local" or "hosted"). With no name, use
    the EXTRACTION_ENGINE setting (default "local").
    """
    name = (name or os.getenv("EXTRACTION_ENGINE", "local")).strip().lower()
    if name not in _ENGINE_CLASSES:
        raise ExtractionError(
            f"Unknown extraction engine {name!r}. Choose one of: {', '.join(_ENGINE_CLASSES)}"
        )
    if name not in _engines:
        _engines[name] = _ENGINE_CLASSES[name]()
    return _engines[name]


# ---------------------------------------------------------------------------
# The one function the rest of the app calls
# ---------------------------------------------------------------------------

def extract(transcript: str, engine: str | ExtractionEngine | None = None,
            today: date | None = None, language: str | None = None) -> Extraction:
    """
    Read one transcript and return the checked Extraction.

    transcript  the text from speech.transcribe(...).text
    engine      "local" or "hosted", or an engine object. Default: the
                EXTRACTION_ENGINE setting.
    today       the date to resolve "Friday" against. Default: today in India.
                (Tests pass a fixed date so results never depend on the calendar.)
    language    "kn" (default: the SPEECH_LANGUAGE setting). Needs examples in
                prompts.EXAMPLES_BY_LANGUAGE.

    An empty transcript returns intent "unclear" WITHOUT calling the AI: it
    costs nothing and cannot make something up from silence.
    Raises ExtractionError if the engine fails or its reply is not usable JSON.
    """
    if not isinstance(transcript, str):
        raise ExtractionError("The transcript must be text.")
    text = transcript.strip()
    if len(text) > MAX_TRANSCRIPT_CHARS:
        raise ExtractionError(f"The transcript is too long ({len(text)} characters).")

    language = (language or os.getenv("SPEECH_LANGUAGE", "kn")).strip().lower()
    today = today or today_in_india()
    engine = engine if isinstance(engine, ExtractionEngine) else get_engine(engine)

    if not text:
        return Extraction(intent="unclear", engine=engine.name)

    try:
        prompt = prompts.build_extraction_prompt(text, today, language)
    except ValueError as err:
        raise ExtractionError(str(err)) from err

    started = time.perf_counter()
    try:
        raw_reply = engine.complete(prompt)
    except ExtractionError as err:
        err.engine = engine.name
        raise
    except Exception as err:  # engines can fail in many ways (no network, bad key, ...)
        raise ExtractionError(f"The {engine.name} engine failed: {err}", engine=engine.name) from err
    elapsed = time.perf_counter() - started

    try:
        result = interpret(raw_reply, today)
    except ExtractionError as err:
        err.engine = engine.name
        raise
    return dataclasses.replace(result, engine=engine.name, elapsed_seconds=round(elapsed, 2))


# ---------------------------------------------------------------------------
# Try it from the terminal:  python -m app.extraction "ಸುರೇಶ್ ಗೆ 650 ಸಾಲ"
# ---------------------------------------------------------------------------

def _describe(result: Extraction) -> list[str]:
    """Plain-text lines describing an Extraction, for the command line."""
    lines = [f"[{result.engine}, {result.elapsed_seconds}s] intent: {result.intent}"]
    if result.intent in ("balance", "different_person"):
        lines.append(f"    about: {result.customer}")
    for entry in result.entries:
        extras = []
        if entry.due_date:
            extras.append(f"due {entry.due_date}")
        if entry.note:
            extras.append(f"note: {entry.note}")
        if entry.warnings:
            extras.append(f"CHECK: {', '.join(entry.warnings)}")
        who = entry.customer or "(cash sale)"
        lines.append(f"    {entry.kind:<8} {who}  ₹{entry.amount}  {'  '.join(extras)}".rstrip())
    for item in result.rejected:
        lines.append(f"    REJECTED ({item.reason}): {item.raw}")
    if result.needs_attention:
        lines.append("    -> needs attention: ask the owner before saving")
    return lines


def main(argv: list[str] | None = None) -> int:
    """Read one transcript from the command line and print what we understood."""
    parser = argparse.ArgumentParser(description="Turn a transcript into checked ledger entries.")
    parser.add_argument("transcript", help="what the owner said, as text (put it in quotes)")
    parser.add_argument("--engine", choices=sorted(_ENGINE_CLASSES), help="default: EXTRACTION_ENGINE setting")
    parser.add_argument("--today", help="pretend today is this date (YYYY-MM-DD), to test due dates")
    args = parser.parse_args(argv)

    try:
        today = date.fromisoformat(args.today) if args.today else None
        result = extract(args.transcript, engine=args.engine, today=today)
    except ValueError:
        print(f"[FAILED] --today must look like 2026-11-20, got {args.today!r}")
        return 1
    except ExtractionError as err:
        print(f"[FAILED] {err}")
        if err.raw:
            print(f"    the AI said: {err.raw!r}")
        return 1
    print("\n".join(_describe(result)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
