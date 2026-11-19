"""
Extra tests for Kannada names (GetLekka).

Kept in a separate file because tests/test_ledger.py says "don't change me".
Run with:   pytest -v
"""
import pytest

from app import db, ledger


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    """Every test gets a new empty database."""
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test.db"))
    db.init_db()


@pytest.fixture
def shop():
    return ledger.get_or_create_shop("919876543210")


def test_kannada_name_is_one_customer(shop):
    a = ledger.get_or_create_customer(shop, "ಸುರೇಶ್")
    b = ledger.get_or_create_customer(shop, "  ಸುರೇಶ್  ")
    assert a == b


def test_invisible_joiner_characters_are_ignored(shop):
    """Same word, but one copy has a zero-width joiner hidden inside."""
    plain = "ಸುರೇಶ್"
    with_joiner = "ಸುರೇಶ್\u200d"
    assert plain != with_joiner  # different bytes...
    assert ledger.get_or_create_customer(shop, plain) == \
        ledger.get_or_create_customer(shop, with_joiner)  # ...same customer


def test_different_unicode_forms_match(shop):
    """'ಕೊ' can be one stored character or two combined; NFC makes them equal."""
    composed = "ೊ"            # ೊ  (one code point)
    decomposed = "ೊ"    # ೆ + ೂ (two code points that combine to the same sign)
    a = ledger.get_or_create_customer(shop, "ಕ" + composed)
    b = ledger.get_or_create_customer(shop, "ಕ" + decomposed)
    assert a == b


def test_different_kannada_names_stay_different(shop):
    assert ledger.get_or_create_customer(shop, "ಸುರೇಶ್") != \
        ledger.get_or_create_customer(shop, "ರಮೇಶ್")


def test_name_is_stored_as_first_written(shop):
    cid = ledger.get_or_create_customer(shop, "  ಸುರೇಶ್ ")
    ledger.get_or_create_customer(shop, "ಸುರೇಶ್")
    with db.connect() as conn:
        row = conn.execute("SELECT name FROM customers WHERE id = ?", (cid,)).fetchone()
    assert row["name"] == "ಸುರೇಶ್"
