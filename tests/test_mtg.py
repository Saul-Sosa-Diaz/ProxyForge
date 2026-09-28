"""Tests for MTGStrategy with Scryfall and Moxfield mocked from fixture JSON."""
from __future__ import annotations

from mocks import FakeResponse, FakeSession, deck, image_response, response_json
from src.parser import parse_deck_file
from src.strategies.mtg import MTGStrategy


def _strategy(routes):
    routes["img.test"] = image_response()
    strategy = MTGStrategy(min_request_interval=0)
    strategy._session = FakeSession(routes)
    return strategy


def _json(name):
    return FakeResponse(json_data=response_json(name))


def test_fixture_deck_is_parsed():
    _, cards = parse_deck_file(deck("mtg"))
    assert cards[0].name == "Dark Leo & Shredder (TMT) 220"
    assert cards[0].foil


def test_collector_number_lookup(tmp_path):
    strategy = _strategy({"/cards/tmt/220": _json("mtg_dark_leo.json")})
    assert strategy.fetch_card_image("Dark Leo & Shredder (TMT) 220", str(tmp_path / "d.png"))
    assert "https://img.test/dark-leo.png" in strategy._session.calls


def test_named_lookup(tmp_path):
    strategy = _strategy({"/cards/named?exact=Dark Leo": _json("mtg_dark_leo.json")})
    assert strategy.fetch_card_image("Dark Leo & Shredder", str(tmp_path / "d.png"))


def test_double_faced_back_image(tmp_path):
    strategy = _strategy({"/cards/named": _json("mtg_delver.json")})
    assert strategy.fetch_card_image("Delver of Secrets", str(tmp_path / "front.png"))
    assert strategy.fetch_card_back_image("Delver of Secrets", str(tmp_path / "back.png"))
    assert "https://img.test/insectile.png" in strategy._session.calls


def test_single_faced_has_no_back(tmp_path):
    strategy = _strategy({"/cards/named": _json("mtg_dark_leo.json")})
    assert not strategy.fetch_card_back_image("Dark Leo & Shredder", str(tmp_path / "b.png"))


def test_falls_back_to_moxfield(tmp_path):
    strategy = _strategy({
        "api.moxfield.com": _json("moxfield_search.json"),
        "card-abc-normal.jpg": image_response(),
    })
    assert strategy.fetch_card_image("Foot Ninjas", str(tmp_path / "f.png"))


def test_not_found_returns_false(tmp_path):
    strategy = _strategy({})
    assert not strategy.fetch_card_image("Unknown Card", str(tmp_path / "u.png"))
