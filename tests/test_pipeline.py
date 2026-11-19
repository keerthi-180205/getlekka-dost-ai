"""
Tests for Chunk 8 + 9: the brain (app/pipeline.py).

The AI is replaced by a scripted stand-in that answers each sentence with a
prepared JSON reply, and speech-to-text by a stand-in too, so every path
through the pipeline is tested with no model, no network and no key.
"Today" is Friday 20 November 2026.
Run with:   pytest -v
"""
import json
from datetime import date

import pytest

from app import db, extraction, ledger, pipeline, questions, replies, speech
from app.replies import Reply

TODAY = date(2026, 11, 20)


# ---------- the scripted AI ----------

def ai_reply(intent="entries", entries=(), customer=None):
    return json.dumps({"intent": intent, "entries": list(entries), "customer": customer}, ensure_ascii=False)


def credit(customer="ಸುರೇಶ್", amount=650, **extra):
    return {"kind": "credit", "customer": customer, "amount": amount, "due": None, "note": None, **extra}


def payment(customer="ರಮೇಶ್", amount=200, **extra):
    return {"kind": "payment", "customer": customer, "amount": amount, "due": None, "note": None, **extra}


def sale(amount=1500):
    return {"kind": "sale", "customer": None, "amount": amount, "due": None, "note": None}


class ScriptedAI(extraction.ExtractionEngine):
    """Returns a prepared reply for each transcript. An unscripted sentence is a test bug."""

    name = "scripted"

    def __init__(self):
        self.script = {}
        self.calls = []

    def complete(self, prompt):
        transcript = prompt.rsplit("Text: ", 1)[1].rsplit("\nJSON:", 1)[0]
        self.calls.append(transcript)
        if transcript not in self.script:
            raise AssertionError(f"No scripted AI reply for {transcript!r}")
        reply = self.script[transcript]
        if isinstance(reply, Exception):
            raise reply
        return reply


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test.db"))
    db.init_db()


@pytest.fixture
def ai(monkeypatch):
    engine = ScriptedAI()
    monkeypatch.setattr(extraction, "get_engine", lambda name=None: engine)
    return engine


@pytest.fixture
def shop():
    return ledger.get_or_create_shop("919876543210")


def say(shop_id, ai, text, answer, **kwargs):
    """Script the AI's answer for `text`, then hand the text to the pipeline."""
    ai.script[text] = answer
    return pipeline.handle_text(shop_id, text, today=TODAY, **kwargs)


def tap(shop_id, button_id):
    return pipeline.handle_button(shop_id, button_id, today=TODAY)


def ids(reply):
    return [b.id for b in reply.buttons]


def titles(reply):
    return [b.title for b in reply.buttons]


def balance(shop_id, name):
    return ledger.get_balance(shop_id, name)


def entry_rows(shop_id):
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT e.id, e.kind, e.amount, c.name AS customer, e.status, e.batch_id, e.due_date "
            "FROM entries e LEFT JOIN customers c ON c.id = e.customer_id "
            "WHERE e.shop_id = ? ORDER BY e.id", (shop_id,))]


def batch_row(batch_id):
    with db.connect() as conn:
        return dict(conn.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone())


def all_batches(shop_id):
    with db.connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM batches WHERE shop_id = ? ORDER BY id", (shop_id,))]


# ===========================================================================
# Recording: the happy path
# ===========================================================================

def test_one_credit_is_saved_and_receipted(shop, ai):
    out = say(shop, ai, "ಸುರೇಶ್ ಗೆ 650 ಸಾಲ", ai_reply("entries", [credit()]))
    assert len(out) == 1
    assert out[0].text.splitlines() == [
        "✅ ಬರೆದುಕೊಂಡಿದ್ದೇನೆ", "• ಸುರೇಶ್ — ₹650 ಸಾಲ (ಬಾಕಿ ₹0 → ₹650)",
    ]
    assert balance(shop, "ಸುರೇಶ್") == 650


def test_the_receipt_has_an_undo_button_for_the_batch(shop, ai):
    out = say(shop, ai, "x", ai_reply("entries", [credit()]))
    batch_id = entry_rows(shop)[0]["batch_id"]
    assert ids(out[0]) == [f"undo:{batch_id}"]


def test_the_batch_keeps_the_transcript_and_what_was_understood(shop, ai):
    raw = ai_reply("entries", [credit()])
    say(shop, ai, "ಸುರೇಶ್ ಗೆ 650 ಸಾಲ", raw)
    batch = all_batches(shop)[0]
    assert batch["transcript"] == "ಸುರೇಶ್ ಗೆ 650 ಸಾಲ"
    assert batch["source"] == "text"
    assert batch["raw_extraction"] == raw
    meta = json.loads(batch["meta"])
    assert meta["intent"] == "entries" and meta["needs_attention"] is False
    assert meta["llm"]["engine"] == "scripted"


def test_the_message_id_is_linked_to_the_batch(shop, ai):
    db.save_incoming("wamid.1", "919876543210", "text", "x")
    say(shop, ai, "x", ai_reply("entries", [credit()]), message_id=db.get_message_id("wamid.1"))
    assert all_batches(shop)[0]["message_id"] == db.get_message_id("wamid.1")


def test_a_due_date_is_worked_out_and_shown(shop, ai):
    out = say(shop, ai, "x", ai_reply("entries", [credit(due="friday")]))
    assert "ಕೊಡುವ ದಿನ: 27 ನವೆಂಬರ್" in out[0].text
    assert entry_rows(shop)[0]["due_date"] == "2026-11-27"


def test_a_voice_note_with_three_entries_is_one_batch_and_one_receipt(shop, ai):
    out = say(shop, ai, "x", ai_reply("entries", [credit(), payment(), sale()]))
    assert len(out) == 1
    assert len(out[0].text.splitlines()) == 4      # header + 3 lines
    assert "ನಗದು ಮಾರಾಟ — ₹1,500" in out[0].text
    rows = entry_rows(shop)
    assert len(rows) == 3 and len({r["batch_id"] for r in rows}) == 1


def test_receipt_shows_the_balance_before_and_after(shop, ai):
    say(shop, ai, "one", ai_reply("entries", [credit(amount=450)]))
    out = say(shop, ai, "two", ai_reply("entries", [credit(amount=650)]))
    assert "(ಬಾಕಿ ₹450 → ₹1,100)" in out[0].text


def test_two_entries_for_one_customer_in_one_note_chain_their_balances(shop, ai):
    out = say(shop, ai, "x", ai_reply("entries", [credit(amount=100), payment("ಸುರೇಶ್", 30)]))
    lines = out[0].text.splitlines()
    assert "(ಬಾಕಿ ₹0 → ₹100)" in lines[1]
    assert "(ಬಾಕಿ ₹100 → ₹70)" in lines[2]


def test_overpayment_says_we_owe_them(shop, ai):
    say(shop, ai, "one", ai_reply("entries", [credit("ರಮೇಶ್", 100)]))
    out = say(shop, ai, "two", ai_reply("entries", [payment("ರಮೇಶ್", 150)]))
    assert "₹50 ವಾಪಸ್ ಕೊಡಬೇಕು" in out[0].text


def test_an_exact_name_match_is_saved_without_asking(shop, ai):
    say(shop, ai, "one", ai_reply("entries", [credit("ಸುರೇಶ್", 100)]))
    out = say(shop, ai, "two", ai_reply("entries", [credit(" ಸುರೇಶ್ ", 50)]))
    assert len(out) == 1 and out[0].text.startswith(replies.SAVED_HEADER)
    assert balance(shop, "ಸುರೇಶ್") == 150
    assert len(ledger.list_customers(shop)) == 1


def test_the_stored_name_is_used_in_the_receipt(shop, ai):
    say(shop, ai, "one", ai_reply("entries", [credit("Suresh Gowda", 100)]))
    out = say(shop, ai, "two", ai_reply("entries", [credit("SURESH gowda", 50)]))
    assert "• Suresh Gowda — ₹50 ಸಾಲ" in out[0].text


# ===========================================================================
# When we cannot use what was said
# ===========================================================================

def test_unclear_gets_the_help_message_and_saves_nothing(shop, ai):
    out = say(shop, ai, "ನಾಳೆ ಮಳೆ ಬರಬಹುದು", ai_reply("unclear"))
    assert out == [Reply(replies.UNCLEAR)]
    assert entry_rows(shop) == []


def test_an_unclear_note_is_still_logged(shop, ai):
    say(shop, ai, "ನಾಳೆ ಮಳೆ ಬರಬಹುದು", ai_reply("unclear"))
    batch = all_batches(shop)[0]
    assert batch["transcript"] == "ನಾಳೆ ಮಳೆ ಬರಬಹುದು"
    assert json.loads(batch["meta"])["intent"] == "unclear"


@pytest.mark.parametrize("silence", ["", "   ", "\n"])
def test_empty_text_is_heard_as_nothing(shop, ai, silence):
    assert pipeline.handle_text(shop, silence, today=TODAY) == [Reply(replies.HEARD_NOTHING)]
    assert ai.calls == []


def test_an_ai_failure_gets_a_polite_message_and_is_logged(shop, ai):
    out = say(shop, ai, "x", RuntimeError("out of memory"))
    assert out == [Reply(replies.TECH_PROBLEM)]
    batch = all_batches(shop)[0]
    assert batch["transcript"] == "x"
    assert "out of memory" in json.loads(batch["meta"])["error"]
    assert entry_rows(shop) == []


def test_unusable_ai_json_is_logged_with_its_raw_text(shop, ai):
    out = say(shop, ai, "x", "I cannot do that.")
    assert out == [Reply(replies.TECH_PROBLEM)]
    assert all_batches(shop)[0]["raw_extraction"] == "I cannot do that."


def test_a_part_with_a_bad_amount_is_reported_and_the_rest_saved(shop, ai):
    out = say(shop, ai, "x", ai_reply("entries", [credit(), payment(amount="ಆರುನೂರು")]))
    assert out[0].text.startswith(replies.SAVED_HEADER)
    assert "ಮೊತ್ತ ಅರ್ಥವಾಗಲಿಲ್ಲ" in out[1].text
    assert balance(shop, "ಸುರೇಶ್") == 650
    assert len(entry_rows(shop)) == 1


def test_only_bad_parts_means_no_receipt(shop, ai):
    out = say(shop, ai, "x", ai_reply("entries", [credit(amount=0)]))
    assert len(out) == 1 and "ಅರ್ಥವಾಗಲಿಲ್ಲ" in out[0].text and not out[0].buttons
    assert entry_rows(shop) == []


def test_a_missing_name_is_reported(shop, ai):
    out = say(shop, ai, "x", ai_reply("entries", [credit(customer=None)]))
    assert "ಹೆಸರು ಅರ್ಥವಾಗಲಿಲ್ಲ" in out[0].text


# ===========================================================================
# "Which Suresh?"
# ===========================================================================

@pytest.fixture
def two_sureshes(shop, ai):
    """A shop that already has 'Suresh milk' (older) and 'Suresh tailor' (newer)."""
    ledger.add_credit(shop, "Suresh milk", 450)
    ledger.add_credit(shop, "Suresh tailor", 300)


def test_an_unsure_name_asks_and_saves_nothing(shop, ai, two_sureshes):
    out = say(shop, ai, "x", ai_reply("entries", [credit("Suresh", 650)]))
    assert len(out) == 1
    assert titles(out[0]) == ["Suresh tailor", "Suresh milk", "ಹೊಸ ಗ್ರಾಹಕ"]       # most recent first
    assert "Suresh" in out[0].text and "₹650 ಸಾಲ" in out[0].text
    assert balance(shop, "Suresh tailor") == 300 and balance(shop, "Suresh milk") == 450
    assert len(entry_rows(shop)) == 2                  # only the two set-up entries
    assert len(questions.open_questions(shop)) == 1


def test_tapping_an_existing_name_saves_it_there(shop, ai, two_sureshes):
    out = say(shop, ai, "x", ai_reply("entries", [credit("Suresh", 650)]))
    result = tap(shop, ids(out[0])[1])                 # option 2 = Suresh milk
    assert result[0].text.splitlines()[1] == "• Suresh milk — ₹650 ಸಾಲ (ಬಾಕಿ ₹450 → ₹1,100)"
    assert balance(shop, "Suresh milk") == 1100 and balance(shop, "Suresh tailor") == 300
    assert questions.open_questions(shop) == []


def test_tapping_new_customer_creates_the_spoken_name(shop, ai, two_sureshes):
    out = say(shop, ai, "x", ai_reply("entries", [credit("Suresh", 650)]))
    result = tap(shop, ids(out[0])[2])                 # option 3 = new
    assert "• Suresh — ₹650 ಸಾಲ (ಬಾಕಿ ₹0 → ₹650)" in result[0].text
    assert balance(shop, "Suresh") == 650
    assert len(ledger.list_customers(shop)) == 3


def test_a_number_typed_as_text_answers_the_question(shop, ai, two_sureshes):
    say(shop, ai, "x", ai_reply("entries", [credit("Suresh", 650)]))
    result = pipeline.handle_text(shop, "1", today=TODAY)
    assert "• Suresh tailor — ₹650 ಸಾಲ" in result[0].text


def test_a_kannada_digit_also_answers(shop, ai, two_sureshes):
    say(shop, ai, "x", ai_reply("entries", [credit("Suresh", 650)]))
    result = pipeline.handle_text(shop, "೨", today=TODAY)
    assert "• Suresh milk — ₹650 ಸಾಲ" in result[0].text


def test_a_number_with_no_open_question_is_just_a_message(shop, ai):
    ai.script["1"] = ai_reply("unclear")
    assert pipeline.handle_text(shop, "1", today=TODAY) == [Reply(replies.UNCLEAR)]


def test_a_double_tap_saves_only_once(shop, ai, two_sureshes):
    out = say(shop, ai, "x", ai_reply("entries", [credit("Suresh", 650)]))
    first = tap(shop, ids(out[0])[0])
    second = tap(shop, ids(out[0])[0])
    assert second == [Reply(replies.ALREADY_ANSWERED)]
    assert first[0].text.startswith(replies.SAVED_HEADER)
    assert balance(shop, "Suresh tailor") == 950       # 300 + 650, not 1600


def test_several_entries_for_one_unsure_name_are_one_question(shop, ai, two_sureshes):
    out = say(shop, ai, "x", ai_reply("entries", [credit("Suresh", 100), payment("Suresh", 40)]))
    assert len(out) == 1 and "₹100 ಸಾಲ, ₹40 ಪಾವತಿ" in out[0].text
    result = tap(shop, ids(out[0])[0])
    assert len(result[0].text.splitlines()) == 3        # header + both entries
    assert balance(shop, "Suresh tailor") == 360


def test_clear_entries_are_saved_while_an_unsure_one_waits(shop, ai, two_sureshes):
    out = say(shop, ai, "x", ai_reply("entries", [credit("ರಾಜು", 200), credit("Suresh", 650)]))
    assert out[0].text.startswith(replies.SAVED_HEADER) and "ರಾಜು" in out[0].text
    assert titles(out[1])[-1] == "ಹೊಸ ಗ್ರಾಹಕ"
    assert balance(shop, "ರಾಜು") == 200 and balance(shop, "Suresh tailor") == 300


def test_two_people_named_in_one_note_are_not_asked_about_each_other(shop, ai):
    """He said 'Suresh milk' AND 'Suresh tailor' in one breath: he knows they differ."""
    out = say(shop, ai, "x", ai_reply("entries", [credit("Suresh milk", 100), credit("Suresh tailor", 200)]))
    assert len(out) == 1 and out[0].text.startswith(replies.SAVED_HEADER)
    assert balance(shop, "Suresh milk") == 100 and balance(shop, "Suresh tailor") == 200
    assert questions.open_questions(shop) == []


def test_names_are_matched_against_the_book_as_it_was_before_the_note(shop, ai):
    """A shop with 'Suresh': a note naming 'Suresh' and 'Suresh tailor' asks only about the NEW name."""
    ledger.add_credit(shop, "Suresh", 50)
    out = say(shop, ai, "x", ai_reply("entries", [credit("Suresh", 100), credit("Suresh tailor", 200)]))
    assert out[0].text.splitlines()[1] == "• Suresh — ₹100 ಸಾಲ (ಬಾಕಿ ₹50 → ₹150)"     # exact: saved
    assert titles(out[1]) == ["Suresh", "ಹೊಸ ಗ್ರಾಹಕ"]                              # "Suresh tailor" asks
    assert balance(shop, "Suresh tailor") == 0


def test_the_same_new_name_twice_in_one_note_is_one_customer(shop, ai):
    out = say(shop, ai, "x", ai_reply("entries", [credit("ರಾಜು", 100), credit(" ರಾಜು ", 50)]))
    assert len(out) == 1 and balance(shop, "ರಾಜು") == 150
    assert len(ledger.list_customers(shop)) == 1


def test_two_different_unsure_names_make_two_questions(shop, ai, two_sureshes):
    ledger.add_credit(shop, "Ramesh a", 10)
    ledger.add_credit(shop, "Ramesh b", 10)
    out = say(shop, ai, "x", ai_reply("entries", [credit("Suresh", 1), credit("Ramesh", 2)]))
    assert len(out) == 2 and len(questions.open_questions(shop)) == 2


def test_an_out_of_range_option_is_refused_and_the_question_stays_open(shop, ai, two_sureshes):
    out = say(shop, ai, "x", ai_reply("entries", [credit("Suresh", 650)]))
    qid = questions.open_questions(shop)[0]["id"]
    assert tap(shop, f"q:{qid}:4") == [Reply(replies.UNCLEAR)]
    assert tap(shop, f"q:{qid}:0") == [Reply(replies.UNCLEAR)]
    assert len(questions.open_questions(shop)) == 1


def test_undoing_the_note_cancels_its_question(shop, ai, two_sureshes):
    out = say(shop, ai, "x", ai_reply("entries", [credit("ರಾಜು", 200), credit("Suresh", 650)]))
    receipt, question = out
    tap(shop, ids(receipt)[0])                          # tap Undo on the receipt
    assert questions.open_questions(shop) == []
    assert tap(shop, ids(question)[0]) == [Reply(replies.ALREADY_ANSWERED)]
    assert balance(shop, "Suresh tailor") == 300        # nothing leaked in


def test_a_kannada_spelling_variant_asks_instead_of_creating_a_duplicate(shop, ai):
    ledger.add_credit(shop, "ಸುರೇಶ್", 100)
    out = say(shop, ai, "x", ai_reply("entries", [credit("ಸುರೇಶ", 50)]))
    assert titles(out[0]) == ["ಸುರೇಶ್", "ಹೊಸ ಗ್ರಾಹಕ"]
    result = tap(shop, ids(out[0])[0])
    assert balance(shop, "ಸುರೇಶ್") == 150
    assert len(ledger.list_customers(shop)) == 1


# ===========================================================================
# "Is this right?"  and  "Credit or payment?"
# ===========================================================================

def test_a_big_amount_asks_before_saving(shop, ai):
    out = say(shop, ai, "x", ai_reply("entries", [credit(amount=150000)]))
    assert len(out) == 1 and titles(out[0]) == [replies.YES_BUTTON, replies.NO_BUTTON]
    assert replies.WARNING_TEXT["large_amount"] in out[0].text
    assert entry_rows(shop) == []


def test_confirming_a_big_amount_saves_it(shop, ai):
    out = say(shop, ai, "x", ai_reply("entries", [credit(amount=150000)]))
    result = tap(shop, ids(out[0])[0])
    assert "• ಸುರೇಶ್ — ₹1,50,000 ಸಾಲ" in result[0].text
    assert balance(shop, "ಸುರೇಶ್") == 150000


def test_refusing_a_big_amount_saves_nothing(shop, ai):
    out = say(shop, ai, "x", ai_reply("entries", [credit(amount=150000)]))
    assert tap(shop, ids(out[0])[1]) == [Reply(replies.NOT_WRITTEN)]
    assert entry_rows(shop) == []


def test_an_unusable_due_date_asks_then_saves_without_it(shop, ai):
    out = say(shop, ai, "x", ai_reply("entries", [credit(due="next month")]))
    assert replies.WARNING_TEXT["due_not_understood"] in out[0].text
    tap(shop, ids(out[0])[0])
    assert entry_rows(shop)[0]["due_date"] is None and balance(shop, "ಸುರೇಶ್") == 650


def test_a_cash_sale_with_a_name_asks_then_saves_as_a_plain_sale(shop, ai):
    out = say(shop, ai, "x", ai_reply("entries", [{**sale(), "customer": "ಸುರೇಶ್"}]))
    assert replies.WARNING_TEXT["sale_has_customer"] in out[0].text
    tap(shop, ids(out[0])[0])
    assert [(r["kind"], r["customer"]) for r in entry_rows(shop)] == [("sale", None)]


def test_an_unsure_name_and_a_big_amount_ask_twice_in_order(shop, ai, two_sureshes):
    out = say(shop, ai, "x", ai_reply("entries", [credit("Suresh", 150000)]))
    assert replies.NEW_CUSTOMER_BUTTON in titles(out[0])
    second = tap(shop, ids(out[0])[0])                  # which Suresh -> Suresh tailor
    assert len(second) == 1 and titles(second[0]) == [replies.YES_BUTTON, replies.NO_BUTTON]
    assert balance(shop, "Suresh tailor") == 300        # still not saved
    third = tap(shop, ids(second[0])[0])                # confirm
    assert "• Suresh tailor — ₹1,50,000 ಸಾಲ" in third[0].text
    assert balance(shop, "Suresh tailor") == 150300


def test_no_credit_or_payment_word_asks_which(shop, ai):
    out = say(shop, ai, "ಸುರೇಶ್ 500", ai_reply("entries", [{**credit(amount=500), "kind": "unknown"}]))
    assert titles(out[0]) == ["ಸಾಲ", "ಪಾವತಿ"] and "ಸುರೇಶ್" in out[0].text
    assert entry_rows(shop) == []


def test_choosing_credit_for_an_unknown_kind(shop, ai):
    out = say(shop, ai, "x", ai_reply("entries", [{**credit(amount=500), "kind": "unknown"}]))
    result = tap(shop, ids(out[0])[0])
    assert "• ಸುರೇಶ್ — ₹500 ಸಾಲ" in result[0].text and balance(shop, "ಸುರೇಶ್") == 500


def test_choosing_payment_for_an_unknown_kind(shop, ai):
    ledger.add_credit(shop, "ಸುರೇಶ್", 800)
    out = say(shop, ai, "x", ai_reply("entries", [{**credit(amount=500), "kind": "unknown"}]))
    result = tap(shop, ids(out[0])[1])
    assert "• ಸುರೇಶ್ — ₹500 ಪಾವತಿ (ಬಾಕಿ ₹800 → ₹300)" in result[0].text


def test_unknown_kind_with_a_bad_amount_is_not_worth_asking(shop, ai):
    out = say(shop, ai, "x", ai_reply("entries", [{**credit(amount="ಆರುನೂರು"), "kind": "unknown"}]))
    assert not out[0].buttons and "ಅರ್ಥವಾಗಲಿಲ್ಲ" in out[0].text
    assert questions.open_questions(shop) == []


def test_unknown_kind_for_an_unsure_name_asks_the_kind_first_then_the_name(shop, ai, two_sureshes):
    out = say(shop, ai, "x", ai_reply("entries", [{**credit("Suresh", 500), "kind": "unknown"}]))
    after_kind = tap(shop, ids(out[0])[0])              # credit
    assert replies.NEW_CUSTOMER_BUTTON in titles(after_kind[0])
    done = tap(shop, ids(after_kind[0])[0])
    assert "• Suresh tailor — ₹500 ಸಾಲ" in done[0].text


# ===========================================================================
# Questions about the book
# ===========================================================================

def test_balance_of_one_customer(shop, ai):
    ledger.add_credit(shop, "ಸುರೇಶ್", 900)
    assert say(shop, ai, "x", ai_reply("balance", customer="ಸುರೇಶ್")) == [Reply("ಸುರೇಶ್: ಬಾಕಿ ₹900")]


def test_balance_when_the_name_fits_several_people_shows_them_all(shop, ai, two_sureshes):
    out = say(shop, ai, "x", ai_reply("balance", customer="Suresh"))
    assert out[0].text.splitlines() == ["Suresh tailor: ಬಾಕಿ ₹300", "Suresh milk: ಬಾಕಿ ₹450"]


def test_balance_of_an_unknown_customer_does_not_create_one(shop, ai):
    out = say(shop, ai, "x", ai_reply("balance", customer="ರಾಜು"))
    assert "ರಾಜು" in out[0].text and "ಇಲ್ಲ" in out[0].text
    assert ledger.list_customers(shop) == []


def test_dues_list(shop, ai):
    ledger.add_credit(shop, "ಸುರೇಶ್", 900)
    ledger.add_credit(shop, "ಕುಮಾರ್", 300)
    out = say(shop, ai, "x", ai_reply("dues"))
    assert out[0].text.splitlines()[1:3] == ["1. ಸುರೇಶ್ — ₹900", "2. ಕುಮಾರ್ — ₹300"]
    assert "ಒಟ್ಟು ಬಾಕಿ: ₹1,200 (2 ಜನ)" in out[0].text


def test_dues_when_nobody_owes(shop, ai):
    assert say(shop, ai, "x", ai_reply("dues")) == [Reply(replies.NO_DUES)]


def test_a_spoken_undo_cancels_the_last_note(shop, ai):
    say(shop, ai, "one", ai_reply("entries", [credit(amount=100)]))
    say(shop, ai, "two", ai_reply("entries", [credit(amount=200), payment("ಸುರೇಶ್", 50)]))
    out = say(shop, ai, "undo", ai_reply("undo"))
    assert out[0].text.splitlines() == [replies.UNDONE_HEADER, "• ಸುರೇಶ್ — ₹200 ಸಾಲ", "• ಸುರೇಶ್ — ₹50 ಪಾವತಿ"]
    assert balance(shop, "ಸುರೇಶ್") == 100


def test_a_spoken_undo_with_nothing_to_undo(shop, ai):
    assert say(shop, ai, "undo", ai_reply("undo")) == [Reply(replies.NOTHING_TO_UNDO)]


def test_the_undo_button_cancels_its_own_note_even_if_newer_ones_exist(shop, ai):
    first = say(shop, ai, "one", ai_reply("entries", [credit(amount=100)]))
    say(shop, ai, "two", ai_reply("entries", [credit("ರಮೇಶ್", 200)]))
    result = tap(shop, ids(first[0])[0])
    assert result[0].text.splitlines()[1] == "• ಸುರೇಶ್ — ₹100 ಸಾಲ"
    assert balance(shop, "ಸುರೇಶ್") == 0 and balance(shop, "ರಮೇಶ್") == 200


def test_tapping_undo_twice(shop, ai):
    out = say(shop, ai, "x", ai_reply("entries", [credit()]))
    tap(shop, ids(out[0])[0])
    assert tap(shop, ids(out[0])[0]) == [Reply(replies.NOTHING_TO_UNDO)]


# ===========================================================================
# "This is a DIFFERENT Suresh"
# ===========================================================================

def test_a_different_person_is_moved_to_a_new_name(shop, ai):
    say(shop, ai, "one", ai_reply("entries", [credit("Suresh", 450)]))
    say(shop, ai, "two", ai_reply("entries", [credit("Suresh", 650)]))          # went to the first Suresh
    ask = say(shop, ai, "different", ai_reply("different_person", customer="Suresh"))
    assert len(ask) == 1 and "ಹಾಲು Suresh" in ask[0].text and "₹650 ಸಾಲ" in ask[0].text
    assert balance(shop, "Suresh") == 1100             # nothing changed yet

    done = pipeline.handle_text(shop, "ಹಾಲು Suresh", today=TODAY)
    assert balance(shop, "Suresh") == 450
    assert balance(shop, "ಹಾಲು Suresh") == 650
    assert "ಹಾಲು Suresh" in done[0].text.splitlines()[-1]
    assert "• ಹಾಲು Suresh — ₹650 ಸಾಲ (ಬಾಕಿ ₹0 → ₹650)" in done[0].text


def test_moving_keeps_history_and_the_same_batch(shop, ai):
    say(shop, ai, "one", ai_reply("entries", [credit("Suresh", 650, due="friday")]))
    say(shop, ai, "different", ai_reply("different_person", customer="Suresh"))
    pipeline.handle_text(shop, "ಹಾಲು Suresh", today=TODAY)
    rows = entry_rows(shop)
    assert [(r["customer"], r["status"]) for r in rows] == [("Suresh", "undone"), ("ಹಾಲು Suresh", "active")]
    assert rows[0]["batch_id"] == rows[1]["batch_id"]
    assert rows[1]["due_date"] == "2026-11-27"           # the due date moved with it


def test_after_moving_undo_cancels_the_whole_note(shop, ai):
    say(shop, ai, "one", ai_reply("entries", [credit("Suresh", 650)]))
    say(shop, ai, "different", ai_reply("different_person", customer="Suresh"))
    done = pipeline.handle_text(shop, "ಹಾಲು Suresh", today=TODAY)
    tap(shop, ids(done[0])[0])
    assert balance(shop, "ಹಾಲು Suresh") == 0 and balance(shop, "Suresh") == 0


def test_a_different_person_only_moves_that_customers_entries(shop, ai):
    say(shop, ai, "one", ai_reply("entries", [credit("Suresh", 650), credit("ರಮೇಶ್", 200)]))
    say(shop, ai, "different", ai_reply("different_person", customer="Suresh"))
    pipeline.handle_text(shop, "ಹಾಲು Suresh", today=TODAY)
    assert balance(shop, "ರಮೇಶ್") == 200 and balance(shop, "Suresh") == 0


def test_the_same_name_again_keeps_the_question_open(shop, ai):
    say(shop, ai, "one", ai_reply("entries", [credit("Suresh", 650)]))
    say(shop, ai, "different", ai_reply("different_person", customer="Suresh"))
    assert pipeline.handle_text(shop, " SURESH ", today=TODAY) == [Reply(replies.NAME_TAKEN_BY_SAME)]
    assert len(questions.open_questions(shop)) == 1
    pipeline.handle_text(shop, "ಹಾಲು Suresh", today=TODAY)
    assert balance(shop, "ಹಾಲು Suresh") == 650


@pytest.mark.parametrize("never_mind", ["ಬೇಡ", "cancel", "No"])
def test_saying_never_mind_leaves_everything_as_it_was(shop, ai, never_mind):
    say(shop, ai, "one", ai_reply("entries", [credit("Suresh", 650)]))
    say(shop, ai, "different", ai_reply("different_person", customer="Suresh"))
    assert pipeline.handle_text(shop, never_mind, today=TODAY) == [Reply(replies.LEFT_AS_IS)]
    assert balance(shop, "Suresh") == 650 and questions.open_questions(shop) == []


def test_the_cancel_button_on_the_name_question(shop, ai):
    say(shop, ai, "one", ai_reply("entries", [credit("Suresh", 650)]))
    ask = say(shop, ai, "different", ai_reply("different_person", customer="Suresh"))
    assert tap(shop, ids(ask[0])[0]) == [Reply(replies.LEFT_AS_IS)]


def test_a_different_person_with_nothing_recent_to_move(shop, ai):
    assert say(shop, ai, "x", ai_reply("different_person", customer="Suresh")) == [Reply(replies.NO_RECENT_ENTRY)]


def test_a_different_person_whose_name_is_not_in_the_last_note(shop, ai):
    say(shop, ai, "one", ai_reply("entries", [credit("ರಮೇಶ್", 100)]))
    assert say(shop, ai, "x", ai_reply("different_person", customer="Suresh")) == [Reply(replies.NO_RECENT_ENTRY)]


def test_a_long_sentence_while_the_name_question_is_open_is_a_normal_note(shop, ai):
    say(shop, ai, "one", ai_reply("entries", [credit("Suresh", 650)]))
    say(shop, ai, "different", ai_reply("different_person", customer="Suresh"))
    out = say(shop, ai, "ರಮೇಶ್ ಗೆ 200 ರೂಪಾಯಿ ಸಾಲ", ai_reply("entries", [credit("ರಮೇಶ್", 200)]))
    assert out[0].text.startswith(replies.SAVED_HEADER)
    assert len(questions.open_questions(shop, ("new_name",))) == 1      # still waiting for the name


def test_a_name_with_no_open_question_is_a_normal_note(shop, ai):
    out = say(shop, ai, "ಹಾಲು ಸುರೇಶ್", ai_reply("unclear"))
    assert out == [Reply(replies.UNCLEAR)]


# ===========================================================================
# Buttons: bad or foreign ids
# ===========================================================================

@pytest.mark.parametrize("bad", ["", "nonsense", "undo", "undo:x", "q:1", "q:a:b", "q:1:2:3", None])
def test_a_button_id_we_did_not_make_is_not_understood(shop, bad):
    assert tap(shop, bad) == [Reply(replies.UNCLEAR)]


def test_a_button_for_a_question_that_does_not_exist(shop):
    assert tap(shop, "q:999:1") == [Reply(replies.ALREADY_ANSWERED)]


def test_one_shop_cannot_answer_anothers_question(ai):
    shop_a = ledger.get_or_create_shop("911111111111")
    shop_b = ledger.get_or_create_shop("912222222222")
    out = say(shop_a, ai, "x", ai_reply("entries", [credit(amount=150000)]))
    assert tap(shop_b, ids(out[0])[0]) == [Reply(replies.ALREADY_ANSWERED)]
    assert entry_rows(shop_a) == []


def test_one_shop_cannot_undo_anothers_note(ai):
    shop_a = ledger.get_or_create_shop("911111111111")
    shop_b = ledger.get_or_create_shop("912222222222")
    out = say(shop_a, ai, "x", ai_reply("entries", [credit()]))
    assert tap(shop_b, ids(out[0])[0]) == [Reply(replies.NOTHING_TO_UNDO)]
    assert balance(shop_a, "ಸುರೇಶ್") == 650


# ===========================================================================
# Voice
# ===========================================================================

@pytest.fixture
def audio(tmp_path):
    path = tmp_path / "note.ogg"
    path.write_bytes(b"not real audio")
    return str(path)


def fake_transcribe(monkeypatch, text="ಸುರೇಶ್ ಗೆ 650 ಸಾಲ", error=None):
    def transcribe(audio_path, language=None, engine=None):
        if error:
            raise error
        return speech.Transcript(text=text, engine="fake-stt", language="kn", elapsed_seconds=1.5)
    monkeypatch.setattr(speech, "transcribe", transcribe)


def test_a_voice_note_is_transcribed_understood_and_saved(shop, ai, audio, monkeypatch):
    fake_transcribe(monkeypatch)
    ai.script["ಸುರೇಶ್ ಗೆ 650 ಸಾಲ"] = ai_reply("entries", [credit()])
    out = pipeline.handle_voice(shop, audio, today=TODAY)
    assert out[0].text.startswith(replies.SAVED_HEADER)
    assert balance(shop, "ಸುರೇಶ್") == 650


def test_a_voice_batch_keeps_the_audio_the_transcript_and_the_timings(shop, ai, audio, monkeypatch):
    fake_transcribe(monkeypatch)
    ai.script["ಸುರೇಶ್ ಗೆ 650 ಸಾಲ"] = ai_reply("entries", [credit()])
    pipeline.handle_voice(shop, audio, today=TODAY)
    batch = all_batches(shop)[0]
    assert batch["source"] == "voice" and batch["audio_path"] == audio
    assert batch["transcript"] == "ಸುರೇಶ್ ಗೆ 650 ಸಾಲ"
    assert json.loads(batch["meta"])["stt"] == {"engine": "fake-stt", "language": "kn", "seconds": 1.5}


def test_silence_is_heard_as_nothing_and_logged(shop, ai, audio, monkeypatch):
    fake_transcribe(monkeypatch, text="")
    assert pipeline.handle_voice(shop, audio, today=TODAY) == [Reply(replies.HEARD_NOTHING)]
    batch = all_batches(shop)[0]
    assert batch["audio_path"] == audio and "nothing heard" in json.loads(batch["meta"])["error"]
    assert ai.calls == []


def test_a_speech_failure_is_polite_and_logged(shop, ai, audio, monkeypatch):
    fake_transcribe(monkeypatch, error=speech.SpeechError("model not installed"))
    assert pipeline.handle_voice(shop, audio, today=TODAY) == [Reply(replies.TECH_PROBLEM)]
    batch = all_batches(shop)[0]
    assert batch["audio_path"] == audio and "model not installed" in json.loads(batch["meta"])["error"]


def test_a_spoken_number_answers_a_question(shop, ai, audio):
    """A voice note whose transcript is just '1' answers the open question."""
    ledger.add_credit(shop, "Suresh milk", 450)
    ledger.add_credit(shop, "Suresh tailor", 300)
    say(shop, ai, "x", ai_reply("entries", [credit("Suresh", 650)]))
    answer = pipeline._handle_utterance(shop, "1", source="voice", message_id=None,
                                        audio_path=audio, stt=None, today=TODAY)
    assert "• Suresh tailor — ₹650 ಸಾಲ" in answer[0].text
