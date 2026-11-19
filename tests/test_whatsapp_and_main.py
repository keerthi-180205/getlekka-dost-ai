"""
Tests for Chunk 9, the WhatsApp edge: app/whatsapp.py, app/audio_store.py, app/main.py.

Meta is faked everywhere: no message is really sent and nothing is downloaded.
Run with:   pytest -v
"""
import json
import os

# main.py loads config.py, which wants WhatsApp tokens unless this is on.
# Set BEFORE importing app.main so these tests never need real secrets.
os.environ["SIMULATE"] = "true"

import pytest
from fastapi.testclient import TestClient

from app import audio_store, db, extraction, ledger, main, replies, speech, whatsapp
from tests.test_pipeline import ScriptedAI, ai_reply, credit, payment

PHONE = "919876543210"


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("AUDIO_DIR", str(tmp_path / "audio"))
    db.init_db()


@pytest.fixture
def ai(monkeypatch):
    engine = ScriptedAI()
    monkeypatch.setattr(extraction, "get_engine", lambda name=None: engine)
    return engine


# ===========================================================================
# whatsapp.py (real mode, with a fake internet)
# ===========================================================================

class FakeResponse:
    def __init__(self, status_code=200, body=None, content=b"", text=""):
        self.status_code, self._body, self.content, self.text = status_code, body or {}, content, text

    def json(self):
        return self._body


@pytest.fixture
def real_mode(monkeypatch):
    """Pretend we are NOT in practice mode, with a fake token and a recording fake internet."""
    monkeypatch.setattr(whatsapp.config, "SIMULATE", False)
    monkeypatch.setattr(whatsapp.config, "WHATSAPP_TOKEN", "TESTTOKEN")

    class Net:
        posts, gets, responses = [], [], []

    net = Net()
    net.posts, net.gets, net.responses = [], [], []

    def fake_post(url, json=None, headers=None, timeout=None):
        net.posts.append({"url": url, "json": json, "headers": headers})
        return net.responses.pop(0) if net.responses else FakeResponse(200, {"messages": [{"id": "wamid.OUT"}]})

    def fake_get(url, headers=None, timeout=None):
        net.gets.append({"url": url, "headers": headers})
        return net.responses.pop(0)

    monkeypatch.setattr(whatsapp.httpx, "post", fake_post)
    monkeypatch.setattr(whatsapp.httpx, "get", fake_get)
    return net


def test_send_text_builds_the_request_and_returns_the_message_id(real_mode):
    assert whatsapp.send_text("919876543210", "ನಮಸ್ಕಾರ") == "wamid.OUT"
    sent = real_mode.posts[0]
    assert sent["json"] == {"messaging_product": "whatsapp", "to": "919876543210",
                            "type": "text", "text": {"body": "ನಮಸ್ಕಾರ"}}
    assert sent["headers"] == {"Authorization": "Bearer TESTTOKEN"}


def test_send_buttons_builds_an_interactive_message(real_mode):
    buttons = [{"id": "undo:3", "title": "ರದ್ದು ಮಾಡಿ"}, {"id": "q:1:2", "title": "ಬೇಡ"}]
    assert whatsapp.send_buttons("919876543210", "ಬರೆದಿದ್ದೇನೆ", buttons) == "wamid.OUT"
    assert real_mode.posts[0]["json"] == {
        "messaging_product": "whatsapp", "to": "919876543210", "type": "interactive",
        "interactive": {
            "type": "button", "body": {"text": "ಬರೆದಿದ್ದೇನೆ"},
            "action": {"buttons": [
                {"type": "reply", "reply": {"id": "undo:3", "title": "ರದ್ದು ಮಾಡಿ"}},
                {"type": "reply", "reply": {"id": "q:1:2", "title": "ಬೇಡ"}},
            ]},
        },
    }


def test_a_failed_send_returns_none(real_mode, capsys):
    real_mode.responses.append(FakeResponse(401, text="token expired"))
    assert whatsapp.send_text("1", "x") is None
    assert "FAILED 401" in capsys.readouterr().out


def test_practice_mode_never_touches_the_internet(monkeypatch):
    monkeypatch.setattr(whatsapp.config, "SIMULATE", True)

    def boom(*args, **kwargs):
        raise AssertionError("the internet must not be used in practice mode")

    monkeypatch.setattr(whatsapp.httpx, "post", boom)
    assert whatsapp.send_text("1", "x").startswith("sim.")
    assert whatsapp.send_buttons("1", "x", [{"id": "a", "title": "b"}]).startswith("sim.")


def test_download_media_asks_meta_for_the_link_then_downloads(real_mode):
    real_mode.responses += [
        FakeResponse(200, {"url": "https://cdn.example/voice", "mime_type": "audio/ogg; codecs=opus"}),
        FakeResponse(200, content=b"OGGDATA"),
    ]
    assert whatsapp.download_media("MEDIA123") == (b"OGGDATA", "audio/ogg; codecs=opus")
    assert real_mode.gets[0]["url"].endswith("/MEDIA123")
    assert real_mode.gets[1]["url"] == "https://cdn.example/voice"
    assert all(g["headers"] == {"Authorization": "Bearer TESTTOKEN"} for g in real_mode.gets)


def test_download_media_when_meta_refuses_the_lookup(real_mode):
    real_mode.responses.append(FakeResponse(400, text="bad id"))
    with pytest.raises(whatsapp.MediaError, match="lookup"):
        whatsapp.download_media("X")


def test_download_media_refuses_a_link_that_is_not_https(real_mode):
    real_mode.responses.append(FakeResponse(200, {"url": "http://evil.example/x"}))
    with pytest.raises(whatsapp.MediaError, match="secure"):
        whatsapp.download_media("X")
    assert len(real_mode.gets) == 1                       # we never fetched it


def test_download_media_when_the_file_download_fails(real_mode):
    real_mode.responses += [FakeResponse(200, {"url": "https://cdn.example/v"}), FakeResponse(404)]
    with pytest.raises(whatsapp.MediaError, match="download failed"):
        whatsapp.download_media("X")


def test_practice_mode_reads_a_simulated_voice_note_from_disk(tmp_path, monkeypatch):
    monkeypatch.setattr(whatsapp.config, "SIMULATE", True)
    note = tmp_path / "note.ogg"
    note.write_bytes(b"AUDIO")
    data, mime = whatsapp.download_media(f"sim:{note}")
    assert data == b"AUDIO" and mime.startswith("audio/")


def test_practice_mode_only_reads_simulated_ids(tmp_path, monkeypatch):
    monkeypatch.setattr(whatsapp.config, "SIMULATE", True)
    with pytest.raises(whatsapp.MediaError, match="practice mode"):
        whatsapp.download_media("/etc/passwd")
    with pytest.raises(whatsapp.MediaError, match="not found"):
        whatsapp.download_media(f"sim:{tmp_path / 'missing.ogg'}")


# ===========================================================================
# audio_store.py
# ===========================================================================

def test_audio_is_saved_under_the_message_id_with_the_right_ending(tmp_path):
    path = audio_store.save_audio("wamid.ABC123", b"DATA", "audio/ogg; codecs=opus")
    assert path.endswith("wamid_ABC123.ogg")
    assert open(path, "rb").read() == b"DATA"
    assert os.path.dirname(path) == str(tmp_path / "audio")        # folder was created


@pytest.mark.parametrize("mime, ending", [
    ("audio/ogg", ".ogg"), ("AUDIO/MPEG", ".mp3"), ("audio/mp4", ".m4a"),
    ("audio/x-wav", ".wav"), ("audio/ogg ; codecs=opus", ".ogg"), ("video/mp4", ".bin"), ("", ".bin"),
])
def test_file_ending_follows_the_mime_type(mime, ending):
    assert audio_store.save_audio("id", b"x", mime).endswith(ending)


def test_an_unsafe_message_id_cannot_escape_the_audio_folder(tmp_path):
    path = audio_store.save_audio("../../etc/evil", b"x", "audio/ogg")
    assert os.path.dirname(os.path.abspath(path)) == os.path.abspath(tmp_path / "audio")
    assert "/" not in os.path.basename(path)


def test_empty_and_oversized_audio_are_refused(monkeypatch):
    with pytest.raises(audio_store.AudioError, match="empty"):
        audio_store.save_audio("id", b"", "audio/ogg")
    monkeypatch.setattr(audio_store, "MAX_AUDIO_BYTES", 3)
    with pytest.raises(audio_store.AudioError, match="too large"):
        audio_store.save_audio("id", b"1234", "audio/ogg")


def test_saving_the_same_message_twice_uses_the_same_file():
    assert audio_store.save_audio("id", b"1", "audio/ogg") == audio_store.save_audio("id", b"2", "audio/ogg")


def test_a_saved_voice_note_has_a_type_the_speech_module_accepts():
    path = audio_store.save_audio("wamid.1", b"x", "audio/ogg; codecs=opus")
    assert speech._check_audio_file(path)


# ===========================================================================
# main.py: the whole door, with a fake Meta
# ===========================================================================

@pytest.fixture
def outbox(monkeypatch):
    """Records everything main.py would send, instead of sending it."""
    sent = []
    monkeypatch.setattr(whatsapp, "send_text",
                        lambda to, body: sent.append(("text", to, body, None)) or f"sim.{len(sent)}")
    monkeypatch.setattr(whatsapp, "send_buttons",
                        lambda to, body, buttons: sent.append(("buttons", to, body, buttons)) or f"sim.{len(sent)}")
    return sent


def text_message(body, msg_id="wamid.1"):
    return {"id": msg_id, "from": PHONE, "type": "text", "text": {"body": body}}


def audio_message(media_id="MEDIA1", msg_id="wamid.A1"):
    return {"id": msg_id, "from": PHONE, "type": "audio", "audio": {"id": media_id, "mime_type": "audio/ogg"}}


def button_message(button_id, msg_id="wamid.B1"):
    return {"id": msg_id, "from": PHONE, "type": "interactive",
            "interactive": {"type": "button_reply", "button_reply": {"id": button_id, "title": "x"}}}


def stored_messages(direction=None):
    with db.connect() as conn:
        rows = conn.execute("SELECT * FROM messages ORDER BY id").fetchall()
    return [dict(r) for r in rows if direction is None or r["direction"] == direction]


def test_a_text_is_saved_answered_and_the_reply_is_recorded(ai, outbox):
    ai.script["ಸುರೇಶ್ ಗೆ 650 ಸಾಲ"] = ai_reply("entries", [credit()])
    main.handle_message(text_message("ಸುರೇಶ್ ಗೆ 650 ಸಾಲ"))

    shop = ledger.get_or_create_shop(PHONE)
    assert ledger.get_balance(shop, "ಸುರೇಶ್") == 650
    kind, to, body, buttons = outbox[0]
    assert (kind, to) == ("buttons", PHONE)
    assert body.startswith(replies.SAVED_HEADER) and buttons[0]["id"].startswith("undo:")

    incoming, outgoing = stored_messages("in"), stored_messages("out")
    assert incoming[0]["body"] == "ಸುರೇಶ್ ಗೆ 650 ಸಾಲ"
    assert outgoing[0]["msg_type"] == "interactive"
    assert json.loads(outgoing[0]["buttons"]) == buttons


def test_the_shop_is_created_from_the_senders_number(ai, outbox):
    ai.script["x"] = ai_reply("unclear")
    main.handle_message(text_message("x"))
    with db.connect() as conn:
        assert conn.execute("SELECT owner_phone FROM shops").fetchone()["owner_phone"] == PHONE


def test_the_batch_is_linked_to_the_incoming_message(ai, outbox):
    ai.script["x"] = ai_reply("entries", [credit()])
    main.handle_message(text_message("x"))
    with db.connect() as conn:
        batch = conn.execute("SELECT message_id FROM batches").fetchone()
    assert batch["message_id"] == db.get_message_id("wamid.1")


def test_a_reply_without_buttons_goes_out_as_plain_text(ai, outbox):
    ai.script["x"] = ai_reply("unclear")
    main.handle_message(text_message("x"))
    assert outbox[0][0] == "text" and outbox[0][2] == replies.UNCLEAR
    assert stored_messages("out")[0]["msg_type"] == "text"


def test_a_duplicate_message_is_ignored(ai, outbox):
    ai.script["x"] = ai_reply("entries", [credit()])
    main.handle_message(text_message("x"))
    main.handle_message(text_message("x"))                 # Meta retried
    assert len(outbox) == 1
    assert ledger.get_balance(ledger.get_or_create_shop(PHONE), "ಸುರೇಶ್") == 650


def test_a_voice_note_is_downloaded_saved_transcribed_and_answered(ai, outbox, monkeypatch):
    monkeypatch.setattr(whatsapp, "download_media", lambda media_id: (b"OGGDATA", "audio/ogg; codecs=opus"))
    monkeypatch.setattr(speech, "transcribe", lambda path, language=None, engine=None: speech.Transcript(
        text="ಸುರೇಶ್ ಗೆ 650 ಸಾಲ", engine="fake-stt", language="kn", elapsed_seconds=0.5))
    ai.script["ಸುರೇಶ್ ಗೆ 650 ಸಾಲ"] = ai_reply("entries", [credit()])

    main.handle_message(audio_message())

    shop = ledger.get_or_create_shop(PHONE)
    assert ledger.get_balance(shop, "ಸುರೇಶ್") == 650
    with db.connect() as conn:
        batch = dict(conn.execute("SELECT * FROM batches").fetchone())
    assert batch["source"] == "voice" and batch["audio_path"].endswith("wamid_A1.ogg")
    assert open(batch["audio_path"], "rb").read() == b"OGGDATA"       # the proof is on disk
    assert batch["message_id"] == db.get_message_id("wamid.A1")


def test_a_failed_download_still_answers_politely(ai, outbox, monkeypatch):
    def broken(media_id):
        raise whatsapp.MediaError("Meta refused")
    monkeypatch.setattr(whatsapp, "download_media", broken)
    main.handle_message(audio_message())
    assert outbox[0][2] == replies.TECH_PROBLEM


def test_an_unexpected_crash_still_answers_and_does_not_escape(ai, outbox, monkeypatch, capsys):
    def explode(shop_id, text, message_id=None, today=None):
        raise RuntimeError("bug!")
    monkeypatch.setattr(main.pipeline, "handle_text", explode)
    main.handle_message(text_message("x"))                 # must not raise
    assert outbox[0][2] == replies.TECH_PROBLEM
    assert "bug!" in capsys.readouterr().err               # the traceback is printed for you


def test_a_button_tap_undoes_the_note(ai, outbox):
    ai.script["x"] = ai_reply("entries", [credit()])
    main.handle_message(text_message("x"))
    undo_id = outbox[0][3][0]["id"]
    main.handle_message(button_message(undo_id))
    assert replies.UNDONE_HEADER in outbox[1][2]
    assert ledger.get_balance(ledger.get_or_create_shop(PHONE), "ಸುರೇಶ್") == 0


def test_a_button_answer_to_a_question_saves_the_entry(ai, outbox):
    shop = ledger.get_or_create_shop(PHONE)
    ledger.add_credit(shop, "Suresh milk", 450)
    ledger.add_credit(shop, "Suresh tailor", 300)
    ai.script["x"] = ai_reply("entries", [credit("Suresh", 650)])
    main.handle_message(text_message("x"))
    first_option = outbox[0][3][0]["id"]
    main.handle_message(button_message(first_option))
    assert ledger.get_balance(shop, "Suresh tailor") == 950


def test_an_interactive_message_without_a_button_is_unsupported(ai, outbox):
    main.handle_message({"id": "wamid.L", "from": PHONE, "type": "interactive",
                         "interactive": {"type": "list_reply", "list_reply": {"id": "a"}}})
    assert outbox[0][2] == replies.UNSUPPORTED


@pytest.mark.parametrize("kind", ["image", "sticker", "video", "document", "location", "button", None])
def test_other_message_types_get_the_unsupported_reply(ai, outbox, kind):
    main.handle_message({"id": f"wamid.{kind}", "from": PHONE, "type": kind})
    assert outbox[0][2] == replies.UNSUPPORTED


def test_a_very_long_receipt_is_split_so_the_buttons_still_fit(ai, outbox):
    many = [credit(f"ಗ್ರಾಹಕ ಒಂದು ಎರಡು {n}", 100 + n) for n in range(40)]
    ai.script["x"] = ai_reply("entries", many)
    main.handle_message(text_message("x"))
    assert [item[0] for item in outbox] == ["text", "buttons"]
    assert len(outbox[0][2]) > replies.BODY_WITH_BUTTONS_MAX          # the long list went as plain text
    assert outbox[1][2] == replies.PRESS_BELOW and len(outbox[1][3]) == 1
    assert len(outbox[1][2]) <= replies.BODY_WITH_BUTTONS_MAX


def test_two_owners_never_see_each_others_books(ai, outbox):
    ai.script["x"] = ai_reply("entries", [credit()])
    main.handle_message(text_message("x"))
    other = {"id": "wamid.2", "from": "911111111111", "type": "text", "text": {"body": "x"}}
    main.handle_message(other)
    a, b = ledger.get_or_create_shop(PHONE), ledger.get_or_create_shop("911111111111")
    assert a != b and ledger.get_balance(a, "ಸುರೇಶ್") == 650 and ledger.get_balance(b, "ಸುರೇಶ್") == 650
    assert len(ledger.list_customers(a)) == 1 and len(ledger.list_customers(b)) == 1


# ---------- the HTTP door itself ----------

def meta_payload(*messages):
    return {"object": "whatsapp_business_account",
            "entry": [{"id": "E", "changes": [{"field": "messages", "value": {"messages": list(messages)}}]}]}


def test_the_webhook_answers_200_and_processes_the_message(ai, outbox):
    ai.script["x"] = ai_reply("entries", [credit()])
    with TestClient(main.app) as client:
        response = client.post("/webhook", json=meta_payload(text_message("x")))
    assert response.status_code == 200 and response.json() == {"status": "ok"}
    assert len(outbox) == 1


def test_a_status_update_with_no_messages_is_ignored(ai, outbox):
    payload = {"entry": [{"changes": [{"value": {"statuses": [{"status": "delivered"}]}}]}]}
    with TestClient(main.app) as client:
        assert client.post("/webhook", json=payload).status_code == 200
    assert outbox == []


def test_two_messages_in_one_delivery_are_both_handled(ai, outbox):
    ai.script["a"] = ai_reply("unclear")
    ai.script["b"] = ai_reply("unclear")
    with TestClient(main.app) as client:
        client.post("/webhook", json=meta_payload(text_message("a", "wamid.a"), text_message("b", "wamid.b")))
    assert len(outbox) == 2


def test_meta_verification_handshake(monkeypatch):
    monkeypatch.setattr(main.config, "VERIFY_TOKEN", "secret-word")
    with TestClient(main.app) as client:
        good = client.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "secret-word",
                                              "hub.challenge": "12345"})
        wrong_token = client.get("/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "nope",
                                                     "hub.challenge": "12345"})
        wrong_mode = client.get("/webhook", params={"hub.mode": "other", "hub.verify_token": "secret-word",
                                                    "hub.challenge": "12345"})
    assert (good.status_code, good.text) == (200, "12345")
    assert wrong_token.status_code == 403 and wrong_mode.status_code == 403
