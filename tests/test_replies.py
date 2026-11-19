"""
Tests for app/replies.py: the Kannada texts, buttons and formatting.

These check the SHAPE of every reply (WhatsApp's limits, the right numbers in
the right places). Whether the Kannada sounds natural is for a native speaker
to judge by reading the file.
Run with:   pytest -v
"""
import pytest

from app import replies
from app.replies import Button, Reply, button, rupees

KANNADA_RANGE = range(0x0C80, 0x0D00)


def has_kannada(text):
    return any(ord(ch) in KANNADA_RANGE for ch in text)


# ---------- rupees ----------

@pytest.mark.parametrize("amount, shown", [
    (0, "₹0"), (5, "₹5"), (650, "₹650"), (999, "₹999"),
    (1000, "₹1,000"), (1500, "₹1,500"), (12345, "₹12,345"),
    (100000, "₹1,00,000"), (150000, "₹1,50,000"), (1234567, "₹12,34,567"),
    (10000000, "₹1,00,00,000"), (-450, "-₹450"), (-1500, "-₹1,500"),
])
def test_rupees_use_indian_grouping(amount, shown):
    assert rupees(amount) == shown


# ---------- dates and money words ----------

@pytest.mark.parametrize("iso, shown", [
    ("2026-11-27", "27 ನವೆಂಬರ್"), ("2026-01-05", "5 ಜನವರಿ"), ("2026-12-31", "31 ಡಿಸೆಂಬರ್"),
])
def test_kannada_dates(iso, shown):
    assert replies.kannada_date(iso) == shown


def test_owed_positive_zero_and_negative():
    assert replies.owed(450) == "₹450"
    assert replies.owed(0) == "₹0"
    assert replies.owed(-100) == "₹100 ವಾಪಸ್ ಕೊಡಬೇಕು"


def test_describe_entry():
    assert replies.describe_entry("credit", "ಸುರೇಶ್", 650) == "ಸುರೇಶ್ — ₹650 ಸಾಲ"
    assert replies.describe_entry("payment", "ರಮೇಶ್", 1200) == "ರಮೇಶ್ — ₹1,200 ಪಾವತಿ"
    assert replies.describe_entry("sale", None, 1500) == "ನಗದು ಮಾರಾಟ — ₹1,500"


# ---------- shortening without breaking Kannada letters ----------

def test_short_text_is_unchanged():
    assert replies._shorten("ಸುರೇಶ್", 20) == "ಸುರೇಶ್"


@pytest.mark.parametrize("text", [
    "ಸುರೇಶ್ ಗೌಡ ಹಾಲಿನ ಅಂಗಡಿ ಮಾಲೀಕರು",
    "ಮಂಜುನಾಥ ಕೃಷ್ಣಮೂರ್ತಿ ಹೆಗಡೆ ಶೆಟ್ಟಿ",
    "ಲಕ್ಷ್ಮೀನಾರಾಯಣ ಸುಬ್ರಹ್ಮಣ್ಯ ಭಟ್ಟ",
    "Suresh Kumar Gowda from the market",
])
@pytest.mark.parametrize("limit", [8, 12, 15, 20])
def test_shortening_never_exceeds_the_limit_or_leaves_a_dangling_sign(text, limit):
    shown = replies._shorten(text, limit)
    assert len(shown) <= limit
    body = shown[:-1] if shown.endswith("…") else shown
    assert not body.endswith("್")                 # no dangling virama
    if shown != text:
        assert shown.endswith("…")
        assert text.startswith(body)
        # the next character in the original must not be a sign belonging to the last kept letter
        import unicodedata
        assert unicodedata.category(text[len(body)]) not in ("Mn", "Mc")


def test_button_titles_are_shortened_to_the_whatsapp_limit():
    long_name = "ಸುರೇಶ್ ಗೌಡ ಹಾಲಿನ ಅಂಗಡಿ ಮಾಲೀಕರು ಮತ್ತು ಇತರರು"
    assert len(button("q:1:1", long_name).title) <= replies.BUTTON_TITLE_MAX
    assert button("q:1:1", "ಸರಿ") == Button("q:1:1", "ಸರಿ")


# ---------- receipt ----------

SAVED = [
    {"kind": "credit", "customer": "ಸುರೇಶ್", "amount": 650, "due_date": "2026-11-27", "before": 450, "after": 1100},
    {"kind": "payment", "customer": "ರಮೇಶ್", "amount": 200, "due_date": None, "before": 200, "after": 0},
    {"kind": "sale", "customer": None, "amount": 1500, "due_date": None, "before": None, "after": None},
]


def test_receipt_shows_each_entry_with_old_and_new_balance():
    reply = replies.receipt(SAVED, 7)
    assert reply.text.splitlines() == [
        "✅ ಬರೆದುಕೊಂಡಿದ್ದೇನೆ",
        "• ಸುರೇಶ್ — ₹650 ಸಾಲ (ಬಾಕಿ ₹450 → ₹1,100), ಕೊಡುವ ದಿನ: 27 ನವೆಂಬರ್",
        "• ರಮೇಶ್ — ₹200 ಪಾವತಿ (ಬಾಕಿ ₹200 → ₹0)",
        "• ನಗದು ಮಾರಾಟ — ₹1,500",
    ]


def test_receipt_has_an_undo_button_for_that_batch():
    reply = replies.receipt(SAVED, 7)
    assert reply.buttons == (Button("undo:7", "↩️ ರದ್ದು ಮಾಡಿ"),)


def test_receipt_can_carry_extra_lines():
    reply = replies.receipt(SAVED[:1], 7, extra_lines=["extra"])
    assert reply.text.splitlines()[-1] == "extra"


def test_receipt_when_we_now_owe_the_customer():
    saved = [{"kind": "payment", "customer": "ರಮೇಶ್", "amount": 150, "due_date": None, "before": 100, "after": -50}]
    assert "(ಬಾಕಿ ₹100 → ₹50 ವಾಪಸ್ ಕೊಡಬೇಕು)" in replies.receipt(saved, 1).text


# ---------- undone ----------

def test_undone_lists_what_was_cancelled():
    entries = [{"kind": "credit", "customer": "ಸುರೇಶ್", "amount": 650},
               {"kind": "sale", "customer": None, "amount": 1500}]
    assert replies.undone(entries).text.splitlines() == [
        "↩️ ರದ್ದು ಮಾಡಿದ್ದೇನೆ:", "• ಸುರೇಶ್ — ₹650 ಸಾಲ", "• ನಗದು ಮಾರಾಟ — ₹1,500",
    ]


def test_undone_with_nothing():
    assert replies.undone([]) == Reply(replies.NOTHING_TO_UNDO)


# ---------- balances and dues ----------

def test_balance_wording():
    assert replies.balance("ಸುರೇಶ್", 900) == "ಸುರೇಶ್: ಬಾಕಿ ₹900"
    assert replies.balance("ಸುರೇಶ್", 0) == "ಸುರೇಶ್: ಬಾಕಿ ಇಲ್ಲ"
    assert replies.balance("ಸುರೇಶ್", -50) == "ಸುರೇಶ್: ನೀವು ₹50 ವಾಪಸ್ ಕೊಡಬೇಕು"


def test_several_balances_one_per_line():
    reply = replies.balances([("ಸುರೇಶ್ ಹಾಲು", 450), ("ಸುರೇಶ್ ಟೈಲರ್", 300)])
    assert reply.text.splitlines() == ["ಸುರೇಶ್ ಹಾಲು: ಬಾಕಿ ₹450", "ಸುರೇಶ್ ಟೈಲರ್: ಬಾಕಿ ₹300"]


def test_unknown_customer_message_names_them():
    assert "ರಾಜು" in replies.no_such_customer("ರಾಜು").text


def test_dues_list_with_total():
    reply = replies.dues([("ಸುರೇಶ್", 900), ("ಕುಮಾರ್", 300)])
    assert reply.text.splitlines() == [
        "📒 ಬಾಕಿ ಪಟ್ಟಿ", "1. ಸುರೇಶ್ — ₹900", "2. ಕುಮಾರ್ — ₹300", "ಒಟ್ಟು ಬಾಕಿ: ₹1,200 (2 ಜನ)",
    ]


def test_dues_when_nobody_owes():
    assert replies.dues([]) == Reply(replies.NO_DUES)


def test_long_dues_list_is_cut_but_the_total_counts_everyone():
    items = [(f"ಗ್ರಾಹಕ {n}", 100) for n in range(1, 31)]
    lines = replies.dues(items).text.splitlines()
    assert lines[1].startswith("1.") and lines[20].startswith("20.")
    assert "ಇನ್ನೂ 10 ಜನ" in lines[21]
    assert lines[-1] == "ಒಟ್ಟು ಬಾಕಿ: ₹3,000 (30 ಜನ)"
    assert len(lines) == 1 + 20 + 1 + 1


# ---------- not understood ----------

def test_not_understood_shows_what_the_ai_returned():
    text = replies.not_understood([({"customer": "ಸುರೇಶ್", "amount": "ಆರುನೂರು"}, "bad_amount"),
                                   ("junk", "not_an_object")])
    assert text.splitlines() == [
        "ಈ ಭಾಗ ಅರ್ಥವಾಗಲಿಲ್ಲ, ಮತ್ತೊಮ್ಮೆ ಹೇಳಿ:",
        "• ಸುರೇಶ್ ಆರುನೂರು (ಮೊತ್ತ ಅರ್ಥವಾಗಲಿಲ್ಲ)",
        "• … (ಅರ್ಥವಾಗಲಿಲ್ಲ)",
    ]


def test_not_understood_with_an_unknown_reason_still_reads_well():
    assert "(ಅರ್ಥವಾಗಲಿಲ್ಲ)" in replies.not_understood([({"customer": "x"}, "something_new")])


# ---------- questions ----------

def test_which_customer_has_each_candidate_plus_new_as_buttons():
    reply = replies.ask_which_customer(9, "ಸುರೇಶ್", "₹650 ಸಾಲ", ["ಸುರೇಶ್ ಹಾಲು", "ಸುರೇಶ್ ಟೈಲರ್"], truncated=False)
    assert [b.title for b in reply.buttons] == ["ಸುರೇಶ್ ಹಾಲು", "ಸುರೇಶ್ ಟೈಲರ್", "ಹೊಸ ಗ್ರಾಹಕ"]
    assert [b.id for b in reply.buttons] == ["q:9:1", "q:9:2", "q:9:3"]
    assert "ಸುರೇಶ್" in reply.text and "₹650 ಸಾಲ" in reply.text
    assert "ಪೂರ್ಣ ಹೆಸರು" not in reply.text


def test_which_customer_hints_when_more_names_exist():
    reply = replies.ask_which_customer(9, "ಸುರೇಶ್", "₹650 ಸಾಲ", ["a", "b"], truncated=True)
    assert "ಪೂರ್ಣ ಹೆಸರು" in reply.text


def test_confirm_lists_the_reasons():
    reply = replies.ask_confirm(4, "ಸುರೇಶ್ — ₹65,000 ಸಾಲ", ["large_amount", "due_not_understood", "mystery"])
    lines = reply.text.splitlines()
    assert lines[0] == "ಸುರೇಶ್ — ₹65,000 ಸಾಲ"
    assert lines[1] == replies.WARNING_TEXT["large_amount"]
    assert lines[2] == replies.WARNING_TEXT["due_not_understood"]
    assert lines[-1] == "ಸರಿಯೇ?"
    assert [b.id for b in reply.buttons] == ["q:4:1", "q:4:2"]
    assert [b.title for b in reply.buttons] == [replies.YES_BUTTON, replies.NO_BUTTON]


def test_ask_kind_offers_credit_and_payment():
    reply = replies.ask_kind(5, "ಸುರೇಶ್", 500)
    assert [b.title for b in reply.buttons] == ["ಸಾಲ", "ಪಾವತಿ"]
    assert "₹500" in reply.text and "ಸುರೇಶ್" in reply.text


def test_ask_new_name_explains_and_offers_cancel():
    reply = replies.ask_new_name(6, "ಸುರೇಶ್", "₹650 ಸಾಲ")
    assert "ಹಾಲು ಸುರೇಶ್" in reply.text
    assert [b.id for b in reply.buttons] == ["q:6:1"]


def test_moved_receipt_mentions_both_names_and_has_undo():
    reply = replies.moved("ಸುರೇಶ್", "ಹಾಲು ಸುರೇಶ್", SAVED[:1], 3)
    assert "ಹಾಲು ಸುರೇಶ್" in reply.text.splitlines()[-1]
    assert reply.buttons[0].id == "undo:3"


# ---------- every reply respects WhatsApp's limits and is in Kannada ----------

def _all_sample_replies():
    names = ["ಸುರೇಶ್ ಗೌಡ ಹಾಲಿನ ಅಂಗಡಿ ಮಾಲೀಕರು", "ಮಂಜುನಾಥ ಕೃಷ್ಣಮೂರ್ತಿ ಹೆಗಡೆ"]
    return [
        replies.receipt(SAVED, 1),
        replies.undone([{"kind": "credit", "customer": "ಸುರೇಶ್", "amount": 650}]),
        replies.dues([("ಸುರೇಶ್", 900)]),
        replies.ask_which_customer(1, "ಸುರೇಶ್", "₹650 ಸಾಲ", names, True),
        replies.ask_confirm(1, "ಸುರೇಶ್ — ₹65,000 ಸಾಲ", ["large_amount"]),
        replies.ask_kind(1, "ಸುರೇಶ್", 500),
        replies.ask_new_name(1, "ಸುರೇಶ್", "₹650 ಸಾಲ"),
        replies.moved("ಸುರೇಶ್", "ಹಾಲು ಸುರೇಶ್", SAVED[:1], 1),
        Reply(replies.UNCLEAR), Reply(replies.HEARD_NOTHING), Reply(replies.TECH_PROBLEM),
        Reply(replies.UNSUPPORTED), Reply(replies.ALREADY_ANSWERED), Reply(replies.NOT_WRITTEN),
        Reply(replies.NO_RECENT_ENTRY), Reply(replies.NOTE_WAS_CANCELLED), Reply(replies.NAME_TAKEN_BY_SAME),
    ]


def test_every_button_title_fits_and_there_are_at_most_three_buttons():
    for reply in _all_sample_replies():
        assert len(reply.buttons) <= replies.BUTTONS_MAX
        assert all(len(b.title) <= replies.BUTTON_TITLE_MAX for b in reply.buttons)
        assert all(b.title for b in reply.buttons)


def test_replies_with_buttons_fit_the_whatsapp_body_limit():
    for reply in _all_sample_replies():
        if reply.buttons:
            assert len(reply.text) <= replies.BODY_WITH_BUTTONS_MAX


def test_every_reply_speaks_kannada():
    for reply in _all_sample_replies():
        assert has_kannada(reply.text), reply.text


def test_button_ids_are_unique_within_a_reply():
    for reply in _all_sample_replies():
        ids = [b.id for b in reply.buttons]
        assert len(ids) == len(set(ids))
