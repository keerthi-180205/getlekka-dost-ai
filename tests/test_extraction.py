"""
Tests for Chunk 7: reading the owner's words (app/prompts.py, app/extraction.py).

No AI model, no internet and no API key are used: the engines are replaced by
small stand-ins that return a prepared reply. "Today" is fixed to Friday
20 November 2026 so no result depends on the real calendar.
Run with:   pytest -v
"""
import json
from datetime import date, datetime, timezone

import httpx
import pytest

from app import extraction, prompts
from app.extraction import (
    Extraction, ExtractionEngine, ExtractionError, ExtractedEntry, interpret, parse_due,
)

TODAY = date(2026, 11, 20)   # a Friday


# ---------- helpers ----------

def reply(intent="entries", entries=(), customer=None):
    """Build the JSON text an AI would send back."""
    return json.dumps({"intent": intent, "entries": list(entries), "customer": customer},
                      ensure_ascii=False)


def credit(customer="ಸುರೇಶ್", amount=650, **extra):
    return {"kind": "credit", "customer": customer, "amount": amount, "due": None, "note": None, **extra}


def payment(customer="ರಮೇಶ್", amount=200, **extra):
    return {"kind": "payment", "customer": customer, "amount": amount, "due": None, "note": None, **extra}


def sale(amount=1500, **extra):
    return {"kind": "sale", "customer": None, "amount": amount, "due": None, "note": None, **extra}


class FakeEngine(ExtractionEngine):
    """A stand-in engine: returns a prepared reply and remembers the prompts it got."""

    name = "fake"

    def __init__(self, text=None):
        self.text = text if text is not None else reply("entries", [credit()])
        self.prompts = []

    def complete(self, prompt):
        self.prompts.append(prompt)
        return self.text


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    """Forget cached engines and any extraction settings between tests."""
    extraction._engines.clear()
    for setting in ("EXTRACTION_ENGINE", "OLLAMA_URL", "OLLAMA_MODEL", "EXTRACTION_HOSTED_MODEL",
                    "GEMINI_API_KEY", "SPEECH_LANGUAGE"):
        monkeypatch.delenv(setting, raising=False)


def test_the_fixed_test_date_really_is_a_friday():
    assert TODAY.strftime("%A") == "Friday"


# ---------- parse_due: the CODE works out dates, never the AI ----------

@pytest.mark.parametrize("phrase, expected", [
    ("today", "2026-11-20"),
    ("tomorrow", "2026-11-21"),
    ("day_after_tomorrow", "2026-11-22"),
    ("day after tomorrow", "2026-11-22"),
    ("saturday", "2026-11-21"),
    ("sunday", "2026-11-22"),
    ("monday", "2026-11-23"),
    ("wednesday", "2026-11-25"),
    ("thursday", "2026-11-26"),
    ("friday", "2026-11-27"),            # today is Friday: next Friday, a week away
    ("  FRIDAY ", "2026-11-27"),
    ("in 5 days", "2026-11-25"),
    ("in 1 day", "2026-11-21"),
    ("in 0 days", "2026-11-20"),
    ("in 7 days", "2026-11-27"),
    ("in 366 days", "2027-11-21"),
    ("2026-12-01", "2026-12-01"),
    ("2026-11-20", "2026-11-20"),        # today itself is fine
])
def test_due_phrases_become_real_dates(phrase, expected):
    assert parse_due(phrase, TODAY) == (expected, True)


@pytest.mark.parametrize("nothing", [None, "", "   ", "null", "None"])
def test_no_due_date_is_understood_and_empty(nothing):
    assert parse_due(nothing, TODAY) == (None, True)


@pytest.mark.parametrize("bad", [
    "2026-11-19",        # in the past
    "2028-01-01",        # more than a year away
    "2026-13-45",        # not a real date
    "20261201",          # not the format we asked for
    "in 367 days",       # too far
    "in 1000 days",
    "next month",        # outside the allowed phrases
    "ಶುಕ್ರವಾರ",           # the AI must answer in English words
    5,                   # not text
    ["friday"],
])
def test_unusable_due_phrases_are_dropped_and_reported(bad):
    assert parse_due(bad, TODAY) == (None, False)


def test_weekday_is_always_one_to_seven_days_ahead():
    """For every starting day and every weekday: never today, never more than a week."""
    names = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    for offset in range(7):                       # every possible 'today'
        start = date(2026, 11, 16 + offset)       # Mon 16 Nov .. Sun 22 Nov
        for name in names:
            due, understood = parse_due(name, start)
            gap = (date.fromisoformat(due) - start).days
            assert understood and 1 <= gap <= 7
            assert date.fromisoformat(due).strftime("%A").lower() == name


# ---------- amounts ----------

@pytest.mark.parametrize("value, expected", [
    (650, 650), (650.0, 650), ("650", 650), (" 650 ", 650), ("1,500", 1500),
    ("₹650", 650), ("₹ 1,50,000", 150000), ("೬೫೦", 650), (1, 1),
])
def test_good_amounts(value, expected):
    assert extraction._parse_amount(value) == expected


@pytest.mark.parametrize("value", [
    0, -5, "0", "-50", 650.5, "650.5", "six hundred", "ಆರುನೂರು", "", None, True, False,
    [650], {"a": 1}, float("nan"), float("inf"),
])
def test_bad_amounts(value):
    assert extraction._parse_amount(value) is None


# ---------- interpret: checking the AI's reply ----------

def test_one_credit_is_understood():
    result = interpret(reply("entries", [credit("ಸುರೇಶ್", 650, due="friday", note="ಅಕ್ಕಿ")]), TODAY)
    assert result.intent == "entries"
    assert result.entries == (ExtractedEntry("credit", "ಸುರೇಶ್", 650, "2026-11-27", "ಅಕ್ಕಿ"),)
    assert result.rejected == ()
    assert not result.needs_attention


def test_several_entries_keep_their_order():
    result = interpret(reply("entries", [credit(), payment(), sale()]), TODAY)
    assert [e.kind for e in result.entries] == ["credit", "payment", "sale"]
    assert [e.customer for e in result.entries] == ["ಸುರೇಶ್", "ರಮೇಶ್", None]
    assert [e.amount for e in result.entries] == [650, 200, 1500]


def test_amounts_given_as_text_are_converted():
    result = interpret(reply("entries", [credit(amount="೬೫೦"), payment(amount="1,200")]), TODAY)
    assert [e.amount for e in result.entries] == [650, 1200]


def test_a_payment_ignores_a_due_date():
    result = interpret(reply("entries", [payment(due="friday")]), TODAY)
    assert result.entries[0].due_date is None
    assert result.entries[0].warnings == ()


def test_names_are_tidied():
    result = interpret(reply("entries", [credit("  ಸುರೇಶ್   ಗೌಡ ")]), TODAY)
    assert result.entries[0].customer == "ಸುರೇಶ್ ಗೌಡ"


def test_long_notes_are_cut():
    result = interpret(reply("entries", [credit(note="x" * 500)]), TODAY)
    assert len(result.entries[0].note) == extraction.MAX_NOTE_CHARS


@pytest.mark.parametrize("empty_note", [None, "", "   ", 5])
def test_empty_notes_become_none(empty_note):
    assert interpret(reply("entries", [credit(note=empty_note)]), TODAY).entries[0].note is None


# -- things that are rejected (never saved) --

@pytest.mark.parametrize("bad_amount", [0, -5, 650.5, "abc", None, True, "ಆರುನೂರು"])
def test_bad_amount_is_rejected(bad_amount):
    result = interpret(reply("entries", [credit(amount=bad_amount)]), TODAY)
    assert result.entries == ()
    assert [r.reason for r in result.rejected] == ["bad_amount"]
    assert result.needs_attention


@pytest.mark.parametrize("bad_kind", ["unknown", "refund", "", None, 5])
def test_unknown_kind_is_rejected(bad_kind):
    result = interpret(reply("entries", [{**credit(), "kind": bad_kind}]), TODAY)
    assert [r.reason for r in result.rejected] == ["unknown_kind"]


@pytest.mark.parametrize("kind_maker", [credit, payment])
@pytest.mark.parametrize("no_name", [None, "", "   ", 7])
def test_credit_and_payment_need_a_customer(kind_maker, no_name):
    result = interpret(reply("entries", [kind_maker(customer=no_name)]), TODAY)
    assert [r.reason for r in result.rejected] == ["missing_customer"]


def test_an_item_that_is_not_an_object_is_rejected():
    result = interpret(reply("entries", ["ಸುರೇಶ್ 650", 5, None]), TODAY)
    assert [r.reason for r in result.rejected] == ["not_an_object"] * 3


def test_good_parts_are_kept_when_one_part_is_bad():
    result = interpret(reply("entries", [credit(), payment(amount=0), sale()]), TODAY)
    assert [e.kind for e in result.entries] == ["credit", "sale"]
    assert [r.reason for r in result.rejected] == ["bad_amount"]
    assert result.needs_attention          # the owner is asked about the bad part


def test_kind_is_not_case_sensitive():
    result = interpret(reply("entries", [{**credit(), "kind": " CREDIT "}]), TODAY)
    assert result.entries[0].kind == "credit"


# -- warnings: saved only after the owner confirms --

def test_large_amount_is_flagged_not_dropped():
    result = interpret(reply("entries", [credit(amount=65000), credit(amount=100000), credit(amount=100001)]), TODAY)
    assert [e.warnings for e in result.entries] == [(), (), (extraction.WARN_LARGE_AMOUNT,)]
    assert result.needs_attention


def test_unusable_due_date_is_dropped_with_a_warning():
    result = interpret(reply("entries", [credit(due="next month")]), TODAY)
    assert result.entries[0].due_date is None
    assert result.entries[0].warnings == (extraction.WARN_DUE_NOT_UNDERSTOOD,)


def test_cash_sale_with_a_customer_name_loses_the_name_and_warns():
    result = interpret(reply("entries", [{**sale(), "customer": "ಸುರೇಶ್"}]), TODAY)
    assert result.entries[0].customer is None
    assert result.entries[0].warnings == (extraction.WARN_SALE_HAS_CUSTOMER,)


# -- the other intents --

def test_balance_question():
    result = interpret(reply("balance", customer=" ಸುರೇಶ್ "), TODAY)
    assert (result.intent, result.customer, result.entries) == ("balance", "ಸುರೇಶ್", ())
    assert not result.needs_attention


def test_balance_without_a_name_is_unclear():
    assert interpret(reply("balance", customer=None), TODAY).intent == "unclear"


def test_different_person_command():
    result = interpret(reply("different_person", customer=" ಸುರೇಶ್ "), TODAY)
    assert (result.intent, result.customer, result.entries) == ("different_person", "ಸುರೇಶ್", ())
    assert not result.needs_attention


def test_different_person_without_a_name_is_unclear():
    assert interpret(reply("different_person", customer=None), TODAY).intent == "unclear"


def test_dues_question():
    assert interpret(reply("dues"), TODAY).intent == "dues"


def test_undo_command():
    assert interpret(reply("undo"), TODAY).intent == "undo"


@pytest.mark.parametrize("intent", ["balance", "dues", "undo", "different_person"])
def test_contradictory_reply_is_unclear(intent):
    """E.g. 'balance' AND a list of entries: we do not guess which half was meant."""
    result = interpret(reply(intent, [credit()], customer="ಸುರೇಶ್"), TODAY)
    assert result.intent == "unclear"
    assert result.entries == ()


def test_unclear_needs_attention():
    result = interpret(reply("unclear"), TODAY)
    assert result.intent == "unclear" and result.needs_attention


@pytest.mark.parametrize("intent", ["gossip", "", None, 5, "ENTRIES_PLEASE"])
def test_unknown_intent_is_unclear(intent):
    assert interpret(json.dumps({"intent": intent, "entries": [credit()]}), TODAY).intent == "unclear"


def test_entries_intent_with_nothing_listed_is_unclear():
    assert interpret(reply("entries", []), TODAY).intent == "unclear"


def test_entries_that_is_not_a_list_is_treated_as_empty():
    assert interpret(json.dumps({"intent": "entries", "entries": "650"}), TODAY).intent == "unclear"


def test_intent_is_not_case_sensitive():
    assert interpret(reply(" DUES "), TODAY).intent == "dues"


# -- reading the AI's text --

def test_json_inside_code_fences_is_found():
    text = "```json\n" + reply("entries", [credit()]) + "\n```"
    assert interpret(text, TODAY).entries[0].amount == 650


def test_json_with_chatter_around_it_is_found():
    text = "Sure! Here you go: " + reply("dues") + " Hope that helps."
    assert interpret(text, TODAY).intent == "dues"


def test_raw_text_is_kept_for_the_audit_trail():
    text = reply("dues")
    assert interpret(text, TODAY).raw_text == text


@pytest.mark.parametrize("garbage", ["", "   ", None, "no json here", "{broken", "[1, 2]", '{"a": }'])
def test_unusable_replies_raise_with_the_raw_text(garbage):
    with pytest.raises(ExtractionError) as caught:
        interpret(garbage, TODAY)
    assert caught.value.raw == garbage


# ---------- the prompt ----------

def test_prompt_contains_the_owner_words_the_date_and_the_rules():
    prompt = prompts.build_extraction_prompt("ಸುರೇಶ್ ಗೆ 650 ಸಾಲ", TODAY)
    assert "ಸುರೇಶ್ ಗೆ 650 ಸಾಲ" in prompt
    assert "2026-11-20" in prompt and "Friday" in prompt
    assert "NEVER add, subtract or work" in prompt
    assert prompt.rstrip().endswith("JSON:")
    assert "{today}" not in prompt and "{weekday}" not in prompt   # every blank was filled


def test_prompt_puts_the_owner_words_last():
    prompt = prompts.build_extraction_prompt("UNIQUE-OWNER-WORDS", TODAY)
    assert prompt.rfind("UNIQUE-OWNER-WORDS") > prompt.rfind("EXAMPLES")


def test_unsupported_language_has_no_examples():
    with pytest.raises(ValueError, match="No examples"):
        prompts.build_extraction_prompt("hello", TODAY, language="ta")


@pytest.mark.parametrize("said, answer", prompts.EXAMPLES_BY_LANGUAGE["kn"],
                         ids=[s[:20] for s, _ in prompts.EXAMPLES_BY_LANGUAGE["kn"]])
def test_every_prompt_example_passes_our_own_checker(said, answer):
    """If an example teaches the AI something our checker would reject, we want to know."""
    result = interpret(json.dumps(answer, ensure_ascii=False), TODAY)
    assert result.intent == answer["intent"]
    # the one deliberate exception: an "unknown" kind is meant to be rejected
    deliberately_rejected = sum(1 for e in answer["entries"] if e["kind"] == "unknown")
    assert len(result.rejected) == deliberately_rejected
    assert not any(entry.warnings for entry in result.entries)


def test_prompt_examples_are_shown_to_the_ai_as_valid_json():
    prompt = prompts.build_extraction_prompt("x", TODAY)
    for _, answer in prompts.EXAMPLES_BY_LANGUAGE["kn"]:
        assert json.dumps(answer, ensure_ascii=False) in prompt


# ---------- extract(): the whole path with a stand-in engine ----------

def test_extract_returns_the_checked_result_with_details():
    engine = FakeEngine(reply("entries", [credit("ಸುರೇಶ್", 650, due="friday")]))
    result = extraction.extract("ಸುರೇಶ್ ಗೆ 650 ಸಾಲ", engine=engine, today=TODAY)
    assert result.entries[0].due_date == "2026-11-27"
    assert result.engine == "fake"
    assert result.elapsed_seconds >= 0
    assert result.raw_text == engine.text


def test_the_engine_receives_the_prompt():
    engine = FakeEngine()
    extraction.extract("ಸುರೇಶ್ ಗೆ 650 ಸಾಲ", engine=engine, today=TODAY)
    assert len(engine.prompts) == 1
    assert "ಸುರೇಶ್ ಗೆ 650 ಸಾಲ" in engine.prompts[0]
    assert "2026-11-20" in engine.prompts[0]


@pytest.mark.parametrize("silence", ["", "   ", "\n\t"])
def test_empty_transcript_is_unclear_and_the_ai_is_not_called(silence):
    engine = FakeEngine()
    result = extraction.extract(silence, engine=engine, today=TODAY)
    assert result.intent == "unclear" and result.needs_attention
    assert result.engine == "fake"
    assert engine.prompts == []


def test_today_defaults_to_india_today(monkeypatch):
    monkeypatch.setattr(extraction, "today_in_india", lambda: TODAY)
    result = extraction.extract("x", engine=FakeEngine(reply("entries", [credit(due="tomorrow")])))
    assert result.entries[0].due_date == "2026-11-21"


def test_extract_wraps_engine_crashes_in_one_clear_error():
    class Crashing(FakeEngine):
        def complete(self, prompt):
            raise RuntimeError("out of memory")

    with pytest.raises(ExtractionError, match="fake engine failed: out of memory") as caught:
        extraction.extract("x", engine=Crashing(), today=TODAY)
    assert caught.value.engine == "fake"


def test_engine_errors_pass_through_with_the_engine_name():
    class Picky(FakeEngine):
        def complete(self, prompt):
            raise ExtractionError("my own clear message")

    with pytest.raises(ExtractionError, match="^my own clear message$") as caught:
        extraction.extract("x", engine=Picky(), today=TODAY)
    assert caught.value.engine == "fake"


def test_invalid_ai_json_raises_with_raw_text_and_engine():
    with pytest.raises(ExtractionError) as caught:
        extraction.extract("x", engine=FakeEngine("I cannot do that."), today=TODAY)
    assert caught.value.raw == "I cannot do that."
    assert caught.value.engine == "fake"


def test_transcript_must_be_text():
    with pytest.raises(ExtractionError, match="must be text"):
        extraction.extract(None, engine=FakeEngine(), today=TODAY)


def test_overlong_transcript_is_refused_before_calling_the_ai():
    engine = FakeEngine()
    with pytest.raises(ExtractionError, match="too long"):
        extraction.extract("x" * (extraction.MAX_TRANSCRIPT_CHARS + 1), engine=engine, today=TODAY)
    assert engine.prompts == []


def test_unsupported_language_is_a_clear_error():
    with pytest.raises(ExtractionError, match="No examples"):
        extraction.extract("x", engine=FakeEngine(), today=TODAY, language="ta")


def test_language_follows_the_shared_setting(monkeypatch):
    monkeypatch.setenv("SPEECH_LANGUAGE", "ta")
    with pytest.raises(ExtractionError, match="No examples"):
        extraction.extract("x", engine=FakeEngine(), today=TODAY)


def test_needs_attention_is_false_only_when_everything_is_clean():
    clean = interpret(reply("entries", [credit(), payment(), sale()]), TODAY)
    assert not clean.needs_attention
    assert Extraction(intent="dues").needs_attention is False
    assert Extraction(intent="unclear").needs_attention is True


# ---------- choosing an engine ----------

def test_default_engine_is_local():
    assert extraction.get_engine().name == "local"


def test_engine_follows_the_setting(monkeypatch):
    monkeypatch.setenv("EXTRACTION_ENGINE", "hosted")
    assert extraction.get_engine().name == "hosted"


def test_explicit_name_beats_the_setting(monkeypatch):
    monkeypatch.setenv("EXTRACTION_ENGINE", "hosted")
    assert extraction.get_engine("local").name == "local"


def test_unknown_engine_lists_the_choices():
    with pytest.raises(ExtractionError, match="local, hosted"):
        extraction.get_engine("magic")


def test_engine_is_created_once_and_reused():
    assert extraction.get_engine("local") is extraction.get_engine("local")


def test_extract_can_pick_the_engine_by_name(monkeypatch):
    fake = FakeEngine(reply("dues"))
    monkeypatch.setitem(extraction._ENGINE_CLASSES, "fakename", lambda: fake)
    assert extraction.extract("x", engine="fakename", today=TODAY).intent == "dues"


# ---------- local engine (Ollama, with a fake HTTP call) ----------

class FakeHttpResponse:
    def __init__(self, status_code=200, body=None, bad_json=False):
        self.status_code = status_code
        self._body = body
        self._bad_json = bad_json

    def json(self):
        if self._bad_json:
            raise ValueError("not json")
        return self._body


class FakePost:
    """Stands in for httpx.post: records the call and returns a prepared response."""

    def __init__(self, response=None, error=None):
        self.response = response or FakeHttpResponse(200, {"message": {"content": reply("dues")}})
        self.error = error
        self.calls = []

    def __call__(self, url, json, timeout):
        self.calls.append({"url": url, "json": json, "timeout": timeout})
        if self.error:
            raise self.error
        return self.response


def test_ollama_returns_the_models_reply():
    engine = extraction.OllamaEngine(post=FakePost())
    assert engine.complete("PROMPT") == reply("dues")


def test_ollama_request_is_built_correctly():
    post = FakePost()
    extraction.OllamaEngine(url="http://box:1234/", model="m1", timeout=9, post=post).complete("PROMPT")
    call = post.calls[0]
    assert call["url"] == "http://box:1234/api/chat"      # trailing slash removed
    assert call["timeout"] == 9
    assert call["json"] == {
        "model": "m1",
        "messages": [{"role": "user", "content": "PROMPT"}],
        "stream": False,                 # one complete answer, not a stream
        "format": "json",                # Ollama must return valid JSON
        "options": {"temperature": 0},   # most likely answer, nothing creative
    }


def test_ollama_defaults_and_settings(monkeypatch):
    default = extraction.OllamaEngine(post=FakePost())
    assert (default.url, default.model) == ("http://localhost:11434", "llama3.1")
    monkeypatch.setenv("OLLAMA_URL", "http://other:1")
    monkeypatch.setenv("OLLAMA_MODEL", "llama3.2")
    configured = extraction.OllamaEngine(post=FakePost())
    assert (configured.url, configured.model) == ("http://other:1", "llama3.2")


def test_ollama_not_running_gives_a_helpful_error():
    engine = extraction.OllamaEngine(post=FakePost(error=httpx.ConnectError("refused")))
    with pytest.raises(ExtractionError, match="ollama serve"):
        engine.complete("x")


def test_ollama_too_slow_gives_a_helpful_error():
    engine = extraction.OllamaEngine(timeout=5, post=FakePost(error=httpx.ReadTimeout("slow")))
    with pytest.raises(ExtractionError, match="within 5 seconds"):
        engine.complete("x")


def test_ollama_missing_model_says_how_to_download_it():
    missing = FakeHttpResponse(404, {"error": "model 'llama3.1' not found"})
    engine = extraction.OllamaEngine(model="llama3.1", post=FakePost(missing))
    with pytest.raises(ExtractionError, match="not found.*ollama pull llama3.1"):
        engine.complete("x")


def test_ollama_error_without_details():
    broken = FakeHttpResponse(500, bad_json=True)
    with pytest.raises(ExtractionError, match=r"\(500\): no details"):
        extraction.OllamaEngine(post=FakePost(broken)).complete("x")


@pytest.mark.parametrize("body", [{}, {"message": {}}, {"message": "text"}, None])
def test_ollama_unexpected_reply_format(body):
    engine = extraction.OllamaEngine(post=FakePost(FakeHttpResponse(200, body)))
    with pytest.raises(ExtractionError, match="unexpected format"):
        engine.complete("x")


def test_extract_through_the_ollama_engine():
    body = {"message": {"content": reply("entries", [credit(due="in 3 days")])}}
    engine = extraction.OllamaEngine(post=FakePost(FakeHttpResponse(200, body)))
    result = extraction.extract("x", engine=engine, today=TODAY)
    assert result.engine == "local"
    assert result.entries[0].due_date == "2026-11-23"


# ---------- hosted engine (fake client) ----------

class FakeHostedResponse:
    def __init__(self, text):
        self.text = text


class FakeClient:
    """Stands in for the hosted service's client and records the request."""

    def __init__(self, text=None):
        self.requests = []
        self.text = reply("dues") if text is None else text
        self.models = self   # client.models.generate_content(...)

    def generate_content(self, model, contents, config):
        self.requests.append({"model": model, "contents": contents, "config": config})
        return FakeHostedResponse(self.text)


def test_hosted_returns_the_models_reply():
    engine = extraction.HostedEngine(api_key="k", client=FakeClient())
    assert engine.complete("PROMPT") == reply("dues")


def test_hosted_request_asks_for_json_with_no_creativity():
    client = FakeClient()
    extraction.HostedEngine(api_key="k", model="m2", client=client).complete("PROMPT")
    request = client.requests[0]
    assert request["model"] == "m2"
    assert request["contents"] == "PROMPT"
    assert request["config"] == {"response_mime_type": "application/json", "temperature": 0}


def test_hosted_model_is_a_setting(monkeypatch):
    monkeypatch.setenv("EXTRACTION_HOSTED_MODEL", "some-model")
    client = FakeClient()
    extraction.HostedEngine(api_key="k", client=client).complete("x")
    assert client.requests[0]["model"] == "some-model"


def test_hosted_needs_an_api_key():
    with pytest.raises(ExtractionError, match="GEMINI_API_KEY"):
        extraction.HostedEngine(api_key="").complete("x")


def test_hosted_reads_the_key_from_the_setting(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "from-env")
    assert extraction.HostedEngine().api_key == "from-env"


def test_hosted_reply_with_no_text_becomes_an_empty_answer_error():
    engine = extraction.HostedEngine(api_key="k", client=FakeClient())
    engine._client.text = None
    with pytest.raises(ExtractionError, match="empty answer"):
        extraction.extract("x", engine=engine, today=TODAY)


def test_hosted_service_errors_become_extraction_errors():
    class Failing(FakeClient):
        def generate_content(self, model, contents, config):
            raise ConnectionError("network down")

    engine = extraction.HostedEngine(api_key="k", client=Failing())
    with pytest.raises(ExtractionError, match="hosted engine failed: network down"):
        extraction.extract("x", engine=engine, today=TODAY)


# ---------- India time ----------

def test_today_in_india_is_a_date_close_to_utc_today():
    utc_today = datetime.now(timezone.utc).date()
    assert abs((extraction.today_in_india() - utc_today).days) <= 1


# ---------- command line ----------

def test_cli_prints_what_was_understood(capsys, monkeypatch):
    engine = FakeEngine(reply("entries", [credit("ಸುರೇಶ್", 650, due="friday", note="ಅಕ್ಕಿ"), sale(1500)]))
    monkeypatch.setattr(extraction, "get_engine", lambda name=None: engine)
    assert extraction.main(["ಸುರೇಶ್ ಗೆ 650 ಸಾಲ", "--today", "2026-11-20"]) == 0
    output = capsys.readouterr().out
    assert "intent: entries" in output
    assert "ಸುರೇಶ್" in output and "₹650" in output and "due 2026-11-27" in output
    assert "(cash sale)" in output and "₹1500" in output


def test_cli_shows_rejected_parts_and_the_warning(capsys, monkeypatch):
    engine = FakeEngine(reply("entries", [credit(), payment(amount=0)]))
    monkeypatch.setattr(extraction, "get_engine", lambda name=None: engine)
    extraction.main(["x"])
    output = capsys.readouterr().out
    assert "REJECTED (bad_amount)" in output
    assert "needs attention" in output


def test_cli_prints_a_balance_question(capsys, monkeypatch):
    monkeypatch.setattr(extraction, "get_engine", lambda name=None: FakeEngine(reply("balance", customer="ಸುರೇಶ್")))
    extraction.main(["x"])
    assert "about: ಸುರೇಶ್" in capsys.readouterr().out


def test_cli_reports_failures_and_shows_what_the_ai_said(capsys, monkeypatch):
    monkeypatch.setattr(extraction, "get_engine", lambda name=None: FakeEngine("I cannot do that."))
    assert extraction.main(["x"]) == 1
    output = capsys.readouterr().out
    assert "[FAILED]" in output and "I cannot do that." in output


def test_cli_rejects_a_bad_today_option(capsys, monkeypatch):
    monkeypatch.setattr(extraction, "get_engine", lambda name=None: FakeEngine())
    assert extraction.main(["x", "--today", "tomorrow"]) == 1
    assert "--today must look like" in capsys.readouterr().out
