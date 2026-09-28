"""Tests for LorcanaStrategy with LorcanaJSON and lorcana.gg mocked from fixtures."""
from __future__ import annotations

from mocks import FakeResponse, FakeSession, deck, image_response, response_json, response_text
from src.parser import parse_deck_file
from src.strategies.lorcana import LorcanaStrategy


def _strategy(extra_routes=None):
    routes = {"allCards.json": FakeResponse(json_data=response_json("lorcana_all_cards.json"))}
    routes.update(extra_routes or {})
    routes["img.test"] = image_response()
    strategy = LorcanaStrategy(api_url=LorcanaStrategy.LORCANAJSON_JSON_URL)
    strategy._session = FakeSession(routes)
    return strategy


def test_fixture_deck_is_parsed():
    _, cards = parse_deck_file(deck("lorcana"))
    assert cards[0].name == "Tramp - Enterprising Dog"
    assert cards[1].art == "base"


def test_default_art_is_most_premium(tmp_path):
    strategy = _strategy()
    assert strategy.fetch_card_image("Tramp - Enterprising Dog", str(tmp_path / "t.png"))
    assert "https://img.test/tramp-enchanted.jpg" in strategy._session.calls


def test_base_art_marker(tmp_path):
    strategy = _strategy()
    assert strategy.fetch_card_image("Tramp - Enterprising Dog", str(tmp_path / "t.png"), art="base")
    assert "https://img.test/tramp-common.jpg" in strategy._session.calls


def test_falls_back_to_lorcana_gg(tmp_path):
    strategy = _strategy({
        "lorcana.gg/cards/lilo-escape-artist": FakeResponse(text=response_text("lorcana_gg_card.html")),
    })
    assert strategy.fetch_card_image("Lilo - Escape Artist", str(tmp_path / "l.png"))


def test_not_found_returns_false(tmp_path):
    strategy = _strategy()
    assert not strategy.fetch_card_image("Unknown Card", str(tmp_path / "u.png"))
