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
PRINT_DPI = 800
MM_PER_INCH = 25.4

# Card pixel size at the default 800 DPI (64x89 mm -> ~1989x2797 px).
CARD_WIDTH_PX = round(CARD_WIDTH_MM * PRINT_DPI / MM_PER_INCH)   # 1989
CARD_HEIGHT_PX = round(CARD_HEIGHT_MM * PRINT_DPI / MM_PER_INCH)  # 2797

# Cut-line geometry (guillotine-friendly continuous lines at every card edge).
CROP_MARK_OFFSET_MM = 0.0     # lines sit exactly on the card edge (cut where you see)
CROP_MARK_THICKNESS_MM = 0.12  # thin line so any kerf drift leaves no visible sliver
CROP_MARK_COLOR = (0, 0, 0)

# Page geometry (A4 portrait by default).
PAGE_WIDTH_MM = 210.0
PAGE_HEIGHT_MM = 297.0
PAGE_MARGIN_MM = 5.0
CARD_GAP_MM = 0.0  # cards touch; one shared cut line separates two adjacent cards


class Exporter:
    """Download unique card images and assemble a print-ready PDF grid.

    The PDF is rendered as a high-resolution raster at ``target_dpi`` so that
    each 64x89 mm card slot measures exactly ``CARD_WIDTH_PX x CARD_HEIGHT_PX``
    pixels (e.g. 1989x2797 px at 800 DPI). When Pillow saves the page with
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
        gap_px = round(self._mm_to_px(self.card_gap_mm))

        # Center the grid block on the page so outer crop marks always sit
        # inside the sheet (never clipped at the page edge).
        block_w_px = cols * card_w_px + (cols - 1) * gap_px
        block_h_px = rows * card_h_px + (rows - 1) * gap_px
        start_x = round((page_w_px - block_w_px) / 2)
        start_y = round((page_h_px - block_h_px) / 2)

        page_images: list[Image.Image] = []
        for page_start in range(0, len(valid_slots), per_page):
            page_slots = valid_slots[page_start : page_start + per_page]
            page = Image.new("RGB", (page_w_px, page_h_px), "white")
            draw = ImageDraw.Draw(page)
            for idx, slot_path in enumerate(page_slots):
                col = idx % cols
                row = idx // cols
                x = start_x + col * (card_w_px + gap_px)
                y = start_y + row * (card_h_px + gap_px)
                self._paste_card(page, slot_path, x, y, card_w_px, card_h_px)
            # Guillotine cut lines across the whole sheet, one per card edge.
            self._draw_cut_lines(draw, start_x, start_y, cols, rows,
                                 card_w_px, card_h_px, gap_px,
                                 page_w_px, page_h_px)
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
        # Each row needs cols*card + (cols-1)*gap. Rearranged so the trailing
        # gap is NOT counted (it doesn't exist after the last card), which is
        # what makes a 3x3 grid fit on A4 with the configured gap.
        usable_w = self.page_width_mm - 2 * self.page_margin_mm
        usable_h = self.page_height_mm - 2 * self.page_margin_mm
        cell_w = CARD_WIDTH_MM + self.card_gap_mm
        cell_h = CARD_HEIGHT_MM + self.card_gap_mm
        cols = max(1, int((usable_w + self.card_gap_mm) // cell_w))
        rows = max(1, int((usable_h + self.card_gap_mm) // cell_h))
        return cols, rows

    def _paste_card(
        self,
        page: Image.Image,
        slot_path: Path,
        x_px: int,
        y_px: int,
        card_w_px: int,
        card_h_px: int,
    ) -> None:
        with Image.open(slot_path) as src:
            src = src.convert("RGB")
            target = (card_w_px, card_h_px)
            if src.size != target:
                src = src.resize(target, Image.LANCZOS)
            page.paste(src, (x_px, y_px))

    def _draw_cut_lines(
        self,
        draw: ImageDraw.ImageDraw,
        start_x: int,
        start_y: int,
        cols: int,
        rows: int,
        card_w_px: int,
        card_h_px: int,
        gap_px: int,
        page_w_px: int,
        page_h_px: int,
    ) -> None:
        """Draw continuous guillotine cut lines across the whole sheet.

        One vertical line per column boundary (cols + 1 lines) and one
        horizontal line per row boundary (rows + 1 lines), each spanning the
        full page so the blade can be aligned end-to-end in one straight cut.
        Lines sit exactly on the card edges (offset 0): outer cuts trim the
        block edge, inner cuts run along the shared edge of two touching
        cards, so a single pass separates both cards with no white border on
        either side (modulo the guillotine kerf).
        """
        lw = max(1, round(self._mm_to_px(CROP_MARK_THICKNESS_MM)))

        # Vertical cut lines: at the left edge of every column + the right
        # edge of the last column.
        for c in range(cols + 1):
            x = start_x + c * (card_w_px + gap_px)
            draw.line([(x, 0), (x, page_h_px)], fill=CROP_MARK_COLOR, width=lw)

        # Horizontal cut lines: at the top edge of every row + the bottom
        # edge of the last row.
        for r in range(rows + 1):
            y = start_y + r * (card_h_px + gap_px)
            draw.line([(0, y), (page_w_px, y)], fill=CROP_MARK_COLOR, width=lw)


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
def _sanitize_filename(name: str) -> str:
    import re

    cleaned = re.sub(r"[^\w\s()-]", "", name)
    cleaned = re.sub(r"\s+", "_", cleaned.strip())
    return cleaned or "card"