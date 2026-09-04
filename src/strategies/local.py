"""Local filesystem image-fetching strategy.

Instead of hitting any remote API, this strategy resolves every card name to
an image file already present on disk inside a user-provided directory
(``--local-dir``). The card name in the decklist **is** the local file name:

    * ``4 Pikachu ex · Ascended Heroes (ASC) #276`` resolves to
      ``<local-dir>/Pikachu ex · Ascended Heroes (ASC) #276.png`` (or any
      other supported extension).
    * The extension may also be included in the decklist itself, e.g.
      ``4 my_custom_card.jpg``.

Resolution order:
    1. Exact file name match (name as written, extension optional).
    2. Case-insensitive match by file name / stem across the directory.
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path

from .base import TCGStrategy, warn_unsupported_art

logger = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp")


class LocalStrategy(TCGStrategy):
    """Resolve card images from a local directory, matched by file name."""

    name = "local"

    DEFAULT_IMAGES_DIR = "input/images"

    def __init__(self, images_dir: str | None = None) -> None:
        self.images_dir = Path(images_dir) if images_dir else Path(self.DEFAULT_IMAGES_DIR)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def fetch_card_image(
        self,
        card_name: str,
        output_path: str,
        art: str | None = None,
    ) -> bool:
        warn_unsupported_art(card_name, art)
        source = self._resolve_local_file(card_name)
        if source is None:
            logger.warning(
                "Card '%s' not found in local directory '%s'",
                card_name,
                self.images_dir,
            )
            return False
        return self._copy_image(source, output_path)

    # ------------------------------------------------------------------
    # Resolution helpers
    # ------------------------------------------------------------------
    def _resolve_local_file(self, card_name: str) -> Path | None:
        if not self.images_dir.is_dir():
            logger.error("Local images directory '%s' does not exist", self.images_dir)
            return None
        exact = self._exact_match(card_name)
        if exact is not None:
            return exact
        return self._case_insensitive_match(card_name)

    def _exact_match(self, card_name: str) -> Path | None:
        """Match the name as written: with its own extension, or by appending
        each supported extension."""
        candidate = self.images_dir / card_name
        if candidate.suffix.lower() in SUPPORTED_EXTENSIONS and candidate.is_file():
            return candidate
        for ext in SUPPORTED_EXTENSIONS:
            candidate = self.images_dir / f"{card_name}{ext}"
            if candidate.is_file():
                return candidate
        return None

    def _case_insensitive_match(self, card_name: str) -> Path | None:
        """Fallback: compare the requested name (lowercased) against every
        file name and stem in the directory."""
        key = card_name.strip().lower()
        for file in sorted(self.images_dir.iterdir()):
            if not file.is_file() or file.suffix.lower() not in SUPPORTED_EXTENSIONS:
                continue
            if file.name.lower() == key or file.stem.lower() == key:
                return file
        return None

    # ------------------------------------------------------------------
    # Filesystem helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _copy_image(source: Path, output_path: str) -> bool:
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        if source.resolve() == out.resolve():
            # Source and destination are the same file; nothing to copy.
            return True
        try:
            shutil.copyfile(source, out)
        except OSError as exc:
            logger.warning("Failed to copy '%s' to '%s': %s", source, out, exc)
            return False
        return out.exists() and out.stat().st_size > 0
