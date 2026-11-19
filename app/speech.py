"""
speech.py - turns a voice note (an audio file) into text.   (Chunk 6)

The flow, in one picture:

    voice note file --> transcribe() --> Transcript(text="ಸುರೇಶ್ ಗೆ 650 ...")
                            |
                            +-- picks an ENGINE (a setting in .env):
                                  "local"  = faster-whisper, runs on this laptop,
                                             free, audio never leaves the machine
                                  "hosted" = Google Gemini over the internet,
                                             needs GEMINI_API_KEY, audio is SENT
                                             to Google (see privacy note below)

Why two engines behind one function: Kannada accuracy is the biggest unknown
of GetLekka. With one switch we can run the same recordings through both
engines and compare (the Chunk 11 accuracy report) without changing any code.

This file only turns sound into words. It does NOT understand them. Turning
"ಸುರೇಶ್ ಗೆ 650 ಸಾಲ" into a ledger entry is Chunk 7, and all money math stays
in ledger.py.

Settings (all optional, read from .env - see .env.example):
    SPEECH_ENGINE         local | hosted                 (default: local)
    SPEECH_LANGUAGE       kn | hi | en                   (default: kn = Kannada)
    WHISPER_MODEL         small | medium | large-v3 ...  (default: medium)
    WHISPER_DEVICE        auto | cpu | cuda              (default: auto)
    WHISPER_COMPUTE_TYPE  auto | int8 | float16 ...      (default: auto)
    GEMINI_API_KEY        your key (hosted engine only; never paste it in chat)
    SPEECH_HOSTED_MODEL   the hosted model name          (default: gemini-2.5-flash,
                          check Google's current model list before relying on it)

Privacy note: with the hosted engine a customer's voice note (which may name
customers and amounts) is sent to Google. Use it for your own test recordings;
get the shop owner's written consent before using it with real pilot shops.

Try it by hand on any recording:
    python -m app.speech my_note.ogg
    python -m app.speech my_note.ogg --engine hosted
"""
from __future__ import annotations

import argparse
import os
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()  # read .env so the settings above are available


class SpeechError(Exception):
    """Anything that goes wrong while turning audio into text (clear message inside)."""


# Languages we know how to ask for. The code goes to Whisper; the name goes
# into the hosted prompt. Adding Hindi or English later is just using these.
LANGUAGE_NAMES = {"kn": "Kannada", "hi": "Hindi", "en": "English"}

# WhatsApp voice notes are .ogg (Opus). The others are for test recordings
# made on a phone or laptop. Anything else is refused with a clear message.
AUDIO_MIME_TYPES = {
    ".ogg": "audio/ogg", ".oga": "audio/ogg", ".opus": "audio/ogg",
    ".mp3": "audio/mpeg", ".wav": "audio/wav", ".m4a": "audio/mp4",
    ".aac": "audio/aac", ".flac": "audio/flac", ".webm": "audio/webm",
}

# WhatsApp itself limits voice notes to about this size, and the hosted
# service has an upload limit too. A bigger file is almost surely a mistake.
MAX_AUDIO_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True)
class Transcript:
    """
    The result of transcribe().

    text             what was said, cleaned (may be empty if nothing was heard)
    engine           "local" or "hosted" - which engine produced it
    language         "kn", "hi" or "en"
    elapsed_seconds  how long the engine took. The FIRST local call is slower
                     because the model is loaded then. Used for the speed and
                     cost report in Chunk 11.
    """
    text: str
    engine: str
    language: str
    elapsed_seconds: float

    @property
    def is_empty(self) -> bool:
        """True when nothing intelligible was heard (silence, noise, no speech)."""
        return not self.text


# ---------------------------------------------------------------------------
# Engines: each one knows how to turn ONE audio file into raw text
# ---------------------------------------------------------------------------

class SpeechEngine:
    """The shape every engine follows: a `name`, and a transcribe() method."""

    name = "base"

    def transcribe(self, audio_path: str, language: str) -> str:
        raise NotImplementedError


class LocalWhisperEngine(SpeechEngine):
    """
    Runs OpenAI's Whisper model on this computer through `faster-whisper`.
    Free and private. The first call downloads/loads the model (can take a
    minute); after that the loaded model is reused.

    A bigger model is more accurate for Kannada but slower and needs more
    memory (your GPU has 4 GB, RAM is tight). Try `medium`, then `large-v3`
    if it fits. The accuracy test (Chunk 11) is how we decide.
    """

    name = "local"

    def __init__(self, model_size: str | None = None, device: str | None = None,
                 compute_type: str | None = None):
        self.model_size = model_size or os.getenv("WHISPER_MODEL", "medium")
        self.device = device or os.getenv("WHISPER_DEVICE", "auto")
        self.compute_type = compute_type or os.getenv("WHISPER_COMPUTE_TYPE", "auto")
        self._model = None  # loaded on first use, then kept

    def _load_model(self):
        """Load the model once. The import is here, not at the top of the file,
        so the rest of the app works even if faster-whisper isn't installed."""
        if self._model is None:
            try:
                from faster_whisper import WhisperModel
            except ImportError as err:
                raise SpeechError(
                    "The local engine needs faster-whisper. Install it with: "
                    "pip install faster-whisper"
                ) from err
            self._model = WhisperModel(
                self.model_size, device=self.device, compute_type=self.compute_type
            )
        return self._model

    def transcribe(self, audio_path: str, language: str) -> str:
        model = self._load_model()
        # language= forces Kannada instead of letting Whisper guess (it can
        # guess wrong on short clips). vad_filter skips long silences, which
        # reduces made-up text. `segments` is a generator: the real work
        # happens while we loop over it below.
        segments, _info = model.transcribe(
            audio_path, language=language, beam_size=5, vad_filter=True
        )
        return " ".join(segment.text.strip() for segment in segments)


class HostedEngine(SpeechEngine):
    """
    Sends the audio to Google's Gemini model and asks for a word-for-word
    transcript. Usually better at Kannada than a small local model, but it
    needs GEMINI_API_KEY, an internet connection, and the audio leaves the
    machine (see the privacy note at the top of this file).
    """

    name = "hosted"

    def __init__(self, api_key: str | None = None, model: str | None = None, client=None):
        self.api_key = api_key if api_key is not None else os.getenv("GEMINI_API_KEY", "")
        self.model = model or os.getenv("SPEECH_HOSTED_MODEL", "gemini-2.5-flash")
        self._client = client  # tests pass a fake client here

    def _get_client(self):
        if self._client is None:
            if not self.api_key:
                raise SpeechError(
                    "The hosted engine needs GEMINI_API_KEY in your .env file "
                    "(create the key yourself; never paste it in a chat)."
                )
            try:
                from google import genai
            except ImportError as err:
                raise SpeechError(
                    "The hosted engine needs google-genai. Install it with: "
                    "pip install google-genai"
                ) from err
            self._client = genai.Client(api_key=self.api_key)
        return self._client

    @staticmethod
    def _audio_part(data: bytes, mime_type: str):
        """Wrap the audio bytes in the format the hosted service expects."""
        from google.genai import types
        return types.Part.from_bytes(data=data, mime_type=mime_type)

    def transcribe(self, audio_path: str, language: str) -> str:
        client = self._get_client()
        language_name = LANGUAGE_NAMES[language]
        # The instruction matters: without "do not translate" a chat model
        # may answer in English or summarise instead of transcribing.
        prompt = (
            f"Transcribe this audio recording exactly as spoken. The speaker is a "
            f"shopkeeper speaking {language_name}, and may mix in English words. "
            f"Write {language_name} in its own script. Do not translate, summarise, "
            f"correct or add anything. Output only the transcript."
        )
        audio = self._audio_part(Path(audio_path).read_bytes(), _mime_type(audio_path))
        response = client.models.generate_content(model=self.model, contents=[prompt, audio])
        return response.text or ""  # text can be None when the service returns nothing


# ---------------------------------------------------------------------------
# Choosing an engine
# ---------------------------------------------------------------------------

_ENGINE_CLASSES = {"local": LocalWhisperEngine, "hosted": HostedEngine}

# Engines are kept after first use so the Whisper model is loaded only once.
_engines: dict[str, SpeechEngine] = {}


def get_engine(name: str | None = None) -> SpeechEngine:
    """
    Return the engine called `name` ("local" or "hosted"). With no name, use
    the SPEECH_ENGINE setting (default "local").
    """
    name = (name or os.getenv("SPEECH_ENGINE", "local")).strip().lower()
    if name not in _ENGINE_CLASSES:
        raise SpeechError(
            f"Unknown speech engine {name!r}. Choose one of: {', '.join(_ENGINE_CLASSES)}"
        )
    if name not in _engines:
        _engines[name] = _ENGINE_CLASSES[name]()
    return _engines[name]


# ---------------------------------------------------------------------------
# The one function the rest of the app calls
# ---------------------------------------------------------------------------

def _mime_type(audio_path: str) -> str:
    """The audio type from the file extension, or a clear error."""
    suffix = Path(audio_path).suffix.lower()
    if suffix not in AUDIO_MIME_TYPES:
        raise SpeechError(
            f"Unsupported audio type {suffix or '(none)'!r}. "
            f"Use one of: {', '.join(sorted(AUDIO_MIME_TYPES))}"
        )
    return AUDIO_MIME_TYPES[suffix]


def _check_audio_file(audio_path: str) -> Path:
    """Refuse a missing, empty, oversized or unsupported file BEFORE any engine runs."""
    path = Path(audio_path)
    if not path.is_file():
        raise SpeechError(f"Audio file not found: {audio_path}")
    size = path.stat().st_size
    if size == 0:
        raise SpeechError(f"Audio file is empty: {audio_path}")
    if size > MAX_AUDIO_BYTES:
        raise SpeechError(f"Audio file is too large ({size} bytes): {audio_path}")
    _mime_type(audio_path)  # raises if the extension isn't supported
    return path


def _clean_transcript(text: str) -> str:
    """
    Tidy the engine's raw text: normalize Unicode (so identical-looking
    Kannada is stored identically), and collapse any run of spaces or
    newlines into a single space.
    """
    return " ".join(unicodedata.normalize("NFC", text or "").split())


def transcribe(audio_path: str, language: str | None = None,
               engine: str | SpeechEngine | None = None) -> Transcript:
    """
    Turn one voice note into text.

    audio_path  path to the audio file (WhatsApp voice notes are .ogg)
    language    "kn", "hi" or "en". Default: the SPEECH_LANGUAGE setting (Kannada).
    engine      "local" or "hosted", or an engine object. Default: the
                SPEECH_ENGINE setting.

    Returns a Transcript. If nothing intelligible was heard the text is empty
    (check `.is_empty`); that is a normal result, not an error, because the
    caller decides what to tell the owner ("I couldn't hear that, please
    send it again"). Anything that breaks raises SpeechError.
    """
    language = (language or os.getenv("SPEECH_LANGUAGE", "kn")).strip().lower()
    if language not in LANGUAGE_NAMES:
        raise SpeechError(
            f"Unsupported language {language!r}. Choose one of: {', '.join(LANGUAGE_NAMES)}"
        )

    path = _check_audio_file(audio_path)
    engine = engine if isinstance(engine, SpeechEngine) else get_engine(engine)

    started = time.perf_counter()
    try:
        raw_text = engine.transcribe(str(path), language)
    except SpeechError:
        raise  # already has a clear message
    except Exception as err:  # engines can fail in many ways (no GPU, no network, ...)
        raise SpeechError(f"The {engine.name} speech engine failed: {err}") from err
    elapsed = time.perf_counter() - started

    return Transcript(
        text=_clean_transcript(raw_text),
        engine=engine.name,
        language=language,
        elapsed_seconds=round(elapsed, 2),
    )


# ---------------------------------------------------------------------------
# Try it from the terminal:  python -m app.speech note.ogg [--engine hosted]
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    """Transcribe one or more files and print the results. Returns 0 if all worked."""
    parser = argparse.ArgumentParser(description="Turn voice notes into text.")
    parser.add_argument("audio_files", nargs="+", help="one or more audio files")
    parser.add_argument("--engine", choices=sorted(_ENGINE_CLASSES), help="default: SPEECH_ENGINE setting")
    parser.add_argument("--language", choices=sorted(LANGUAGE_NAMES), help="default: SPEECH_LANGUAGE setting")
    args = parser.parse_args(argv)

    failures = 0
    for audio_file in args.audio_files:
        try:
            result = transcribe(audio_file, language=args.language, engine=args.engine)
        except SpeechError as err:
            failures += 1
            print(f"[FAILED] {audio_file}: {err}")
            continue
        label = "(nothing heard)" if result.is_empty else result.text
        print(f"[{result.engine}, {result.language}, {result.elapsed_seconds}s] {audio_file}")
        print(f"    {label}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
