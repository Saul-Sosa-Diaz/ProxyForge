"""Tests for MTGStrategy with MPC Autofill (primary), Scryfall and Moxfield mocked from fixture JSON."""
from __future__ import annotations

from mocks import FakeResponse, FakeSession, deck, image_response, response_json
from src.exporter import Exporter
from src.parser import parse_deck_file, parse_deck_text
from src.strategies.mtg import MTGStrategy


def _strategy(routes):
    routes["img.test"] = image_response()
    strategy = MTGStrategy(min_request_interval=0)
    strategy._session = FakeSession(routes)
    return strategy


def _json(name):
    return FakeResponse(json_data=response_json(name))


def _mpc_routes():
    return {
        "2/sources": _json("mpc_sources.json"),
        "editorSearch": _json("mpc_editor_search.json"),
        "2/cards": _json("mpc_cards.json"),
        "2/DFCPairs": _json("mpc_dfc_pairs.json"),
    }


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


def test_mpc_primary_source(tmp_path):
    strategy = _strategy(_mpc_routes())

    assert strategy.fetch_card_image("Lightning Bolt", str(tmp_path / "l.png"))

    assert "https://img.test/mpc-full.png" in strategy._session.calls
    assert not any("/cards/named" in call for call in strategy._session.calls)


def test_mpc_ignores_art_marker(tmp_path):
    strategy = _strategy(_mpc_routes())

    assert strategy.fetch_card_image(
        "Lightning Bolt", str(tmp_path / "l.png"), art="m21 borderless"
    )

    assert "https://img.test/mpc-full.png" in strategy._session.calls
    assert not any("/cards/search" in call for call in strategy._session.calls)


def test_mpc_miss_falls_back_to_scryfall(tmp_path):
    strategy = _strategy({
        "2/sources": _json("mpc_sources.json"),
        "editorSearch": _json("mpc_editor_search_empty.json"),
        "/cards/named?exact=Lightning Bolt": _json("mtg_dark_leo.json"),
    })

    assert strategy.fetch_card_image("Lightning Bolt", str(tmp_path / "l.png"))

    assert "https://img.test/dark-leo.png" in strategy._session.calls


def test_mpc_back_image(tmp_path):
    strategy = _strategy(_mpc_routes())

    assert strategy.fetch_card_image("Delver of Secrets", str(tmp_path / "front.png"))
    assert strategy.fetch_card_back_image("Delver of Secrets", str(tmp_path / "back.png"))

    assert "https://img.test/mpc-front-full.png" in strategy._session.calls
    assert "https://img.test/mpc-back-full.png" in strategy._session.calls


def test_pin_art_returns_mpc_identifier(tmp_path):
    strategy = _strategy(_mpc_routes())

    assert strategy.pin_art("Lightning Bolt (2X2) 117", "m21") == "mpc:mpc-id-1"
    assert strategy.pin_art("Delver of Secrets") == "mpc:mpc-front-1"
    assert strategy.pin_back("Delver of Secrets", "mpc:mpc-front-1") == (
        "Insectile Aberration",
        "mpc:mpc-back-1",
    )
    assert strategy.pin_back("Lightning Bolt", "mpc:mpc-id-1") is None


def test_pin_art_keeps_marker_when_mpc_misses(tmp_path):
    strategy = _strategy({
        "2/sources": _json("mpc_sources.json"),
        "editorSearch": _json("mpc_editor_search_empty.json"),
    })

    assert strategy.pin_art("Lightning Bolt", "m21") == "m21"
    assert strategy.pin_back("Lightning Bolt", "m21") is None


def test_pinned_mpc_art_skips_search(tmp_path):
    routes = _mpc_routes()
    # The search ranking changed since the card was pinned.
    routes["editorSearch"] = FakeResponse(
        json_data={"results": {"lightning bolt": {"CARD": ["mpc-id-2", "mpc-id-1"]}}}
    )
    strategy = _strategy(routes)

    assert strategy.fetch_card_image("Lightning Bolt", str(tmp_path / "l.png"), art="mpc:mpc-id-1")

    assert "https://img.test/mpc-full.png" in strategy._session.calls
    assert not any("editorSearch" in call for call in strategy._session.calls)


def test_missing_pinned_mpc_card_falls_back_to_search(tmp_path):
    strategy = _strategy(_mpc_routes())

    assert strategy.fetch_card_image("Delver of Secrets", str(tmp_path / "d.png"), art="mpc:gone-card-id")

    assert "https://img.test/mpc-front-full.png" in strategy._session.calls


def test_saved_decklist_reproduces_mpc_images(tmp_path):
    cards = parse_deck_text("2 Lightning Bolt (2X2) 117 *F*\n1 Delver of Secrets\n")
    exporter = Exporter(_strategy(_mpc_routes()), str(tmp_path / "first"), target_dpi=50)
    exporter.export_deck("deck", cards)

    saved = (tmp_path / "first" / "deck" / "deck.txt").read_text(encoding="utf-8")
    assert saved == (
        "2 Lightning Bolt (2X2) 117 [mpc:mpc-id-1] *F*\n"
        "1 Delver of Secrets [mpc:mpc-front-1] / Insectile Aberration [mpc:mpc-back-1]\n"
    )

    # Re-run from the saved decklist once MPC ranks other images first.
    routes = _mpc_routes()
    routes["editorSearch"] = FakeResponse(json_data={"results": {}})
    strategy = _strategy(routes)
    rerun = Exporter(strategy, str(tmp_path / "second"), target_dpi=50)
    rerun.export_deck("deck", parse_deck_text(saved))

    calls = strategy._session.calls
    assert not any("editorSearch" in call for call in calls)
    for url in ("mpc-full.png", "mpc-front-full.png", "mpc-back-full.png"):
        assert f"https://img.test/{url}" in calls
    assert (tmp_path / "second" / "deck" / "deck.txt").read_text(encoding="utf-8") == saved


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
