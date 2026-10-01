"""Data models for the TCG Card Image Downloader."""
from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field


class DeckCard(BaseModel):
    """A single card entry from a decklist."""

    quantity: int = Field(gt=0, description="Number of copies of the card.")
    name: str = Field(min_length=1, description="Full card name.")
    foil: bool = Field(
        default=False,
        description="True when the decklist marks the card as foil/premium (e.g. a trailing '*F*').",
    )
    art: str | None = Field(
        default=None,
        description=(
            "Art variant requested for this card via a trailing '[variant]' marker "
            "(Lorcana: '[enchanted]'; MTG: '[m21]', '[2x2:117]', '[borderless]', "
            "'[m21 borderless]'). Only honored by strategies that support art "
            "selection (Lorcana, MTG); when unset the strategy default applies (best "
            "available art)."
        ),
    )
    back_name: str | None = Field(
        default=None,
        description=(
            "Optional back-face card name for double-sided cards, given after a "
            "'/' separator on the same decklist line "
            "('2 Lightning Bolt / Shock'). When set, the back image is printed "
            "in the mirrored slot of a separate '_back' PDF so manual duplex "
            "(flip on long edge) aligns front and back."
        ),
    )
    back_art: str | None = Field(
        default=None,
        description=(
            "Art variant requested for the back face via a trailing '[variant]' "
            "marker on the back side of the '/' separator."
        ),
    )
    back_foil: bool = Field(
        default=False,
        description=(
            "Foil marker parsed from the back side of the '/' separator. "
            "It is informational only: PDF grouping (regular vs foil) always "
            "follows the front-face 'foil' flag so front/back pages stay aligned."
        ),
    )
    front_only: bool = Field(
        default=False,
        description=(
            "Print the front face only, without any back (written as '/ -' in the "
            "decklist: '1 Delver of Secrets / -'). Skips the automatic back face "
            "a strategy would add (e.g. MTG double-faced cards)."
        ),
    )


class ArtOption(BaseModel):
    """One selectable art for a card (offered by the web UI art picker)."""

    value: str = Field(description="'[art]' marker that selects this art (e.g. '2x2:117', 'enchanted').")
    label: str = Field(description="Human-readable description (set, number, variant...).")
    image_url: str = Field(description="Preview image URL of this art.")
    keywords: str = Field(
        default="",
        description="Extra text the art picker filter matches (tags, artist...), not displayed.",
    )


class ResolvedCard(BaseModel):
    """A decklist entry after its images were fetched (download phase output).

    ``front_path`` is ``None`` when the front image could not be fetched; the
    entry is then left out of the PDFs. ``back_path`` is ``None`` for
    single-sided cards and for failed back downloads (``card.back_name`` set).
    """

    card: DeckCard
    front_path: Path | None = None
    back_path: Path | None = None


class DownloadResult(BaseModel):
    """Result of a single card image fetch operation."""

    card_name: str
    success: bool
    image_path: str | None = None
    source: str | None = None
    error: str | None = None