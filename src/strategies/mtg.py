"""MTG image-fetching strategy (placeholder).

Ready for Scryfall API integration. Currently raises ``NotImplementedError``
when invoked so the application fails loudly instead of silently.
"""
from __future__ import annotations

from .base import TCGStrategy


class MTGStrategy(TCGStrategy):
    """Stubbed MTG strategy conforming to :class:`TCGStrategy`."""

    name = "mtg"
    SCRYFALL_API = "https://api.scryfall.com"

    def fetch_card_image(self, card_name: str, output_path: str) -> bool:
        raise NotImplementedError(
            "MTG strategy is not yet implemented. Scryfall integration is pending."
        )