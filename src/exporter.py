"""Output generation: image download, de-duplication and print-ready PDF."""
from __future__ import annotations

import logging
from collections.abc import Iterable
from pathlib import Path

from PIL import Image, ImageDraw

from .models import DeckCard
from .strategies.base import TCGStrategy

logger = logging.getLogger(__name__)

# Physical card dimensions in millimetres (target print size).
CARD_WIDTH_MM = 64.0
CARD_HEIGHT_MM = 89.0

# Print resolution.
PRINT_DPI = 300
MM_PER_INCH = 25.4

# Card pixel size at the default 300 DPI (64x89 mm -> ~755x1051 px).
CARD_WIDTH_PX = round(CARD_WIDTH_MM * PRINT_DPI / MM_PER_INCH)   # 756
CARD_HEIGHT_PX = round(CARD_HEIGHT_MM * PRINT_DPI / MM_PER_INCH)  # 1051

# Crop mark geometry (all in millimetres, converted to pixels at render time).
CROP_MARK_LEN_MM = 3.0        # length of each crop-mark tick
CROP_MARK_OFFSET_MM = 2.0     # gap between card edge and crop mark
CROP_MARK_THICKNESS_MM = 0.25  # line thickness
CROP_MARK_COLOR = (0, 0, 0)

# Page geometry (A4 portrait by default).
PAGE_WIDTH_MM = 210.0
PAGE_HEIGHT_MM = 297.0
PAGE_MARGIN_MM = 10.0
CARD_GAP_MM = 8.0  # spacing between adjacent card slots (room for crop marks)


class Exporter:
    """Download unique card images and assemble a print-ready PDF grid.

    The PDF is rendered as a high-resolution raster at ``target_dpi`` so that
    each 64x89 mm card slot measures exactly ``CARD_WIDTH_PX x CARD_HEIGHT_PX``
    pixels (e.g. 756x1051 px at 300 DPI). When Pillow saves the page with
    ``resolution=target_dpi`` the resulting MediaBox is the true physical page
    size (A4 = 595x842 pt), so every card prints at exactly 64x89 mm.
    """

    def __init__(
        self,
        strategy: TCGStrategy,
        output_base_dir: str = "/app/output",
        page_width_mm: float = PAGE_WIDTH_MM,
        page_height_mm: float = PAGE_HEIGHT_MM,
        page_margin_mm: float = PAGE_MARGIN_MM,
        card_gap_mm: float = CARD_GAP_MM,
        target_dpi: int = PRINT_DPI,
    ) -> None:
        self.strategy = strategy
        self.output_base = Path(output_base_dir)
        self.page_width_mm = page_width_mm
        self.page_height_mm = page_height_mm
        self.page_margin_mm = page_margin_mm
        self.card_gap_mm = card_gap_mm
        self.target_dpi = target_dpi

    def _mm_to_px(self, value_mm: float) -> float:
        return value_mm * self.target_dpi / MM_PER_INCH

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------
    def export_deck(self, deck_name: str, cards: list[DeckCard]) -> Path:
        deck_dir = self.output_base / deck_name
        images_dir = deck_dir / "images"
        images_dir.mkdir(parents=True, exist_ok=True)

        expanded = self._download_unique_cards(cards, images_dir)
        pdf_path = deck_dir / f"{deck_name}_printable.pdf"
        self._build_pdf(expanded, pdf_path)
        logger.info("PDF written to %s", pdf_path)
        return pdf_path

    # ------------------------------------------------------------------
    # De-duplicated downloads
    # ------------------------------------------------------------------
    def _download_unique_cards(
        self,
        cards: list[DeckCard],
        images_dir: Path,
    ) -> list[tuple[Path, int]]:
        """Download each unique card once and expand quantities after the fact.

        Returns a list of ``(image_path, quantity)`` tuples in deck order.
        """
        unique: dict[str, Path] = {}
        expanded: list[tuple[Path, int]] = []

        for card in cards:
            key = _sanitize_filename(card.name)
            if key in unique:
                image_path = unique[key]
            else:
                image_path = images_dir / f"{key}.png"
                if not image_path.exists():
                    logger.info("Fetching image for '%s'...", card.name)
                    ok = self.strategy.fetch_card_image(card.name, str(image_path))
                    if not ok:
                        logger.warning("Failed to fetch image for '%s'", card.name)
                        if image_path.exists():
                            image_path.unlink(missing_ok=True)
                        continue
                unique[key] = image_path
            expanded.append((image_path, card.quantity))

        return expanded

    # ------------------------------------------------------------------
    # PDF assembly
    # ------------------------------------------------------------------
    def _build_pdf(
        self,
        expanded: list[tuple[Path, int]],
        pdf_path: Path,
    ) -> None:
        slots = self._flatten_slots(expanded)
        if not slots:
            raise RuntimeError("No images available to build the PDF.")

        cols, rows = self._compute_grid()
        per_page = cols * rows
        valid_slots = [s for s in slots if s.exists()]
        if not valid_slots:
            raise RuntimeError("No downloaded images exist on disk.")

        page_w_px = round(self._mm_to_px(self.page_width_mm))
        page_h_px = round(self._mm_to_px(self.page_height_mm))
        card_w_px = round(self._mm_to_px(CARD_WIDTH_MM))
        card_h_px = round(self._mm_to_px(CARD_HEIGHT_MM))
        gap_px = self._mm_to_px(self.card_gap_mm)
        margin_px = self._mm_to_px(self.page_margin_mm)

        page_images: list[Image.Image] = []
        for page_start in range(0, len(valid_slots), per_page):
            page_slots = valid_slots[page_start : page_start + per_page]
            page = Image.new("RGB", (page_w_px, page_h_px), "white")
            draw = ImageDraw.Draw(page)
            for idx, slot_path in enumerate(page_slots):
                col = idx % cols
                row = idx // cols
                x = margin_px + col * (card_w_px + gap_px)
                y = margin_px + row * (card_h_px + gap_px)
                self._paste_card(page, slot_path, x, y, card_w_px, card_h_px)
                self._draw_crop_marks(draw, x, y, card_w_px, card_h_px)
            page_images.append(page)

        page_images[0].save(
            str(pdf_path),
            "PDF",
            resolution=self.target_dpi,
            save_all=True,
            append_images=page_images[1:],
        )

    def _flatten_slots(self, expanded: Iterable[tuple[Path, int]]) -> list[Path]:
        """Expand ``(image_path, quantity)`` into a flat list of per-card slots."""
        slots: list[Path] = []
        for image_path, quantity in expanded:
            if not image_path.exists():
                continue
            slots.extend([image_path] * quantity)
        return slots

    def _compute_grid(self) -> tuple[int, int]:
        usable_w = self.page_width_mm - 2 * self.page_margin_mm
        usable_h = self.page_height_mm - 2 * self.page_margin_mm
        cols = max(1, int(usable_w // (CARD_WIDTH_MM + self.card_gap_mm)))
        rows = max(1, int(usable_h // (CARD_HEIGHT_MM + self.card_gap_mm)))
        return cols, rows

    def _paste_card(
        self,
        page: Image.Image,
        slot_path: Path,
        x_px: float,
        y_px: float,
        card_w_px: int,
        card_h_px: int,
    ) -> None:
        with Image.open(slot_path) as src:
            src = src.convert("RGB")
            target = (card_w_px, card_h_px)
            if src.size != target:
                src = src.resize(target, Image.LANCZOS)
            page.paste(src, (round(x_px), round(y_px)))

    def _draw_crop_marks(
        self,
        draw: ImageDraw.ImageDraw,
        x: float,
        y: float,
        w: float,
        h: float,
    ) -> None:
        """Draw four-corner crop marks around a 64x89 mm card slot."""
        offset = self._mm_to_px(CROP_MARK_OFFSET_MM)
        length = self._mm_to_px(CROP_MARK_LEN_MM)
        lw = max(1, round(self._mm_to_px(CROP_MARK_THICKNESS_MM)))

        # Corner anchor points (just outside the card edges).
        left = x - offset
        right = x + w + offset
        top = y - offset
        bottom = y + h + offset

        # Top-left
        draw.line([(left - length, top), (left, top)], fill=CROP_MARK_COLOR, width=lw)
        draw.line([(left, top - length), (left, top)], fill=CROP_MARK_COLOR, width=lw)
        # Top-right
        draw.line([(right, top), (right + length, top)], fill=CROP_MARK_COLOR, width=lw)
        draw.line([(right, top - length), (right, top)], fill=CROP_MARK_COLOR, width=lw)
        # Bottom-left
        draw.line([(left - length, bottom), (left, bottom)], fill=CROP_MARK_COLOR, width=lw)
        draw.line([(left, bottom), (left, bottom + length)], fill=CROP_MARK_COLOR, width=lw)
        # Bottom-right
        draw.line([(right, bottom), (right + length, bottom)], fill=CROP_MARK_COLOR, width=lw)
        draw.line([(right, bottom), (right, bottom + length)], fill=CROP_MARK_COLOR, width=lw)


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
def _sanitize_filename(name: str) -> str:
    import re

    cleaned = re.sub(r"[^\w\s()-]", "", name)
    cleaned = re.sub(r"\s+", "_", cleaned.strip())
    return cleaned or "card"