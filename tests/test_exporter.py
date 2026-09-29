"""Tests for PDF generation in Exporter (using a fake strategy, low DPI for speed)."""
from __future__ import annotations

import re

import pytest
from PIL import Image

from mocks import CARD_PNG, IMAGES_DIR, FakeStrategy, deck
from src.exporter import Exporter, _source_bleed_mm, estimate_print_dpi
from src.models import DeckCard
from src.parser import parse_deck_file
from src.pdf import PdfWriter
from src.strategies.local import LocalStrategy

LOW_DPI = 50


def _page_count(pdf_path) -> int:
    return len(re.findall(rb"/Type\s*/Page(?!s)", pdf_path.read_bytes()))


def _export(strategy, tmp_path, cards):
    return Exporter(strategy, str(tmp_path), target_dpi=LOW_DPI).export_deck("deck", cards)


def test_single_sided_pdf(tmp_path):
    pdf = _export(FakeStrategy(), tmp_path, [DeckCard(quantity=4, name="Bolt")])

    assert pdf == tmp_path / "deck" / "deck.pdf"
    assert pdf.read_bytes().startswith(b"%PDF")
    assert _page_count(pdf) == 1


def test_ten_cards_need_two_pages(tmp_path):
    pdf = _export(FakeStrategy(), tmp_path, [DeckCard(quantity=10, name="Bolt")])
    assert _page_count(pdf) == 2  # 3x3 grid on A4


def test_duplicates_are_downloaded_once(tmp_path):
    strategy = FakeStrategy()
    _export(strategy, tmp_path, [
        DeckCard(quantity=2, name="Bolt"),
        DeckCard(quantity=1, name="Bolt", foil=True),
    ])
    assert strategy.calls == ["Bolt"]


def test_foil_cards_go_to_separate_pdf(tmp_path):
    _export(FakeStrategy(), tmp_path, [
        DeckCard(quantity=1, name="Bolt"),
        DeckCard(quantity=1, name="Shock", foil=True),
    ])
    assert (tmp_path / "deck" / "deck.pdf").exists()
    assert (tmp_path / "deck" / "foil_deck.pdf").exists()


def test_explicit_back_creates_front_and_back_pdfs(tmp_path):
    _export(FakeStrategy(), tmp_path, [DeckCard(quantity=2, name="Delver", back_name="Insectile")])

    front = tmp_path / "deck" / "front_deck.pdf"
    back = tmp_path / "deck" / "back_deck.pdf"
    assert front.exists() and back.exists()
    assert _page_count(front) == _page_count(back)
    assert not (tmp_path / "deck" / "deck.pdf").exists()


def test_automatic_back_from_strategy(tmp_path):
    _export(FakeStrategy(backs={"Delver"}), tmp_path, [DeckCard(quantity=1, name="Delver")])
    assert (tmp_path / "deck" / "front_deck.pdf").exists()
    assert (tmp_path / "deck" / "back_deck.pdf").exists()


def test_failed_cards_are_skipped(tmp_path):
    pdf = _export(FakeStrategy(known={"Bolt"}), tmp_path, [
        DeckCard(quantity=1, name="Bolt"),
        DeckCard(quantity=1, name="Missing"),
    ])
    assert pdf.exists()


def test_nothing_to_render_raises(tmp_path):
    with pytest.raises(RuntimeError):
        _export(FakeStrategy(known=set()), tmp_path, [DeckCard(quantity=1, name="Missing")])


def test_existing_image_is_not_fetched_again(tmp_path):
    strategy = FakeStrategy()
    cards = [DeckCard(quantity=1, name="Bolt")]
    _export(strategy, tmp_path, cards)
    _export(strategy, tmp_path, cards)
    assert strategy.calls == ["Bolt"]


def test_local_fixture_deck_end_to_end(tmp_path):
    deck_name, cards = parse_deck_file(deck("local"))
    strategy = LocalStrategy(str(IMAGES_DIR))

    pdf = Exporter(strategy, str(tmp_path), target_dpi=LOW_DPI).export_deck(deck_name, cards)

    assert pdf == tmp_path / "local" / "local.pdf"
    assert _page_count(pdf) == 1


def test_resolve_images_keeps_failed_entries(tmp_path):
    exporter = Exporter(FakeStrategy(known={"Bolt"}), str(tmp_path), target_dpi=LOW_DPI)
    resolved = exporter.resolve_images("deck", [
        DeckCard(quantity=1, name="Bolt"),
        DeckCard(quantity=1, name="Missing"),
    ])

    assert [r.card.name for r in resolved] == ["Bolt", "Missing"]
    assert resolved[0].front_path.exists()
    assert resolved[1].front_path is None
    assert not (tmp_path / "deck" / "deck.pdf").exists()  # nothing rendered yet


def test_resolve_images_reports_progress(tmp_path):
    calls = []
    Exporter(FakeStrategy(), str(tmp_path)).resolve_images(
        "deck",
        [DeckCard(quantity=1, name="Bolt"), DeckCard(quantity=1, name="Shock")],
        on_progress=lambda i, total, card: calls.append((i, total, card.name)),
    )
    assert calls == [(0, 2, "Bolt"), (1, 2, "Shock")]


def test_render_pdfs_returns_every_pdf(tmp_path):
    exporter = Exporter(FakeStrategy(), str(tmp_path), target_dpi=LOW_DPI)
    resolved = exporter.resolve_images("deck", [
        DeckCard(quantity=1, name="Bolt"),
        DeckCard(quantity=1, name="Delver", back_name="Insectile"),
        DeckCard(quantity=1, name="Shock", foil=True),
    ])

    pdfs = exporter.render_pdfs("deck", resolved)

    assert [p.name for p in pdfs] == [
        "deck.pdf", "front_deck.pdf", "back_deck.pdf", "foil_deck.pdf",
    ]
    assert all(p.exists() for p in pdfs)


def test_render_pdfs_uses_edited_quantities(tmp_path):
    exporter = Exporter(FakeStrategy(), str(tmp_path), target_dpi=LOW_DPI)
    resolved = exporter.resolve_images("deck", [DeckCard(quantity=1, name="Bolt")])
    edited = [resolved[0].model_copy(update={"card": resolved[0].card.model_copy(update={"quantity": 10})})]

    pdf = exporter.render_pdfs("deck", edited)[0]

    assert _page_count(pdf) == 2


def test_art_marker_does_not_leak_into_file_name(tmp_path):
    resolved = Exporter(FakeStrategy(), str(tmp_path)).resolve_images(
        "deck", [DeckCard(quantity=1, name="Bolt", art="2x2:117 showcase")]
    )
    assert resolved[0].front_path.name == "Bolt_2x2-117-showcase.png"


def test_star_printing_gets_its_own_file(tmp_path):
    exporter = Exporter(FakeStrategy(), str(tmp_path))
    star, plain = exporter.resolve_images("deck", [
        DeckCard(quantity=1, name="Sol Ring", art="sld:1512★"),
        DeckCard(quantity=1, name="Sol Ring", art="sld:1512"),
    ])
    assert star.front_path != plain.front_path


@pytest.mark.parametrize(
    ("size", "expected"),
    [((69, 94), 3.0), ((3264, 4440), 3.19), ((744, 1040), 0.0), ((63, 88), 0.0), ((100, 100), 0.0)],
)
def test_source_bleed_is_derived_from_the_aspect_ratio(tmp_path, size, expected):
    image = tmp_path / "card.png"
    Image.new("RGB", size).save(image)

    assert _source_bleed_mm(image) == pytest.approx(expected, abs=0.01)


def test_source_bleed_replaces_the_mirrored_bleed(tmp_path):
    image = tmp_path / "mpc.png"
    Image.new("RGB", (69, 94)).save(image)  # 63x88 mm card + 3 mm bleed per side
    exporter = Exporter(FakeStrategy(), str(tmp_path))
    geo = exporter._page_geometry()

    ops = exporter._card_ops(PdfWriter(), geo, image, 0, 0)

    assert ops.count(" Do Q") == 1  # no mirrored strips
    x = geo.start_x
    assert f"{x - geo.bleed:.4f}".rstrip("0") in ops  # clipped at the 1 mm bleed
    assert f"{x - 3 * 72 / 25.4:.4f}".rstrip("0") in ops  # image starts 3 mm before the trim


def test_plain_card_keeps_the_mirrored_bleed(tmp_path):
    exporter = Exporter(FakeStrategy(), str(tmp_path))

    ops = exporter._card_ops(PdfWriter(), exporter._page_geometry(), CARD_PNG, 0, 0)

    assert ops.count(" Do Q") == 9  # 8 mirrored strips + the card


@pytest.mark.parametrize(("size", "expected"), [((3264, 4440), 1195), ((745, 1040), 300), ((63, 88), 25)])
def test_estimate_print_dpi_ignores_the_source_bleed(tmp_path, size, expected):
    image = tmp_path / "card.png"
    Image.new("RGB", size).save(image)

    assert estimate_print_dpi(image) == expected
