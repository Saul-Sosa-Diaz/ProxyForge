"""Base abstract strategy for TCG image fetching."""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod

from ..models import ArtOption, DeckCard

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

    def prefetch(self, cards: list[DeckCard]) -> None:
        """Warm the strategy's caches for a whole deck before it is resolved.

        Called once by the exporter before the per-card fetches so sources
        with batch endpoints (e.g. MPC Autofill) can look many cards up in
        a few requests. The default does nothing. Must not raise.

        Args:
            cards: Decklist entries about to be resolved.
        """

    def pin_art(self, card_name: str, art: str | None = None) -> str | None:
        """Turn an ``[art]`` request into the marker of the exact image it yields.

        Called by the exporter before downloading so the decklist it writes
        back reproduces the same image on later runs, even when the source
        would rank its results differently (e.g. MPC Autofill search). The
        default keeps ``art`` unchanged. Must not raise.

        Args:
            card_name: Full name of the card, as written in the decklist.
            art: Requested ``[art]`` marker (``None`` for the default art).

        Returns:
            The ``[art]`` marker to download and persist.
        """
        return art

    def pin_back(self, card_name: str, art: str | None = None) -> tuple[str, str | None] | None:
        """Pin the automatic back face of a card as an explicit ``/ Back``.

        Lets the exporter write ``Front [art] / Back [back_art]`` so the
        automatic back (see :meth:`fetch_card_back_image`) is also
        reproducible. The default pins nothing. Must not raise.

        Args:
            card_name: Full name of the front card.
            art: Front ``[art]`` marker already returned by :meth:`pin_art`.

        Returns:
            ``(back_name, back_art)``, or ``None`` to keep the automatic back.
        """
        return None

    def list_art_options(self, card_name: str) -> list[ArtOption]:
        """List the arts available for a card (web UI art picker).

        Each option's ``value`` is an ``[art]`` marker that, passed to
        :meth:`fetch_card_image`, downloads exactly that art. Only
        strategies with ``supports_art = True`` override this; the default
        offers no alternatives. Must not raise.

        Args:
            card_name: Full name of the card, as written in the decklist.

        Returns:
            The selectable arts (empty when none are known).
        """
        return []