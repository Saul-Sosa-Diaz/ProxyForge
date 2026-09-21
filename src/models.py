"""Data models for the TCG Card Image Downloader."""
from __future__ import annotations

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


class DownloadResult(BaseModel):
    """Result of a single card image fetch operation."""

    card_name: str
    success: bool
    image_path: str | None = None
    source: str | None = None
    error: str | None = None