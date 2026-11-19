# GetLekka

**A voice-first credit book (udhaar / saala / baaki) for small shops, on WhatsApp. Kannada first.**

A shop owner sends a **Kannada voice note** like *"ಸುರೇಶ್ ಗೆ 650 ಸಾಲ, ಶುಕ್ರವಾರ ಕೊಡ್ತಾರೆ"*.
GetLekka writes it into the credit book, replies with a receipt showing the old and new
balance, and keeps every change undoable. When it is not sure, it asks instead of guessing.

> **Status: prototype in development.** The code is written and tested with stand-in
> engines. It has **not** yet been run on real Kannada voice, has **no** users, and is
> **not** live on WhatsApp. Accuracy on real speech is unknown until the accuracy test
> in `eval/` is run.

## What it does

- Understands several entries in one voice note (credit, payment, cash sale).
- Answers "how much does Suresh owe?", "who owes me?" and "undo".
- Asks the owner when it is unsure (two customers with the same name, a big amount,
  no credit/payment word) and saves nothing until answered.
- Receipts with an **Undo** button; nothing is ever deleted, only marked undone.
- Keeps the voice note, transcript and the AI's raw answer as proof.
- Speech-to-text and the AI can run **locally** (faster-whisper, Ollama) or **hosted**,
  behind one switch, so they can be compared on the same recordings.

## How it works

```
voice note -> speech-to-text -> AI reads the words -> code checks every number
          -> ledger (all money math) -> Kannada reply with buttons
```

The AI only *reads* the words. Code validates every amount and date, and only
`ledger.py` does money math. See [ARCHITECTURE.md](ARCHITECTURE.md) for the full
flow, the golden rules, the file guide and what is still open.

## Quick start

Requires Python 3.12.

```bash
pip install -r requirements.txt
python -m pytest -q
```

Copy `.env.example` to `.env` and fill in only what you need. **Never commit `.env`.**

Chat with it from the terminal (practice mode, no WhatsApp needed), in two terminals:

```bash
SIMULATE=true uvicorn app.main:app --port 8000
```
```bash
python simulate.py
```

Useful tools:

```bash
python -m app.extraction "ಸುರೇಶ್ ಗೆ 650 ಸಾಲ" --engine hosted   # try the AI on one sentence
python -m app.speech note.ogg                                  # try speech on one recording
python -m app.evaluate --stage text --engine hosted            # accuracy on the 54-sentence test set
python -m app.usage                                            # how a shop is using it
```

Local engines need `faster-whisper` and [Ollama](https://ollama.com) with a model;
hosted engines need `GEMINI_API_KEY` in `.env`.

## Project layout

| Path | What it is |
|---|---|
| `app/ledger.py`, `app/db.py` | The credit book and the database |
| `app/speech.py`, `app/extraction.py`, `app/prompts.py` | Voice to text, text to checked entries |
| `app/matching.py`, `app/questions.py`, `app/pipeline.py` | Which customer, asking questions, the decisions |
| `app/replies.py` | Everything the bot says, in Kannada |
| `app/main.py`, `app/whatsapp.py`, `app/audio_store.py` | The WhatsApp side |
| `app/evaluate.py`, `app/usage.py`, `eval/` | Accuracy report, usage numbers, test sentences |
| `tests/` | The tests; each file matches a module |
| `simulate.py` | Practice chat |

## Not done yet

Kannada voice replies, the 9 PM summary, reminder messages, shop onboarding, webhook
signature checks, and testing against a real WhatsApp Business account.

## Privacy

Voice notes and databases contain customers' names and money amounts. They are
excluded by `.gitignore` and must never be committed.
