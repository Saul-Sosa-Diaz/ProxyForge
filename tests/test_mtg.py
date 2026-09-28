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


def test_list_art_options_lists_printings(tmp_path):
    strategy = _strategy({"/cards/search": _json("mtg_prints_sol_ring.json")})

    options = strategy.list_art_options("Sol Ring (C21) 263")

    assert [o.value for o in options] == ["c21:263", "sld:1512★", "sunf:7"]  # plst:C21-263 skipped
    assert options[0].label == "Commander 2021 (C21) #263"
    assert options[0].image_url == "https://img.test/sol-c21.jpg"
    assert "borderless" in options[1].label
    assert options[2].image_url == "https://img.test/sol-front.jpg"  # front face of a DFC
    assert 'q=!"Sol Ring"' in strategy._session.calls[0]


def test_list_art_options_falls_back_to_extras(tmp_path):
    strategy = _strategy({"include:extras": _json("mtg_prints_sol_ring.json")})

    options = strategy.list_art_options("Bird (teoc)")

    assert options
    assert "include:extras" in strategy._session.calls[-1]


def test_art_option_downloads_that_printing(tmp_path):
    strategy = _strategy({
        "/cards/search": _json("mtg_prints_sol_ring.json"),
        "/cards/c21/263": _json("mtg_dark_leo.json"),
    })
    option = strategy.list_art_options("Dark Leo & Shredder")[0]

    assert strategy.fetch_card_image("Dark Leo & Shredder", str(tmp_path / "d.png"), art=option.value)
    assert any("/cards/c21/263" in call for call in strategy._session.calls)


def test_list_art_options_without_results_is_empty():
    assert _strategy({}).list_art_options("Unknown Card") == []


def test_collector_lookup_accepts_flavor_name(tmp_path, caplog):
    strategy = _strategy({"/cards/sld/7037": _json("mtg_chaos_emerald.json")})

    assert strategy.fetch_card_image("Chaos Emerald (SLD) 7037", str(tmp_path / "c.png"))

    assert "https://img.test/chaos-emerald.png" in strategy._session.calls
    assert not any("/cards/named" in call for call in strategy._session.calls)
    assert "ignoring the collector number" not in caplog.text


def test_collector_lookup_still_rejects_wrong_card(tmp_path, caplog):
    strategy = _strategy({
        "/cards/bro/334": _json("mtg_chaos_emerald.json"),
        "/cards/named?exact=Diabolic Intent": _json("mtg_dark_leo.json"),
    })

    assert strategy.fetch_card_image("Diabolic Intent (BRO) 334", str(tmp_path / "d.png"))

    assert "ignoring the collector number" in caplog.text
    assert "https://img.test/dark-leo.png" in strategy._session.calls


def test_flavor_name_art_options_show_real_card(tmp_path):
    strategy = _strategy({"/cards/search": _json("mtg_prints_chaos_emerald.json")})

    options = strategy.list_art_options("Chaos Emerald (SLD) 7037")

    assert [o.value for o in options] == ["sld:7037"]
    assert options[0].label == "Secret Lair Drop (SLD) #7037 · borderless · Lotus Petal"
    assert 'q=!"Chaos Emerald"' in strategy._session.calls[0]
