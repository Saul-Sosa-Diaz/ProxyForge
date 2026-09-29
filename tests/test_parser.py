"""Tests for decklist parsing from text (web UI input)."""
from __future__ import annotations

import pytest

from mocks import deck
from src.models import DeckCard
from src.parser import format_deck, format_deck_line, parse_deck_file, parse_deck_text


def test_parse_deck_text_matches_file_parsing():
    _, from_file = parse_deck_file(deck("local"))
    with open(deck("local"), encoding="utf-8") as f:
        assert parse_deck_text(f.read()) == from_file


def test_parse_deck_text_markers_and_back_face():
    cards = parse_deck_text("\n4 Lightning Bolt [2x2 117] *F* / Mountain [fullart]\n\n")

    assert len(cards) == 1
    card = cards[0]
    assert (card.quantity, card.name, card.art, card.foil) == (4, "Lightning Bolt", "2x2:117", True)
    assert (card.back_name, card.back_art) == ("Mountain", "fullart")


def test_parse_deck_text_invalid_line_raises():
    with pytest.raises(ValueError, match="Lightning Bolt"):
        parse_deck_text("Lightning Bolt")


def test_format_deck_round_trips():
    text = (
        "4 Lightning Bolt [2x2:117] *F* / Mountain [fullart]\n"
        "1 Leonardo, the Balance (TMC) 1 *F*\n"
        "2 Hades - King of Olympus [enchanted]\n"
        "1 Sol Ring (TLE) 316 [sld:1512★]\n"
        "3 Fire // Ice\n"
        "1 Delver of Secrets / Insectile Aberration *F*\n"
    )
    cards = parse_deck_text(text)

    assert format_deck(cards) == text
    assert parse_deck_text(format_deck(cards)) == cards


def test_mpc_marker_keeps_identifier_case():
    text = "1 Delver of Secrets [mpc:1k4w07AFcKua0ldRpmTgXx0pTsEGv4kxg] *F* / Insectile Aberration [MPC:1H9E_z-Y]\n"
    card = parse_deck_text(text)[0]

    assert card.art == "mpc:1k4w07AFcKua0ldRpmTgXx0pTsEGv4kxg"
    assert card.back_art == "mpc:1H9E_z-Y"
    assert card.foil
    assert format_deck([card]) == text.replace("MPC:", "mpc:")


def test_format_deck_line_minimal():
    assert format_deck_line(DeckCard(quantity=2, name="Island")) == "2 Island"
