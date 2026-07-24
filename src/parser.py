"""Parser for standard TCG decklist (.txt) files."""
from __future__ import annotations

import re
from pathlib import Path

from .models import DeckCard


def slugify(name: str) -> str:
    """Convert a card name into a filesystem-friendly slug."""
    slug = name.strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    slug = slug.strip("-")
    return slug


def parse_deck_file(file_path: str) -> tuple[str, list[DeckCard]]:
    """Read a decklist .txt file and return (deck_name, cards).

    Each non-empty line is expected to follow the format:
        <quantity> <full card name>
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
            cards.append(
                DeckCard(
                    quantity=int(quantity),
                    name=name.strip(),
                )
            )

    return deck_name, cards