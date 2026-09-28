"""Tests for PokemonStrategy with pkmncards.com mocked from fixture HTML."""
from __future__ import annotations

from mocks import CARD_PNG, FakeResponse, FakeSession, deck, image_response, response_text
from src.parser import parse_deck_file
from src.strategies.pokemon import PokemonStrategy


def _strategy(routes):
    strategy = PokemonStrategy()
    strategy._session = FakeSession(routes)
    return strategy


def test_fixture_deck_is_parsed():
    _, cards = parse_deck_file(deck("pokemon"))
    assert [c.name for c in cards] == ["Victini · White Flare (WHT)", "Zekrom ex · Black Bolt"]


def test_search_selects_best_matching_title(tmp_path):
    strategy = _strategy({
        "/?s=": FakeResponse(text=response_text("pokemon_search.html")),
        "victini-wht.png": image_response(),
    })
    out = tmp_path / "victini.png"

    assert strategy.fetch_card_image("Victini · White Flare (WHT)", str(out))
    assert out.read_bytes() == CARD_PNG.read_bytes()
    assert not any("victini-blk.png" in c for c in strategy._session.calls)


def test_falls_back_to_card_page(tmp_path):
    strategy = _strategy({
        "/?s=": FakeResponse(text="<html>no results</html>"),
        "/card/zekrom-ex-black-bolt/": FakeResponse(text=response_text("pokemon_card_page.html")),
        "zekrom.png": image_response(),
    })

    assert strategy.fetch_card_image("Zekrom ex · Black Bolt", str(tmp_path / "z.png"))


def test_not_found_returns_false(tmp_path):
    strategy = _strategy({})
    assert not strategy.fetch_card_image("Missingno", str(tmp_path / "m.png"))
