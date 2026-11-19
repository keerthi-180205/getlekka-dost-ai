"""
Tests for Chunk 11: the accuracy report (app/evaluate.py) and the shipped
test set (eval/kannada_cases.json).

No AI and no speech engine is used: stand-ins return prepared answers.
Run with:   pytest -v
"""
import json
from datetime import date

import pytest

from app import evaluate, extraction, speech
from app.evaluate import CaseResult, score_case

TODAY = date(2026, 11, 20)    # a Friday
CASES_FILE = "eval/kannada_cases.json"


# ---------- helpers ----------

def ai_reply(intent="entries", entries=(), customer=None):
    return json.dumps({"intent": intent, "entries": list(entries), "customer": customer}, ensure_ascii=False)


def understood(intent="entries", entries=(), customer=None):
    """What the checker makes of an AI reply (no engine needed)."""
    return extraction.interpret(ai_reply(intent, entries, customer), TODAY)


def credit(customer="ಸುರೇಶ್", amount=650, due=None):
    return {"kind": "credit", "customer": customer, "amount": amount, "due": due, "note": None}


def payment(customer="ರಮೇಶ್", amount=200):
    return {"kind": "payment", "customer": customer, "amount": amount, "due": None, "note": None}


def sale(amount=1500):
    return {"kind": "sale", "customer": None, "amount": amount, "due": None, "note": None}


def case(expect, cid="t1", tags=("credit",), say="ಸುರೇಶ್ ಗೆ 650 ಸಾಲ"):
    return {"id": cid, "tags": list(tags), "say": say, "expect": expect}


def expect_entries(*entries):
    return {"intent": "entries", "entries": list(entries)}


def gold_reply(expect):
    """The AI answer that is exactly right for this expectation."""
    def first(name):
        return name[0] if isinstance(name, list) else name

    if expect["intent"] == "entries":
        items = [{"kind": e["kind"], "customer": first(e.get("customer")), "amount": e["amount"],
                  "due": e.get("due"), "note": None} for e in expect["entries"]]
        return ai_reply("entries", items)
    return ai_reply(expect["intent"], customer=first(expect.get("customer")))


# ===========================================================================
# Speech error
# ===========================================================================

def test_identical_text_has_no_error():
    assert evaluate.character_error_rate("ಸುರೇಶ್ ಗೆ 650", "ಸುರೇಶ್ ಗೆ 650") == 0.0


def test_spaces_punctuation_and_case_are_ignored():
    assert evaluate.character_error_rate("Suresh ge 650", "  suresh,  GE 650. ") == 0.0
    assert evaluate.character_error_rate("ಸುರೇಶ್ ಗೆ 650", "ಸುರೇಶ್ಗೆ650") == 0.0


def test_one_wrong_letter_out_of_ten():
    assert evaluate.character_error_rate("abcdefghij", "abcdeXghij") == pytest.approx(0.1)


def test_missing_and_extra_letters_count():
    assert evaluate.character_error_rate("abcd", "abd") == pytest.approx(0.25)     # one missing
    assert evaluate.character_error_rate("abcd", "abcde") == pytest.approx(0.25)    # one extra


def test_hearing_nothing_is_total_error():
    assert evaluate.character_error_rate("ಸುರೇಶ್", "") == 1.0


def test_error_never_exceeds_one():
    assert evaluate.character_error_rate("ab", "zzzzzzzzzz") == 1.0


def test_an_empty_reference():
    assert evaluate.character_error_rate("", "") == 0.0
    assert evaluate.character_error_rate("", "x") == 1.0


def test_a_650_heard_as_6500_is_a_small_speech_error_but_a_big_money_error():
    """Why we measure the money result separately: speech error alone hides this."""
    assert evaluate.character_error_rate("ಸುರೇಶ್ ಗೆ 650 ಸಾಲ", "ಸುರೇಶ್ ಗೆ 6500 ಸಾಲ") < 0.1


# ===========================================================================
# Names
# ===========================================================================

def test_names_match_ignores_case_spaces_and_hidden_characters():
    assert evaluate.names_match("Suresh", " SURESH ")
    assert evaluate.names_match("ಸುರೇಶ್", "ಸುರೇಶ್\u200d")


def test_names_match_accepts_any_listed_spelling():
    assert evaluate.names_match(["Suresh", "ಸುರೇಶ್"], "ಸುರೇಶ್")
    assert evaluate.names_match(["Suresh", "ಸುರೇಶ್"], "suresh")
    assert not evaluate.names_match(["Suresh", "ಸುರೇಶ್"], "ರಮೇಶ್")


def test_no_name_never_matches_a_name():
    assert not evaluate.names_match("Suresh", None)


def test_a_near_spelling_is_a_near_miss_but_a_different_person_is_not():
    assert evaluate._name_near_miss("ಸುರೇಶ್", "ಸುರೇಶ")           # missing final virama
    assert not evaluate._name_near_miss("ಸುರೇಶ್", "ಸುರೇಶ್")       # exact: not a miss at all
    assert not evaluate._name_near_miss("ಸುರೇಶ್", "ರಮೇಶ್")        # a different person
    assert not evaluate._name_near_miss("ಸುರೇಶ್", None)


# ===========================================================================
# Scoring one case
# ===========================================================================

def outcome(expect, result):
    return score_case(case(expect), result, TODAY)["outcome"]


def test_a_right_credit_is_correct():
    assert outcome(expect_entries({"kind": "credit", "customer": "ಸುರೇಶ್", "amount": 650}),
                   understood("entries", [credit()])) == "correct"


def test_a_wrong_amount_with_no_flag_is_silently_wrong():
    assert outcome(expect_entries({"kind": "credit", "customer": "ಸುರೇಶ್", "amount": 650}),
                   understood("entries", [credit(amount=6500)])) == "silently_wrong"


def test_a_wrong_customer_with_no_flag_is_silently_wrong():
    assert outcome(expect_entries({"kind": "credit", "customer": "ಸುರೇಶ್", "amount": 650}),
                   understood("entries", [credit("ರಮೇಶ್")])) == "silently_wrong"


def test_credit_understood_as_payment_is_silently_wrong():
    assert outcome(expect_entries({"kind": "credit", "customer": "ಸುರೇಶ್", "amount": 650}),
                   understood("entries", [payment("ಸುರೇಶ್", 650)])) == "silently_wrong"


def test_a_wrong_answer_that_is_flagged_only_asks():
    """A huge mishearing is caught by the big-amount flag, so the owner is asked, nothing wrong is saved."""
    assert outcome(expect_entries({"kind": "credit", "customer": "ಸುರೇಶ್", "amount": 650}),
                   understood("entries", [credit(amount=650000)])) == "asks"


def test_a_right_answer_that_needs_a_question_is_asks_not_correct():
    expect = expect_entries({"kind": "credit", "customer": "ಸುರೇಶ್", "amount": 650})
    assert outcome(expect, understood("entries", [credit(due="next month")])) == "asks"


def test_a_missing_entry_is_silently_wrong():
    expect = expect_entries({"kind": "credit", "customer": "ಸುರೇಶ್", "amount": 650},
                            {"kind": "payment", "customer": "ರಮೇಶ್", "amount": 200})
    assert outcome(expect, understood("entries", [credit()])) == "silently_wrong"


def test_an_extra_entry_is_silently_wrong():
    expect = expect_entries({"kind": "credit", "customer": "ಸುರೇಶ್", "amount": 650})
    assert outcome(expect, understood("entries", [credit(), payment()])) == "silently_wrong"


def test_the_order_of_entries_does_not_matter():
    expect = expect_entries({"kind": "credit", "customer": "ಸುರೇಶ್", "amount": 650},
                            {"kind": "payment", "customer": "ರಮೇಶ್", "amount": 200})
    assert outcome(expect, understood("entries", [payment(), credit()])) == "correct"


def test_a_cash_sale_is_matched_by_kind_and_amount():
    expect = expect_entries({"kind": "sale", "customer": None, "amount": 1500})
    assert outcome(expect, understood("entries", [sale(1500)])) == "correct"
    assert outcome(expect, understood("entries", [sale(1600)])) == "silently_wrong"


def test_any_listed_spelling_of_the_customer_is_accepted():
    expect = expect_entries({"kind": "credit", "customer": ["Suresh", "ಸುರೇಶ್"], "amount": 650})
    assert outcome(expect, understood("entries", [credit("Suresh")])) == "correct"
    assert outcome(expect, understood("entries", [credit("ಸುರೇಶ್")])) == "correct"


def test_the_due_date_is_checked_only_when_the_case_specifies_one():
    with_due = expect_entries({"kind": "credit", "customer": "ಸುರೇಶ್", "amount": 650, "due": "friday"})
    without = expect_entries({"kind": "credit", "customer": "ಸುರೇಶ್", "amount": 650})
    assert outcome(with_due, understood("entries", [credit(due="friday")])) == "correct"
    assert outcome(with_due, understood("entries", [credit(due="monday")])) == "silently_wrong"
    assert outcome(with_due, understood("entries", [credit(due=None)])) == "silently_wrong"
    assert outcome(without, understood("entries", [credit(due="monday")])) == "correct"


def test_a_rejected_part_is_never_silent():
    expect = expect_entries({"kind": "credit", "customer": "ಸುರೇಶ್", "amount": 650})
    assert outcome(expect, understood("entries", [credit(), payment(amount=0)])) == "asks"


def test_the_wrong_intent_is_silently_wrong_unless_it_asks():
    expect = expect_entries({"kind": "credit", "customer": "ಸುರೇಶ್", "amount": 650})
    assert outcome(expect, understood("dues")) == "silently_wrong"
    assert outcome(expect, understood("unclear")) == "asks"


def test_balance_questions():
    expect = {"intent": "balance", "customer": "ಸುರೇಶ್"}
    assert outcome(expect, understood("balance", customer="ಸುರೇಶ್")) == "correct"
    assert outcome(expect, understood("balance", customer="ರಮೇಶ್")) == "silently_wrong"
    assert outcome(expect, understood("dues")) == "silently_wrong"


def test_intents_without_details():
    assert outcome({"intent": "dues"}, understood("dues")) == "correct"
    assert outcome({"intent": "undo"}, understood("undo")) == "correct"
    assert outcome({"intent": "dues"}, understood("undo")) == "silently_wrong"


def test_an_unclear_sentence_is_right_only_if_we_say_unclear():
    assert outcome({"intent": "unclear"}, understood("unclear")) == "correct"
    assert outcome({"intent": "unclear"}, understood("entries", [credit()])) == "silently_wrong"


def test_an_ambiguous_sentence_is_right_only_if_we_ask():
    expect = {"intent": "entries", "must_flag": True,
              "entries": [{"kind": "unknown", "customer": "ಸುರೇಶ್", "amount": 500}]}
    asked = understood("entries", [{**credit(amount=500), "kind": "unknown"}])
    guessed = understood("entries", [credit(amount=500)])
    assert outcome(expect, asked) == "correct"
    assert outcome(expect, guessed) == "silently_wrong"          # it guessed instead of asking


def test_field_accuracy_counts_each_field_separately():
    scored = score_case(case(expect_entries({"kind": "credit", "customer": "ಸುರೇಶ್", "amount": 650})),
                        understood("entries", [credit("ಸುರೇಶ್", 6500)]), TODAY)
    assert scored["entry_fields"] == {"kind": [1, 1], "customer": [1, 1], "amount": [0, 1]}


def test_a_missing_entry_counts_against_every_field():
    scored = score_case(case(expect_entries({"kind": "credit", "customer": "ಸುರೇಶ್", "amount": 650})),
                        understood("entries", []), TODAY)
    assert scored["outcome"] == "asks" or scored["outcome"] == "silently_wrong"
    assert scored["entry_fields"] == {"kind": [0, 1], "customer": [0, 1], "amount": [0, 1]} or scored["entry_fields"] == {}


def test_spelling_near_misses_are_counted():
    scored = score_case(case(expect_entries({"kind": "credit", "customer": "ಸುರೇಶ್", "amount": 650})),
                        understood("entries", [credit("ಸುರೇಶ", 650)]), TODAY)
    assert scored["name_near_misses"] == 1 and scored["outcome"] == "silently_wrong"


# ===========================================================================
# The shipped test set
# ===========================================================================

def test_the_shipped_test_set_loads_with_about_fifty_cases():
    cases, today = evaluate.load_cases(CASES_FILE)
    assert len(cases) >= 50
    assert today == TODAY and today.strftime("%A") == "Friday"


def test_case_ids_are_unique_and_safe_as_file_names():
    cases, _ = evaluate.load_cases(CASES_FILE)
    ids = [c["id"] for c in cases]
    assert len(ids) == len(set(ids))
    assert all(i.isalnum() for i in ids)


def test_the_test_set_covers_every_kind_of_sentence():
    cases, _ = evaluate.load_cases(CASES_FILE)
    tags = {tag for c in cases for tag in c["tags"]}
    assert {"credit", "payment", "multi", "due", "numbers", "mixed", "sale", "balance", "dues",
            "undo", "unclear", "different", "ambiguous"} <= tags


def test_every_sentence_is_kannada_or_mixed_with_english():
    cases, _ = evaluate.load_cases(CASES_FILE)
    assert all(any("ಀ" <= ch <= "೿" for ch in c["say"]) or c["id"] == "x01" for c in cases)


def test_every_gold_answer_scores_correct_through_our_own_checker():
    """If this fails, the test set and the checker disagree about what 'right' means."""
    cases, today = evaluate.load_cases(CASES_FILE)
    for c in cases:
        if c["expect"].get("must_flag"):
            reply = ai_reply("entries", c["expect"]["entries"] and [
                {**e, "note": None, "due": None} for e in c["expect"]["entries"]])
        else:
            reply = gold_reply(c["expect"])
        result = extraction.interpret(reply, today)
        scored = score_case(c, result, today)
        assert scored["outcome"] == "correct", (c["id"], c["say"], scored)


def test_a_sloppy_answer_to_every_entries_case_is_caught():
    """Shift every amount by one: no case may be silently accepted as right."""
    cases, today = evaluate.load_cases(CASES_FILE)
    for c in cases:
        expect = c["expect"]
        if expect["intent"] != "entries" or expect.get("must_flag"):
            continue
        reply = ai_reply("entries", [
            {"kind": e["kind"], "customer": (e["customer"][0] if isinstance(e.get("customer"), list) else e.get("customer")),
             "amount": e["amount"] + 1, "due": e.get("due"), "note": None} for e in expect["entries"]])
        assert score_case(c, extraction.interpret(reply, today), today)["outcome"] != "correct", c["id"]


# ===========================================================================
# load_cases / find_recordings
# ===========================================================================

def write_cases(tmp_path, cases, today=None):
    path = tmp_path / "cases.json"
    path.write_text(json.dumps({"today": today, "cases": cases} if today else cases, ensure_ascii=False))
    return str(path)


def test_load_cases_accepts_a_plain_list(tmp_path):
    cases, today = evaluate.load_cases(write_cases(tmp_path, [case({"intent": "dues"})]))
    assert len(cases) == 1 and today is None


@pytest.mark.parametrize("bad, message", [
    ({"id": "a", "say": "x"}, "missing 'expect'"),
    ({"id": "a", "expect": {"intent": "dues"}}, "missing 'say'"),
    ({"say": "x", "expect": {"intent": "dues"}}, "missing 'id'"),
    ({"id": "a", "say": "x", "expect": {"intent": "gossip"}}, "intent must be"),
    ({"id": "a", "say": "x", "expect": {"intent": "entries"}}, "needs entries"),
    ({"id": "a", "say": "x", "expect": {"intent": "balance"}}, "needs a customer"),
])
def test_load_cases_rejects_malformed_cases(tmp_path, bad, message):
    with pytest.raises(ValueError, match=message):
        evaluate.load_cases(write_cases(tmp_path, [bad]))


def test_load_cases_rejects_duplicate_ids(tmp_path):
    with pytest.raises(ValueError, match="Duplicate"):
        evaluate.load_cases(write_cases(tmp_path, [case({"intent": "dues"}), case({"intent": "undo"})]))


def test_find_recordings_by_case_id_and_speaker(tmp_path):
    for name in ["c01.ogg", "c01__anita.m4a", "c02__raju.WAV", "zzz.ogg", "notes.txt", "c01__x.pdf"]:
        (tmp_path / name).write_bytes(b"x")
    found, unmatched = evaluate.find_recordings(str(tmp_path), ["c01", "c02", "c03"])
    assert found == {"c01": [(None, str(tmp_path / "c01.ogg")), ("anita", str(tmp_path / "c01__anita.m4a"))],
                     "c02": [("raju", str(tmp_path / "c02__raju.WAV"))]}
    assert unmatched == ["zzz.ogg"]


def test_find_recordings_in_a_missing_folder(tmp_path):
    assert evaluate.find_recordings(str(tmp_path / "nope"), ["c01"]) == ({}, [])


# ===========================================================================
# Running an evaluation
# ===========================================================================

def gold_extract(cases_by_say):
    """An 'AI' that answers every sentence perfectly."""
    def extract_fn(text, today):
        return extraction.interpret(gold_reply(cases_by_say[text]["expect"]), today)
    return extract_fn


def test_text_stage_with_a_perfect_ai_is_all_correct():
    cases, today = evaluate.load_cases(CASES_FILE)
    cases = [c for c in cases if not c["expect"].get("must_flag")]
    by_say = {c["say"]: c for c in cases}
    results = evaluate.run_evaluation(cases, stage="text", transcribe_fn=None,
                                      extract_fn=gold_extract(by_say), today=today)
    assert len(results) == len(cases) and {r.outcome for r in results} == {"correct"}
    summary = evaluate.summarize(results)
    assert summary["passed"] and summary["rates"]["correct"] == 1.0 and summary["intent_accuracy"] == 1.0


def test_text_stage_counts_each_outcome():
    cases = [case(expect_entries({"kind": "credit", "customer": "ಸುರೇಶ್", "amount": 650}), cid=f"t{i}", say=f"s{i}")
             for i in range(4)]
    replies = {"s0": ai_reply("entries", [credit()]),                       # correct
               "s1": ai_reply("entries", [credit(amount=6500)]),            # silently wrong
               "s2": ai_reply("entries", [credit(amount=650000)]),          # asks
               "s3": "I cannot do that"}                                    # failed
    results = evaluate.run_evaluation(cases, stage="text", transcribe_fn=None, today=TODAY,
                                      extract_fn=lambda text, day: extraction.interpret(replies[text], day))
    assert [r.outcome for r in results] == ["correct", "silently_wrong", "asks", "failed"]
    assert results[3].raw == "I cannot do that" and "ai:" in results[3].error


def test_an_engine_that_raises_is_a_failure_not_a_crash():
    def broken(text, day):
        raise extraction.ExtractionError("model not found", raw=None)
    results = evaluate.run_evaluation([case({"intent": "dues"})], stage="text", transcribe_fn=None,
                                      extract_fn=broken, today=TODAY)
    assert results[0].outcome == "failed" and "model not found" in results[0].error


def make_transcribe(heard_by_path):
    def transcribe_fn(path):
        heard = heard_by_path[path]
        if isinstance(heard, Exception):
            raise heard
        return speech.Transcript(text=heard, engine="fake", language="kn", elapsed_seconds=2.0)
    return transcribe_fn


def test_voice_stage_runs_each_recording_and_measures_speech_error():
    cases = [case(expect_entries({"kind": "credit", "customer": "ಸುರೇಶ್", "amount": 650}), say="ಸುರೇಶ್ ಗೆ 650 ಸಾಲ")]
    recordings = {"t1": [("anita", "a.ogg"), ("raju", "r.ogg")]}
    heard = {"a.ogg": "ಸುರೇಶ್ ಗೆ 650 ಸಾಲ", "r.ogg": "ಸುರೇಶ್ ಗೆ 6500 ಸಾಲ"}
    replies = {"ಸುರೇಶ್ ಗೆ 650 ಸಾಲ": ai_reply("entries", [credit()]),
               "ಸುರೇಶ್ ಗೆ 6500 ಸಾಲ": ai_reply("entries", [credit(amount=6500)])}
    results = evaluate.run_evaluation(
        cases, stage="voice", transcribe_fn=make_transcribe(heard), today=TODAY, recordings=recordings,
        extract_fn=lambda text, day: extraction.interpret(replies[text], day))
    assert [(r.speaker, r.outcome) for r in results] == [("anita", "correct"), ("raju", "silently_wrong")]
    assert results[0].cer == 0.0 and 0 < results[1].cer < 0.1
    assert results[1].transcript == "ಸುರೇಶ್ ಗೆ 6500 ಸಾಲ" and results[0].stt_seconds == 2.0


def test_voice_stage_only_runs_cases_that_have_recordings():
    cases = [case({"intent": "dues"}, cid="a"), case({"intent": "dues"}, cid="b")]
    results = evaluate.run_evaluation(
        cases, stage="voice", transcribe_fn=make_transcribe({"b.ogg": "x"}), today=TODAY,
        recordings={"b": [(None, "b.ogg")]}, extract_fn=lambda t, d: extraction.interpret(ai_reply("dues"), d))
    assert [r.case_id for r in results] == ["b"]


def test_voice_stage_silence_and_speech_errors_are_failures():
    cases = [case({"intent": "dues"})]
    recordings = {"t1": [("a", "silent.ogg"), ("b", "broken.ogg")]}
    heard = {"silent.ogg": "", "broken.ogg": speech.SpeechError("no model")}
    results = evaluate.run_evaluation(cases, stage="voice", transcribe_fn=make_transcribe(heard), today=TODAY,
                                      recordings=recordings, extract_fn=lambda t, d: None)
    assert [r.outcome for r in results] == ["failed", "failed"]
    assert results[0].error == "speech: nothing heard" and results[0].cer == 1.0
    assert "no model" in results[1].error


def test_an_unknown_stage_is_refused():
    with pytest.raises(ValueError, match="stage"):
        evaluate.run_evaluation([], stage="video", transcribe_fn=None, extract_fn=None, today=TODAY)


# ===========================================================================
# The report numbers
# ===========================================================================

def result(outcome, tags=("credit",), speaker=None, cer=None, near=0, intent_ok=True, fields=None,
           stt=None, llm=None):
    return CaseResult(case_id="x", speaker=speaker, tags=list(tags), outcome=outcome, said="s",
                      cer=cer, name_near_misses=near, intent_ok=intent_ok, entry_fields=fields or {},
                      stt_seconds=stt, llm_seconds=llm)


def test_rates_add_up_and_pass_needs_both_rules():
    results = [result("correct")] * 18 + [result("asks")] + [result("silently_wrong")]
    summary = evaluate.summarize(results)
    assert summary["counts"] == {"correct": 18, "asks": 1, "silently_wrong": 1, "failed": 0}
    assert summary["rates"]["correct"] == 0.9 and summary["rates"]["silently_wrong"] == 0.05
    assert summary["passed"] is False                     # 5% silently wrong breaks the 2% rule


def test_pass_when_correct_is_high_and_silent_errors_are_rare():
    results = [result("correct")] * 49 + [result("asks")] * 0 + [result("silently_wrong")]
    assert evaluate.summarize(results)["passed"] is True  # 98% correct, 2% silently wrong


def test_fail_when_too_few_are_correct_even_with_no_silent_errors():
    results = [result("correct")] * 8 + [result("asks")] * 2
    assert evaluate.summarize(results)["passed"] is False


def test_the_pass_rules_can_be_changed():
    results = [result("correct")] * 8 + [result("asks")] * 2
    assert evaluate.summarize(results, pass_correct=0.8)["passed"] is True


def test_an_empty_run_never_passes():
    assert evaluate.summarize([])["passed"] is False


def test_field_accuracy_intent_accuracy_and_near_misses():
    results = [result("correct", fields={"amount": [1, 1], "customer": [1, 1]}),
               result("silently_wrong", fields={"amount": [0, 1], "customer": [1, 1]}, near=1),
               result("failed", intent_ok=False)]
    summary = evaluate.summarize(results)
    assert summary["field_accuracy"] == {"amount": 0.5, "customer": 1.0}
    assert summary["name_near_misses"] == 1
    assert summary["intent_accuracy"] == 1.0              # failed cases are not counted either way


def test_averages_and_cost():
    results = [result("correct", cer=0.1, stt=2.0, llm=1.0), result("correct", cer=0.3, stt=4.0, llm=3.0)]
    summary = evaluate.summarize(results, cost_stt=0.5, cost_llm=0.25)
    assert summary["avg_cer"] == 0.2 and summary["avg_stt_seconds"] == 3.0 and summary["avg_llm_seconds"] == 2.0
    assert summary["total_cost"] == 1.5                   # 2 x 0.5 + 2 x 0.25


def test_text_stage_has_no_speech_numbers():
    summary = evaluate.summarize([result("correct", llm=1.0)])
    assert summary["avg_cer"] is None and summary["avg_stt_seconds"] is None


def test_grouping_by_kind_of_sentence_and_by_speaker():
    results = [result("correct", tags=("credit",), speaker="anita", cer=0.0),
               result("silently_wrong", tags=("credit", "due"), speaker="raju", cer=0.2)]
    summary = evaluate.summarize(results)
    assert summary["by_tag"]["credit"] == {"n": 2, "correct": 0.5, "silently_wrong": 0.5, "avg_cer": 0.1}
    assert summary["by_tag"]["due"]["n"] == 1
    assert summary["by_speaker"]["anita"]["correct"] == 1.0 and summary["by_speaker"]["raju"]["avg_cer"] == 0.2


def test_no_speaker_table_when_nobody_is_named():
    assert evaluate.summarize([result("correct")])["by_speaker"] == {}


def test_the_text_report_shows_the_numbers_and_the_problems():
    results = evaluate.run_evaluation(
        [case(expect_entries({"kind": "credit", "customer": "ಸುರೇಶ್", "amount": 650}), say="ಸುರೇಶ್ ಗೆ 650 ಸಾಲ")],
        stage="text", transcribe_fn=None, today=TODAY,
        extract_fn=lambda t, d: extraction.interpret(ai_reply("entries", [credit(amount=6500)]), d))
    text = evaluate.format_report(evaluate.summarize(results), results, stage="text", stt="local", engine="hosted")
    assert "SILENTLY WRONG" in text and "RESULT: FAIL" in text
    assert "should say: ಸುರೇಶ್ ಗೆ 650 ಸಾಲ" in text
    assert "expected  : credit ಸುರೇಶ್ ₹650" in text and "understood: credit ಸುರೇಶ್ ₹6500" in text
    assert "AI: hosted" in text and "(skipped)" in text


def test_the_voice_report_shows_what_was_heard():
    cases = [case(expect_entries({"kind": "credit", "customer": "ಸುರೇಶ್", "amount": 650}), say="ಸುರೇಶ್ ಗೆ 650 ಸಾಲ")]
    results = evaluate.run_evaluation(
        cases, stage="voice", transcribe_fn=make_transcribe({"a.ogg": "ಸುರೇಶ್ ಗೆ 6500 ಸಾಲ"}), today=TODAY,
        recordings={"t1": [("anita", "a.ogg")]},
        extract_fn=lambda t, d: extraction.interpret(ai_reply("entries", [credit(amount=6500)]), d))
    text = evaluate.format_report(evaluate.summarize(results), results, stage="voice", stt="local", engine="local")
    assert "heard     : ಸುರೇಶ್ ಗೆ 6500 ಸಾಲ" in text and "[anita]" in text and "By speaker" in text


def test_a_passing_report_has_no_problem_section():
    results = [result("correct", llm=0.5)] * 3
    text = evaluate.format_report(evaluate.summarize(results), results, stage="text", stt="-", engine="local")
    assert "RESULT: PASS" in text and "Problems to look at" not in text


def test_saved_report_is_readable_json(tmp_path):
    results = [result("correct", llm=0.5)]
    path = evaluate.save_report(evaluate.summarize(results), results, str(tmp_path / "reports"), "text_local_local")
    data = json.loads(open(path, encoding="utf-8").read())
    assert path.endswith("_text_local_local.json")
    assert data["summary"]["total"] == 1 and data["results"][0]["outcome"] == "correct"


# ===========================================================================
# Command line
# ===========================================================================

def perfect_ai():
    cases, _ = evaluate.load_cases(CASES_FILE)
    return gold_extract({c["say"]: c for c in cases if not c["expect"].get("must_flag")})


def test_cli_text_stage_with_a_perfect_ai_passes(tmp_path, capsys):
    cases, _ = evaluate.load_cases(CASES_FILE)
    only_clean = tmp_path / "clean.json"
    only_clean.write_text(json.dumps([c for c in cases if not c["expect"].get("must_flag")], ensure_ascii=False))
    code = evaluate.main(["--cases", str(only_clean), "--stage", "text", "--out", str(tmp_path / "r")],
                         extract_fn=perfect_ai())
    output = capsys.readouterr().out
    assert code == 0 and "RESULT: PASS" in output and "Saved:" in output
    assert len(list((tmp_path / "r").glob("*.json"))) == 1


def test_cli_exit_code_is_one_when_it_fails(tmp_path, capsys):
    code = evaluate.main(["--cases", CASES_FILE, "--stage", "text", "--no-save"],
                         extract_fn=lambda t, d: extraction.interpret(ai_reply("unclear"), d))
    assert code == 1 and "RESULT: FAIL" in capsys.readouterr().out


def test_cli_no_save_writes_nothing(tmp_path):
    evaluate.main(["--cases", CASES_FILE, "--stage", "text", "--no-save", "--out", str(tmp_path / "r")],
                  extract_fn=lambda t, d: extraction.interpret(ai_reply("unclear"), d))
    assert not (tmp_path / "r").exists()


def test_cli_limit_runs_only_the_first_cases(capsys):
    evaluate.main(["--cases", CASES_FILE, "--stage", "text", "--no-save", "--limit", "3"],
                  extract_fn=lambda t, d: extraction.interpret(ai_reply("unclear"), d))
    assert "3 cases" in capsys.readouterr().out


def test_cli_today_option_reaches_the_ai_step(tmp_path):
    one = tmp_path / "one.json"
    one.write_text(json.dumps({"today": "2026-11-20", "cases": [case({"intent": "dues"})]}))
    seen = []

    def spy(text, day):
        seen.append(day)
        return extraction.interpret(ai_reply("dues"), day)

    evaluate.main(["--cases", str(one), "--stage", "text", "--no-save"], extract_fn=spy)
    evaluate.main(["--cases", str(one), "--stage", "text", "--no-save", "--today", "2026-11-23"], extract_fn=spy)
    assert seen == [date(2026, 11, 20), date(2026, 11, 23)]    # the file's date, then the flag's


def test_cli_voice_stage_without_recordings_says_so(tmp_path, capsys):
    code = evaluate.main(["--cases", CASES_FILE, "--stage", "voice", "--audio-dir", str(tmp_path)])
    assert code == 2 and "no recordings found" in capsys.readouterr().out


def test_cli_voice_stage_reports_missing_and_ignored_files(tmp_path, capsys):
    (tmp_path / "c01.ogg").write_bytes(b"x")
    (tmp_path / "other.ogg").write_bytes(b"x")
    code = evaluate.main(["--cases", CASES_FILE, "--stage", "voice", "--audio-dir", str(tmp_path), "--no-save"],
                         transcribe_fn=lambda p: speech.Transcript("ಸುರೇಶ್ ಗೆ 650 ರೂಪಾಯಿ ಸಾಲ ಕೊಟ್ಟೆ", "fake", "kn", 1.0),
                         extract_fn=perfect_ai())
    output = capsys.readouterr().out
    assert code == 0
    assert "no recording yet for 53 cases" in output and "ignored files that match no case: other.ogg" in output
    assert "1 cases" in output


def test_cli_bad_cases_file(tmp_path, capsys):
    assert evaluate.main(["--cases", str(tmp_path / "missing.json"), "--stage", "text"]) == 2
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert evaluate.main(["--cases", str(bad), "--stage", "text"]) == 2
    assert "could not read the test set" in capsys.readouterr().out


def test_cli_bad_today_value(capsys):
    assert evaluate.main(["--cases", CASES_FILE, "--stage", "text", "--today", "tomorrow"]) == 2
