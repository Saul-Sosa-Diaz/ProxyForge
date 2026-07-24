"""Base abstract strategy for TCG image fetching."""
from __future__ import annotations

from abc import ABC, abstractmethod


class TCGStrategy(ABC):
    """Abstract interface that all TCG image-fetching strategies must implement."""

    name: str = "base"

    @abstractmethod
    def fetch_card_image(self, card_name: str, output_path: str) -> bool:
        """Fetch a single card image and write it to ``output_path``.

        Args:
            card_name: Full name of the card to fetch.
            output_path: Filesystem path where the image must be saved.

        Returns:
            ``True`` if an image was successfully saved, otherwise ``False``.
        """
        raise NotImplementedError