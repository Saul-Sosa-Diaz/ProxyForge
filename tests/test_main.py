"""Tests for the command-line entry point (offline: local strategy only)."""
from __future__ import annotations

from mocks import IMAGES_DIR
from src.main import main


def _run(tmp_path, text: str, tcg: str = "local") -> int:
    deck_file = tmp_path / "mixed.txt"
    deck_file.write_text(text, encoding="utf-8")
    return main([
        "--input", str(deck_file), "--tcg", tcg, "--output", str(tmp_path / "out"),
        "--local-dir", str(IMAGES_DIR), "--dpi", "50",
    ])


def test_section_games_are_fetched_by_their_strategy(tmp_path):
    # '[local]' inside a Pokémon deck: no network needed for these lines.
    code = _run(tmp_path, "[local]\n2 Pikachu\n1 Charizard\n", tcg="pokemon")

    assert code == 0
    assert (tmp_path / "out" / "pokemon" / "mixed" / "mixed.pdf").exists()
    assert (tmp_path / "out" / "pokemon" / "mixed" / "images" / "local_Pikachu.png").exists()
    saved = tmp_path / "out" / "pokemon" / "mixed" / "mixed.txt"
    assert saved.read_text(encoding="utf-8") == "[local]\n2 Pikachu\n1 Charizard\n"


def test_unknown_section_is_a_deck_error(tmp_path):
    assert _run(tmp_path, "1 Pikachu\n[yugioh]\n1 Kuriboh\n") == 2
