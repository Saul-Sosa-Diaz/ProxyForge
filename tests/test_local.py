"""Tests for LocalStrategy using the fixture deck and images."""
from __future__ import annotations

from mocks import IMAGES_DIR, deck
from src.parser import parse_deck_file
from src.strategies.local import LocalStrategy


def test_fetches_every_card_of_fixture_deck(tmp_path):
    _, cards = parse_deck_file(deck("local"))
    strategy = LocalStrategy(str(IMAGES_DIR))

    for card in cards:
        out = tmp_path / f"{card.name}.png"
        assert strategy.fetch_card_image(card.name, str(out))
        assert out.stat().st_size > 0


def test_matches_case_insensitive(tmp_path):
    strategy = LocalStrategy(str(IMAGES_DIR))
    assert strategy.fetch_card_image("PIKACHU", str(tmp_path / "out.png"))


def test_matches_name_with_extension(tmp_path):
    strategy = LocalStrategy(str(IMAGES_DIR))
    assert strategy.fetch_card_image("Charizard.jpg", str(tmp_path / "out.png"))


def test_missing_card_returns_false(tmp_path):
    strategy = LocalStrategy(str(IMAGES_DIR))
    assert not strategy.fetch_card_image("Does not exist", str(tmp_path / "out.png"))


def test_missing_directory_returns_false(tmp_path):
    strategy = LocalStrategy(str(tmp_path / "nope"))
    assert not strategy.fetch_card_image("Pikachu", str(tmp_path / "out.png"))
