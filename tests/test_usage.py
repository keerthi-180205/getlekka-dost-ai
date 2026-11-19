"""
Tests for app/usage.py: the usage numbers for the live-shop stage.

Times are controlled by replacing db._now, so every number is predictable.
Run with:   pytest -v
"""
import json

import pytest

from app import db, ledger, questions, usage


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test.db"))
    db.init_db()


@pytest.fixture
def clock(monkeypatch):
    """Set the 'current' UTC time that new rows get."""
    def set_time(iso):
        monkeypatch.setattr(db, "_now", lambda: iso)
    return set_time


@pytest.fixture
def shop():
    return ledger.get_or_create_shop("919876543210")


def note(shop, *amounts, source="voice", meta=None, customer="Suresh"):
    batch = ledger.create_batch(shop, source, meta=meta)
    for amount in amounts:
        ledger.add_credit(shop, customer, amount, batch_id=batch)
    return batch


def test_empty_database():
    summary = usage.usage_summary()
    assert summary["notes"] == 0 and summary["avg_entries_per_note"] is None
    assert usage.format_usage(summary) == "No notes recorded yet."


def test_notes_are_counted_by_source(shop, clock):
    clock("2026-11-20T05:00:00+00:00")
    note(shop, 100)
    note(shop, 200, source="text")
    note(shop, 300)
    assert usage.usage_summary()["by_source"] == {"voice": 2, "text": 1}


def test_days_and_hours_are_in_india_time(shop, clock):
    clock("2026-11-20T20:00:00+00:00")        # 01:30 on the 21st in India
    note(shop, 100)
    clock("2026-11-20T05:00:00+00:00")        # 10:30 on the 20th in India
    note(shop, 100)
    summary = usage.usage_summary()
    assert summary["notes_by_day"] == {"2026-11-20": 1, "2026-11-21": 1}
    assert summary["notes_by_hour"] == {1: 1, 10: 1}
    assert (summary["active_days"], summary["first_day"], summary["last_day"]) == (2, "2026-11-20", "2026-11-21")


def test_entries_per_note_shows_instant_versus_end_of_day_habits(shop, clock):
    clock("2026-11-20T05:00:00+00:00")
    note(shop, 100)
    note(shop, 100)
    note(shop, 100, 200, 300, 400)
    summary = usage.usage_summary()
    assert summary["entries_per_note"] == {1: 2, 4: 1}
    assert summary["notes_with_entries"] == 3 and summary["avg_entries_per_note"] == 2.0


def test_notes_without_entries_do_not_count_as_notes_with_entries(shop, clock):
    clock("2026-11-20T05:00:00+00:00")
    ledger.create_batch(shop, "voice", meta={"intent": "dues"})     # a question, no entries
    note(shop, 100)
    summary = usage.usage_summary()
    assert summary["notes"] == 2 and summary["notes_with_entries"] == 1


def test_undo_rate(shop, clock):
    clock("2026-11-20T05:00:00+00:00")
    first, second = note(shop, 100), note(shop, 200)
    note(shop, 300)
    note(shop, 400)
    ledger.undo_batch(shop, first)
    summary = usage.usage_summary()
    assert summary["undone_notes"] == 1 and summary["undo_rate"] == 0.25


def test_intents_failures_and_timings_come_from_the_meta(shop, clock):
    clock("2026-11-20T05:00:00+00:00")
    ledger.create_batch(shop, "voice", meta={"intent": "entries", "stt": {"seconds": 2.0}, "llm": {"seconds": 1.0}})
    ledger.create_batch(shop, "voice", meta={"intent": "dues", "stt": {"seconds": 4.0}, "llm": {"seconds": 3.0}})
    ledger.create_batch(shop, "voice", meta={"error": "model not found"})
    ledger.create_batch(shop, "text")                                # no meta at all
    summary = usage.usage_summary()
    assert summary["by_intent"] == {"entries": 1, "dues": 1}
    assert summary["failed_notes"] == 1
    assert summary["avg_stt_seconds"] == 3.0 and summary["avg_llm_seconds"] == 2.0


def test_questions_are_counted_by_kind_and_openness(shop):
    batch = note(shop, 100)
    answered = questions.create_question(shop, batch, "choose_customer", {})
    questions.create_question(shop, batch, "confirm_entry", {})
    questions.create_question(shop, batch, "confirm_entry", {})
    questions.claim_question(shop, answered)
    summary = usage.usage_summary()
    assert summary["questions_asked"] == 3
    assert summary["questions_by_kind"] == {"choose_customer": 1, "confirm_entry": 2}
    assert summary["questions_open"] == 2


def test_one_shop_can_be_picked_by_phone(clock):
    clock("2026-11-20T05:00:00+00:00")
    shop_a = ledger.get_or_create_shop("911111111111")
    shop_b = ledger.get_or_create_shop("912222222222")
    note(shop_a, 100)
    note(shop_b, 100)
    note(shop_b, 100)
    assert usage.usage_summary()["notes"] == 3
    assert usage.usage_summary("911111111111")["notes"] == 1
    assert usage.usage_summary("912222222222")["notes"] == 2
    assert usage.usage_summary("910000000000")["notes"] == 0


def test_the_report_never_shows_customer_names_or_amounts(shop, clock):
    clock("2026-11-20T05:00:00+00:00")
    note(shop, 123456, customer="ರಹಸ್ಯ")
    text = usage.format_usage(usage.usage_summary())
    assert "ರಹಸ್ಯ" not in text and "123456" not in text and "123,456" not in text


def test_the_text_report_has_the_headline_numbers(shop, clock):
    clock("2026-11-20T05:00:00+00:00")
    ledger.create_batch(shop, "voice", meta={"intent": "entries", "stt": {"seconds": 2.0}, "llm": {"seconds": 1.0}})
    note(shop, 100, 200)
    text = usage.format_usage(usage.usage_summary())
    for expected in ["Notes recorded      : 2", "Active days         : 1", "Entries per note", "Undo ",
                     "Questions we asked", "Average time        : speech 2.0s, AI 1.0s"]:
        assert expected in text


def test_command_line(shop, clock, capsys):
    clock("2026-11-20T05:00:00+00:00")
    note(shop, 100)
    assert usage.main([]) == 0
    assert "Notes recorded      : 1" in capsys.readouterr().out
    assert usage.main(["--phone", "910000000000"]) == 0
    assert "No notes recorded yet." in capsys.readouterr().out
