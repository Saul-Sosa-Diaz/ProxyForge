"""Tests for MTGStrategy with MPC Autofill (primary), Scryfall and Moxfield mocked from fixture JSON."""
from __future__ import annotations

from mocks import FakeResponse, FakeSession, deck, image_response, response_json
from src.exporter import Exporter
from src.parser import parse_deck_file, parse_deck_text
from src.strategies.mtg import MTGStrategy


def _strategy(routes):
    routes["img.test"] = image_response()
    strategy = MTGStrategy(min_request_interval=0, mpc_min_request_interval=0)
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


def _mpc_calls(strategy):
    return [call for call in strategy._session.calls if "mpcfill.com" in call]


def test_card_without_same_printing_on_mpc_uses_scryfall(tmp_path):
    strategy = _strategy({**_mpc_routes(), "/cards/named?exact=Lightning Bolt": _json("mtg_dark_leo.json")})

    # MPC only has 'Lightning Bolt (Full Art)' (no [M21] printing).
    assert strategy.fetch_card_image("Lightning Bolt", str(tmp_path / "l.png"), art="m21")

    assert "https://img.test/dark-leo.png" in strategy._session.calls
    assert "https://img.test/mpc-full.png" not in strategy._session.calls


def test_line_without_a_set_never_asks_mpc(tmp_path):
    strategy = _strategy({**_mpc_routes(), "/cards/named?exact=Lightning Bolt": _json("mtg_dark_leo.json")})

    assert strategy.fetch_card_image("Lightning Bolt", str(tmp_path / "l.png"))

    assert "https://img.test/dark-leo.png" in strategy._session.calls
    assert _mpc_calls(strategy) == []


def _printing_routes():
    def doc(identifier, name, dpi):
        return {"identifier": identifier, "name": name, "dpi": dpi,
                "downloadLink": f"https://img.test/{identifier}.png"}

    routes = _mpc_routes()
    routes["editorSearch"] = FakeResponse(json_data={"results": {"dark leo & shredder": {"CARD": [
        "tmt-220-low", "tmt-220", "tmt-221", "tmc-220", "tmt-plain", "other-card",
    ]}}})
    routes["2/cards"] = FakeResponse(json_data={"results": {
        "tmt-220-low": doc("tmt-220-low", "Dark Leo & Shredder (Normal) [TMT] {220}", 800),
        "tmt-220": doc("tmt-220", "Dark Leo & Shredder [TMT] {0220} (Artist)", 1200),
        "tmt-221": doc("tmt-221", "Dark Leo & Shredder [TMT] {221}", 1500),
        "tmc-220": doc("tmc-220", "Dark Leo & Shredder [TMC] {220}", 1500),
        "tmt-plain": doc("tmt-plain", "Dark Leo & Shredder [TMT]", 1500),
        "other-card": doc("other-card", "Dark Leo & Shredder Fan Art [TMT] {220}", 1500),
    }})
    routes["/cards/tmt/999"] = _json("mtg_dark_leo.json")
    return routes


def test_same_printing_on_mpc_is_used_and_pinned(tmp_path):
    strategy = _strategy(_printing_routes())

    assert strategy.pin_art("Dark Leo & Shredder (TMT) 220") == "mpc:tmt-220"  # highest DPI
    assert strategy.fetch_card_image("Dark Leo & Shredder (TMT) 220", str(tmp_path / "d.png"))
    assert "https://img.test/tmt-220.png" in strategy._session.calls
    assert not any("scryfall" in call for call in strategy._session.calls)
    # An art marker wins over the name annotation.
    assert strategy.pin_art("Dark Leo & Shredder (TMC) 1", "tmt:221") == "mpc:tmt-221"


def test_same_printing_needs_the_collector_number(tmp_path):
    strategy = _strategy(_printing_routes())

    assert strategy.pin_art("Dark Leo & Shredder (TMT) 999") is None
    assert strategy.fetch_card_image("Dark Leo & Shredder (TMT) 999", str(tmp_path / "d.png"))
    assert "https://img.test/dark-leo.png" in strategy._session.calls  # Scryfall
    # A set-only line accepts any image of that set.
    assert strategy.pin_art("Dark Leo & Shredder", "tmt") in {"mpc:tmt-221", "mpc:tmt-plain"}


def test_mpc_marker_downloads_that_image(tmp_path):
    strategy = _strategy(_mpc_routes())

    assert strategy.fetch_card_image("Lightning Bolt", str(tmp_path / "l.png"), art="mpc:mpc-id-1")

    assert "https://img.test/mpc-full.png" in strategy._session.calls
    assert not any("editorSearch" in call for call in strategy._session.calls)
    assert not any("scryfall" in call for call in strategy._session.calls)


def test_unavailable_mpc_image_falls_back_to_scryfall(tmp_path):
    strategy = _strategy({**_mpc_routes(), "/cards/named?exact=Lightning Bolt": _json("mtg_dark_leo.json")})

    assert strategy.fetch_card_image("Lightning Bolt", str(tmp_path / "l.png"), art="mpc:gone-card-id")

    assert "https://img.test/dark-leo.png" in strategy._session.calls


def test_mpc_front_gets_its_mpc_back(tmp_path):
    strategy = _strategy(_mpc_routes())

    assert strategy.pin_back("Delver of Secrets", "mpc:mpc-front-1") == (
        "Insectile Aberration",
        "mpc:mpc-back-1",
    )
    assert strategy.fetch_card_back_image("Delver of Secrets", str(tmp_path / "b.png"), art="mpc:mpc-front-1")
    assert "https://img.test/mpc-back-full.png" in strategy._session.calls
    # Scryfall fronts keep their automatic Scryfall back.
    assert strategy.pin_back("Delver of Secrets") is None
    assert strategy.pin_back("Delver of Secrets", "m21") is None


def test_pin_art_keeps_lines_without_a_same_printing(tmp_path):
    strategy = _strategy(_mpc_routes())

    assert strategy.pin_art("Lightning Bolt") is None
    assert strategy.pin_art("Lightning Bolt", "mpc:mpc-id-1") == "mpc:mpc-id-1"
    assert _mpc_calls(strategy) == []  # no set: MPC is not asked
    assert strategy.pin_art("Lightning Bolt", "m21") == "m21"  # no [M21] image on MPC


def test_saved_decklist_keeps_mpc_choices(tmp_path):
    routes = {**_mpc_routes(), "/cards/tmt/220": _json("mtg_dark_leo.json")}
    cards = parse_deck_text(
        "2 Lightning Bolt (2X2) 117 [mpc:mpc-id-1] *F*\n"
        "1 Delver of Secrets [mpc:mpc-front-1]\n"
        "1 Dark Leo & Shredder (TMT) 220\n"
    )
    Exporter(_strategy(routes), str(tmp_path / "first"), target_dpi=50).export_deck("deck", cards)

    saved = (tmp_path / "first" / "deck" / "deck.txt").read_text(encoding="utf-8")
    assert saved == (
        "2 Lightning Bolt (2X2) 117 [mpc:mpc-id-1] *F*\n"
        "1 Delver of Secrets [mpc:mpc-front-1] / Insectile Aberration [mpc:mpc-back-1]\n"
        "1 Dark Leo & Shredder (TMT) 220\n"
    )

    # Re-run from the saved decklist once MPC ranks other images first.
    routes["editorSearch"] = FakeResponse(json_data={"results": {}})
    strategy = _strategy(routes)
    Exporter(strategy, str(tmp_path / "second"), target_dpi=50).export_deck("deck", parse_deck_text(saved))

    calls = strategy._session.calls
    searches = [call for call in calls if "editorSearch" in call]
    assert not any("Lightning Bolt" in call or "Delver" in call for call in searches)
    for url in ("mpc-full.png", "mpc-front-full.png", "mpc-back-full.png", "dark-leo.png"):
        assert f"https://img.test/{url}" in calls
    assert (tmp_path / "second" / "deck" / "deck.txt").read_text(encoding="utf-8") == saved


def test_prefetch_batches_the_whole_deck(tmp_path):
    routes = {**_mpc_routes(), "/cards/tmt/220": _json("mtg_dark_leo.json")}
    strategy = _strategy(routes)
    cards = parse_deck_text(
        "1 Lightning Bolt [mpc:mpc-id-1]\n1 Delver of Secrets [mpc:mpc-front-1]\n1 Dark Leo & Shredder (TMT) 220\n"
    )

    Exporter(strategy, str(tmp_path), target_dpi=50).resolve_images("deck", cards)

    calls = _mpc_calls(strategy)
    searches = [call for call in calls if "editorSearch" in call]
    assert len(searches) == 1  # one search for the whole deck
    assert "Insectile Aberration" in searches[0] and "Dark Leo" in searches[0]
    assert "Lightning Bolt" not in searches[0]  # pinned: fetched by identifier
    assert sum("mpcfill.com/2/cards" in call for call in calls) == 1


def test_deck_without_sets_never_calls_mpc(tmp_path):
    strategy = _strategy({**_mpc_routes(), "/cards/named?exact=Dark Leo": _json("mtg_dark_leo.json")})

    Exporter(strategy, str(tmp_path), target_dpi=50).resolve_images(
        "deck", parse_deck_text("1 Dark Leo & Shredder\n")
    )

    assert _mpc_calls(strategy) == []


def test_list_art_options_lists_printings(tmp_path):
    strategy = _strategy({"/cards/search": _json("mtg_prints_sol_ring.json")})

    options = strategy.list_art_options("Sol Ring (C21) 263")

    # plst:C21-263 skipped
    assert [o.value for o in options] == ["c21:263", "sld:1512★", "sunf:7"]
    assert options[0].label == "Scryfall · ~300 DPI · Commander 2021 (C21) #263"
    assert options[0].image_url == "https://img.test/sol-c21.jpg"
    assert "borderless" in options[1].label
    assert options[2].image_url == "https://img.test/sol-front.jpg"  # front face of a DFC
    assert any('q=!"Sol Ring"' in call for call in strategy._session.calls)


def test_list_art_options_falls_back_to_extras(tmp_path):
    strategy = _strategy({"include:extras": _json("mtg_prints_sol_ring.json")})

    options = strategy.list_art_options("Bird (teoc)")

    assert options
    assert any("include:extras" in call for call in strategy._session.calls)


def test_art_option_downloads_that_printing(tmp_path):
    strategy = _strategy({
        "/cards/search": _json("mtg_prints_sol_ring.json"),
        "/cards/c21/263": _json("mtg_dark_leo.json"),
    })
    option = strategy.list_art_options("Dark Leo & Shredder")[0]

    assert strategy.fetch_card_image("Dark Leo & Shredder", str(tmp_path / "d.png"), art=option.value)
    assert any("/cards/c21/263" in call for call in strategy._session.calls)


def test_list_art_options_puts_mpc_arts_first_by_dpi(tmp_path):
    routes = _mpc_routes()
    routes["editorSearch"] = _json("mpc_editor_search.json")
    routes["2/cards"] = FakeResponse(json_data={"results": {
        "mpc-id-1": {"identifier": "mpc-id-1", "name": "Lightning Bolt", "dpi": 600, "size": 2_000_000,
                     "sourceName": "Low", "downloadLink": "https://img.test/low.png",
                     "mediumThumbnailUrl": "https://img.test/low-thumb.jpg"},
        "mpc-id-2": {"identifier": "mpc-id-2", "name": "Lightning Bolt (Full Art)", "dpi": 1200,
                     "size": 3_700_000, "sourceName": "MrTeferi",
                     "downloadLink": "https://img.test/high.png",
                     "mediumThumbnailUrl": "https://img.test/high-thumb.jpg"},
    }})
    routes["/cards/search"] = _json("mtg_prints_sol_ring.json")
    strategy = _strategy(routes)

    options = strategy.list_art_options("Lightning Bolt")

    assert [o.value for o in options[:2]] == ["mpc:mpc-id-2", "mpc:mpc-id-1"]
    assert options[0].label == "MPC · 1200 DPI · 3.7 MB · MrTeferi · Lightning Bolt (Full Art)"
    assert options[0].image_url == "https://img.test/high-thumb.jpg"
    assert options[2].value == "c21:263"  # Scryfall printings follow
    assert strategy.fetch_card_image("Lightning Bolt", str(tmp_path / "l.png"), art=options[0].value)
    assert "https://img.test/high.png" in strategy._session.calls


def test_mpc_stand_ins_are_never_the_pinned_back(tmp_path):
    routes = _mpc_routes()
    routes["editorSearch"] = FakeResponse(
        json_data={"results": {"insectile aberration": {"CARD": ["mpc-check", "mpc-real"]}}}
    )
    routes["2/cards"] = FakeResponse(json_data={"results": {
        "mpc-check": {"identifier": "mpc-check", "name": "Insectile Aberration (Checklist)",
                      "dpi": 1210, "downloadLink": "https://img.test/checklist.png"},
        "mpc-real": {"identifier": "mpc-real", "name": "Insectile Aberration", "dpi": 800,
                     "downloadLink": "https://img.test/real.png"},
    }})
    strategy = _strategy(routes)

    assert strategy.pin_back("Delver of Secrets", "mpc:mpc-front-1") == (
        "Insectile Aberration",
        "mpc:mpc-real",
    )


def test_scryfall_prefix_forces_the_official_image(tmp_path):
    strategy = _strategy({**_mpc_routes(), "/cards/c21/263": _json("mtg_dark_leo.json")})

    assert strategy.fetch_card_image("Dark Leo & Shredder", str(tmp_path / "d.png"), art="scryfall:c21:263")

    assert "https://img.test/dark-leo.png" in strategy._session.calls
    assert not any("editorSearch" in call for call in strategy._session.calls)


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
    assert options[0].label == "Scryfall · ~300 DPI · Secret Lair Drop (SLD) #7037 · borderless · Lotus Petal"
    assert any('q=!"Chaos Emerald"' in call for call in strategy._session.calls)


class _Queue:
    """Route value that answers with each queued response in turn (last repeats)."""

    def __init__(self, *responses):
        self.responses = list(responses)


class _QueueSession(FakeSession):
    def _answer(self, response):
        if isinstance(response, _Queue):
            return response.responses.pop(0) if len(response.responses) > 1 else response.responses[0]
        return response

    def get(self, url, params=None, **kwargs):
        return self._answer(super().get(url, params=params, **kwargs))

    def post(self, url, json=None, data=None, **kwargs):
        return self._answer(super().post(url, json=json, data=data, **kwargs))


def _rate_limited():
    response = FakeResponse(status_code=429)
    response.headers["Retry-After"] = "10"
    return response


def _queue_strategy(routes, monkeypatch, sleeps):
    import src.strategies.mtg as mtg

    monkeypatch.setattr(mtg.time, "sleep", sleeps.append)
    routes["img.test"] = image_response()
    strategy = MTGStrategy(min_request_interval=0, mpc_min_request_interval=0)
    strategy._session = _QueueSession(routes)
    return strategy


def test_mpc_rate_limit_waits_and_retries(tmp_path, monkeypatch):
    sleeps = []
    routes = _mpc_routes()
    routes["2/cards"] = _Queue(_rate_limited(), _json("mpc_cards.json"))
    strategy = _queue_strategy(routes, monkeypatch, sleeps)

    assert strategy.fetch_card_image("Lightning Bolt", str(tmp_path / "l.png"), art="mpc:mpc-id-1")

    assert sleeps == [10.0]  # honored Retry-After
    assert "https://img.test/mpc-full.png" in strategy._session.calls


def test_rate_limited_lookup_is_not_cached_as_a_miss(tmp_path, monkeypatch):
    sleeps = []
    routes = _mpc_routes()
    routes["2/cards"] = _Queue(*[_rate_limited()] * 4, _json("mpc_cards.json"))
    routes["/cards/named?exact=Lightning Bolt"] = _json("mtg_dark_leo.json")
    strategy = _queue_strategy(routes, monkeypatch, sleeps)

    # Still rate limited after every retry: Scryfall serves this one...
    assert strategy.fetch_card_image("Lightning Bolt", str(tmp_path / "a.png"), art="mpc:mpc-id-1")
    assert "https://img.test/dark-leo.png" in strategy._session.calls
    # ...but MPC is asked again next time instead of being skipped for good.
    assert strategy.fetch_card_image("Lightning Bolt", str(tmp_path / "b.png"), art="mpc:mpc-id-1")
    assert "https://img.test/mpc-full.png" in strategy._session.calls


def test_rate_limited_sources_do_not_disable_mpc(tmp_path, monkeypatch):
    sleeps = []
    routes = _mpc_routes()
    routes["2/sources"] = _Queue(*[_rate_limited()] * 4, _json("mpc_sources.json"))
    strategy = _queue_strategy(routes, monkeypatch, sleeps)

    assert strategy.list_art_options("Lightning Bolt") == []
    assert [o.value for o in strategy.list_art_options("Lightning Bolt")] == ["mpc:mpc-id-1"]


def test_art_options_can_be_found_by_set_name_and_initials(tmp_path):
    routes = _mpc_routes()
    routes["2/cards"] = FakeResponse(json_data={"results": {
        "mpc-id-1": {"identifier": "mpc-id-1", "name": "Island [TMT] {192}", "dpi": 800,
                     "downloadLink": "https://img.test/tmt.png",
                     "smallThumbnailUrl": "https://img.test/tmt-thumb.jpg"},
    }})
    routes["api.scryfall.com/sets"] = FakeResponse(
        json_data={"data": [{"code": "tmt", "name": "Teenage Mutant Ninja Turtles"}]}
    )
    strategy = _strategy(routes)

    option = strategy.list_art_options("Lightning Bolt")[0]

    assert "Teenage Mutant Ninja Turtles" in option.keywords
    assert "tmnt" in option.keywords.split()


def test_tokens_are_searched_and_matched_by_printing(tmp_path):
    def doc(identifier, name, dpi=800):
        return {"identifier": identifier, "name": name, "dpi": dpi, "cardType": "TOKEN",
                "downloadLink": f"https://img.test/{identifier}.png",
                "smallThumbnailUrl": f"https://img.test/{identifier}-thumb.jpg"}

    routes = _mpc_routes()
    routes["editorSearch"] = FakeResponse(json_data={"results": {
        # MPC answers per name and card type; tokens live under "TOKEN".
        "squirrel": {"CARD": ["card-squirrel-general"], "TOKEN": ["tok-tblb", "tok-tunf", "tok-old"]},
        "eldrazi spawn": {"TOKEN": ["spawn-tmh3"]},
    }})
    routes["2/cards"] = FakeResponse(json_data={"results": {
        "card-squirrel-general": doc("card-squirrel-general", "Chatterfang, Squirrel General", 1200),
        "tok-tblb": doc("tok-tblb", "Squirrel [TBLB 23]"),
        "tok-tunf": doc("tok-tunf", "Squirrel token [TUNF]{8} [hd]"),
        "tok-old": doc("tok-old", "Squirrel token [old] [hd]", 1200),
        "spawn-tmh3": doc("spawn-tmh3", "Eldrazi Spawn (Aleksi Briclot) [TMH3] {2}"),
    }})
    strategy = _strategy(routes)

    assert strategy.pin_art("Squirrel (tunf)") == "mpc:tok-tunf"
    assert strategy.pin_art("Squirrel (tblb) 23") == "mpc:tok-tblb"
    assert strategy.pin_art("Eldrazi Spawn (tmh3)") == "mpc:spawn-tmh3"
    search = next(call for call in strategy._session.calls if "editorSearch" in call)
    assert '"cardType": "TOKEN"' in search and '"cardType": "CARD"' in search
    # The token picker lists the tokens (and the fuzzy card hit) of that name.
    values = [o.value for o in strategy.list_art_options("Squirrel (tunf)")]
    assert {"mpc:tok-tblb", "mpc:tok-tunf", "mpc:tok-old"} <= set(values)
