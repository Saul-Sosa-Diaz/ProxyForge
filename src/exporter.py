"""Output generation: image download, de-duplication and print-ready PDF."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import NamedTuple

from .models import DeckCard, ResolvedCard
from .parser import format_deck
from .pdf import PdfWriter, draw_image_ops, line_ops
from .strategies.base import TCGStrategy

logger = logging.getLogger(__name__)

# Physical card dimensions in millimetres (target print size).
# Standard TCG card size (63x88 mm); must never change so cards fit sleeves.
CARD_WIDTH_MM = 63.0
CARD_HEIGHT_MM = 88.0

# Print resolution. By default the PDF embeds every card at its source
# resolution (``target_dpi=None``); a DPI resamples the cards to that
# resolution instead (smaller files, e.g. PRINT_DPI or 300 for drafts).
PRINT_DPI = 800
MM_PER_INCH = 25.4
POINTS_PER_INCH = 72.0
# How far bleed strips reach under the card so no hairline shows between them.
_BLEED_OVERLAP_PT = 0.5

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

    The PDF is vector: pages measure the true physical size (A4 = 595x842
    pt) and each card image is placed at exactly 63x88 mm. By default
    (``target_dpi=None``) the downloaded file is embedded without resampling
    or lossy re-encoding (see :mod:`src.pdf`), so the print gets every pixel
    of the source (e.g. 1200 DPI MPC Autofill scans). An explicit
    ``target_dpi`` resamples each card to that resolution for smaller files.

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
        target_dpi: int | None = None,
    ) -> None:
        self.strategy = strategy
        self.output_base = Path(output_base_dir)
        self.page_width_mm = page_width_mm
        self.page_height_mm = page_height_mm
        self.page_margin_mm = page_margin_mm
        self.card_gap_mm = card_gap_mm
        self.target_dpi = target_dpi

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def export_deck(self, deck_name: str, cards: list[DeckCard]) -> Path:
        """Download images and build the PDFs in one go (CLI entry point).

        Equivalent to :meth:`resolve_images` followed by :meth:`save_decklist`
        and :meth:`render_pdfs`.

        Returns:
            The first PDF written (see :meth:`render_pdfs` for the order).
        """
        resolved = self.resolve_images(deck_name, cards)
        self.save_decklist(deck_name, resolved)
        return self.render_pdfs(deck_name, resolved)[0]

    def save_decklist(self, deck_name: str, resolved: list[ResolvedCard]) -> Path:
        """Write ``<output>/<deck_name>/<deck_name>.txt`` with the pinned arts.

        The entries carry the ``[art]`` markers chosen by
        :meth:`TCGStrategy.pin_art` (e.g. ``[mpc:<identifier>]``), so
        feeding this file back as ``--input`` downloads the same images.
        """
        deck_dir = self.output_base / deck_name
        deck_dir.mkdir(parents=True, exist_ok=True)
        path = deck_dir / f"{deck_name}.txt"
        path.write_text(format_deck(item.card for item in resolved), encoding="utf-8")
        logger.info("Pinned decklist written to %s", path)
        return path

    def resolve_images(
        self,
        deck_name: str,
        cards: list[DeckCard],
        on_progress: Callable[[int, int, DeckCard], None] | None = None,
    ) -> list[ResolvedCard]:
        """Download phase: fetch every unique face image once.

        Images land in ``<output>/<deck_name>/images`` and are reused across
        calls (an existing ``.png`` is never fetched again), so resolving a
        single edited entry later only downloads what changed. The same
        image file is shared by entries that only differ in finish (foil vs
        regular) or by a front and a back requesting the same name +
        ``[art]``; entries with a different ``[art]`` marker get their own
        image file. Entries without an explicit ``/ Back`` ask the strategy
        for an automatic back face (e.g. MTG double-faced cards).

        Args:
            deck_name: Deck name; selects the output subfolder.
            cards: Decklist entries in deck order.
            on_progress: Optional callback ``(index, total, card)`` invoked
                before each entry is resolved.

        Returns:
            One :class:`ResolvedCard` per entry in deck order, including
            failed ones (``front_path is None``).
        """
        images_dir = self.output_base / deck_name / "images"
        images_dir.mkdir(parents=True, exist_ok=True)
        unique: dict[str, Path] = {}
        resolved: list[ResolvedCard] = []
        for index, card in enumerate(cards):
            if on_progress is not None:
                on_progress(index, len(cards), card)
            resolved.append(self._resolve_card(card, images_dir, unique))
        return resolved

    def render_pdfs(self, deck_name: str, resolved: list[ResolvedCard]) -> list[Path]:
        """Render phase: emit one PDF per finish (regular / foil).

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

        Args:
            deck_name: Deck name; selects the output subfolder and PDF names.
            resolved: Output of :meth:`resolve_images` (possibly edited).
                Entries without a front image are skipped.

        Returns:
            Every PDF written, regular before foil and, within a finish,
            single-sided before the front/back pair.

        Raises:
            RuntimeError: If no entry has a front image to render.
        """
        deck_dir = self.output_base / deck_name
        deck_dir.mkdir(parents=True, exist_ok=True)

        pdf_paths: list[Path] = []
        for foil, prefix, tag in ((False, "", "PDF"), (True, "foil_", "Foil PDF")):
            group = [
                (r.front_path, r.back_path, r.card.quantity)
                for r in resolved
                if r.front_path is not None and r.card.foil == foil
            ]
            if not group:
                logger.debug("Deck '%s' has no %s cards.", deck_name, tag)
                continue
            singles = [(f, q) for f, b, q in group if b is None]
            duals = [(f, b, q) for f, b, q in group if b is not None]
            if singles:
                pdf_path = deck_dir / f"{prefix}{deck_name}.pdf"
                self._build_pdf(singles, pdf_path)
                logger.info("%s written to %s", tag, pdf_path)
                pdf_paths.append(pdf_path)
            pdf_paths.extend(self._emit_dual_pdfs(deck_dir, deck_name, duals, foil=foil))
        if not pdf_paths:
            raise RuntimeError("No images available to build the PDF.")
        return pdf_paths

    def _emit_dual_pdfs(
        self,
        deck_dir: Path,
        deck_name: str,
        duals: list[tuple[Path, Path | None, int]],
        foil: bool,
    ) -> list[Path]:
        """Emit the separated double-sided PDFs for one finish.

        Returns the PDFs written (front, then back when any back image
        exists); empty when there is nothing to render. ``duals`` holds
        ``(front, back_or_None, qty)`` pairs in deck order; fronts keep that
        order and backs mirror it, so both PDFs share pagination 1:1 with no
        blank pages (a failed back download degrades to a blank slot).
        """
        if not duals:
            return []
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
            return []
        dual_front_pdf = deck_dir / f"{prefix}{deck_name}.pdf"
        self._build_front_pages(front_flat, dual_front_pdf)
        logger.info("%s front PDF written to %s", tag, dual_front_pdf)
        written = [dual_front_pdf]
        if any(b is not None for b in back_flat):
            dual_back_pdf = deck_dir / f"{back_prefix}{deck_name}.pdf"
            self._build_back_pages(back_flat, dual_back_pdf)
            logger.info("%s back PDF written to %s", tag, dual_back_pdf)
            written.append(dual_back_pdf)
        return written

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

    def _resolve_card(
        self,
        card: DeckCard,
        images_dir: Path,
        unique: dict[str, Path],
    ) -> ResolvedCard:
        """Fetch the front and back images of one decklist entry.

        A failed front download leaves the entry without images (its back
        has no slot to align to); a failed back download degrades to a
        blank back slot. The returned card carries the pinned ``[art]``
        markers (see :meth:`_pin_card`).
        """
        card = self._pin_card(card)
        front_path = self._fetch_single_image(card.name, card.art, images_dir, unique)
        if front_path is None:
            return ResolvedCard(card=card)
        if not card.back_name:
            back_path = self._fetch_automatic_back(card.name, card.art, images_dir, unique)
            return ResolvedCard(card=card, front_path=front_path, back_path=back_path)
        back_path = self._fetch_single_image(
            card.back_name, card.back_art, images_dir, unique
        )
        if back_path is None:
            logger.warning(
                "Failed to fetch back image for '%s' (front '%s'); "
                "leaving its back slot blank.",
                card.back_name,
                card.name,
            )
        return ResolvedCard(card=card, front_path=front_path, back_path=back_path)

    def _pin_card(self, card: DeckCard) -> DeckCard:
        """Replace each face's ``[art]`` with the one pinned by the strategy.

        An automatic back the strategy can pin becomes an explicit
        ``/ Back [art]`` so the saved decklist reproduces it as well.
        """
        update: dict[str, str | None] = {}
        art = self.strategy.pin_art(card.name, card.art)
        if art != card.art:
            update["art"] = art
        if card.back_name:
            back_art = self.strategy.pin_art(card.back_name, card.back_art)
            if back_art != card.back_art:
                update["back_art"] = back_art
        else:
            back = self.strategy.pin_back(card.name, art)
            if back is not None:
                update["back_name"], update["back_art"] = back
        return card.model_copy(update=update) if update else card

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
    def _page_geometry(self) -> _Geometry:
        """Compute shared page geometry so front/back PDFs coincide exactly.

        Every value is in PDF points (1/72 inch) measured from the top-left
        corner of the page. Both front and back builders must use this
        single source so every 63x88 mm slot, gutter, margin and crop mark
        lands on the same physical coordinates.
        """
        cols, rows = self._compute_grid()
        card_w = _mm_to_pt(CARD_WIDTH_MM)
        card_h = _mm_to_pt(CARD_HEIGHT_MM)
        gap = _mm_to_pt(self.card_gap_mm)
        page_w = _mm_to_pt(self.page_width_mm)
        page_h = _mm_to_pt(self.page_height_mm)
        # Bleed must never overrun the gutter (a white strip has to remain
        # between adjacent cards) nor the page margin (outer bleed stays on
        # the sheet).
        bleed = _mm_to_pt(min(BLEED_MM, self.card_gap_mm / 2.0, self.page_margin_mm))
        # Center the grid block on the page so outer crop marks always sit
        # inside the sheet (never clipped at the page edge).
        block_w = cols * card_w + (cols - 1) * gap
        block_h = rows * card_h + (rows - 1) * gap
        return _Geometry(
            cols=cols,
            rows=rows,
            page_w=page_w,
            page_h=page_h,
            card_w=card_w,
            card_h=card_h,
            gap=gap,
            bleed=bleed,
            start_x=(page_w - block_w) / 2,
            start_y=(page_h - block_h) / 2,
        )

    def _image_size(self) -> tuple[int, int] | None:
        """Pixel size cards are resampled to (``None`` = source pixels)."""
        if self.target_dpi is None:
            return None
        return (
            round(CARD_WIDTH_MM * self.target_dpi / MM_PER_INCH),
            round(CARD_HEIGHT_MM * self.target_dpi / MM_PER_INCH),
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
        geo = self._page_geometry()
        per_page = geo.cols * geo.rows
        writer = PdfWriter()
        for page_start in range(0, len(front_slots), per_page):
            page_slots = front_slots[page_start : page_start + per_page]
            ops: list[str] = []
            for idx, slot_path in enumerate(page_slots):
                col = idx % geo.cols
                row = idx // geo.cols
                ops.append(self._card_ops(writer, geo, slot_path, col, row))
            # Trim (crop) marks in the outer margins, one tick per card edge.
            ops.append(self._crop_mark_ops(geo))
            writer.add_page(geo.page_w, geo.page_h, "\n".join(ops))
        writer.save(pdf_path)

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
        geo = self._page_geometry()
        per_page = geo.cols * geo.rows

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

        writer = PdfWriter()
        for page_slots in pages:
            ops: list[str] = []
            for idx, slot_path in enumerate(page_slots):
                if slot_path is None or not slot_path.exists():
                    continue  # single-sided front -> blank back keeps alignment
                col = idx % geo.cols
                row = idx // geo.cols
                mirror_col = (geo.cols - 1) - col
                ops.append(self._card_ops(writer, geo, slot_path, mirror_col, row))
            ops.append(self._crop_mark_ops(geo))
            writer.add_page(geo.page_w, geo.page_h, "\n".join(ops))
        writer.save(pdf_path)

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

    def _card_ops(
        self,
        writer: PdfWriter,
        geo: _Geometry,
        slot_path: Path,
        col: int,
        row: int,
    ) -> str:
        """Operators placing one card in grid slot ``(col, row)`` with bleed.

        The card image is stretched to exactly 63x88 mm at its source
        resolution (the printer does the only resampling). The bleed strip
        that extends ``bleed`` into the gutter redraws the same image
        mirrored around each card edge and clipped to the strip, so the
        trim line (where the crop marks point) coincides exactly with the
        visible card edge.
        """
        name = writer.add_image(slot_path, self._image_size())
        w, h, b = geo.card_w, geo.card_h, geo.bleed
        x = geo.start_x + col * (w + geo.gap)
        # PDF user space starts at the bottom-left corner of the page.
        y = geo.page_h - (geo.start_y + row * (h + geo.gap)) - h
        ops: list[str] = []
        if b > 0:
            # Strips reach _BLEED_OVERLAP_PT under the card so no hairline
            # gap shows between them; the card itself is drawn on top.
            o = _BLEED_OVERLAP_PT
            strips = (
                # (clip x, clip y, clip w, clip h), (a, d, e, f) of [a 0 0 d e f]
                ((x - b, y, b + o, h), (-w, h, x, y)),  # left, mirrored X
                ((x + w - o, y, b + o, h), (-w, h, x + 2 * w, y)),  # right
                ((x, y - b, w, b + o), (w, -h, x, y)),  # bottom, mirrored Y
                ((x, y + h - o, w, b + o), (w, -h, x, y + 2 * h)),  # top
                ((x - b, y - b, b + o, b + o), (-w, -h, x, y)),  # corners: both
                ((x + w - o, y - b, b + o, b + o), (-w, -h, x + 2 * w, y)),
                ((x - b, y + h - o, b + o, b + o), (-w, -h, x, y + 2 * h)),
                ((x + w - o, y + h - o, b + o, b + o), (-w, -h, x + 2 * w, y + 2 * h)),
            )
            for clip, (a, d, e, f) in strips:
                ops.append(draw_image_ops(name, (a, 0, 0, d, e, f), clip))
        ops.append(draw_image_ops(name, (w, 0, 0, h, x, y)))
        return "\n".join(ops)

    def _crop_mark_ops(self, geo: _Geometry) -> str:
        """Operators drawing trim (crop) marks in the outer margins.

        Every card edge is projected into the top/bottom (vertical cuts) and
        left/right (horizontal cuts) margins as a short vector tick pointing
        exactly at the card edge, so a guillotine cut aligned with a tick
        trims the card to precisely 63x88 mm. At the block corners the
        perpendicular ticks form a cut cross for the corner card.
        """
        mark_len = _mm_to_pt(CROP_MARK_LENGTH_MM)
        off = geo.bleed  # keep the clean margin free of artwork and marks overlap
        pitch_x = geo.card_w + geo.gap
        pitch_y = geo.card_h + geo.gap

        # Nominal trim positions (PDF coordinates): left/right edge of every
        # column, top/bottom edge of every row (with a gutter each card owns
        # its own trim line).
        xs: list[float] = []
        for c in range(geo.cols):
            left = geo.start_x + c * pitch_x
            xs.extend((left, left + geo.card_w))
        ys: list[float] = []
        for r in range(geo.rows):
            top = geo.page_h - (geo.start_y + r * pitch_y)
            ys.extend((top, top - geo.card_h))

        block_left = geo.start_x
        block_right = geo.start_x + geo.cols * pitch_x - geo.gap
        block_top = geo.page_h - geo.start_y
        block_bottom = block_top - (geo.rows * pitch_y - geo.gap)

        r, g, b = (c / 255 for c in CROP_MARK_COLOR)
        ops = [f"{r:g} {g:g} {b:g} RG {_mm_to_pt(CROP_MARK_THICKNESS_MM):.4f} w 0 J"]
        # Vertical cut ticks in the top and bottom margins.
        for x in xs:
            ops.append(line_ops(x, block_top + off, x, block_top + off + mark_len))
            ops.append(line_ops(x, block_bottom - off, x, block_bottom - off - mark_len))
        # Horizontal cut ticks in the left and right margins.
        for y in ys:
            ops.append(line_ops(block_left - off - mark_len, y, block_left - off, y))
            ops.append(line_ops(block_right + off, y, block_right + off + mark_len, y))
        return "\n".join(ops)


class _Geometry(NamedTuple):
    """Page layout in PDF points, measured from the page's top-left corner."""

    cols: int
    rows: int
    page_w: float
    page_h: float
    card_w: float
    card_h: float
    gap: float
    bleed: float
    start_x: float
    start_y: float


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
def _mm_to_pt(value_mm: float) -> float:
    return value_mm * POINTS_PER_INCH / MM_PER_INCH


def _sanitize_filename(name: str) -> str:
    cleaned = re.sub(r"[^\w\s()-]", "", name)
    cleaned = re.sub(r"\s+", "_", cleaned.strip())
    return cleaned or "card"


def _image_key(name: str, art: str | None) -> str:
    """De-duplication key (and file stem) for one card face image."""
    key = _sanitize_filename(name)
    if art:
        # Art values like '2x2:117' or 'm21 borderless' must not leak ':' or
        # spaces into the file name (':' is invalid on Windows).
        # '★' marks a distinct MTG printing (sld:1512★ vs sld:1512).
        art_slug = re.sub(r"[^\w]+", "-", art.replace("★", "star")).strip("-")
        key = f"{key}_{art_slug}"
    return key
