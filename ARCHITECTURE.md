# GetLekka: architecture and reading guide

A WhatsApp assistant that keeps a small shop's credit book (udhaar / saala / baaki).
The owner speaks a **Kannada voice note**; GetLekka records credit and payments,
answers "who owes what", and asks when it is not sure. Prototype scope: **Kannada voice only**.

> Status: all the prototype code is written and tested with stand-in engines
> (743 tests). **No real speech model or AI has run on real Kannada yet**, so accuracy is unknown.
> Section 6 lists what is still open.

## 1. The flow

```
owner's voice note ──► Meta ──► main.py (webhook door)
                                   │ saves the message (skips duplicates), downloads + stores the audio
                                   ▼
                              pipeline.py  (the brain)
        speech.py ──► text ──► extraction.py + prompts.py ──► checked entries
                                   │
              matching.py: which customer?   questions.py: ask when unsure
                                   │
                              ledger.py  (ALL money math; SQLite via db.py)
                                   │
                              replies.py  (every sentence, in Kannada)
                                   ▼
                    main.py sends text + buttons ──► Meta ──► owner's phone
```

## 2. Golden rules (never break these)

1. **Only `ledger.py` does money math.** The AI only *reads* words; code checks every number and date.
2. **Never guess where money goes.** Unsure means ask; an unsure entry is not saved until answered.
3. **Never delete.** Undo marks entries/batches `undone`; history stays.
4. **Balances are calculated every time**, never stored.
5. **Everything is per shop** (`shop_id` on every query) and the owner's phone number *is* the shop.
6. **Nothing the owner said is lost**: transcript, audio path and the AI's raw answer are saved on a *batch*, even when we could not understand the note.

## 3. Files (suggested reading order)

| # | File | What it is |
|---|---|---|
| 1 | `app/db.py` | The 6 tables and the automatic upgrade of old databases |
| 2 | `app/ledger.py` | Shops, customers, credit/payment/sale, balances, dues, undo (single entry, whole note, one note) |
| 3 | `app/speech.py` | Voice note to text. Local (faster-whisper) or hosted (Gemini) behind one switch |
| 4 | `app/prompts.py` | The instructions and Kannada examples given to the AI (edit freely) |
| 5 | `app/extraction.py` | Text to checked entries: amounts, kinds, due dates ("Friday" worked out by code), warnings |
| 6 | `app/matching.py` | Exact name, new name, or "ask which one" (same first word, or near spelling) |
| 7 | `app/questions.py` | Pending questions: create, read, and claim (a double tap answers once) |
| 8 | `app/pipeline.py` | The brain: decides save / ask / answer and returns the replies |
| 9 | `app/replies.py` | All Kannada wording, buttons, WhatsApp limits (**review the Kannada here**) |
| 10 | `app/main.py`, `app/whatsapp.py`, `app/audio_store.py` | The door: receive, download voice, store audio, send text and buttons |
| 11 | `app/evaluate.py` + `eval/kannada_cases.json` | The accuracy and cost report on 54 Kannada sentences |
| 12 | `app/usage.py` | How owners really use it (notes per day, entries per note, undo, questions) |
| 13 | `simulate.py` | Chat with the server from a terminal: text, `/voice file`, `/tap N` |

Each test file matches a module (`tests/test_<module>.py`). Read the tests of a
function before its code; they show what it must do.

## 4. The database (6 tables)

`messages` (every message in/out, with buttons) · `shops` · `customers` ·
`batches` (one voice note: transcript, audio path, raw AI answer, meta, status) ·
`entries` (credit / payment / sale, each pointing to a batch) ·
`pending_questions` (what we asked and are waiting on).

## 5. How to run things

```bash
python -m pytest -q                                   # all tests
python -m app.speech note.ogg --engine hosted         # try speech on one recording
python -m app.extraction "ಸುರೇಶ್ ಗೆ 650 ಸಾಲ, ಶುಕ್ರವಾರ ಕೊಡ್ತಾರೆ" --engine hosted
python -m app.evaluate --stage text --engine hosted   # accuracy of understanding (no recordings needed)
python -m app.evaluate --stage voice --stt local --engine local   # the real test (recordings in eval/audio/)
python -m app.usage                                   # usage numbers
SIMULATE=true uvicorn app.main:app --port 8000        # the server in practice mode
python simulate.py                                    # chat with it (second terminal)
```

Settings live in `.env` (see `.env.example`). Local engines need `faster-whisper`
and Ollama with a model; hosted needs `GEMINI_API_KEY`.

## 6. Not done, not verified, or to decide

- **Never run on real Kannada.** Install the packages, record the 54 sentences (names `c01.ogg` or `c01__anita.m4a` in `eval/audio/`), run the two `evaluate` commands.
- **Not built:** Kannada voice replies (text-to-speech), the 9 PM summary, reminder text, shop onboarding, webhook signature check, customer phone numbers.
- **Not tested against real Meta:** buttons, voice download and sending are written to Meta's documented format and tested with fakes. Check limits (3 buttons, 20-character titles, 1024-character body) on a real account.
- **Big-amount line is ₹1,00,000.** A mishearing like 650 heard as 65000 is *not* flagged. Planned fix: flag amounts far above that customer's usual.
- **Name matching threshold (0.85)** separates spelling variants from different people by a thin margin; re-check with real data.
- **Kannada wording** in `replies.py` and `prompts.py` needs a native speaker's review.
- **The project `venv` is broken** (moved folder); tests currently run on Anaconda Python. Recreate it before installing packages.
- The database file is still called `dukaandost.db`; `practice.db` is scratch data.
