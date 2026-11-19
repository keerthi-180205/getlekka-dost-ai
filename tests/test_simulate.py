"""
Tests for simulate.py: the practice chat tool. Only the parts that need no
running server: building Meta-shaped messages, reading commands, showing replies.

It also proves the loop closes: what simulate.py builds is something main.py
(with a fake Meta) really understands.
Run with:   pytest -v
"""
import json
import os

os.environ["SIMULATE"] = "true"

import pytest

import simulate
from app import db, extraction, ledger, main, speech, whatsapp
from tests.test_pipeline import ScriptedAI, ai_reply, credit


def message_of(payload):
    return payload["entry"][0]["changes"][0]["value"]["messages"][0]


# ---------- building messages ----------

def test_text_payload_has_the_shape_meta_sends():
    message = message_of(simulate.fake_meta_payload("ಸುರೇಶ್ 650"))
    assert message["type"] == "text" and message["text"] == {"body": "ಸುರೇಶ್ 650"}
    assert message["from"] == simulate.FAKE_PHONE and message["id"].startswith("wamid.sim.")


def test_every_payload_gets_a_new_message_id():
    ids = {message_of(simulate.fake_meta_payload("x"))["id"] for _ in range(5)}
    assert len(ids) == 5


def test_voice_payload_points_at_the_file_with_a_full_path(tmp_path):
    note = tmp_path / "note.ogg"
    note.write_bytes(b"x")
    message = message_of(simulate.fake_voice_payload(str(note)))
    assert message["type"] == "audio"
    assert message["audio"]["id"] == f"sim:{note}"


def test_button_payload_carries_the_id_back():
    message = message_of(simulate.fake_button_payload("undo:3", "ರದ್ದು"))
    assert message["type"] == "interactive"
    assert message["interactive"]["button_reply"] == {"id": "undo:3", "title": "ರದ್ದು"}


# ---------- reading what you typed ----------

BUTTONS = [{"id": "undo:3", "title": "ರದ್ದು"}, {"id": "q:1:2", "title": "ಬೇಡ"}]


def test_plain_text_is_sent_as_text():
    kind, payload = simulate.parse_command("  ಸುರೇಶ್ ಗೆ 650 ಸಾಲ ", [])
    assert kind == "send" and message_of(payload)["text"]["body"] == "ಸುರೇಶ್ ಗೆ 650 ಸಾಲ"


def test_empty_input_is_skipped():
    assert simulate.parse_command("   ", []) == ("skip", None)


def test_voice_command_with_a_real_file(tmp_path):
    note = tmp_path / "n.ogg"
    note.write_bytes(b"x")
    kind, payload = simulate.parse_command(f"/voice {note}", [])
    assert kind == "send" and message_of(payload)["type"] == "audio"


def test_voice_command_errors(tmp_path):
    assert simulate.parse_command("/voice", [])[0] == "error"
    kind, message = simulate.parse_command(f"/voice {tmp_path / 'missing.ogg'}", [])
    assert kind == "error" and "not found" in message


def test_tap_presses_the_numbered_button():
    kind, payload = simulate.parse_command("/tap 2", BUTTONS)
    assert kind == "send" and message_of(payload)["interactive"]["button_reply"]["id"] == "q:1:2"


@pytest.mark.parametrize("line", ["/tap", "/tap 0", "/tap 3", "/tap x", "/tap -1"])
def test_tap_with_a_bad_number(line):
    kind, message = simulate.parse_command(line, BUTTONS)
    assert kind == "error" and "1 to 2" in message


def test_tap_without_buttons():
    kind, message = simulate.parse_command("/tap 1", [])
    assert kind == "error" and "no buttons" in message


# ---------- showing replies ----------

def test_a_reply_with_buttons_shows_them_numbered():
    assert simulate.format_reply("ಬರೆದಿದ್ದೇನೆ", BUTTONS) == "ಬರೆದಿದ್ದೇನೆ\n    [1] ರದ್ದು\n    [2] ಬೇಡ"


def test_a_reply_without_buttons_is_just_the_text():
    assert simulate.format_reply("ಹಲೋ", []) == "ಹಲೋ"


# ---------- the loop closes: what the simulator builds, the server understands ----------

@pytest.fixture
def server_side(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("AUDIO_DIR", str(tmp_path / "audio"))
    db.init_db()
    sent = []
    monkeypatch.setattr(whatsapp, "send_text", lambda to, body: sent.append((body, None)) or f"sim.{len(sent)}")
    monkeypatch.setattr(whatsapp, "send_buttons",
                        lambda to, body, buttons: sent.append((body, buttons)) or f"sim.{len(sent)}")
    ai = ScriptedAI()
    monkeypatch.setattr(extraction, "get_engine", lambda name=None: ai)
    return ai, sent


def test_a_simulated_voice_note_and_button_tap_work_end_to_end(server_side, tmp_path, monkeypatch):
    ai, sent = server_side
    note = tmp_path / "note.ogg"
    note.write_bytes(b"FAKE-OGG")
    monkeypatch.setattr(speech, "transcribe", lambda path, language=None, engine=None: speech.Transcript(
        text="ಸುರೇಶ್ ಗೆ 650 ಸಾಲ", engine="fake", language="kn", elapsed_seconds=0.1))
    ai.script["ಸುರೇಶ್ ಗೆ 650 ಸಾಲ"] = ai_reply("entries", [credit()])

    _, voice = simulate.parse_command(f"/voice {note}", [])
    main.handle_message(message_of(voice))
    shop = ledger.get_or_create_shop(simulate.FAKE_PHONE)
    assert ledger.get_balance(shop, "ಸುರೇಶ್") == 650

    body, buttons = sent[-1]
    _, tapped = simulate.parse_command("/tap 1", buttons)
    main.handle_message(message_of(tapped))
    assert ledger.get_balance(shop, "ಸುರೇಶ್") == 0


def test_buttons_saved_by_the_server_are_readable_by_the_simulator(server_side, tmp_path, monkeypatch):
    """The simulator reads replies from the database; check that path with a real file."""
    ai, _ = server_side
    ai.script["x"] = ai_reply("entries", [credit()])
    main.handle_message(message_of(simulate.fake_meta_payload("x")))

    monkeypatch.setattr(simulate, "DB_PATH", db.DB_PATH)
    replies = simulate.wait_for_replies(after_id=0, timeout=2, quiet=0.2)
    assert len(replies) == 1
    text, buttons = replies[0]
    assert "ಬರೆದುಕೊಂಡಿದ್ದೇನೆ" in text
    assert buttons[0]["id"].startswith("undo:")
