"""Tests for the Streamlit web UI (offline: local strategy + fixture images)."""
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest

from mocks import CARD_BACK_PNG, CARD_PNG, IMAGES_DIR, FakeStrategy, deck
from src.app import FAILED_TAB, PDF_GROUPS, _zip_files
from src.exporter import Exporter
from src.models import ArtOption, DeckCard
from src.parser import parse_deck_text

APP = str(Path(__file__).resolve().parent.parent / "src" / "app.py")
TIMEOUT = 60


class FakeArtStrategy(FakeStrategy):
    """Fake strategy with an art picker: ``[alt]`` downloads the back fixture."""

    supports_art = True

    def fetch_card_image(self, card_name, output_path, art=None):
        ok = super().fetch_card_image(card_name, output_path, art)
        if ok and art == "alt":
            Path(output_path).write_bytes(CARD_BACK_PNG.read_bytes())
        return ok

    def list_art_options(self, card_name):
        return [
            ArtOption(value="base", label="Base", image_url=str(CARD_PNG)),
            ArtOption(value="alt", label="Alternativo", image_url=str(CARD_BACK_PNG)),
        ]


def _button(at: AppTest, label: str):
    return next(b for b in at.button if label in b.label)


def _resolve_local_deck(tmp_path, text: str, strategy=None) -> AppTest:
    at = AppTest.from_file(APP, default_timeout=TIMEOUT).run()
    at.sidebar.selectbox[0].set_value("local").run()
    at.sidebar.text_input[0].set_value(str(IMAGES_DIR))
    at.sidebar.text_input[1].set_value(str(tmp_path))
    at.sidebar.selectbox[1].set_value(300)
    at.text_area[0].input(text)
    at.text_input[0].input("preview")
    if strategy is not None:
        at.session_state["strategy"] = strategy
        at.session_state["strategy_key"] = ("local", str(IMAGES_DIR))
    at.run()
    _button(at, "Buscar imágenes").click().run()
    return at


def _open_tab(at: AppTest, label: str) -> AppTest:
    at.session_state["preview-tab"] = label
    return at.run()


def _tab_labels(at: AppTest) -> list[str]:
    return [tab.label for tab in at.tabs]


def test_preview_lists_found_and_missing_cards(tmp_path):
    at = _resolve_local_deck(tmp_path, "2 Pikachu\n1 Mewtwo")

    assert not at.exception
    resolved = at.session_state.resolved
    assert [r.card.name for r in resolved] == ["Pikachu", "Mewtwo"]
    assert resolved[0].front_path is not None
    assert resolved[1].front_path is None
    assert _tab_labels(at) == ["🃏 Normales", FAILED_TAB]

    at = _open_tab(at, FAILED_TAB)
    assert any("Mewtwo" in e.value for e in at.error)


def test_preview_is_split_by_pdf_file(tmp_path):
    at = _resolve_local_deck(
        tmp_path, "2 Pikachu\n1 Charizard *F*\n1 Pikachu / Charizard\n1 Charizard *F* / Pikachu"
    )

    assert not at.exception
    assert _tab_labels(at) == [group.title for group in PDF_GROUPS]


def test_front_back_pairs_are_stacked(tmp_path):
    at = _resolve_local_deck(tmp_path, "1 Pikachu / Charizard\n2 Charizard / Pikachu")
    at = _open_tab(at, "🔁 Front / Back")

    html = "".join(m.value for m in at.markdown)
    assert html.count('class="pf-stack"') == 2
    # Back image first (behind), front second (on top), same pair number.
    assert html.index("Reverso #1") < html.index("Anverso #1") < html.index("Reverso #2")


def test_failed_back_is_printed_single_sided(tmp_path):
    at = _resolve_local_deck(tmp_path, "1 Charizard / Missing")

    assert _tab_labels(at) == ["🃏 Normales"]
    assert any("Reverso no encontrado: Missing" in w.value for w in at.warning)


def test_settings_notice_only_after_changing_settings(tmp_path):
    at = _resolve_local_deck(tmp_path, "1 Pikachu")
    at.run()  # plain rerun: same settings
    assert not at.info

    at.sidebar.text_input[1].set_value(str(tmp_path / "other")).run()
    assert any("otros ajustes" in i.value for i in at.info)


def test_generate_pdf_offers_downloads_per_file(tmp_path):
    at = _resolve_local_deck(tmp_path, "2 Pikachu\n1 Pikachu / Charizard")

    _button(at, "Generar PDFs").click().run()

    assert not at.exception
    files = at.session_state.pdfs
    assert [p.name for p in files] == [
        "preview.pdf", "front_preview.pdf", "back_preview.pdf", "preview.txt",
    ]
    assert all(p.parent == tmp_path / "local" / "preview" and p.exists() for p in files)
    labels = [b.proto.label for b in at.get("download_button")]
    assert "⬇️ preview.pdf" in labels
    assert "📦 Descargar todo (4 ficheros, .zip)" in labels

    at = _open_tab(at, "🔁 Front / Back")
    labels = [b.proto.label for b in at.get("download_button")]
    assert {"⬇️ front_preview.pdf", "⬇️ back_preview.pdf"} <= set(labels)


class ManyArtsStrategy(FakeArtStrategy):
    """Card with more arts than one picker step (like basic lands on MPC)."""

    def list_art_options(self, card_name):
        return [
            ArtOption(
                value=f"art{i}",
                label=f"MPC · Island [{'TMT' if i == 90 else 'M21'}] {{{i}}}",
                image_url=str(CARD_PNG),
                keywords="Full-Art" if i == 7 else "",
            )
            for i in range(120)
        ]


def _use_buttons(at: AppTest) -> int:
    return [b.label for b in at.button].count("Usar este arte")


def test_art_picker_draws_large_lists_step_by_step(tmp_path):
    at = _resolve_local_deck(tmp_path, "1 Island", strategy=ManyArtsStrategy())
    _button(at, "Arte").click().run()

    assert _use_buttons(at) == 48
    _button(at, "Mostrar 48 más").click().run()
    assert not at.exception
    assert _use_buttons(at) == 96
    _button(at, "Mostrar 24 más").click().run()
    assert _use_buttons(at) == 120
    assert not [b for b in at.button if "Mostrar" in b.label]

    filter_box = next(t for t in at.text_input if t.label == "Filtrar")
    filter_box.input("island tmt").run()
    assert _use_buttons(at) == 1  # every word must match; the step restarts
    filter_box.input("full-art").run()  # keywords are searched too
    assert _use_buttons(at) == 1


def test_art_picker_replaces_the_card_image(tmp_path):
    at = _resolve_local_deck(tmp_path, "1 Bolt", strategy=FakeArtStrategy())
    original = at.session_state.resolved[0].front_path.read_bytes()

    _button(at, "Arte").click().run()
    assert [b.label for b in at.button].count("Usar este arte") == 2
    _button(at, "Usar este arte").click().run()  # first option: "base"

    assert not at.exception
    item = at.session_state.resolved[0]
    assert item.card.art == "base"

    _button(at, "Arte").click().run()
    assert "✔️ Seleccionado" in [b.label for b in at.button]
    next(b for b in at.button if b.label == "Usar este arte" and not b.disabled).click().run()
    item = at.session_state.resolved[0]
    assert item.card.art == "alt"
    assert item.front_path.name == "Bolt_alt.png"
    assert item.front_path.read_bytes() != original


def test_invalid_decklist_shows_error(tmp_path):
    at = _resolve_local_deck(tmp_path, "Pikachu sin cantidad")

    assert not at.exception
    assert "resolved" not in at.session_state
    assert any("No se pudo leer" in e.value for e in at.error)


def test_pdf_groups_match_exporter_file_names(tmp_path):
    """Each preview tab must name exactly the PDFs the exporter writes for it."""
    exporter = Exporter(FakeStrategy(backs={"Delver"}), str(tmp_path), target_dpi=50)
    resolved = exporter.resolve_images("deck", [
        DeckCard(quantity=1, name="Bolt"),
        DeckCard(quantity=1, name="Shock", foil=True),
        DeckCard(quantity=1, name="Delver"),
        DeckCard(quantity=1, name="Fire", foil=True, back_name="Ice"),
    ])

    written = {p.name for p in exporter.render_pdfs("deck", resolved)}

    expected = set()
    for group in PDF_GROUPS:
        members = [r for r in resolved if group.matches(r)]
        assert len(members) == 1
        expected.update(group.pdf_names("deck"))
    assert written == expected


def test_move_card_between_regular_and_foil(tmp_path):
    at = _resolve_local_deck(tmp_path, "2 Pikachu\n1 Charizard")
    assert _tab_labels(at) == ["🃏 Normales"]

    _button(at, "Pasar a foil").click().run()  # first card: Pikachu

    assert not at.exception
    assert [r.card.foil for r in at.session_state.resolved] == [True, False]
    assert _tab_labels(at) == ["🃏 Normales", "✨ Foil"]
    assert any("Pikachu → Foil" in t.proto.body for t in at.toast)

    at = _open_tab(at, "✨ Foil")
    _button(at, "Pasar a normales").click()
    at.session_state["preview-tab"] = "✨ Foil"  # AppTest would resend the old tab
    at.run()
    assert [r.card.foil for r in at.session_state.resolved] == [False, False]


def _move_sides(at: AppTest, label: str, tab: str) -> AppTest:
    _button(_open_tab(at, tab), label).click()
    at.session_state["preview-tab"] = tab  # AppTest would resend the old tab
    return at.run()


def test_move_card_between_front_back_and_single_sided(tmp_path):
    at = _resolve_local_deck(tmp_path, "1 Pikachu / Charizard\n2 Bolt", strategy=FakeStrategy(backs={"Bolt"}))
    written = tmp_path / "local" / "preview" / "preview.txt"
    assert _tab_labels(at) == ["🔁 Front / Back"]
    at = _open_tab(at, "🔁 Front / Back")

    at = _move_sides(at, "Solo anverso", "🔁 Front / Back")  # Pikachu / Charizard

    assert not at.exception
    assert _tab_labels(at) == ["🃏 Normales", "🔁 Front / Back"]
    assert written.read_text(encoding="utf-8") == "1 Pikachu / -\n2 Bolt\n"
    at = _move_sides(at, "Solo anverso", "🔁 Front / Back")  # automatic back of Bolt
    assert _tab_labels(at) == ["🃏 Normales"]
    assert written.read_text(encoding="utf-8") == "1 Pikachu / -\n2 Bolt / -\n"

    at = _move_sides(at, "Pasar a Front / Back", "🃏 Normales")  # restores Charizard
    at = _move_sides(at, "Pasar a Front / Back", "🃏 Normales")  # Bolt: asks the strategy again
    assert not at.exception
    assert _tab_labels(at) == ["🔁 Front / Back"]
    assert all(r.back_path is not None for r in at.session_state.resolved)
    assert written.read_text(encoding="utf-8") == "1 Pikachu / Charizard\n2 Bolt\n"


def test_move_whole_tab_to_foil(tmp_path):
    at = _resolve_local_deck(tmp_path, "2 Pikachu\n1 Charizard")

    _button(at, "Pasar todas a foil").click().run()

    assert [r.card.foil for r in at.session_state.resolved] == [True, True]
    assert _tab_labels(at) == ["✨ Foil"]


def test_decklist_is_shown_next_to_the_original_and_saved_live(tmp_path):
    original = "2 Pikachu\n1 Pikachu / Charizard"
    at = _resolve_local_deck(tmp_path, original)
    written = tmp_path / "local" / "preview" / "preview.txt"
    assert written.read_text(encoding="utf-8") == "2 Pikachu\n1 Pikachu / Charizard\n"
    assert any("sin cambios" in c.value for c in at.caption)

    _button(at, "Pasar a foil").click().run()

    expected = "2 Pikachu *F*\n1 Pikachu / Charizard\n"
    assert parse_deck_text(expected) == [r.card for r in at.session_state.resolved]
    assert [c.value for c in at.code] == [original, expected.strip()]
    assert any("1 línea cambiada" in c.value for c in at.caption)
    assert written.read_text(encoding="utf-8") == expected  # saved without generating PDFs
    assert not [b for b in at.get("download_button") if "decklist" in b.proto.label]

    _button(at, "Generar PDFs").click().run()
    assert written in at.session_state.pdfs


def test_zip_contains_every_generated_file(tmp_path):
    import io
    import zipfile

    files = [tmp_path / "a.pdf", tmp_path / "a.txt"]
    for f in files:
        f.write_text("x", encoding="utf-8")
    names = zipfile.ZipFile(io.BytesIO(_zip_files(files))).namelist()
    assert names == ["a.pdf", "a.txt"]


class _PreviewResponse:
    def __init__(self, status_code, content=b""):
        self.status_code = status_code
        self.content = content


def test_art_preview_retries_throttled_downloads(monkeypatch):
    import src.app as app

    responses = [_PreviewResponse(429), _PreviewResponse(200, CARD_PNG.read_bytes())]
    monkeypatch.setattr(app.requests, "get", lambda url, timeout, headers: responses.pop(0))
    monkeypatch.setattr(app.time, "sleep", lambda seconds: None)

    preview = app._fetch_art_thumbnail("https://drive.google.com/thumbnail?id=x")

    assert preview is not None and preview.startswith(b"\xff\xd8")  # JPEG
    assert responses == []


def test_art_preview_gives_up_after_retries(monkeypatch):
    import src.app as app

    monkeypatch.setattr(app.requests, "get", lambda url, timeout, headers: _PreviewResponse(429))
    monkeypatch.setattr(app.time, "sleep", lambda seconds: None)

    assert app._fetch_art_thumbnail("https://drive.google.com/thumbnail?id=y") is None


def test_art_preview_reads_local_files():
    import src.app as app

    assert app._fetch_art_thumbnail(str(CARD_PNG)).startswith(b"\xff\xd8")
    assert app._fetch_art_thumbnail(str(CARD_PNG) + ".missing") is None


def test_art_preview_is_cached_on_disk(tmp_path, monkeypatch):
    import src.app as app

    url = "https://drive.google.com/thumbnail?id=cached"
    calls = []

    def fake_get(url, timeout, headers):
        calls.append(url)
        return _PreviewResponse(200, CARD_PNG.read_bytes())

    monkeypatch.setattr(app, "ART_THUMBNAIL_CACHE_DIR", tmp_path)
    monkeypatch.setattr(app.requests, "get", fake_get)
    store, _ = app._art_thumbnail_store()

    assert app._cached_art_thumbnail(url) == (False, None)
    preview = app._load_art_thumbnail(url)
    store.pop(url)  # simulate an app restart: memory cache gone

    assert app._cached_art_thumbnail(url) == (True, preview)
    assert len(calls) == 1
    assert list(tmp_path.glob("*.jpg"))


def test_art_preview_sends_a_user_agent_and_skips_permanent_errors(monkeypatch):
    import src.app as app

    calls = []

    def fake_get(url, timeout, headers):
        calls.append(headers)
        return _PreviewResponse(400)

    monkeypatch.setattr(app.requests, "get", fake_get)
    monkeypatch.setattr(app.time, "sleep", lambda seconds: None)

    assert app._fetch_art_thumbnail("https://cards.scryfall.io/normal/x.jpg") is None
    assert len(calls) == 1  # 400 is not retried
    assert "python-requests" not in calls[0]["User-Agent"]
