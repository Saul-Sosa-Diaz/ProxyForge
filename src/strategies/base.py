"""Base abstract strategy for TCG image fetching."""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)


def warn_unsupported_art(card_name: str, art: str | None) -> None:
    """Log a warning when a per-card ``[art]`` marker reaches a strategy
    that does not honor art selection."""
    if art is not None:
        logger.warning(
            "Art marker '[%s]' on card '%s' is only supported by the Lorcana "
            "strategy; ignoring it.",
            art,
            card_name,
        )


class TCGStrategy(ABC):
    """Abstract interface that all TCG image-fetching strategies must implement."""

    name: str = "base"

    # Whether this strategy honors per-card art-variant requests
    # (``DeckCard.art`` / the ``[art]`` decklist marker).
    supports_art: bool = False

    @abstractmethod
    def fetch_card_image(
        self,
        card_name: str,
        output_path: str,
        art: str | None = None,
    ) -> bool:
        """Fetch a single card image and write it to ``output_path``.

        Args:
            card_name: Full name of the card to fetch.
            output_path: Filesystem path where the image must be saved.
            art: Optional art variant requested for this specific card via a
                decklist marker (e.g. ``"enchanted"``). Only strategies with
                ``supports_art = True`` honor it; the others must accept and
                ignore it.

        Returns:
            ``True`` if an image was successfully saved, otherwise ``False``.
        """
        raise NotImplementedError