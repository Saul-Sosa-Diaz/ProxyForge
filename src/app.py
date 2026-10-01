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
import difflib
import functools
import hashlib
import io
import logging
import os
import re
import sys
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

import requests
import streamlit as st
from PIL import Image

# Bootstrap the project root onto sys.path so the ``src`` package is importable
# when Streamlit runs this file as a script.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.exporter import PRINT_DPI, Exporter, estimate_print_dpi
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
# Card images below this resolution get a warning badge (Scryfall scans ~300).
GOOD_PRINT_DPI = 600
GRID_COLUMNS = 4
ART_GRID_COLUMNS = 4
# Height (px) of the scrollable art-picker grid.
ART_GRID_HEIGHT = 640
# Arts drawn per step: cards like basic lands have thousands of arts (2000+
# MPC images for Island), so the grid grows with "Mostrar más" instead of
# drawing (and downloading previews for) all of them at once.
ART_PICKER_STEP = 48
THUMBNAIL_SIZE = (360, 504)
# Height (px) of each side of the original / current decklist comparison.
DECKLIST_HEIGHT = 320
# Art-picker previews are fetched by the server (Google Drive rejects bursts
# of browser requests), in parallel, with retries, and cached in memory and
# on disk. Drive takes ~1 s to render each thumbnail whatever its size, so
# parallelism is what matters: 32 workers stayed free of HTTP 429 in tests.
ART_THUMBNAIL_SIZE = (240, 336)
ART_THUMBNAIL_WORKERS = 24
ART_THUMBNAIL_CACHE_DIR = Path(os.environ.get("PROXYFORGE_CACHE_DIR", "data")) / "thumbnails"
ART_THUMBNAIL_RETRIES = 3
ART_THUMBNAIL_TIMEOUT = 20
# Scryfall's image CDN rejects the default python-requests User-Agent (HTTP 400).
ART_THUMBNAIL_HEADERS = {"User-Agent": "tgc-card-image-downloader/1.0", "Accept": "image/*"}

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


@st.cache_resource
def _art_thumbnail_store() -> tuple[dict[str, bytes | None], threading.Lock]:
    """Process-wide cache of art-picker previews (``None`` = unavailable)."""
    return {}, threading.Lock()


def _cached_art_thumbnail(url: str) -> tuple[bool, bytes | None]:
    """``(found, preview)`` from the memory or disk cache, without fetching."""
    store, lock = _art_thumbnail_store()
    with lock:
        if url in store:
            return True, store[url]
    path = _art_thumbnail_path(url)
    if path is not None and path.exists():
        data = path.read_bytes()
        with lock:
            store[url] = data
        return True, data
    return False, None


def _load_art_thumbnail(url: str) -> bytes | None:
    """Fetch one preview and cache it (failures only in memory, to retry later)."""
    data = _fetch_art_thumbnail(url)
    store, lock = _art_thumbnail_store()
    with lock:
        store[url] = data
    path = _art_thumbnail_path(url)
    if data is not None and path is not None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        except OSError as exc:
            logger.debug("Could not cache preview %s: %s", url, exc)
    return data


def _art_thumbnail_path(url: str) -> Path | None:
    """Disk cache file of a remote preview (local files are not cached)."""
    if not url.startswith(("http://", "https://")):
        return None
    return ART_THUMBNAIL_CACHE_DIR / f"{hashlib.sha1(url.encode()).hexdigest()}.jpg"


def _show_art_preview(slot, preview: bytes | None) -> None:
    if preview is not None:
        slot.image(preview)
    else:
        slot.caption("🖼️ Vista previa no disponible")


def _stream_art_thumbnails(pending: dict[str, list]) -> None:
    """Fill each placeholder as soon as its preview arrives (grid order first).

    A click on the grid reruns the script mid-way: the pool is then shut
    down without waiting, and downloads already running still land in the
    cache for the next run.
    """
    pool = ThreadPoolExecutor(ART_THUMBNAIL_WORKERS)
    try:
        futures = {pool.submit(_load_art_thumbnail, url): slots for url, slots in pending.items()}
        for future in as_completed(futures):
            for slot in futures[future]:
                _show_art_preview(slot, future.result())
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def _fetch_art_thumbnail(url: str) -> bytes | None:
    """Download (or read) one preview and shrink it to a small JPEG."""
    if url.startswith(("http://", "https://")):
        data = None
        for attempt in range(ART_THUMBNAIL_RETRIES):
            try:
                resp = requests.get(
                    url, timeout=ART_THUMBNAIL_TIMEOUT, headers=ART_THUMBNAIL_HEADERS
                )
            except requests.RequestException as exc:
                logger.debug("Preview %s failed: %s", url, exc)
            else:
                if resp.status_code == 200:
                    data = resp.content
                    break
                logger.debug("Preview %s returned HTTP %s", url, resp.status_code)
                if resp.status_code != 429 and resp.status_code < 500:
                    break  # permanent (e.g. 400/404): retrying will not help
            time.sleep(0.5 * 2**attempt)  # back off: Drive answers bursts with 429
        if data is None:
            return None
        source: io.BytesIO | str = io.BytesIO(data)
    else:
        source = url
    try:
        with Image.open(source) as img:
            img = img.convert("RGB")
            img.thumbnail(ART_THUMBNAIL_SIZE)
            buffer = io.BytesIO()
            img.save(buffer, "JPEG", quality=85)
    except OSError:
        return None
    return buffer.getvalue()


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
    st.session_state.update(
        resolved=resolved, deck_name=deck_name, context=ctx, pdfs=[], deck_text=text
    )
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
    if item.front_path is not None:
        dpi = estimate_print_dpi(item.front_path)
        if dpi is not None:
            icon = "🖼️" if dpi >= GOOD_PRINT_DPI else "⚠️"
            badges.append(f"{icon} {dpi} DPI")

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
    elif card.front_only:
        st.caption("Solo anverso (`/ -`)")

    if item.front_path is not None and st.button(
        "🃏 Pasar a normales" if card.foil else "✨ Pasar a foil",
        key=f"move-{index}",
        width="stretch",
    ):
        _set_foil([index], not card.foil)
    if item.back_path is not None and st.button(
        "📄 Solo anverso",
        key=f"front-only-{index}",
        width="stretch",
        help="Quita el reverso y la pasa a las cartas de una cara (p. ej. si la imagen "
        "de MPC ya junta las dos caras).",
    ):
        _drop_back(index)
    elif card.front_only and item.front_path is not None and st.button(
        "🔁 Pasar a Front / Back",
        key=f"front-back-{index}",
        width="stretch",
        help="Vuelve a ponerle su reverso.",
    ):
        _restore_back(index, ctx, deck_name)

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


def _drop_back(index: int) -> None:
    """Move a double-sided card to the single-sided PDFs (front only).

    The removed back is remembered so :func:`_restore_back` puts the very
    same face back.
    """
    item: ResolvedCard = st.session_state.resolved[index]
    card = item.card
    removed: dict[tuple[str, str | None], tuple] = st.session_state.setdefault("removed_backs", {})
    removed[(card.name, card.art)] = (card.back_name, card.back_art, card.back_foil, item.back_path)
    front_only = card.model_copy(
        update={"back_name": None, "back_art": None, "back_foil": False, "front_only": True}
    )
    _replace_card(index, item.model_copy(update={"card": front_only, "back_path": None}))
    st.session_state.flash = f"{card.name} → solo anverso"
    st.rerun()


def _restore_back(index: int, ctx: _Context, deck_name: str) -> None:
    """Move a front-only card back to the front/back PDFs with its back face.

    Reuses the back removed in this session; otherwise the card is resolved
    again so the strategy adds its back (e.g. MTG double-faced cards).
    """
    item: ResolvedCard = st.session_state.resolved[index]
    card = item.card.model_copy(update={"front_only": False})
    removed = st.session_state.get("removed_backs", {}).pop((card.name, card.art), None)
    if removed is not None and removed[3].exists():
        back_name, back_art, back_foil, back_path = removed
        card = card.model_copy(
            update={"back_name": back_name, "back_art": back_art, "back_foil": back_foil}
        )
        updated = item.model_copy(update={"card": card, "back_path": back_path})
    else:
        with st.spinner(f"Buscando el reverso de {card.name}…"):
            updated = _get_exporter(ctx).resolve_images(deck_name, [card])[0]
    if updated.front_path is None or updated.back_path is None:
        st.session_state.flash_warning = f"No se encontró un reverso para {card.name}; sigue con solo anverso."
        st.rerun()
    _replace_card(index, updated)
    st.session_state.flash = f"{card.name} → Front / Back"
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
    if info_col.button(
        "🔄 Recargar artes",
        help="Vuelve a consultar las fuentes (p. ej. si MPC Autofill no respondió).",
    ):
        st.session_state.get("art_options", {}).pop((ctx.tcg, card.name), None)

    with st.spinner("Buscando artes disponibles…"):
        options = _art_options(ctx, card.name)
    if not options:
        st.info("No hay artes alternativos para esta carta.")
        return

    total = len(options)
    query = st.text_input(
        "Filtrar",
        placeholder="Fuente, DPI, set, variante, etiqueta… (p. ej. «tmt», «mpc 1200», «full-art»)",
    )
    options = _filter_art_options(options, query)
    # How many arts are drawn; restarts when the filter changes.
    limit_key = f"art-limit-{index}"
    if st.session_state.get(f"{limit_key}-query") != query:
        st.session_state[f"{limit_key}-query"] = query
        st.session_state[limit_key] = ART_PICKER_STEP
    limit = st.session_state.get(limit_key, ART_PICKER_STEP)
    visible = options[:limit]
    found_text = f"{len(options)} de {total} artes" if query.strip() else f"{total} artes"
    st.caption(f"{found_text} · mostrando {len(visible)}")
    if not options:
        st.info(
            "Ningún arte coincide. Prueba con el código del set (p. ej. «tmt»), su nombre "
            "o sus iniciales («tmnt»), la fuente o el DPI."
        )
    # The grid is drawn at once with a placeholder per preview; missing
    # previews are then streamed into their placeholders as they arrive.
    pending: dict[str, list] = {}
    grid = st.container(height=ART_GRID_HEIGHT, border=False)
    for row_start in range(0, len(visible), ART_GRID_COLUMNS):
        columns = grid.columns(ART_GRID_COLUMNS)
        for column, option in zip(columns, visible[row_start : row_start + ART_GRID_COLUMNS]):
            with column:
                slot = st.empty()
                found, preview = _cached_art_thumbnail(option.image_url)
                if found:
                    _show_art_preview(slot, preview)
                else:
                    slot.caption("⏳ Cargando vista previa…")
                    pending.setdefault(option.image_url, []).append(slot)
                st.caption(option.label)
                selected = option.value == card.art
                if st.button(
                    "✔️ Seleccionado" if selected else "Usar este arte",
                    key=f"use-art-{index}-{option.value}",
                    type="primary" if selected else "secondary",
                    disabled=selected,
                ):
                    _choose_art(index, item, ctx, deck_name, option.value)
    remaining = len(options) - len(visible)
    if remaining:
        # on_click (no st.rerun): only the dialog reruns, so it stays open
        # and keeps its scroll position while the next arts are appended.
        grid.button(
            f"⬇️ Mostrar {min(remaining, ART_PICKER_STEP)} más (quedan {remaining})",
            key=f"more-art-{index}",
            width="stretch",
            on_click=_show_more_arts,
            args=(limit_key,),
        )
    if pending:
        _stream_art_thumbnails(pending)


def _show_more_arts(limit_key: str) -> None:
    st.session_state[limit_key] = st.session_state.get(limit_key, ART_PICKER_STEP) + ART_PICKER_STEP


def _filter_art_options(options: list[ArtOption], query: str) -> list[ArtOption]:
    """Arts whose label, marker or keywords contain every word of ``query``."""
    words = query.lower().split()
    if not words:
        return options
    return [
        option
        for option in options
        if all(
            word in f"{option.label} {option.value} {option.keywords}".lower() for word in words
        )
    ]


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
    _render_decklist(resolved, ctx, deck_name)

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

    generate_col, zip_col = st.columns(2)
    if generate_col.button("🖨️ Generar PDFs", type="primary", disabled=not has_cards):
        try:
            quality = "calidad original" if dpi is None else f"{dpi} DPI"
            with st.spinner(f"Generando PDFs ({quality})…"):
                pdfs = _get_exporter(ctx, dpi).render_pdfs(deck_name, resolved)
            st.session_state.pdfs = pdfs + [_save_decklist(resolved, ctx, deck_name)]
        except Exception as exc:  # noqa: BLE001 - surface any failure in the UI
            logger.exception("PDF rendering failed")
            st.error(f"Error al generar el PDF: {exc}")

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


def _save_decklist(resolved: list[ResolvedCard], ctx: _Context, deck_name: str) -> Path:
    """Write ``<output>/<tcg>/<deck>/<deck>.txt`` when its contents changed."""
    path = Path(ctx.output_dir) / ctx.tcg / deck_name / f"{deck_name}.txt"
    text = format_deck(item.card for item in resolved)
    try:
        unchanged = path.read_text(encoding="utf-8") == text
    except OSError:
        unchanged = False
    if not unchanged:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return path


def _changed_lines(original: str, current: str) -> int:
    """Entries added or modified in ``current`` (formatting-only edits ignored)."""
    try:
        before = format_deck(parse_deck_text(original)).splitlines()
    except ValueError:
        before = original.splitlines()
    diff = difflib.ndiff(before, current.splitlines())
    return sum(1 for line in diff if line.startswith("+ "))


def _render_decklist(resolved: list[ResolvedCard], ctx: _Context, deck_name: str) -> None:
    """Original decklist next to the live one, saved to disk on every change.

    The right side is rebuilt from the current cards on each rerun (art
    choices, foil moves, copies, edits), so it always matches what the
    PDFs will contain and can be fed back to the web UI or the CLI.
    """
    original: str = st.session_state.get("deck_text", "")
    current = format_deck(item.card for item in resolved)
    try:
        path = _save_decklist(resolved, ctx, deck_name)
    except OSError as exc:
        logger.warning("Could not save the decklist: %s", exc)
        path = None
    changed = _changed_lines(original, current)

    with st.expander("📝 Decklist · original ↔ con tus cambios", expanded=True):
        left, right = st.columns(2)
        with left:
            st.caption("Original")
            st.code(original.strip() or "(vacía)", language=None, height=DECKLIST_HEIGHT)
        with right:
            summary = (
                "sin cambios" if not changed
                else "1 línea cambiada" if changed == 1
                else f"{changed} líneas cambiadas"
            )
            st.caption(f"Con tus cambios · {summary}")
            st.code(current.strip(), language=None, height=DECKLIST_HEIGHT)
        if path is not None:
            st.caption(f"Se guarda automáticamente en `{path}`.")


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
    if flash_warning := st.session_state.pop("flash_warning", None):
        st.toast(flash_warning, icon="⚠️")

    ctx, dpi = _render_sidebar()
    _render_deck_input(ctx)
    if "resolved" in st.session_state:
        _render_preview(ctx, dpi)
        art_index = st.session_state.get("art_dialog")
        if art_index is not None and art_index < len(st.session_state.resolved):
            _art_dialog(art_index)


if __name__ == "__main__":
    main()
