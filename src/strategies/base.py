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
            "and MTG strategies; ignoring it.",
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

    def fetch_card_back_image(
        self,
        card_name: str,
        output_path: str,
        art: str | None = None,
    ) -> bool:
        """Fetch the automatic back-face image of a double-faced card.

        Called by the exporter only when the decklist line names no
        explicit back (no ``/ Back`` part): strategies whose cards can
        have two physical faces (e.g. MTG transform / modal DFCs) may
        resolve and save the back face here so a single decklist line
        yields both sides. The default implementation reports no back
        face (``False``) without touching ``output_path``.

        Args:
            card_name: Full name of the card (same value passed to
                :meth:`fetch_card_image` for the front face).
            output_path: Filesystem path where the back image must be saved.
            art: Same art variant requested for the front face, so both
                faces come from the same printing.

        Returns:
            ``True`` if a back-face image was successfully saved,
            otherwise ``False`` (single-faced card or unresolvable back).
        """
        return False