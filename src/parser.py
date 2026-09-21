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

# Trailing art-variant marker: '4 Hades - King of Olympus [enchanted]'
# (Lorcana) or '4 Lightning Bolt [2x2]' / '4 Lightning Bolt [borderless]'
# (MTG). Only recognized art expressions are stripped, so bracketed text
# belonging to a card name is left untouched.
_LORCANA_ART_KEYWORDS = ("best", "enchanted", "iconic", "epic", "special", "base")

# MTG variant keywords (canonical form -> aliases). "best"/"base" are shared
# with Lorcana; "normal"/"standard" are aliases for "base".
_MTG_VARIANT_ALIASES: dict[str, tuple[str, ...]] = {
    "best": ("best",),
    "base": ("base", "normal", "standard"),
    "fullart": ("full", "fullart", "full-art", "full_art", "full art"),
    "borderless": ("borderless",),
    "showcase": ("showcase",),
    "extended": ("extended", "extendedart", "extended-art", "extended_art", "extended art"),
    "retro": ("retro", "oldframe", "old-frame", "old frame"),
    "promo": ("promo",),
}
# Canonical variant -> normalized lookup (alias without separators -> canonical).
_MTG_VARIANT_LOOKUP: dict[str, str] = {}
for _canonical, _aliases in _MTG_VARIANT_ALIASES.items():
    for _alias in _aliases:
        _MTG_VARIANT_LOOKUP[re.sub(r"[\s_\-]+", "", _alias.lower())] = _canonical

# Backwards-compatible alias: Lorcana-only keywords previously exposed here.
_ART_KEYWORDS = _LORCANA_ART_KEYWORDS

# Generic trailing bracket; content is validated by _parse_art_content so
# unknown brackets stay part of the card name.
_ART_MARKER = re.compile(
    r"\s*\[\s*([^\[\]]{1,40})\s*\]\s*$",
    re.IGNORECASE,
)

_SET_CODE = r"[a-z0-9]{2,5}"
_COLLECTOR = r"(?:\d{1,4}[a-z\u2605]{0,2}|\u2605)"
_SET_COLLECTOR_JOINED = re.compile(
    rf"^(?P<set>{_SET_CODE})\s*[:\-#]\s*(?P<collector>{_COLLECTOR})$",
    re.IGNORECASE,
)
_SET_ONLY = re.compile(rf"^(?P<set>{_SET_CODE})$", re.IGNORECASE)
_COLLECTOR_ONLY = re.compile(rf"^(?P<collector>{_COLLECTOR})$", re.IGNORECASE)


def _strip_foil_marker(name: str) -> tuple[str, bool]:
    """Remove a trailing foil marker, returning ``(clean_name, is_foil)``."""
    match = _FOIL_MARKER.search(name)
    if not match:
        return name, False
    clean = name[: match.start()].strip()
    if not clean:
        return name, False
    return clean, True


def _normalize_variant_token(token: str) -> str | None:
    """Map a single token to its canonical MTG variant, if any."""
    key = re.sub(r"[\s_\-]+", "", token.lower())
    return _MTG_VARIANT_LOOKUP.get(key)


def _parse_art_content(content: str) -> str | None:
    """Validate bracket content and return its normalized art string.

    Accepted forms (case-insensitive):
        - Lorcana keywords: best, enchanted, iconic, epic, special, base
        - MTG variants: fullart (full), borderless, showcase, extended,
          retro, promo, base (normal/standard), best
        - MTG set code: m21, 2x2, dmu, pltr ...
        - MTG set + collector: 2x2:117, 2x2-117, 2x2 117, pltr 253s ...
        - MTG set (+collector) + variant combos: m21 borderless,
          borderless m21, 2x2:117 showcase, showcase 2x2 117 ...

    Returns the normalized art string (e.g. ``"m21"``, ``"2x2:117"``,
    ``"borderless"``, ``"m21 borderless"``) or ``None`` when the content
    is not a recognized art expression.
    """
    text = content.strip().lower()
    if not text:
        return None
    # Collapse separators: commas become spaces, multi-spaces collapse.
    text = text.replace(",", " ")
    text = re.sub(r"\s+", " ", text).strip()
    # Multi-word variant aliases ("full art", "extended art", "old frame").
    text = re.sub(r"\bfull\s+art\b", "fullart", text)
    text = re.sub(r"\bextended\s+art\b", "extendedart", text)
    text = re.sub(r"\bold\s+frame\b", "oldframe", text)

    # 1. Single Lorcana keyword (kept as-is, already lowercase).
    if text in _LORCANA_ART_KEYWORDS:
        return text

    # 2. Single MTG variant.
    variant = _normalize_variant_token(text)
    if variant is not None:
        return variant

    # 3. Single set code.
    m = _SET_ONLY.match(text)
    if m:
        return m.group("set").lower()

    # 4. Joined set + collector (':', '-', '#' separators).
    m = _SET_COLLECTOR_JOINED.match(text)
    if m:
        return f"{m.group('set').lower()}:{m.group('collector').lower()}"

    tokens = text.split(" ")

    # 5. Spaced set + collector ("2x2 117", "pltr 253s").
    if len(tokens) == 2:
        # Variant + set combos ("m21 borderless", "borderless m21").
        variants = [_normalize_variant_token(t) for t in tokens]
        if variants[0] is not None and _SET_ONLY.match(tokens[1]):
            return f"{tokens[1].lower()} {variants[0]}"
        if variants[1] is not None and _SET_ONLY.match(tokens[0]):
            return f"{tokens[0].lower()} {variants[1]}"
        # Variant + joined set:collector ("showcase 2x2:117").
        for i, variant_tok in enumerate(variants):
            if variant_tok is None:
                continue
            other = tokens[1 - i]
            jm = _SET_COLLECTOR_JOINED.match(other)
            if jm:
                return f"{jm.group('set').lower()}:{jm.group('collector').lower()} {variant_tok}"
        # Spaced set + collector.
        if _SET_ONLY.match(tokens[0]) and _COLLECTOR_ONLY.match(tokens[1]):
            return f"{tokens[0].lower()}:{tokens[1].lower()}"
        return None

    # 6. Three tokens: variant + spaced set + collector in any order
    # ("showcase 2x2 117", "2x2 117 showcase").
    if len(tokens) == 3:
        for i, tok in enumerate(tokens):
            variant_tok = _normalize_variant_token(tok)
            if variant_tok is None:
                continue
            rest = [tokens[j] for j in range(3) if j != i]
            if _SET_ONLY.match(rest[0]) and _COLLECTOR_ONLY.match(rest[1]):
                return f"{rest[0].lower()}:{rest[1].lower()} {variant_tok}"
            if _SET_ONLY.match(rest[1]) and _COLLECTOR_ONLY.match(rest[0]):
                return f"{rest[1].lower()}:{rest[0].lower()} {variant_tok}"
        return None

    return None


def _strip_art_marker(name: str) -> tuple[str, str | None]:
    """Remove a trailing ``[art]`` marker, returning ``(clean_name, art)``."""
    match = _ART_MARKER.search(name)
    if not match:
        return name, None
    art = _parse_art_content(match.group(1))
    if art is None:
        return name, None
    clean = name[: match.start()].strip()
    if not clean:
        return name, None
    return clean, art


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


def _split_front_back(name_part: str, raw_line: str) -> tuple[str, str | None]:
    """Split the post-quantity text into ``(front_part, back_part)``.

    A single ``/`` separator marks a double-sided card: everything before
    it is the front face, everything after it is the back face. Only one
    separator is allowed per line. A missing/blank back part means
    single-sided. A ``|`` is accepted as a legacy alias for ``/``.

    A double slash (``//``, e.g. MTG split cards like ``Fire // Ice``) is
    never a separator: only a ``/`` neither preceded nor followed by
    another ``/`` splits.
    """
    if "|" in name_part:
        parts = name_part.split("|")
        if len(parts) != 2 or any(
            re.search(r"(?<!/)/(?!/)", part) for part in parts
        ):
            raise ValueError(
                f"Invalid decklist line (only one '/' separator allowed): {raw_line!r}"
            )
        return parts[0], parts[1]
    indices = [m.start() for m in re.finditer(r"(?<!/)/(?!/)", name_part)]
    if not indices:
        return name_part, None
    if len(indices) > 1:
        raise ValueError(
            f"Invalid decklist line (only one '/' separator allowed): {raw_line!r}"
        )
    idx = indices[0]
    return name_part[:idx], name_part[idx + 1 :]


def parse_deck_file(file_path: str) -> tuple[str, list[DeckCard]]:
    """Read a decklist .txt file and return (deck_name, cards).

    Each non-empty line is expected to follow the format:
        <quantity> <full card name> [art] [*F*] [/ <back card name> [art] [*F*]]

    A trailing ``*F*``/``*G*`` marks the entry as foil and a trailing
    ``[art]`` requests a specific art variant. Lorcana markers are
    ``[enchanted]``/``[iconic]``/``[epic]``/``[special]``/``[base]``/``[best]``;
    MTG markers are a set code (``[m21]``, ``[2x2]``), a set + collector
    number (``[2x2:117]``, ``[2x2-117]``, ``[pltr 253s]``), a variant
    (``[fullart]``, ``[borderless]``, ``[showcase]``, ``[extended]``,
    ``[retro]``, ``[promo]``, ``[base]``/``[best]``) or a set (+collector)
    + variant combo (``[m21 borderless]``, ``[2x2:117 showcase]``).
    Both markers are stripped from the name and exposed as
    ``DeckCard.foil`` / ``DeckCard.art``; they may appear in either order.

    Double-sided cards append the back face after a ``/`` separator on the
    same line (the quantity applies to both faces, the back face takes no
    quantity of its own)::

        4 Delver of Secrets / Insectile Aberration
        2 Hades - King of Olympus [enchanted] / Hades - Lord of the Dead [base]
        1 Lightning Bolt [m21] *F* / Mountain [fullart]

    (``|`` still works as a legacy alias for ``/``. ``//`` inside a card
    name, e.g. MTG split cards like ``Fire // Ice``, is never a separator.)

    Each side strips its own trailing ``[art]`` / ``*F*`` markers
    independently (``back_name`` / ``back_art`` / ``back_foil``). PDF
    grouping (regular vs foil) always follows the front-face foil flag so
    front/back pages stay aligned.
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
            quantity, name_part = match.groups()
            front_part, back_part = _split_front_back(name_part, raw_line)
            if not front_part.strip():
                raise ValueError(f"Invalid decklist line (missing front name): {raw_line!r}")
            clean_name, foil, art = _strip_trailing_markers(front_part.strip())
            if not clean_name:
                raise ValueError(f"Invalid decklist line (missing front name): {raw_line!r}")
            back_name: str | None = None
            back_art: str | None = None
            back_foil = False
            if back_part is not None and back_part.strip():
                clean_back, clean_back_foil, clean_back_art = _strip_trailing_markers(
                    back_part.strip()
                )
                if not clean_back:
                    raise ValueError(
                        f"Invalid decklist line (missing back name): {raw_line!r}"
                    )
                back_name = clean_back
                back_art = clean_back_art
                back_foil = clean_back_foil
            cards.append(
                DeckCard(
                    quantity=int(quantity),
                    name=clean_name,
                    foil=foil,
                    art=art,
                    back_name=back_name,
                    back_art=back_art,
                    back_foil=back_foil,
                )
            )

    return deck_name, cards
