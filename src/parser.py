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


def _strip_foil_marker(name: str) -> tuple[str, bool]:
    """Remove a trailing foil marker, returning ``(clean_name, is_foil)``."""
    match = _FOIL_MARKER.search(name)
    if not match:
        return name, False
    clean = name[: match.start()].strip()
    if not clean:
        return name, False
    return clean, True


def parse_deck_file(file_path: str) -> tuple[str, list[DeckCard]]:
    """Read a decklist .txt file and return (deck_name, cards).

    Each non-empty line is expected to follow the format:
        <quantity> <full card name> [*F*]

    A trailing ``*F*``/``*G*`` marks the entry as foil: it is stripped from
    the name and exposed as ``DeckCard.foil``.
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
            clean_name, foil = _strip_foil_marker(name.strip())
            cards.append(
                DeckCard(
                    quantity=int(quantity),
                    name=clean_name,
                    foil=foil,
                )
            )

    return deck_name, cards
