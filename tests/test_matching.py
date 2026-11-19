"""
Tests for Chunk 8, part 1: which customer did the owner mean? (app/matching.py)

Run with:   pytest -v
"""
import pytest

from app import db, ledger, matching
from app.matching import Resolution


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    """Every test gets a new empty database."""
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test.db"))
    db.init_db()


@pytest.fixture
def shop():
    return ledger.get_or_create_shop("919876543210")


# ---------- could_be_same_person ----------

@pytest.mark.parametrize("a, b", [
    ("suresh", "suresh"),
    ("suresh", "suresh milk"),          # same first word
    ("suresh tailor", "suresh milk"),
    ("suresh kumar", "suresh"),
    ("ಸುರೇಶ", "ಸುರೇಶ್"),                  # Kannada spelling with / without the final virama
    ("ಕುಮಾರ", "ಕುಮಾರ್"),
    ("ರಮೇಶ", "ರಮೇಶ್"),
    ("ಗೀತಾ", "ಗೀತ"),
    ("ಲಕ್ಷ್ಮಿ", "ಲಕ್ಷ್ಮೀ"),
    ("suresh", "suresha"),
    ("kumar", "kumari"),
])
def test_names_that_could_be_the_same_person(a, b):
    assert matching.could_be_same_person(a, b)
    assert matching.could_be_same_person(b, a)      # works both ways


@pytest.mark.parametrize("a, b", [
    ("suresh", "ramesh"),
    ("suresh", "mahesh"),
    ("kumar", "raju"),
    ("geeta", "savita"),
    ("ಸುರೇಶ್", "ರಮೇಶ್"),
    ("ರಮೇಶ್", "ಮಹೇಶ್"),                      # the closest pair of DIFFERENT people we measured (0.80)
    ("ಕುಮಾರ್", "ರಾಜು"),
    ("ಪ್ರಕಾಶ್", "ಪ್ರಭಾಕರ್"),
    ("ಮಂಜುನಾಥ್", "ಮಂಜುಳಾ"),
    ("ಅನಿತಾ", "ಅನಿಲ್"),
])
def test_names_that_are_different_people(a, b):
    assert not matching.could_be_same_person(a, b)


@pytest.mark.parametrize("a, b", [("", "suresh"), ("suresh", ""), ("", "")])
def test_empty_names_never_match(a, b):
    assert not matching.could_be_same_person(a, b)


def test_the_threshold_sits_between_spellings_and_different_people():
    """Guards the measured gap: variants >= 0.86, different people <= 0.80."""
    assert 0.80 < matching.NEAR_MATCH_RATIO <= 0.86


# ---------- resolve_customer ----------

def test_exact_match_uses_the_stored_name(shop):
    ledger.add_credit(shop, "  Suresh Gowda ", 100)
    assert matching.resolve_customer(shop, "SURESH   gowda") == Resolution("exact", "Suresh Gowda")


def test_exact_match_in_kannada(shop):
    ledger.add_credit(shop, "ಸುರೇಶ್", 100)
    assert matching.resolve_customer(shop, "ಸುರೇಶ್‍") == Resolution("exact", "ಸುರೇಶ್")


def test_nobody_close_means_a_new_customer(shop):
    ledger.add_credit(shop, "Ramesh", 100)
    assert matching.resolve_customer(shop, "  Geeta  ") == Resolution("new", "Geeta")


def test_first_customer_of_a_shop_is_new(shop):
    assert matching.resolve_customer(shop, "ಸುರೇಶ್") == Resolution("new", "ಸುರೇಶ್")


def test_two_people_with_the_same_first_name_make_us_ask(shop):
    ledger.add_credit(shop, "Suresh milk", 100)
    ledger.add_credit(shop, "Suresh tailor", 100)
    result = matching.resolve_customer(shop, "Suresh")
    assert result.status == "ask"
    assert result.name == "Suresh"
    assert result.candidates == ("Suresh tailor", "Suresh milk")     # most recent first
    assert not result.truncated


def test_a_close_spelling_makes_us_ask(shop):
    ledger.add_credit(shop, "ಸುರೇಶ್", 100)
    result = matching.resolve_customer(shop, "ಸುರೇಶ")
    assert (result.status, result.candidates) == ("ask", ("ಸುರೇಶ್",))


def test_a_longer_name_of_an_existing_customer_makes_us_ask(shop):
    ledger.add_credit(shop, "Suresh", 100)
    result = matching.resolve_customer(shop, "Suresh Kumar")
    assert (result.status, result.name, result.candidates) == ("ask", "Suresh Kumar", ("Suresh",))


def test_exact_match_wins_even_if_others_share_the_first_name(shop):
    """Owner says 'Suresh', shop has 'Suresh' AND 'Suresh tailor': he means 'Suresh'."""
    ledger.add_credit(shop, "Suresh tailor", 100)
    ledger.add_credit(shop, "Suresh", 100)
    assert matching.resolve_customer(shop, "Suresh") == Resolution("exact", "Suresh")


def test_only_two_candidates_are_offered_and_we_say_so(shop):
    for name in ("Suresh a", "Suresh b", "Suresh c"):
        ledger.add_credit(shop, name, 10)
    result = matching.resolve_customer(shop, "Suresh")
    assert len(result.candidates) == matching.MAX_CANDIDATES == 2
    assert result.candidates == ("Suresh c", "Suresh b")
    assert result.truncated


def test_ask_keeps_the_name_as_the_owner_said_it(shop):
    ledger.add_credit(shop, "Suresh milk", 100)
    assert matching.resolve_customer(shop, "  ಸುರೇಶ್   ").name == "ಸುರೇಶ್"


def test_other_shops_customers_are_invisible():
    shop_a = ledger.get_or_create_shop("911111111111")
    shop_b = ledger.get_or_create_shop("912222222222")
    ledger.add_credit(shop_a, "Suresh milk", 100)
    assert matching.resolve_customer(shop_b, "Suresh").status == "new"


@pytest.mark.parametrize("empty", ["", "   ", "‍"])
def test_empty_name_is_refused(shop, empty):
    with pytest.raises(ValueError):
        matching.resolve_customer(shop, empty)


def test_resolving_never_changes_the_book(shop):
    ledger.add_credit(shop, "Suresh milk", 100)
    before = ledger.list_customers(shop)
    matching.resolve_customer(shop, "Suresh")
    matching.resolve_customer(shop, "Brand New")
    assert ledger.list_customers(shop) == before


# ---------- find_candidates ----------

def test_find_candidates_has_no_limit_and_skips_the_exact_match(shop):
    for name in ("Suresh a", "Suresh b", "Suresh c", "Suresh"):
        ledger.add_credit(shop, name, 10)
    assert matching.find_candidates(shop, "Suresh") == ["Suresh c", "Suresh b", "Suresh a"]


def test_find_candidates_empty_when_nobody_is_close(shop):
    ledger.add_credit(shop, "Ramesh", 10)
    assert matching.find_candidates(shop, "Geeta") == []


# ---------- tidy_name ----------

def test_tidy_name_keeps_case_but_cleans_spaces_and_unicode():
    assert matching.tidy_name("  Suresh   Gowda ") == "Suresh Gowda"
    assert matching.tidy_name("ಕೊ") == matching.tidy_name("ಕೊ")
