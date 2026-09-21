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
# Standard TCG card size (63x88 mm); must never change so cards fit sleeves.
CARD_WIDTH_MM = 63.0
CARD_HEIGHT_MM = 88.0

# Print resolution.
PRINT_DPI = 800
MM_PER_INCH = 25.4

# Card pixel size at the default 800 DPI (63x88 mm -> ~1984x2772 px).
CARD_WIDTH_PX = round(CARD_WIDTH_MM * PRINT_DPI / MM_PER_INCH)  # 1984
CARD_HEIGHT_PX = round(CARD_HEIGHT_MM * PRINT_DPI / MM_PER_INCH)  # 2772

# Professional cut geometry: gutter between cards, mirrored-edge bleed and
# trim (crop) marks placed in the outer margins of the sheet.
# Bleed is 1.0 mm: each card is still rendered at exactly 63x88 mm, plus a
# 1 mm mirrored copy of its outer edge extending into the gutter. The trim
# line (where the crop marks point) stays exactly on the visible card edge,
# so a perfect cut yields 63x88 mm and a ~1 mm off cut still hits artwork
# instead of leaving a white sliver. With a 3 mm gutter, 1 mm of clean
# white remains between adjacent bleeds so the cut line stays visible.
BLEED_MM = 1.0  # mirrored-edge overrun past the card edge
CROP_MARK_LENGTH_MM = 3.0  # tick length, pointing outward from the block
CROP_MARK_THICKNESS_MM = 0.12  # thin line so any kerf drift leaves no visible sliver
CROP_MARK_COLOR = (0, 0, 0)

# Page geometry (A4 portrait by default).
PAGE_WIDTH_MM = 210.0
PAGE_HEIGHT_MM = 297.0
PAGE_MARGIN_MM = 5.0
# Gutter (separation) between adjacent cards: 3 mm of clean white paper.
CARD_GAP_MM = 3.0


class Exporter:
    """Download unique card images and assemble a print-ready PDF grid.

    The PDF is rendered as a high-resolution raster at ``target_dpi`` so that
    each 63x88 mm card slot measures exactly ``CARD_WIDTH_PX x CARD_HEIGHT_PX``
    pixels (e.g. 1984x2772 px at 800 DPI). When Pillow saves the page with
    ``resolution=target_dpi`` the resulting MediaBox is the true physical page
    size (A4 = 595x842 pt), so every card prints at exactly 63x88 mm.

    Professional sheet layout: cards are separated by a ``card_gap_mm``
    gutter and rendered at exactly 63x88 mm (never rescaled beyond that
    size). Crop marks in the outer page margins point exactly at the visible
    card edges, so a cut aligned with a mark trims the card to precisely
    63x88 mm. A mirrored-edge bleed (``BLEED_MM`` = 1.0 mm) extends a
    mirrored copy of the artwork into the gutter; the trim line stays on
    the visible card edge, so mis-cuts up to ~1 mm still hit artwork
    instead of white paper.
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
        """Download images and emit one PDF per finish (regular / foil).

        Foil entries (``DeckCard.foil``) are rendered into
        ``foil_<deck_name>.pdf`` so they can be printed on
        separate (e.g. holographic) stock; the remaining cards go into
        ``<deck_name>.pdf``. A PDF is only built for a finish
        that has at least one card.

        Single-sided cards go into ``<deck_name>.pdf`` (or
        ``foil_<deck_name>.pdf`` for foil fronts). Double-sided
        entries (``DeckCard.back_name``) are separated into their own
        ``front_<deck_name>.pdf`` (fronts, deck order) plus
        ``back_<deck_name>.pdf`` (backs, deck order;
        ``foil_front_<deck_name>.pdf`` / ``foil_back_<deck_name>.pdf``
        for foil fronts) so they print on
        their own: same page count front/back (page *N* of the back
        belongs behind page *N* of the front), no blank pages except
        failed back downloads. The back grid coincides exactly with the
        front grid — same 63x88 mm slots, same gutter, same margins, same
        upright orientation — with columns mirrored so manual duplex
        ("flip on long edge", print at 100 %) aligns each back with its
        front. Grouping always follows the front-face foil flag.
        """
        deck_dir = self.output_base / deck_name
        images_dir = deck_dir / "images"
        images_dir.mkdir(parents=True, exist_ok=True)

        expanded = self._download_unique_cards(cards, images_dir)
        regular = [(f, b, qty) for f, b, qty, foil in expanded if not foil]
        foils = [(f, b, qty) for f, b, qty, foil in expanded if foil]
        if not regular and not foils:
            raise RuntimeError("No images available to build the PDF.")

        pdf_path: Path | None = None
        if regular:
            reg_singles = [(f, q) for f, b, q in regular if b is None]
            reg_duals = [(f, b, q) for f, b, q in regular if b is not None]
            if reg_singles:
                front_pdf_path = deck_dir / f"{deck_name}.pdf"
                self._build_pdf(reg_singles, front_pdf_path)
                logger.info("PDF written to %s", front_pdf_path)
                pdf_path = pdf_path or front_pdf_path
            else:
                logger.debug(
                    "Deck '%s' has no single-sided regular cards; no regular PDF generated.",
                    deck_name,
                )
            dual_pdf_path = self._emit_dual_pdfs(
                deck_dir, deck_name, reg_duals, foil=False
            )
            pdf_path = pdf_path or dual_pdf_path
        if foils:
            foil_singles = [(f, q) for f, b, q in foils if b is None]
            foil_duals = [(f, b, q) for f, b, q in foils if b is not None]
            if foil_singles:
                foil_pdf_path = deck_dir / f"foil_{deck_name}.pdf"
                self._build_pdf(foil_singles, foil_pdf_path)
                logger.info("Foil PDF written to %s", foil_pdf_path)
                pdf_path = pdf_path or foil_pdf_path
            else:
                logger.debug(
                    "Deck '%s' has no single-sided foil cards; no foil PDF generated.",
                    deck_name,
                )
            dual_foil_pdf_path = self._emit_dual_pdfs(
                deck_dir, deck_name, foil_duals, foil=True
            )
            pdf_path = pdf_path or dual_foil_pdf_path
        else:
            logger.debug(
                "Deck '%s' has no foil cards; no foil PDF generated.", deck_name
            )
        if pdf_path is None:
            raise RuntimeError("No images available to build the PDF.")
        return pdf_path

    def _emit_dual_pdfs(
        self,
        deck_dir: Path,
        deck_name: str,
        duals: list[tuple[Path, Path | None, int]],
        foil: bool,
    ) -> Path | None:
        """Emit the separated double-sided PDFs for one finish.

        Returns the dual front PDF path, or ``None`` when there is
        nothing to render. ``duals`` holds ``(front, back_or_None, qty)``
        pairs in deck order; fronts keep that order and backs mirror it,
        so both PDFs share pagination 1:1 with no blank pages (a failed
        back download degrades to a blank slot).
        """
        if not duals:
            return None
        tag = "Foil dual-sided" if foil else "Dual-sided"
        prefix = "foil_front_" if foil else "front_"
        back_prefix = "foil_back_" if foil else "back_"
        front_flat, back_flat = self._expand_paired_slots(duals)
        front_flat, back_flat = self._drop_missing_fronts(front_flat, back_flat)
        if not front_flat:
            logger.warning(
                "%s entries for deck '%s' have no downloadable images; skipping dual PDFs.",
                tag,
                deck_name,
            )
            return None
        dual_front_pdf = deck_dir / f"{prefix}{deck_name}.pdf"
        self._build_front_pages(front_flat, dual_front_pdf)
        logger.info("%s front PDF written to %s", tag, dual_front_pdf)
        if any(b is not None for b in back_flat):
            dual_back_pdf = deck_dir / f"{back_prefix}{deck_name}.pdf"
            self._build_back_pages(back_flat, dual_back_pdf)
            logger.info("%s back PDF written to %s", tag, dual_back_pdf)
        return dual_front_pdf

    # ------------------------------------------------------------------
    # De-duplicated downloads
    # ------------------------------------------------------------------
    def _fetch_single_image(
        self,
        name: str,
        art: str | None,
        images_dir: Path,
        unique: dict[str, Path],
    ) -> Path | None:
        """Fetch one face image (de-duplicated); ``None`` on failure."""
        key = _image_key(name, art)
        if key in unique:
            return unique[key]
        image_path = images_dir / f"{key}.png"
        if not image_path.exists():
            logger.info("Fetching image for '%s'...", name)
            ok = self.strategy.fetch_card_image(name, str(image_path), art=art)
            if not ok:
                logger.warning("Failed to fetch image for '%s'", name)
                if image_path.exists():
                    image_path.unlink(missing_ok=True)
                return None
        unique[key] = image_path
        return image_path

    def _download_unique_cards(
        self,
        cards: list[DeckCard],
        images_dir: Path,
    ) -> list[tuple[Path, Path | None, int, bool]]:
        """Download each unique card once and expand quantities after the fact.

        Returns a list of ``(front_path, back_path_or_None, quantity,
        front_foil)`` tuples in deck order. The same image file is shared
        by entries that only differ in finish (foil vs regular) or by a
        front and a back requesting the same name + ``[art]``; entries with
        a different ``[art]`` marker get their own image file. A failed
        front download skips the whole entry (its back has no slot to
        align to); a failed back download degrades to a blank back slot.
        Entries without an explicit ``/ Back`` ask the strategy for an
        automatic back face (e.g. MTG double-faced cards), so a single
        decklist line can still yield both sides.
        """
        unique: dict[str, Path] = {}
        expanded: list[tuple[Path, Path | None, int, bool]] = []

        for card in cards:
            front_path = self._fetch_single_image(
                card.name, card.art, images_dir, unique
            )
            if front_path is None:
                continue
            back_path: Path | None = None
            if card.back_name:
                fetched_back = self._fetch_single_image(
                    card.back_name, card.back_art, images_dir, unique
                )
                if fetched_back is None:
                    logger.warning(
                        "Failed to fetch back image for '%s' (front '%s'); "
                        "leaving its back slot blank.",
                        card.back_name,
                        card.name,
                    )
                    back_path = None
                else:
                    back_path = fetched_back
            else:
                back_path = self._fetch_automatic_back(
                    card.name, card.art, images_dir, unique
                )
            expanded.append((front_path, back_path, card.quantity, card.foil))

        return expanded

    def _fetch_automatic_back(
        self,
        name: str,
        art: str | None,
        images_dir: Path,
        unique: dict[str, Path],
    ) -> Path | None:
        """Fetch a strategy-provided back face (de-duplicated); ``None`` if none.

        Used only when the decklist line names no explicit back. The file
        is stored next to the front image under ``<front key>_back.png``.
        """
        key = f"{_image_key(name, art)}_back"
        if key in unique:
            return unique[key]
        image_path = images_dir / f"{key}.png"
        if not image_path.exists():
            if not self.strategy.fetch_card_back_image(
                name, str(image_path), art=art
            ):
                if image_path.exists():
                    image_path.unlink(missing_ok=True)
                return None
            if not image_path.exists():
                return None
        unique[key] = image_path
        return image_path

    def _expand_paired_slots(
        self,
        paired: list[tuple[Path, Path | None, int]],
    ) -> tuple[list[Path], list[Path | None]]:
        """Expand ``(front, back_or_None, qty)`` into parallel flat slot lists.

        Both lists share the same order and length so index ``i`` of the
        back list is the reverse face of index ``i`` of the front list
        (``None`` = single-sided, prints blank).
        """
        front_slots: list[Path] = []
        back_slots: list[Path | None] = []
        for front_path, back_path, quantity in paired:
            front_slots.extend([front_path] * quantity)
            back_slots.extend([back_path] * quantity)
        return front_slots, back_slots

    def _drop_missing_fronts(
        self,
        front_slots: list[Path],
        back_slots: list[Path | None],
    ) -> tuple[list[Path], list[Path | None]]:
        """Drop indices whose front image is missing, keeping pairs aligned.

        Missing/failed back images become ``None`` (blank) instead of
        shifting pagination.
        """
        kept_front: list[Path] = []
        kept_back: list[Path | None] = []
        for front_path, back_path in zip(front_slots, back_slots):
            if not front_path.exists():
                continue
            kept_front.append(front_path)
            if back_path is None or not back_path.exists():
                kept_back.append(None)
            else:
                kept_back.append(back_path)
        return kept_front, kept_back

    # ------------------------------------------------------------------
    # PDF assembly
    # ------------------------------------------------------------------
    def _page_geometry(self) -> tuple[int, int, int, int, int, int, int, int, int, int, int]:
        """Compute shared raster geometry so front/back PDFs coincide exactly.

        Returns ``(cols, rows, per_page, page_w_px, page_h_px, card_w_px,
        card_h_px, gap_px, bleed_px, start_x, start_y)``. Both front and
        back builders must use this single source so every 63x88 mm slot,
        gutter, margin and crop mark lands on the same physical coordinates.
        """
        cols, rows = self._compute_grid()
        per_page = cols * rows
        page_w_px = round(self._mm_to_px(self.page_width_mm))
        page_h_px = round(self._mm_to_px(self.page_height_mm))
        card_w_px = round(self._mm_to_px(CARD_WIDTH_MM))
        card_h_px = round(self._mm_to_px(CARD_HEIGHT_MM))
        gap_px = round(self._mm_to_px(self.card_gap_mm))
        # Bleed must never overrun the gutter (a white strip has to remain
        # between adjacent cards) nor the page margin (outer bleed stays on
        # the sheet).
        bleed_mm = min(BLEED_MM, self.card_gap_mm / 2.0, self.page_margin_mm)
        bleed_px = round(self._mm_to_px(bleed_mm))

        # Center the grid block on the page so outer crop marks always sit
        # inside the sheet (never clipped at the page edge).
        block_w_px = cols * card_w_px + (cols - 1) * gap_px
        block_h_px = rows * card_h_px + (rows - 1) * gap_px
        start_x = round((page_w_px - block_w_px) / 2)
        start_y = round((page_h_px - block_h_px) / 2)
        return (
            cols,
            rows,
            per_page,
            page_w_px,
            page_h_px,
            card_w_px,
            card_h_px,
            gap_px,
            bleed_px,
            start_x,
            start_y,
        )

    def _save_pages(self, page_images: list[Image.Image], pdf_path: Path) -> None:
        if not page_images:
            raise RuntimeError("No images available to build the PDF.")
        page_images[0].save(
            str(pdf_path),
            "PDF",
            resolution=self.target_dpi,
            save_all=True,
            append_images=page_images[1:],
        )

    def _build_pdf(
        self,
        expanded: list[tuple[Path, int]],
        pdf_path: Path,
    ) -> None:
        """Legacy front-only builder (kept for backwards compatibility)."""
        slots = self._flatten_slots(expanded)
        if not slots:
            raise RuntimeError("No images available to build the PDF.")
        valid_slots = [s for s in slots if s.exists()]
        if not valid_slots:
            raise RuntimeError("No downloaded images exist on disk.")
        self._build_front_pages(valid_slots, pdf_path)

    def _build_front_pages(
        self,
        front_slots: list[Path],
        pdf_path: Path,
    ) -> None:
        """Render front pages in natural order (no mirroring)."""
        if not front_slots:
            raise RuntimeError("No images available to build the PDF.")
        (
            cols,
            rows,
            per_page,
            page_w_px,
            page_h_px,
            card_w_px,
            card_h_px,
            gap_px,
            bleed_px,
            start_x,
            start_y,
        ) = self._page_geometry()

        page_images: list[Image.Image] = []
        for page_start in range(0, len(front_slots), per_page):
            page_slots = front_slots[page_start : page_start + per_page]
            page = Image.new("RGB", (page_w_px, page_h_px), "white")
            draw = ImageDraw.Draw(page)
            for idx, slot_path in enumerate(page_slots):
                col = idx % cols
                row = idx // cols
                x = start_x + col * (card_w_px + gap_px)
                y = start_y + row * (card_h_px + gap_px)
                self._paste_card(page, slot_path, x, y, card_w_px, card_h_px, bleed_px)
            # Trim (crop) marks in the outer margins, one tick per card edge.
            self._draw_crop_marks(
                draw,
                start_x,
                start_y,
                cols,
                rows,
                card_w_px,
                card_h_px,
                gap_px,
                bleed_px,
            )
            page_images.append(page)

        self._save_pages(page_images, pdf_path)

    def _build_back_pages(
        self,
        back_slots: list[Path | None],
        pdf_path: Path,
    ) -> None:
        """Render back pages mirrored for manual duplex (flip on long edge).

        ``back_slots`` is in front order (index ``i`` is the back of front
        ``i``; ``None`` = single-sided blank). Each page keeps the exact
        same slot size, gutter, margins and upright orientation as the
        front PDF; only columns are mirrored
        (``mirror_col = cols - 1 - col``) so that after printing the fronts,
        flipping the stack like a book (long edge) and printing the backs
        at 100 % puts every back exactly behind its front. Crop marks are
        identical to the front pages. Trailing fully-blank pages are
        dropped (never rendered); middle pages keep their blanks to
        preserve page-to-page alignment.
        """
        if not back_slots or not any(b is not None for b in back_slots):
            raise RuntimeError("No back images available to build the back PDF.")
        (
            cols,
            rows,
            per_page,
            page_w_px,
            page_h_px,
            card_w_px,
            card_h_px,
            gap_px,
            bleed_px,
            start_x,
            start_y,
        ) = self._page_geometry()

        # Split into pages and drop trailing fully-blank ones so the back
        # PDF never ends with (nor, combined with backs-first ordering,
        # contains) empty pages. Middle pages keep their blanks so page N
        # of the back still belongs behind page N of the front.
        pages = [
            back_slots[page_start : page_start + per_page]
            for page_start in range(0, len(back_slots), per_page)
        ]
        while pages and all(s is None or not s.exists() for s in pages[-1]):
            pages.pop()
        if not pages:
            raise RuntimeError("No back images available to build the back PDF.")

        page_images: list[Image.Image] = []
        for page_slots in pages:
            page = Image.new("RGB", (page_w_px, page_h_px), "white")
            draw = ImageDraw.Draw(page)
            for idx, slot_path in enumerate(page_slots):
                if slot_path is None or not slot_path.exists():
                    continue  # single-sided front -> blank back keeps alignment
                col = idx % cols
                row = idx // cols
                mirror_col = (cols - 1) - col
                x = start_x + mirror_col * (card_w_px + gap_px)
                y = start_y + row * (card_h_px + gap_px)
                self._paste_card(page, slot_path, x, y, card_w_px, card_h_px, bleed_px)
            self._draw_crop_marks(
                draw,
                start_x,
                start_y,
                cols,
                rows,
                card_w_px,
                card_h_px,
                gap_px,
                bleed_px,
            )
            page_images.append(page)

        self._save_pages(page_images, pdf_path)

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
        bleed_px: int,
    ) -> None:
        """Paste a card at its exact nominal size with mirrored-edge bleed.

        The card artwork itself is rendered at exactly ``card_w_px x
        card_h_px`` (63x88 mm, never enlarged). The bleed strip that extends
        ``bleed_px`` into the gutter is a mirrored copy of the artwork's
        outer edge, so the trim line (where the crop marks point) coincides
        exactly with the visible card edge.
        """
        with Image.open(slot_path) as src:
            src = src.convert("RGB")
            if src.size != (card_w_px, card_h_px):
                src = src.resize((card_w_px, card_h_px), Image.LANCZOS)
            if bleed_px <= 0:
                page.paste(src, (x_px, y_px))
                return

            b = bleed_px
            w, h = card_w_px, card_h_px
            canvas = Image.new("RGB", (w + 2 * b, h + 2 * b))
            canvas.paste(src, (b, b))
            # Edge strips mirrored into the gutter (left, right, top, bottom).
            canvas.paste(
                src.crop((0, 0, b, h)).transpose(Image.FLIP_LEFT_RIGHT), (0, b)
            )
            canvas.paste(
                src.crop((w - b, 0, w, h)).transpose(Image.FLIP_LEFT_RIGHT), (b + w, b)
            )
            canvas.paste(
                src.crop((0, 0, w, b)).transpose(Image.FLIP_TOP_BOTTOM), (b, 0)
            )
            canvas.paste(
                src.crop((0, h - b, w, h)).transpose(Image.FLIP_TOP_BOTTOM), (b, b + h)
            )
            # Corner squares mirrored diagonally.
            canvas.paste(src.crop((0, 0, b, b)).transpose(Image.ROTATE_180), (0, 0))
            canvas.paste(
                src.crop((w - b, 0, w, b)).transpose(Image.ROTATE_180), (b + w, 0)
            )
            canvas.paste(
                src.crop((0, h - b, b, h)).transpose(Image.ROTATE_180), (0, b + h)
            )
            canvas.paste(
                src.crop((w - b, h - b, w, h)).transpose(Image.ROTATE_180),
                (b + w, b + h),
            )
            page.paste(canvas, (x_px - b, y_px - b))

    def _draw_crop_marks(
        self,
        draw: ImageDraw.ImageDraw,
        start_x: int,
        start_y: int,
        cols: int,
        rows: int,
        card_w_px: int,
        card_h_px: int,
        gap_px: int,
        bleed_px: int,
    ) -> None:
        """Draw trim (crop) marks in the outer margins of the sheet.

        Every card edge is projected into the top/bottom (vertical cuts) and
        left/right (horizontal cuts) margins as a short tick pointing
        exactly at the card edge, so a guillotine cut aligned with a tick
        trims the card to precisely 63x88 mm. At the block corners the
        perpendicular ticks form a cut cross for the corner card.
        """
        lw = max(1, round(self._mm_to_px(CROP_MARK_THICKNESS_MM)))
        mark_len = round(self._mm_to_px(CROP_MARK_LENGTH_MM))
        off = bleed_px  # keep the clean margin free of artwork and marks overlap

        pitch_x = card_w_px + gap_px
        pitch_y = card_h_px + gap_px

        # Nominal trim positions: left/right edge of every column, top/bottom
        # edge of every row (with a gutter each card owns its own trim line).
        xs: list[int] = []
        for c in range(cols):
            left = start_x + c * pitch_x
            xs.extend((left, left + card_w_px))
        ys: list[int] = []
        for r in range(rows):
            top = start_y + r * pitch_y
            ys.extend((top, top + card_h_px))

        block_left = start_x
        block_right = start_x + cols * pitch_x - gap_px  # last column right edge
        block_top = start_y
        block_bottom = start_y + rows * pitch_y - gap_px  # last row bottom edge

        # Vertical cut ticks in the top and bottom margins.
        for x in xs:
            draw.line(
                [(x, block_top - off - mark_len), (x, block_top - off)],
                fill=CROP_MARK_COLOR,
                width=lw,
            )
            draw.line(
                [(x, block_bottom + off), (x, block_bottom + off + mark_len)],
                fill=CROP_MARK_COLOR,
                width=lw,
            )

        # Horizontal cut ticks in the left and right margins.
        for y in ys:
            draw.line(
                [(block_left - off - mark_len, y), (block_left - off, y)],
                fill=CROP_MARK_COLOR,
                width=lw,
            )
            draw.line(
                [(block_right + off, y), (block_right + off + mark_len, y)],
                fill=CROP_MARK_COLOR,
                width=lw,
            )


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
def _sanitize_filename(name: str) -> str:
    import re

    cleaned = re.sub(r"[^\w\s()-]", "", name)
    cleaned = re.sub(r"\s+", "_", cleaned.strip())
    return cleaned or "card"


def _image_key(name: str, art: str | None) -> str:
    """De-duplication key (and file stem) for one card face image."""
    key = _sanitize_filename(name)
    if art:
        key = f"{key}_{art}"
    return key
