"""
usage.py - how are shop owners actually using GetLekka?   (for the live-shop stage)

Read-only. It looks at what the database already records and answers the
questions that decide whether the idea works:

    - Do they keep using it?                 notes per day, number of active days
    - When do they use it?                   by hour of the day (India time)
    - One entry after each customer, or one long note at the end of the day?
                                             entries per note
    - Do they trust it?                      how often they press Undo
    - Is it easy?                            how often we had to ask a question
    - Does it break?                         notes we could not use

Run it:
    python -m app.usage                       all shops
    python -m app.usage --phone 919876543210  one shop

A "note" here is one batch: one voice note or one typed message that we
recorded, including questions and commands. Only the shop owner's own numbers
and counts are shown, never the customers' names or amounts.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime, timedelta, timezone

from app import db

INDIA_TIME = timezone(timedelta(hours=5, minutes=30))


def _in_india(iso_time: str) -> datetime:
    """A stored UTC time ('2026-11-20T20:00:00+00:00') as India time."""
    return datetime.fromisoformat(iso_time).astimezone(INDIA_TIME)


def _average(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 2) if values else None


def usage_summary(phone: str | None = None) -> dict:
    """
    The numbers, as a plain dictionary. `phone` limits it to one shop
    (the owner's WhatsApp number); None means every shop.
    """
    shop_filter, params = ("WHERE s.owner_phone = ?", (phone,)) if phone else ("", ())
    with db.connect() as conn:
        batches = conn.execute(
            "SELECT b.id, b.source, b.status, b.created_at, b.meta, "
            "       (SELECT COUNT(*) FROM entries e WHERE e.batch_id = b.id) AS entry_count "
            "FROM batches b JOIN shops s ON s.id = b.shop_id " + shop_filter + " ORDER BY b.id",
            params).fetchall()
        asked = conn.execute(
            "SELECT q.kind, q.status FROM pending_questions q JOIN shops s ON s.id = q.shop_id "
            + shop_filter, params).fetchall()

    notes_by_day: Counter = Counter()
    notes_by_hour: Counter = Counter()
    by_source: Counter = Counter()
    by_intent: Counter = Counter()
    entries_per_note: Counter = Counter()
    stt_seconds: list[float] = []
    llm_seconds: list[float] = []
    failed = 0

    for batch in batches:
        when = _in_india(batch["created_at"])
        notes_by_day[when.date().isoformat()] += 1
        notes_by_hour[when.hour] += 1
        by_source[batch["source"]] += 1
        meta = json.loads(batch["meta"]) if batch["meta"] else {}
        if meta.get("error"):
            failed += 1
        if meta.get("intent"):
            by_intent[meta["intent"]] += 1
        if (meta.get("stt") or {}).get("seconds") is not None:
            stt_seconds.append(meta["stt"]["seconds"])
        if (meta.get("llm") or {}).get("seconds") is not None:
            llm_seconds.append(meta["llm"]["seconds"])
        if batch["entry_count"]:
            entries_per_note[batch["entry_count"]] += 1

    with_entries = sum(entries_per_note.values())
    undone = sum(1 for b in batches if b["status"] == "undone" and b["entry_count"])
    entry_total = sum(count * n for count, n in entries_per_note.items())
    return {
        "notes": len(batches),
        "active_days": len(notes_by_day),
        "first_day": min(notes_by_day) if notes_by_day else None,
        "last_day": max(notes_by_day) if notes_by_day else None,
        "notes_by_day": dict(sorted(notes_by_day.items())),
        "notes_by_hour": dict(sorted(notes_by_hour.items())),
        "by_source": dict(by_source),
        "by_intent": dict(by_intent),
        "notes_with_entries": with_entries,
        "entries_per_note": dict(sorted(entries_per_note.items())),
        "avg_entries_per_note": round(entry_total / with_entries, 2) if with_entries else None,
        "undone_notes": undone,
        "undo_rate": round(undone / with_entries, 3) if with_entries else None,
        "questions_asked": len(asked),
        "questions_by_kind": dict(Counter(row["kind"] for row in asked)),
        "questions_open": sum(1 for row in asked if row["status"] == "open"),
        "failed_notes": failed,
        "avg_stt_seconds": _average(stt_seconds),
        "avg_llm_seconds": _average(llm_seconds),
    }


def format_usage(summary: dict) -> str:
    """The summary as text for the terminal."""
    s = summary
    if not s["notes"]:
        return "No notes recorded yet."
    lines = [
        f"Notes recorded      : {s['notes']}  ({', '.join(f'{n} {k}' for k, n in s['by_source'].items())})",
        f"Active days         : {s['active_days']}  ({s['first_day']} to {s['last_day']})",
        "Notes per day       : " + ", ".join(f"{day[5:]}: {n}" for day, n in s["notes_by_day"].items()),
        "Busiest hours (IST) : " + ", ".join(
            f"{hour:02d}h: {n}" for hour, n in sorted(s["notes_by_hour"].items(), key=lambda i: -i[1])[:5]),
        f"Entries per note   : {s['avg_entries_per_note']} on average   "
        f"(1 entry: {s['entries_per_note'].get(1, 0)} notes, 2+ entries: "
        f"{sum(n for k, n in s['entries_per_note'].items() if k >= 2)} notes)",
        f"What they said      : " + ", ".join(f"{k} {n}" for k, n in s["by_intent"].items()),
        f"Undo                : {s['undone_notes']} of {s['notes_with_entries']} notes with entries"
        + (f"  ({s['undo_rate'] * 100:.0f}%)" if s["undo_rate"] is not None else ""),
        f"Questions we asked  : {s['questions_asked']}  "
        f"({', '.join(f'{k} {n}' for k, n in s['questions_by_kind'].items()) or 'none'}), "
        f"{s['questions_open']} still open",
        f"Notes we could not use: {s['failed_notes']}",
    ]
    timing = [f"speech {s['avg_stt_seconds']}s" if s["avg_stt_seconds"] is not None else None,
              f"AI {s['avg_llm_seconds']}s" if s["avg_llm_seconds"] is not None else None]
    if any(timing):
        lines.append("Average time        : " + ", ".join(t for t in timing if t))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="How are shop owners using GetLekka?")
    parser.add_argument("--phone", help="only this shop (the owner's WhatsApp number)")
    args = parser.parse_args(argv)
    db.init_db()
    print(format_usage(usage_summary(args.phone)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
