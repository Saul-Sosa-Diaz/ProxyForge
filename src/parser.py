"""Parser for standard TCG decklist (.txt) files."""
from __future__ import annotations

import re
from pathlib import Path

from .models import DeckCard


def slugify(name: str) -> str:
    """Convert a card name into a filesystem-friendly slug."""
    slug = name.strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    return slug.strip("-")


# Trailing foil/premium marker used by MTG deck exporters (Manabox '*F*',
# MTGO '*G*'): '1 Barad-dûr (PLTR) 253s *F*'.
_FOIL_MARKER = re.compile(r"\s*\*+\s*[FGfg]{1,2}\s*\*+\s*$")

# Trailing art-variant marker (Lorcana): '4 Hades - King of Olympus [enchanted]'.
# Only recognized keywords are stripped, so bracketed content belonging to a
# card name (e.g. Pokémon set titles) is left untouched.
_ART_KEYWORDS = ("best", "enchanted", "iconic", "epic", "special", "base")
_ART_MARKER = re.compile(
    r"\s*\[\s*(" + "|".join(_ART_KEYWORDS) + r")\s*\]\s*$",
    re.IGNORECASE,
)


def _strip_foil_marker(name: str) -> tuple[str, bool]:
    """Remove a trailing foil marker, returning ``(clean_name, is_foil)``."""
    match = _FOIL_MARKER.search(name)
    if not match:
        return name, False
    clean = name[: match.start()].strip()
    if not clean:
        return name, False
    return clean, True


def _strip_art_marker(name: str) -> tuple[str, str | None]:
    """Remove a trailing ``[art]`` marker, returning ``(clean_name, art)``."""
    match = _ART_MARKER.search(name)
    if not match:
        return name, None
    clean = name[: match.start()].strip()
    if not clean:
        return name, None
    return clean, match.group(1).lower()


def _strip_trailing_markers(name: str) -> tuple[str, bool, str | None]:
    """Strip trailing ``[art]`` and foil markers in any order.

    Two passes let both ``name [art] *F*`` and ``name *F* [art]`` resolve.
    """
    foil = False
    art: str | None = None
    for _ in range(2):
        name, is_foil = _strip_foil_marker(name)
        foil = foil or is_foil
        name, card_art = _strip_art_marker(name)
        art = art or card_art
    return name, foil, art


def parse_deck_file(file_path: str) -> tuple[str, list[DeckCard]]:
    """Read a decklist .txt file and return (deck_name, cards).

    Each non-empty line is expected to follow the format:
        <quantity> <full card name> [art] [*F*]

    A trailing ``*F*``/``*G*`` marks the entry as foil and a trailing
    ``[art]`` requests a specific art variant (e.g. ``[enchanted]``,
    Lorcana-only). Both markers are stripped from the name and exposed as
    ``DeckCard.foil`` / ``DeckCard.art``; they may appear in either order.
    """
    path = Path(file_path)
    deck_name = path.stem
    cards: list[DeckCard] = []

    with path.open("r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line:
                continue
            match = re.match(r"^(\d+)\s+(.+)$", line)
            if not match:
                raise ValueError(f"Invalid decklist line: {raw_line!r}")
            quantity, name = match.groups()
            clean_name, foil, art = _strip_trailing_markers(name.strip())
            cards.append(
                DeckCard(
                    quantity=int(quantity),
                    name=clean_name,
                    foil=foil,
                    art=art,
                )
            )

    return deck_name, cards
