"""Streamlit web UI: preview the card images before building the PDFs.

Run with:
    streamlit run src/app.py

The flow mirrors the CLI but splits the export in two phases so the user can
review (and fix) the fetched images before rendering:
    decklist -> Exporter.resolve_images -> preview/edit -> Exporter.render_pdfs

The preview is split into one tab per generated PDF (regular, foil,
front/back, foil front/back), each with its own download button once the
PDFs are rendered.
"""
from __future__ import annotations

import base64
import functools
import io
import logging
import os
import re
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

import streamlit as st
from PIL import Image

# Bootstrap the project root onto sys.path so the ``src`` package is importable
# when Streamlit runs this file as a script.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.exporter import PRINT_DPI, Exporter
from src.main import _STRATEGY_REGISTRY, _make_strategy
from src.models import ArtOption, DeckCard, ResolvedCard
from src.parser import format_deck, parse_deck_text
from src.strategies.base import TCGStrategy
from src.strategies.local import LocalStrategy

logger = logging.getLogger("tcg-downloader.web")

DEFAULT_OUTPUT_DIR = os.environ.get("PROXYFORGE_OUTPUT_DIR", "output")
DEFAULT_DECK_NAME = "mazo"
# None = original images (maximum quality); a DPI resamples to smaller files.
DPI_OPTIONS = (None, PRINT_DPI, 600, 300)
GRID_COLUMNS = 4
ART_GRID_COLUMNS = 4
ART_OPTIONS_PER_PAGE = 12
THUMBNAIL_SIZE = (360, 504)

TCG_LABELS = {
    "local": "Imágenes locales",
    "lorcana": "Disney Lorcana",
    "mtg": "Magic: The Gathering",
    "pokemon": "Pokémon TCG",
}

FAILED_TAB = "⚠️ No encontradas"

# Front/back pair: the front sits on top and the back peeks out behind it;
# hovering brings the back to the front.
_STACK_CSS = """
<style>
.pf-stack{position:relative;width:100%;padding:0 16% 12% 0;box-sizing:border-box}
.pf-stack figure{margin:0}
.pf-stack img{width:100%;display:block;border-radius:4.5%/3.2%;
  box-shadow:0 2px 8px rgba(0,0,0,.35)}
.pf-front{position:relative;z-index:2}
.pf-back{position:absolute;left:16%;bottom:0;width:84%;z-index:1}
.pf-stack:hover .pf-back{z-index:3}
.pf-tag{position:absolute;left:6px;bottom:6px;font-size:.7rem;line-height:1.4;
  padding:1px 7px;border-radius:8px;background:rgba(0,0,0,.7);color:#fff}
</style>
"""


class _Context(NamedTuple):
    """Settings a preview was resolved with (edits and renders must reuse them).

    A tuple on purpose: Streamlit re-executes this script (redefining the
    class) on every rerun, and tuples still compare by value across runs.
    """

    tcg: str
    local_dir: str | None
    output_dir: str


@dataclass(frozen=True)
class _PdfGroup:
    """Cards that end up in the same PDF file(s), as grouped by the exporter."""

    title: str
    foil: bool
    dual: bool

    def matches(self, item: ResolvedCard) -> bool:
        return (
            item.front_path is not None
            and item.card.foil == self.foil
            and (item.back_path is not None) == self.dual
        )

    def pdf_names(self, deck_name: str) -> list[str]:
        prefix = "foil_" if self.foil else ""
        if self.dual:
            return [f"{prefix}front_{deck_name}.pdf", f"{prefix}back_{deck_name}.pdf"]
        return [f"{prefix}{deck_name}.pdf"]


PDF_GROUPS = (
    _PdfGroup("🃏 Normales", foil=False, dual=False),
    _PdfGroup("✨ Foil", foil=True, dual=False),
    _PdfGroup("🔁 Front / Back", foil=False, dual=True),
    _PdfGroup("✨🔁 Foil Front / Back", foil=True, dual=True),
)


# --- Helpers ---------------------------------------------------------------


@st.cache_data(show_spinner=False, max_entries=2000)
def _thumbnail(path: str, mtime: float) -> bytes:
    """Small JPEG preview of a card image (``mtime`` busts the cache)."""
    with Image.open(path) as img:
        if img.mode in ("P", "LA") or "transparency" in img.info:
            img = img.convert("RGBA")
        img = img.convert("RGB")
        img.thumbnail(THUMBNAIL_SIZE)
        buffer = io.BytesIO()
        img.save(buffer, "JPEG", quality=85)
    return buffer.getvalue()


def _thumbnail_of(path: Path) -> bytes:
    return _thumbnail(str(path), path.stat().st_mtime)


def _data_uri(path: Path) -> str:
    return "data:image/jpeg;base64," + base64.b64encode(_thumbnail_of(path)).decode("ascii")


def _clean_deck_name(name: str) -> str:
    """Deck name used as output folder: no path separators or reserved chars."""
    return re.sub(r'[\\/:*?"<>|]+', "_", name).strip() or DEFAULT_DECK_NAME


def _get_strategy(ctx: _Context) -> TCGStrategy:
    """Reuse one strategy per context (keeps HTTP sessions and caches warm)."""
    key = (ctx.tcg, ctx.local_dir)
    if st.session_state.get("strategy_key") != key:
        st.session_state.strategy = _make_strategy(ctx.tcg, ctx.local_dir)
        st.session_state.strategy_key = key
    return st.session_state.strategy


def _get_exporter(ctx: _Context, dpi: int | None = None) -> Exporter:
    return Exporter(
        strategy=_get_strategy(ctx),
        output_base_dir=str(Path(ctx.output_dir) / ctx.tcg),
        target_dpi=dpi,
    )


def _edited_card(card: DeckCard, name: str, quantity: int, foil: bool) -> DeckCard:
    """Apply form edits to a card, validating the name through the parser.

    A renamed card drops its previous ``[art]`` (it belonged to another
    card) unless the new name carries its own marker.

    Raises:
        ValueError: If the name is not a valid decklist entry.
    """
    parsed = parse_deck_text(f"{quantity} {name.strip()}")[0]
    art = parsed.art if parsed.art or parsed.name != card.name else card.art
    return card.model_copy(
        update={"name": parsed.name, "art": art, "quantity": quantity, "foil": foil}
    )


def _art_options(ctx: _Context, card_name: str) -> list[ArtOption]:
    """Art options of a card, fetched once per session."""
    cache: dict[tuple[str, str], list[ArtOption]] = st.session_state.setdefault(
        "art_options", {}
    )
    key = (ctx.tcg, card_name)
    if key not in cache:
        cache[key] = _get_strategy(ctx).list_art_options(card_name)
    return cache[key]


def _replace_card(index: int, item: ResolvedCard) -> None:
    st.session_state.resolved[index] = item
    st.session_state.pdfs = []


def _zip_files(paths: list[Path]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as archive:
        for path in paths:
            archive.write(path, path.name)
    return buffer.getvalue()


# --- Page sections ---------------------------------------------------------


def _render_sidebar() -> tuple[_Context, int | None]:
    with st.sidebar:
        st.header("Ajustes")
        tcg = st.selectbox(
            "Juego",
            list(_STRATEGY_REGISTRY),
            format_func=lambda key: TCG_LABELS.get(key, key),
        )
        local_dir = None
        if tcg == "local":
            local_dir = st.text_input(
                "Carpeta de imágenes locales",
                LocalStrategy.DEFAULT_IMAGES_DIR,
                help="Los nombres del mazo deben coincidir con los ficheros.",
            )
        output_dir = st.text_input("Carpeta de salida", DEFAULT_OUTPUT_DIR)
        dpi = st.selectbox(
            "Resolución del PDF (DPI)",
            DPI_OPTIONS,
            format_func=lambda dpi: "Original (máxima calidad)" if dpi is None else f"{dpi} DPI",
            help=(
                "Original incrusta cada imagen descargada sin reescalar ni recomprimir; "
                "un DPI fijo genera PDFs más ligeros (300 para borradores)."
            ),
        )
    return _Context(tcg=tcg, local_dir=local_dir, output_dir=output_dir), dpi


def _render_deck_input(ctx: _Context) -> None:
    uploaded = st.file_uploader("Decklist (.txt)", type=["txt"])
    if uploaded is not None:
        text = uploaded.getvalue().decode("utf-8")
        default_name = Path(uploaded.name).stem
        with st.expander("Contenido del fichero"):
            st.code(text, language=None)
    else:
        text = st.text_area(
            "…o pega la decklist",
            height=200,
            placeholder="4 Lightning Bolt [m21]\n1 Delver of Secrets\n2 Island *F*",
        )
        default_name = DEFAULT_DECK_NAME
    deck_name = _clean_deck_name(st.text_input("Nombre del mazo", default_name))

    if not st.button("🔍 Buscar imágenes", type="primary", disabled=not text.strip()):
        return
    try:
        cards = parse_deck_text(text)
    except ValueError as exc:
        st.error(f"No se pudo leer la decklist: {exc}")
        return
    if not cards:
        st.warning("La decklist no contiene cartas.")
        return

    progress = st.progress(0.0, text="Buscando imágenes…")

    def on_progress(index: int, total: int, card: DeckCard) -> None:
        progress.progress(index / total, text=f"({index + 1}/{total}) {card.name}")

    try:
        resolved = _get_exporter(ctx).resolve_images(deck_name, cards, on_progress)
    except Exception as exc:  # noqa: BLE001 - surface any failure in the UI
        logger.exception("Image resolution failed")
        st.error(f"Error al buscar las imágenes: {exc}")
        return
    finally:
        progress.empty()
    st.session_state.update(resolved=resolved, deck_name=deck_name, context=ctx, pdfs=[])
    _close_art_dialog()


def _render_front_back(item: ResolvedCard, pair_number: int) -> None:
    """Front on top, back behind it (hover brings the back forward)."""
    assert item.front_path is not None and item.back_path is not None
    st.markdown(
        '<div class="pf-stack">'
        f'<figure class="pf-back"><img src="{_data_uri(item.back_path)}" alt="Reverso">'
        f'<figcaption class="pf-tag">Reverso #{pair_number}</figcaption></figure>'
        f'<figure class="pf-front"><img src="{_data_uri(item.front_path)}" alt="Anverso">'
        f'<figcaption class="pf-tag">Anverso #{pair_number}</figcaption></figure>'
        "</div>",
        unsafe_allow_html=True,
    )


def _render_card(
    index: int,
    item: ResolvedCard,
    ctx: _Context,
    deck_name: str,
    pair_number: int | None = None,
) -> None:
    card = item.card
    badges = [f"**{card.quantity}×**"]
    if card.art:
        badges.append(f"🎨 `{card.art}`")
    if card.foil:
        badges.append("✨ foil")

    if item.front_path is None:
        st.error(f"No encontrada: **{card.name}**")
    elif pair_number is not None:
        _render_front_back(item, pair_number)
    else:
        st.image(_thumbnail_of(item.front_path))
    st.markdown(f"{card.name}  \n" + " · ".join(badges))
    if card.back_name and item.front_path is not None and item.back_path is None:
        st.warning(f"Reverso no encontrado: {card.back_name}. Se imprimirá solo el anverso.")
    elif card.back_name:
        st.caption(f"Reverso: {card.back_name}")

    if item.front_path is not None and st.button(
        "🃏 Pasar a normales" if card.foil else "✨ Pasar a foil",
        key=f"move-{index}",
        width="stretch",
    ):
        _set_foil([index], not card.foil)

    strategy = _get_strategy(ctx)
    art_col, edit_col = st.columns(2)
    if strategy.supports_art and art_col.button("🎨 Arte", key=f"art-{index}"):
        st.session_state.art_dialog = index
    with edit_col.popover("✏️ Editar"):
        with st.form(f"edit-{index}"):
            name = st.text_input("Nombre", card.name)
            quantity = st.number_input("Copias", min_value=1, value=card.quantity, step=1)
            foil = st.checkbox("Foil", card.foil)
            if st.form_submit_button("Aplicar", type="primary"):
                _apply_edit(index, item, ctx, deck_name, name, int(quantity), foil)
        if st.button("🗑️ Quitar del mazo", key=f"remove-{index}"):
            st.session_state.resolved.pop(index)
            st.session_state.pdfs = []
            _close_art_dialog()
            st.rerun()


def _set_foil(indices: list[int], foil: bool) -> None:
    """Move cards between the regular and foil PDFs (images stay valid)."""
    resolved: list[ResolvedCard] = st.session_state.resolved
    for index in indices:
        item = resolved[index]
        _replace_card(index, item.model_copy(update={"card": item.card.model_copy(update={"foil": foil})}))
    target = "Foil" if foil else "Normales"
    names = resolved[indices[0]].card.name if len(indices) == 1 else f"{len(indices)} cartas"
    st.session_state.flash = f"{names} → {target}"
    st.rerun()


def _apply_edit(
    index: int,
    item: ResolvedCard,
    ctx: _Context,
    deck_name: str,
    name: str,
    quantity: int,
    foil: bool,
) -> None:
    try:
        card = _edited_card(item.card, name, quantity, foil)
    except ValueError as exc:
        st.error(str(exc))
        return
    unchanged_images = card.name == item.card.name and card.art == item.card.art
    if unchanged_images and item.front_path is not None:
        # Only quantity/finish changed: the images stay valid.
        _replace_card(index, item.model_copy(update={"card": card}))
    else:
        with st.spinner(f"Buscando {card.name}…"):
            _replace_card(index, _get_exporter(ctx).resolve_images(deck_name, [card])[0])
    st.rerun()


def _close_art_dialog() -> None:
    st.session_state.pop("art_dialog", None)


# Kept open through ``st.session_state.art_dialog`` (card index) rather than
# the button's one-shot value, so full-app reruns don't close it.
@st.dialog("🎨 Elegir arte", width="large", on_dismiss=_close_art_dialog)
def _art_dialog(index: int) -> None:
    ctx: _Context = st.session_state.context
    deck_name: str = st.session_state.deck_name
    item: ResolvedCard = st.session_state.resolved[index]
    card = item.card

    current_col, info_col = st.columns([1, 3])
    if item.front_path is not None:
        current_col.image(_thumbnail_of(item.front_path), caption="Arte actual")
    info_col.markdown(f"### {card.name}")
    info_col.markdown(
        f"Marcador actual: `{card.art}`"
        if card.art
        else "Sin marcador de arte: se usa la impresión del decklist o la predeterminada."
    )
    if card.art and info_col.button("↩️ Volver al arte por defecto"):
        _choose_art(index, item, ctx, deck_name, None)

    with st.spinner("Buscando artes disponibles…"):
        options = _art_options(ctx, card.name)
    if not options:
        st.info("No hay artes alternativos para esta carta.")
        return

    query = st.text_input("Filtrar", placeholder="Set, código, variante…").strip().lower()
    if query:
        options = [o for o in options if query in o.label.lower() or query in o.value]
    pages = max(1, -(-len(options) // ART_OPTIONS_PER_PAGE))
    page = 1
    if pages > 1:
        page = int(st.number_input(f"Página (de {pages})", 1, pages, 1))
    st.caption(f"{len(options)} artes")
    start = (page - 1) * ART_OPTIONS_PER_PAGE
    visible = options[start : start + ART_OPTIONS_PER_PAGE]
    for row_start in range(0, len(visible), ART_GRID_COLUMNS):
        columns = st.columns(ART_GRID_COLUMNS)
        for column, option in zip(columns, visible[row_start : row_start + ART_GRID_COLUMNS]):
            with column:
                st.image(option.image_url, caption=option.label)
                selected = option.value == card.art
                if st.button(
                    "✔️ Seleccionado" if selected else "Usar este arte",
                    key=f"use-art-{index}-{option.value}",
                    type="primary" if selected else "secondary",
                    disabled=selected,
                ):
                    _choose_art(index, item, ctx, deck_name, option.value)


def _choose_art(
    index: int, item: ResolvedCard, ctx: _Context, deck_name: str, art: str | None
) -> None:
    card = item.card.model_copy(update={"art": art})
    with st.spinner("Descargando el arte elegido…"):
        updated = _get_exporter(ctx).resolve_images(deck_name, [card])[0]
    if updated.front_path is None:
        st.error("No se pudo descargar ese arte; se mantiene el actual.")
        return
    _replace_card(index, updated)
    _close_art_dialog()
    st.rerun()


def _render_downloads(pdf_names: list[str]) -> None:
    generated = {p.name: p for p in st.session_state.get("pdfs", [])}
    columns = st.columns(max(1, len(pdf_names)))
    for column, name in zip(columns, pdf_names):
        if name in generated:
            column.download_button(
                f"⬇️ {name}",
                data=functools.partial(generated[name].read_bytes),
                file_name=name,
                mime="application/pdf",
                key=f"download-{name}",
                on_click="ignore",
            )
        else:
            column.caption(f"📄 {name} — pulsa «Generar PDFs»")


def _render_card_grid(
    entries: list[tuple[int, ResolvedCard]], ctx: _Context, deck_name: str, dual: bool
) -> None:
    for row_start in range(0, len(entries), GRID_COLUMNS):
        columns = st.columns(GRID_COLUMNS)
        row = entries[row_start : row_start + GRID_COLUMNS]
        for offset, (column, (index, item)) in enumerate(zip(columns, row)):
            pair_number = row_start + offset + 1 if dual else None
            with column, st.container(border=True, key=f"card-{index}"):
                _render_card(index, item, ctx, deck_name, pair_number)


def _render_preview(current: _Context, dpi: int | None) -> None:
    resolved: list[ResolvedCard] = st.session_state.resolved
    ctx: _Context = st.session_state.context
    deck_name: str = st.session_state.deck_name

    st.subheader(f"Vista previa · {deck_name}")
    if ctx != current:
        st.info(
            f"La vista previa se generó para **{TCG_LABELS.get(ctx.tcg, ctx.tcg)}** con "
            "otros ajustes. Pulsa «Buscar imágenes» para actualizarla."
        )

    found = [r for r in resolved if r.front_path is not None]
    failed = [(i, r) for i, r in enumerate(resolved) if r.front_path is None]
    metrics = st.columns(4)
    metrics[0].metric("Entradas", len(resolved))
    metrics[1].metric("Copias a imprimir", sum(r.card.quantity for r in found))
    metrics[2].metric("Dos caras", sum(1 for r in found if r.back_path is not None))
    metrics[3].metric("No encontradas", len(failed))

    _render_pdf_actions(dpi, has_cards=bool(found))

    groups = [
        (group, [(i, r) for i, r in enumerate(resolved) if group.matches(r)])
        for group in PDF_GROUPS
    ]
    groups = [(group, entries) for group, entries in groups if entries]
    labels = [group.title for group, _ in groups] + ([FAILED_TAB] if failed else [])
    if not labels:
        return
    tabs = st.tabs(labels, key="preview-tab", on_change="rerun")
    st.markdown(_STACK_CSS, unsafe_allow_html=True)

    for tab, (group, entries) in zip(tabs, groups):
        if tab.open is False:
            continue  # hidden tab: skip rendering its images
        with tab:
            copies = sum(item.card.quantity for _, item in entries)
            summary_col, move_col = st.columns([3, 1])
            summary_col.caption(f"{len(entries)} cartas · {copies} copias")
            target = "normales" if group.foil else "foil"
            if move_col.button(
                f"Pasar todas a {target}", key=f"move-all-{group.title}", width="stretch"
            ):
                _set_foil([index for index, _ in entries], not group.foil)
            if group.dual:
                st.caption(
                    "Cada anverso está delante y su reverso detrás, con el mismo número "
                    "de pareja. Pasa el ratón por encima para traer el reverso delante."
                )
            _render_downloads(group.pdf_names(deck_name))
            _render_card_grid(entries, ctx, deck_name, dual=group.dual)

    if failed and tabs[-1].open is not False:
        with tabs[-1]:
            st.caption("Estas cartas no se incluirán en ningún PDF. Corrige el nombre o reinténtalo.")
            if st.button("🔄 Reintentar todas"):
                exporter = _get_exporter(ctx)
                with st.spinner("Reintentando…"):
                    for index, item in failed:
                        resolved[index] = exporter.resolve_images(deck_name, [item.card])[0]
                st.session_state.pdfs = []
                st.rerun()
            _render_card_grid(failed, ctx, deck_name, dual=False)


def _render_pdf_actions(dpi: int | None, has_cards: bool) -> None:
    resolved: list[ResolvedCard] = st.session_state.resolved
    ctx: _Context = st.session_state.context
    deck_name: str = st.session_state.deck_name

    decklist = format_deck(item.card for item in resolved)
    generate_col, txt_col, zip_col = st.columns(3)
    if generate_col.button("🖨️ Generar PDFs", type="primary", disabled=not has_cards):
        try:
            quality = "calidad original" if dpi is None else f"{dpi} DPI"
            with st.spinner(f"Generando PDFs ({quality})…"):
                pdfs = _get_exporter(ctx, dpi).render_pdfs(deck_name, resolved)
            decklist_file = pdfs[0].parent / f"{deck_name}.txt"
            decklist_file.write_text(decklist, encoding="utf-8")
            st.session_state.pdfs = pdfs + [decklist_file]
        except Exception as exc:  # noqa: BLE001 - surface any failure in the UI
            logger.exception("PDF rendering failed")
            st.error(f"Error al generar el PDF: {exc}")

    txt_col.download_button(
        "📝 Descargar decklist (.txt)",
        data=decklist,
        file_name=f"{deck_name}.txt",
        mime="text/plain",
        key="download-decklist",
        on_click="ignore",
        help="El mazo con tus cambios (arte, foil, copias) para regenerarlo en la web o la CLI.",
    )
    generated: list[Path] = st.session_state.get("pdfs", [])
    if generated:
        zip_col.download_button(
            f"📦 Descargar todo ({len(generated)} ficheros, .zip)",
            data=functools.partial(_zip_files, list(generated)),
            file_name=f"{deck_name}.zip",
            mime="application/zip",
            key="download-all",
            on_click="ignore",
        )
        st.success(
            f"PDFs y decklist generados en `{generated[0].parent}`. "
            "Descárgalos desde cada pestaña o todos juntos."
        )


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    st.set_page_config(page_title="ProxyForge", page_icon="🃏", layout="wide")
    st.title("🃏 ProxyForge")
    st.caption("Revisa las cartas antes de generar los PDFs listos para imprimir.")

    if flash := st.session_state.pop("flash", None):
        st.toast(flash, icon="✅")

    ctx, dpi = _render_sidebar()
    _render_deck_input(ctx)
    if "resolved" in st.session_state:
        _render_preview(ctx, dpi)
        art_index = st.session_state.get("art_dialog")
        if art_index is not None and art_index < len(st.session_state.resolved):
            _art_dialog(art_index)


if __name__ == "__main__":
    main()
