"""
Tests for Chunk 6: speech-to-text (app/speech.py).

No real model, no internet and no API key are used. The engines are replaced
by small stand-ins, so these tests are fast and run anywhere.
Run with:   pytest -v
"""
import sys
import types

import pytest

from app import speech


# ---------- helpers ----------

class FakeEngine(speech.SpeechEngine):
    """A stand-in engine: returns the text it was given and remembers how it was called."""

    name = "fake"

    def __init__(self, text="ಸುರೇಶ್ ಗೆ 650 ಸಾಲ"):
        self.text = text
        self.calls = []

    def transcribe(self, audio_path, language):
        self.calls.append((audio_path, language))
        return self.text


class BrokenEngine(speech.SpeechEngine):
    name = "broken"

    def transcribe(self, audio_path, language):
        raise RuntimeError("no GPU found")


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    """Forget cached engines and any speech settings between tests."""
    speech._engines.clear()
    for setting in ("SPEECH_ENGINE", "SPEECH_LANGUAGE", "WHISPER_MODEL", "WHISPER_DEVICE",
                    "WHISPER_COMPUTE_TYPE", "GEMINI_API_KEY", "SPEECH_HOSTED_MODEL"):
        monkeypatch.delenv(setting, raising=False)


@pytest.fixture
def audio(tmp_path):
    """A tiny pretend voice note. The fake engines never read its contents."""
    path = tmp_path / "note.ogg"
    path.write_bytes(b"not real audio")
    return str(path)


# ---------- transcribe(): the main function ----------

def test_transcribe_returns_the_text_with_details(audio):
    engine = FakeEngine("ಸುರೇಶ್ ಗೆ 650 ಸಾಲ")
    result = speech.transcribe(audio, engine=engine)
    assert result.text == "ಸುರೇಶ್ ಗೆ 650 ಸಾಲ"
    assert result.engine == "fake"
    assert result.language == "kn"           # Kannada is the default
    assert result.elapsed_seconds >= 0
    assert not result.is_empty


def test_engine_receives_the_file_and_language(audio):
    engine = FakeEngine()
    speech.transcribe(audio, language="hi", engine=engine)
    assert engine.calls == [(audio, "hi")]


def test_language_comes_from_the_setting(audio, monkeypatch):
    monkeypatch.setenv("SPEECH_LANGUAGE", "en")
    assert speech.transcribe(audio, engine=FakeEngine()).language == "en"


def test_language_is_not_case_sensitive(audio):
    assert speech.transcribe(audio, language=" KN ", engine=FakeEngine()).language == "kn"


def test_unsupported_language_is_refused(audio):
    with pytest.raises(speech.SpeechError, match="language"):
        speech.transcribe(audio, language="ta", engine=FakeEngine())


def test_text_is_cleaned(audio):
    messy = "  ಸುರೇಶ್   ಗೆ \n 650\t ಸಾಲ  "
    assert speech.transcribe(audio, engine=FakeEngine(messy)).text == "ಸುರೇಶ್ ಗೆ 650 ಸಾಲ"


def test_text_is_unicode_normalized(audio):
    """Two spellings that look the same must come out as identical text."""
    composed = "ಕೊ"
    decomposed = "ಕೊ"
    assert composed != decomposed
    a = speech.transcribe(audio, engine=FakeEngine(composed)).text
    b = speech.transcribe(audio, engine=FakeEngine(decomposed)).text
    assert a == b


@pytest.mark.parametrize("silence", ["", "   ", "\n"])
def test_nothing_heard_is_an_empty_result_not_an_error(audio, silence):
    result = speech.transcribe(audio, engine=FakeEngine(silence))
    assert result.text == ""
    assert result.is_empty


def test_engine_can_be_chosen_by_name(audio, monkeypatch):
    fake = FakeEngine("hello")
    monkeypatch.setitem(speech._ENGINE_CLASSES, "fakename", lambda: fake)
    assert speech.transcribe(audio, engine="fakename").text == "hello"


# ---------- file checks happen BEFORE any engine runs ----------

def test_missing_file_is_refused(tmp_path):
    engine = FakeEngine()
    with pytest.raises(speech.SpeechError, match="not found"):
        speech.transcribe(str(tmp_path / "nope.ogg"), engine=engine)
    assert engine.calls == []


def test_empty_file_is_refused(tmp_path):
    empty = tmp_path / "empty.ogg"
    empty.write_bytes(b"")
    engine = FakeEngine()
    with pytest.raises(speech.SpeechError, match="empty"):
        speech.transcribe(str(empty), engine=engine)
    assert engine.calls == []


def test_unsupported_file_type_is_refused(tmp_path):
    odd = tmp_path / "note.txt"
    odd.write_bytes(b"hello")
    with pytest.raises(speech.SpeechError, match="Unsupported audio type"):
        speech.transcribe(str(odd), engine=FakeEngine())


def test_oversized_file_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(speech, "MAX_AUDIO_BYTES", 5)
    big = tmp_path / "big.ogg"
    big.write_bytes(b"123456")
    with pytest.raises(speech.SpeechError, match="too large"):
        speech.transcribe(str(big), engine=FakeEngine())


def test_a_folder_is_not_an_audio_file(tmp_path):
    with pytest.raises(speech.SpeechError, match="not found"):
        speech.transcribe(str(tmp_path), engine=FakeEngine())


@pytest.mark.parametrize("name", ["a.ogg", "a.OGG", "a.opus", "a.mp3", "a.wav", "a.m4a"])
def test_common_audio_types_are_accepted(tmp_path, name):
    path = tmp_path / name
    path.write_bytes(b"x")
    assert speech.transcribe(str(path), engine=FakeEngine("ok")).text == "ok"


# ---------- engine failures become one clear error ----------

def test_engine_crash_becomes_a_speech_error(audio):
    with pytest.raises(speech.SpeechError, match="broken speech engine failed: no GPU found"):
        speech.transcribe(audio, engine=BrokenEngine())


def test_speech_errors_pass_through_unchanged(audio):
    class Picky(speech.SpeechEngine):
        name = "picky"

        def transcribe(self, audio_path, language):
            raise speech.SpeechError("my own clear message")

    with pytest.raises(speech.SpeechError, match="^my own clear message$"):
        speech.transcribe(audio, engine=Picky())


# ---------- get_engine ----------

def test_default_engine_is_local():
    assert speech.get_engine().name == "local"


def test_engine_follows_the_setting(monkeypatch):
    monkeypatch.setenv("SPEECH_ENGINE", "hosted")
    assert speech.get_engine().name == "hosted"


def test_explicit_name_beats_the_setting(monkeypatch):
    monkeypatch.setenv("SPEECH_ENGINE", "hosted")
    assert speech.get_engine("local").name == "local"


def test_unknown_engine_name_lists_the_choices():
    with pytest.raises(speech.SpeechError, match="local, hosted"):
        speech.get_engine("magic")


def test_engine_is_created_once_and_reused():
    assert speech.get_engine("local") is speech.get_engine("local")


# ---------- local engine (fake faster-whisper library) ----------

class FakeSegment:
    def __init__(self, text):
        self.text = text


class FakeWhisperModel:
    """Stands in for faster_whisper.WhisperModel and records how it was used."""
    created = []

    def __init__(self, size, device, compute_type):
        self.args = (size, device, compute_type)
        self.transcribe_calls = []
        FakeWhisperModel.created.append(self)

    def transcribe(self, path, **options):
        self.transcribe_calls.append((path, options))
        return iter([FakeSegment(" ಸುರೇಶ್ "), FakeSegment("650 ")]), object()


@pytest.fixture
def fake_whisper(monkeypatch):
    FakeWhisperModel.created = []
    monkeypatch.setitem(sys.modules, "faster_whisper", types.SimpleNamespace(WhisperModel=FakeWhisperModel))
    return FakeWhisperModel


def test_local_engine_joins_the_segments(audio, fake_whisper):
    result = speech.transcribe(audio, engine="local")
    assert result.text == "ಸುರೇಶ್ 650"
    assert result.engine == "local"


def test_local_engine_asks_whisper_for_kannada(audio, fake_whisper):
    speech.transcribe(audio, engine="local")
    path, options = fake_whisper.created[0].transcribe_calls[0]
    assert path == audio
    assert options["language"] == "kn"        # forced, never guessed


def test_local_engine_uses_the_model_settings(audio, fake_whisper, monkeypatch):
    monkeypatch.setenv("WHISPER_MODEL", "large-v3")
    monkeypatch.setenv("WHISPER_DEVICE", "cpu")
    monkeypatch.setenv("WHISPER_COMPUTE_TYPE", "int8")
    speech.transcribe(audio, engine="local")
    assert fake_whisper.created[0].args == ("large-v3", "cpu", "int8")


def test_local_engine_defaults(audio, fake_whisper):
    speech.transcribe(audio, engine="local")
    assert fake_whisper.created[0].args == ("medium", "auto", "auto")


def test_local_model_is_loaded_only_once(audio, fake_whisper):
    speech.transcribe(audio, engine="local")
    speech.transcribe(audio, engine="local")
    assert len(fake_whisper.created) == 1
    assert len(fake_whisper.created[0].transcribe_calls) == 2


def test_local_engine_explains_a_missing_package(audio, monkeypatch):
    monkeypatch.setitem(sys.modules, "faster_whisper", None)   # makes the import fail
    with pytest.raises(speech.SpeechError, match="pip install faster-whisper"):
        speech.transcribe(audio, engine="local")


# ---------- hosted engine (fake client) ----------

class FakeResponse:
    def __init__(self, text):
        self.text = text


class FakeClient:
    """Stands in for the hosted service's client and records the request."""

    def __init__(self, reply="ಸುರೇಶ್ 650"):
        self.requests = []
        self.reply = reply
        self.models = self   # client.models.generate_content(...)

    def generate_content(self, model, contents):
        self.requests.append({"model": model, "contents": contents})
        return FakeResponse(self.reply)


@pytest.fixture
def no_real_audio_part(monkeypatch):
    """Skip building the real upload object; keep just the bytes and type."""
    monkeypatch.setattr(speech.HostedEngine, "_audio_part", staticmethod(lambda data, mime: (data, mime)))


def test_hosted_engine_returns_the_reply(audio, no_real_audio_part):
    engine = speech.HostedEngine(api_key="k", client=FakeClient("ಸುರೇಶ್ 650"))
    assert speech.transcribe(audio, engine=engine).text == "ಸುರೇಶ್ 650"


def test_hosted_engine_sends_audio_and_a_transcribe_only_prompt(audio, no_real_audio_part):
    client = FakeClient()
    speech.transcribe(audio, engine=speech.HostedEngine(api_key="k", client=client))
    request = client.requests[0]
    prompt, (data, mime) = request["contents"]
    assert data == b"not real audio"
    assert mime == "audio/ogg"
    assert "Kannada" in prompt
    assert "Do not translate" in prompt
    assert "Output only the transcript" in prompt


def test_hosted_prompt_follows_the_language(audio, no_real_audio_part):
    client = FakeClient()
    speech.transcribe(audio, language="hi", engine=speech.HostedEngine(api_key="k", client=client))
    assert "Hindi" in client.requests[0]["contents"][0]


def test_hosted_model_name_is_a_setting(audio, no_real_audio_part, monkeypatch):
    monkeypatch.setenv("SPEECH_HOSTED_MODEL", "some-model")
    client = FakeClient()
    speech.transcribe(audio, engine=speech.HostedEngine(api_key="k", client=client))
    assert client.requests[0]["model"] == "some-model"


def test_hosted_engine_handles_a_reply_with_no_text(audio, no_real_audio_part):
    engine = speech.HostedEngine(api_key="k", client=FakeClient(reply=None))
    result = speech.transcribe(audio, engine=engine)
    assert result.is_empty


def test_hosted_engine_needs_an_api_key(audio):
    with pytest.raises(speech.SpeechError, match="GEMINI_API_KEY"):
        speech.transcribe(audio, engine=speech.HostedEngine(api_key=""))


def test_hosted_engine_reads_the_key_from_the_setting(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "from-env")
    assert speech.HostedEngine().api_key == "from-env"


def test_hosted_errors_become_speech_errors(audio, no_real_audio_part):
    class Failing(FakeClient):
        def generate_content(self, model, contents):
            raise ConnectionError("network down")

    engine = speech.HostedEngine(api_key="k", client=Failing())
    with pytest.raises(speech.SpeechError, match="hosted speech engine failed: network down"):
        speech.transcribe(audio, engine=engine)


# ---------- command line ----------

def test_cli_prints_the_transcript(audio, capsys, monkeypatch):
    monkeypatch.setattr(speech, "get_engine", lambda name=None: FakeEngine("ಸುರೇಶ್ 650"))
    assert speech.main([audio]) == 0
    output = capsys.readouterr().out
    assert "ಸುರೇಶ್ 650" in output
    assert "fake" in output


def test_cli_reports_a_bad_file_and_exits_non_zero(tmp_path, audio, capsys, monkeypatch):
    monkeypatch.setattr(speech, "get_engine", lambda name=None: FakeEngine("ok"))
    code = speech.main([str(tmp_path / "missing.ogg"), audio])
    output = capsys.readouterr().out
    assert code == 1
    assert "[FAILED]" in output
    assert "ok" in output            # the good file was still processed


def test_cli_says_when_nothing_was_heard(audio, capsys, monkeypatch):
    monkeypatch.setattr(speech, "get_engine", lambda name=None: FakeEngine(""))
    speech.main([audio])
    assert "(nothing heard)" in capsys.readouterr().out
