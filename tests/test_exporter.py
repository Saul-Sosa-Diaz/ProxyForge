"""Tests for PDF generation in Exporter (using a fake strategy, low DPI for speed)."""
from __future__ import annotations

import re

import pytest

from mocks import IMAGES_DIR, FakeStrategy, deck
from src.exporter import Exporter
from src.models import DeckCard
from src.parser import parse_deck_file
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
