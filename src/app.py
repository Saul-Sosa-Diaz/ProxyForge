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
from typing import Callable, NamedTuple
from urllib.parse import unquote, urlparse

import requests
import streamlit as st
from PIL import Image, ImageDraw, ImageFont

# Bootstrap the project root onto sys.path so the ``src`` package is importable
# when Streamlit runs this file as a script.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.exporter import PRINT_DPI, Exporter, estimate_print_dpi
from src.main import _STRATEGY_REGISTRY, _make_strategy
from src.models import ArtOption, DeckCard, ResolvedCard
from src.parser import FILE_ART_PREFIX, format_deck, is_file_art, parse_deck_text
from src.pdf import MAX_SHADOW_LIFT, ToneAdjustment
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
# Own images ('[file:...]' arts) kept as uploaded; anything else becomes PNG.
CUSTOM_IMAGE_EXTENSIONS = {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp"}
CUSTOM_IMAGE_TYPES = ["png", "jpg", "jpeg", "webp", "gif", "bmp", "tif", "tiff"]
# Sheet splitter: largest grid offered and width (px) of its preview.
SHEET_MAX_CELLS_PER_SIDE = 20
SHEET_PREVIEW_WIDTH = 900

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


def _get_strategy(ctx: _Context, tcg: str | None = None) -> TCGStrategy:
    """Reuse one strategy per game (keeps HTTP sessions and caches warm).

    ``tcg`` defaults to the deck's game; other games (cards of a mixed deck)
    are kept in ``st.session_state.extra_strategies``.
    """
    tcg = tcg or ctx.tcg
    local_dir = ctx.local_dir if tcg == "local" else None
    if tcg != ctx.tcg:
        strategies: dict[tuple[str, str | None], TCGStrategy] = st.session_state.setdefault(
            "extra_strategies", {}
        )
        if (tcg, local_dir) not in strategies:
            strategies[(tcg, local_dir)] = _make_strategy(tcg, local_dir)
        return strategies[(tcg, local_dir)]
    key = (tcg, local_dir)
    if st.session_state.get("strategy_key") != key:
        st.session_state.strategy = _make_strategy(tcg, local_dir)
        st.session_state.strategy_key = key
    return st.session_state.strategy


def _get_exporter(
    ctx: _Context, dpi: int | None = None, foil_tone: ToneAdjustment | None = None
) -> Exporter:
    return Exporter(
        strategy=_get_strategy(ctx),
        output_base_dir=str(Path(ctx.output_dir) / ctx.tcg),
        target_dpi=dpi,
        foil_tone=foil_tone,
        strategy_factory=lambda tcg: _get_strategy(ctx, tcg),
    )


def _card_game(card: DeckCard, ctx: _Context) -> str:
    """Game whose strategy fetches ``card`` (its section, else the deck's)."""
    return card.tcg or ctx.tcg


def _own_game(tcg: str, ctx: _Context) -> str | None:
    """``DeckCard.tcg`` for a card of game ``tcg`` (``None`` = the deck's)."""
    return None if tcg == ctx.tcg else tcg


def _edited_card(
    card: DeckCard, name: str, quantity: int, foil: bool, tcg: str | None
) -> DeckCard:
    """Apply form edits to a card, validating the name through the parser.

    A renamed card drops its previous ``[art]`` (it belonged to another
    card) unless the new name carries its own marker; moving the card to
    another game (``tcg``) drops the source-specific arts of both faces
    (own ``[file:...]`` images stay).

    Raises:
        ValueError: If the name is not a valid decklist entry.
    """
    parsed = parse_deck_text(f"{quantity} {name.strip()}")[0]
    art = parsed.art if parsed.art or parsed.name != card.name else card.art
    update = {"name": parsed.name, "art": art, "quantity": quantity, "foil": foil}
    if tcg != card.tcg:
        update["tcg"] = tcg
        if not parsed.art and not is_file_art(art):
            update["art"] = None
        if not is_file_art(card.back_art):
            update["back_art"] = None
    return card.model_copy(update=update)


def _art_options(ctx: _Context, card: DeckCard) -> list[ArtOption]:
    """Art options of a card, fetched once per session."""
    cache: dict[tuple[str, str], list[ArtOption]] = st.session_state.setdefault(
        "art_options", {}
    )
    game = _card_game(card, ctx)
    key = (game, card.name)
    if key not in cache:
        cache[key] = _get_strategy(ctx, game).list_art_options(card.name)
    return cache[key]


def _save_custom_image(data: bytes, card_name: str, local_dir: str | None) -> str:
    """Store an own card image in the local images folder.

    PNG, JPEG and WebP files are kept byte for byte (original quality);
    other formats are converted to PNG. The file name carries a content
    hash, so a new image never overwrites an earlier one.

    Returns:
        The file name, to use as a ``[file:<name>]`` art marker.

    Raises:
        ValueError: If ``data`` is not an image.
    """
    try:
        with Image.open(io.BytesIO(data)) as img:
            img.load()
            ext = CUSTOM_IMAGE_EXTENSIONS.get(img.format or "")
            if ext is None:
                buffer = io.BytesIO()
                img.convert("RGBA" if "A" in img.getbands() else "RGB").save(buffer, "PNG")
                data, ext = buffer.getvalue(), ".png"
    except (OSError, ValueError) as exc:
        raise ValueError("El fichero no es una imagen válida.") from exc
    stem = re.sub(r"[^\w\s()-]+", "", card_name).strip()[:60] or "carta"
    file_name = f"{stem}-{hashlib.sha1(data).hexdigest()[:10]}{ext}"
    folder = Path(local_dir or LocalStrategy.DEFAULT_IMAGES_DIR)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / file_name).write_bytes(data)
    return file_name


def _download_custom_image(url: str) -> bytes:
    """Download an own card image from a URL.

    Raises:
        ValueError: If the URL is not http(s) or the download fails.
    """
    if not url.startswith(("http://", "https://")):
        raise ValueError("La URL debe empezar por http:// o https://.")
    try:
        resp = requests.get(url, timeout=ART_THUMBNAIL_TIMEOUT, headers=ART_THUMBNAIL_HEADERS)
    except requests.RequestException as exc:
        raise ValueError(f"No se pudo descargar la imagen: {exc}") from exc
    if resp.status_code != 200:
        raise ValueError(f"No se pudo descargar la imagen (HTTP {resp.status_code}).")
    return resp.content


def _custom_image_data(uploaded, url: str) -> bytes:
    """Bytes of an uploaded image, or else of the image at ``url``."""
    if uploaded is not None:
        return uploaded.getvalue()
    return _download_custom_image(url.strip())


def _custom_image_sources(uploaded: list, urls: str) -> list[tuple[str, Callable[[], bytes]]]:
    """Own images to add: uploaded files first, then one URL per line.

    Returns:
        ``(default card name, loader of the image bytes)`` pairs; the loader
        raises ``ValueError`` if a URL cannot be downloaded.
    """
    sources: list[tuple[str, Callable[[], bytes]]] = [
        (Path(file.name).stem, file.getvalue) for file in uploaded
    ]
    for url in filter(None, (line.strip() for line in urls.splitlines())):
        stem = Path(unquote(urlparse(url).path)).stem or "Carta propia"
        sources.append((stem, functools.partial(_download_custom_image, url)))
    return sources


def _custom_card(
    data: bytes, card_name: str, quantity: int, foil: bool, ctx: _Context
) -> DeckCard:
    """Deck line for an own image, saved in the local images folder.

    Raises:
        ValueError: If the name is not a valid card name or ``data`` is not an image.
    """
    # Validates the name ('/' would split it into two faces).
    parsed = parse_deck_text(f"{quantity} {card_name}")[0]
    file_name = _save_custom_image(data, parsed.name, ctx.local_dir)
    # 'local': no strategy should look the name up (e.g. an MTG back).
    return parsed.model_copy(
        update={
            "art": f"{FILE_ART_PREFIX}{file_name}",
            "foil": foil,
            "back_name": None,
            "front_only": False,
            "tcg": _own_game("local", ctx),
        }
    )


def _open_sheet(data: bytes) -> Image.Image:
    """Decode a sheet of cards as RGB(A).

    Raises:
        ValueError: If ``data`` is not an image.
    """
    try:
        with Image.open(io.BytesIO(data)) as img:
            img.load()
            alpha = "A" in img.getbands() or "transparency" in img.info
            return img.convert("RGBA" if alpha else "RGB")
    except (OSError, ValueError) as exc:
        raise ValueError("El fichero no es una imagen válida.") from exc


def _sheet_cells(
    size: tuple[int, int], rows: int, cols: int, margin: int = 0, gap: int = 0
) -> list[tuple[int, int, int, int]]:
    """Boxes of a ``rows`` x ``cols`` grid, left to right and top to bottom.

    Args:
        size: Sheet ``(width, height)`` in pixels.
        rows: Number of rows.
        cols: Number of columns.
        margin: Outer border (px) cut away on every side.
        gap: Space (px) between neighbouring cards, cut away too.

    Returns:
        ``(left, top, right, bottom)`` boxes in pixels.

    Raises:
        ValueError: If the margin and gaps leave no room for the cards.
    """
    width, height = size
    cell_w = (width - 2 * margin - (cols - 1) * gap) / cols
    cell_h = (height - 2 * margin - (rows - 1) * gap) / rows
    if cell_w < 1 or cell_h < 1:
        raise ValueError("El margen y la separación no dejan sitio para las cartas.")
    boxes = []
    for row in range(rows):
        top = margin + row * (cell_h + gap)
        for col in range(cols):
            left = margin + col * (cell_w + gap)
            boxes.append((round(left), round(top), round(left + cell_w), round(top + cell_h)))
    return boxes


def _split_sheet(
    data: bytes, rows: int, cols: int, margin: int = 0, gap: int = 0
) -> list[bytes]:
    """Cut a sheet of cards into one PNG (lossless) per grid cell.

    Returns:
        The card images, left to right and top to bottom.

    Raises:
        ValueError: If ``data`` is not an image or the grid does not fit.
    """
    sheet = _open_sheet(data)
    cells = []
    for box in _sheet_cells(sheet.size, rows, cols, margin, gap):
        buffer = io.BytesIO()
        sheet.crop(box).save(buffer, "PNG")
        cells.append(buffer.getvalue())
    return cells


@st.cache_data(show_spinner=False, max_entries=16)
def _sheet_preview(
    data: bytes, rows: int, cols: int, margin: int, gap: int, skipped: tuple[int, ...]
) -> bytes:
    """Downscaled sheet with the cut grid drawn on it and each cell numbered.

    Discarded cells (1-based numbers in ``skipped``) are greyed out.
    """
    sheet = _open_sheet(data).convert("RGB")
    boxes = _sheet_cells(sheet.size, rows, cols, margin, gap)
    scale = min(1.0, SHEET_PREVIEW_WIDTH / sheet.width)
    sheet = sheet.resize((max(1, round(sheet.width * scale)), max(1, round(sheet.height * scale))))
    overlay = Image.new("RGBA", sheet.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    cell_px = min(boxes[0][2] - boxes[0][0], boxes[0][3] - boxes[0][1]) * scale
    font = ImageFont.load_default(size=max(12, min(32, round(cell_px / 5))))
    for number, box in enumerate(boxes, start=1):
        left, top, right, bottom = (round(v * scale) for v in box)
        if number in skipped:
            draw.rectangle((left, top, right, bottom), fill=(40, 40, 40, 170))
        draw.rectangle((left, top, right, bottom), outline=(255, 64, 64, 255), width=2)
        label = str(number)
        text_box = draw.textbbox((left + 6, top + 4), label, font=font)
        draw.rectangle(
            (text_box[0] - 4, text_box[1] - 2, text_box[2] + 4, text_box[3] + 2),
            fill=(0, 0, 0, 190),
        )
        draw.text((left + 6, top + 4), label, fill=(255, 255, 255, 255), font=font)
    buffer = io.BytesIO()
    Image.alpha_composite(sheet.convert("RGBA"), overlay).convert("RGB").save(
        buffer, "JPEG", quality=85
    )
    return buffer.getvalue()


@st.cache_data(show_spinner=False, max_entries=4, ttl=3600)
def _download_sheet(url: str) -> bytes:
    """Sheet image at ``url``, cached so the live preview downloads it once."""
    return _download_custom_image(url)


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


def _render_sidebar() -> tuple[_Context, int | None, ToneAdjustment | None]:
    with st.sidebar:
        st.header("Ajustes")
        tcg = st.selectbox(
            "Juego",
            list(_STRATEGY_REGISTRY),
            format_func=lambda key: TCG_LABELS.get(key, key),
        )
        local_dir = st.text_input(
            "Carpeta de imágenes locales",
            LocalStrategy.DEFAULT_IMAGES_DIR,
            help=(
                "Para las cartas de «Imágenes locales» (los nombres del mazo deben "
                "coincidir con los ficheros) y las imágenes propias que subas, en "
                "cualquier juego."
            ),
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
        foil_tone = _render_foil_tone()
    return _Context(tcg=tcg, local_dir=local_dir, output_dir=output_dir), dpi, foil_tone


def _render_foil_tone() -> ToneAdjustment | None:
    """Sidebar controls of the foil lightening (``None`` = images as downloaded)."""
    enabled = st.checkbox(
        "✨ Aclarar foils",
        help=(
            "Solo en los PDFs foil: la tinta sobre foil sale más oscura y tapa el brillo, "
            "así que la imagen se «quema» a propósito (en pantalla se verá lavada)."
        ),
    )
    if not enabled:
        return None
    defaults = ToneAdjustment()
    exposure = st.slider(
        "Exposición / brillo (+%)", 0, 30, round(defaults.exposure * 100),
        help="Sube la exposición general; se recomienda entre +10 % y +20 %.",
    )
    shadows = st.slider(
        "Levantar sombras (%)", 0, round(MAX_SHADOW_LIFT * 100), round(defaults.shadows * 100),
        help=(
            "Como en Niveles/Curvas: el negro pasa a este gris y los tonos oscuros suben, "
            "para que grises oscuros y marrones no se conviertan en negro sólido."
        ),
    )
    opacity = st.slider(
        "Opacidad (%)", 80, 100, round(defaults.opacity * 100),
        help="Opacidad de la imagen sobre el blanco; entre 90 % y 95 % aclara los tonos generales.",
    )
    return ToneAdjustment(exposure=exposure / 100, shadows=shadows / 100, opacity=opacity / 100)


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
            placeholder=(
                "4 Lightning Bolt [m21]\n1 Delver of Secrets\n2 Island *F*\n"
                "[lorcana]\n1 Hades - King of Olympus [enchanted]\n[local]\n3 mi_token.png"
            ),
            help=(
                "Las líneas sin sección usan el juego elegido en Ajustes. Una línea "
                "«[mtg]», «[lorcana]», «[pokemon]» o «[local]» cambia el juego de las "
                "cartas que vienen debajo, para mezclar juegos en los mismos PDFs."
            ),
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
    if not _check_games(cards):
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


def _check_games(cards: list[DeckCard]) -> bool:
    """Show an error (and return ``False``) for ``[section]`` games that do not exist."""
    unknown = sorted({card.tcg for card in cards if card.tcg} - set(_STRATEGY_REGISTRY))
    if unknown:
        st.error(
            "Sección de juego desconocida: "
            + ", ".join(f"`[{tcg}]`" for tcg in unknown)
            + ". Usa "
            + ", ".join(f"`[{tcg}]`" for tcg in _STRATEGY_REGISTRY)
            + "."
        )
    return not unknown


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
    game = _card_game(card, ctx)
    badges = [f"**{card.quantity}×**"]
    if game != ctx.tcg:
        badges.append(f"🎲 {TCG_LABELS.get(game, game)}")
    if is_file_art(card.art):
        badges.append("📤 imagen propia")
    elif card.art:
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

    art_col, edit_col = st.columns(2)
    # Every card can take an own image; the picker also lists the source's arts.
    if art_col.button("🎨 Arte", key=f"art-{index}"):
        st.session_state.art_dialog = index
    with edit_col.popover("✏️ Editar"):
        with st.form(f"edit-{index}"):
            name = st.text_input("Nombre", card.name)
            quantity = st.number_input("Copias", min_value=1, value=card.quantity, step=1)
            foil = st.checkbox("Foil", card.foil)
            new_game = _game_selectbox("Juego", game, key=f"edit-game-{index}")
            if st.form_submit_button("Aplicar", type="primary"):
                tcg = card.tcg if new_game == game else _own_game(new_game, ctx)
                _apply_edit(index, item, ctx, deck_name, name, int(quantity), foil, tcg)
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
    tcg: str | None,
) -> None:
    try:
        card = _edited_card(item.card, name, quantity, foil, tcg)
    except ValueError as exc:
        st.error(str(exc))
        return
    unchanged_images = (card.name, card.art, card.tcg) == (item.card.name, item.card.art, item.card.tcg)
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
    game = _card_game(card, ctx)
    supports_art = _get_strategy(ctx, game).supports_art
    if supports_art and info_col.button(
        "🔄 Recargar artes",
        help="Vuelve a consultar las fuentes (p. ej. si MPC Autofill no respondió).",
    ):
        st.session_state.get("art_options", {}).pop((game, card.name), None)

    options: list[ArtOption] = []
    if supports_art:
        with st.spinner("Buscando artes disponibles…"):
            options = _art_options(ctx, card)
    _render_custom_art(index, item, ctx, deck_name, expanded=not options)
    if not options:
        if supports_art:
            st.info("No hay artes alternativos para esta carta: usa una imagen propia.")
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


def _render_custom_art(
    index: int, item: ResolvedCard, ctx: _Context, deck_name: str, expanded: bool
) -> None:
    """Own image for one face: an uploaded file or a URL, for any game."""
    card = item.card
    with st.expander("📤 Usar una imagen propia", expanded=expanded):
        st.caption(
            "Sube una imagen o pega su URL. Se guarda en la carpeta de imágenes locales "
            "y la decklist la recuerda como `[file:…]`, en cualquier juego."
        )
        back = False
        if card.back_name:
            face = st.radio(
                "Cara", ("Anverso", "Reverso"), horizontal=True, key=f"custom-face-{index}"
            )
            back = face == "Reverso"
        uploaded = st.file_uploader("Imagen", type=CUSTOM_IMAGE_TYPES, key=f"custom-file-{index}")
        url = st.text_input("…o URL de la imagen", key=f"custom-url-{index}")
        if st.button(
            "Usar esta imagen",
            key=f"custom-use-{index}",
            type="primary",
            disabled=uploaded is None and not url.strip(),
        ):
            try:
                file_name = _save_custom_image(
                    _custom_image_data(uploaded, url),
                    card.back_name if back else card.name,
                    ctx.local_dir,
                )
            except ValueError as exc:
                st.error(str(exc))
                return
            _choose_art(index, item, ctx, deck_name, f"{FILE_ART_PREFIX}{file_name}", back=back)


def _choose_art(
    index: int,
    item: ResolvedCard,
    ctx: _Context,
    deck_name: str,
    art: str | None,
    back: bool = False,
) -> None:
    card = item.card.model_copy(update={"back_art" if back else "art": art})
    with st.spinner("Descargando el arte elegido…"):
        updated = _get_exporter(ctx).resolve_images(deck_name, [card])[0]
    if updated.front_path is None or (back and updated.back_path is None):
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


def _render_preview(
    current: _Context, dpi: int | None, foil_tone: ToneAdjustment | None = None
) -> None:
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

    _render_pdf_actions(dpi, foil_tone, has_cards=bool(found))
    _render_decklist(resolved, ctx, deck_name)
    _render_add_cards(ctx, deck_name)

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


def _game_selectbox(label: str, value: str, key: str) -> str:
    games = list(_STRATEGY_REGISTRY)
    return st.selectbox(
        label,
        games,
        index=games.index(value) if value in games else 0,
        format_func=lambda game: TCG_LABELS.get(game, game),
        key=key,
    )


def _render_add_cards(ctx: _Context, deck_name: str) -> None:
    """Append cards of any game (or with an own image) to the current deck."""
    with st.expander("➕ Añadir cartas"):
        by_name, by_image = st.columns(2)
        with by_name, st.form("add-cards", clear_on_submit=True):
            st.markdown("**Por nombre, de cualquier juego**")
            game = _game_selectbox("Juego", ctx.tcg, key="add-game")
            text = st.text_area(
                "Líneas de decklist",
                placeholder="1 Lightning Bolt [m21]\n2 Hades - King of Olympus [enchanted] *F*",
                help="Mismo formato que la decklist; también admite secciones «[juego]».",
            )
            if st.form_submit_button("➕ Añadir", type="primary"):
                try:
                    cards = parse_deck_text(text, default_tcg=_own_game(game, ctx))
                except ValueError as exc:
                    st.error(f"No se pudo leer: {exc}")
                    return
                if cards and _check_games(cards):
                    _add_cards(ctx, deck_name, cards)
        with by_image, st.form("add-custom-card", clear_on_submit=True):
            st.markdown("**Con imágenes propias**")
            uploaded = st.file_uploader(
                "Imágenes", type=CUSTOM_IMAGE_TYPES, accept_multiple_files=True
            )
            urls = st.text_area("…o URLs de las imágenes (una por línea)")
            name = st.text_input(
                "Nombre", placeholder="Por defecto, el del fichero", help="Solo con una imagen."
            )
            quantity = st.number_input("Copias de cada una", min_value=1, value=1, step=1)
            foil = st.checkbox("Foil")
            if st.form_submit_button("➕ Añadir", type="primary"):
                sources = _custom_image_sources(uploaded or [], urls)
                if not sources:
                    st.error("Sube una o más imágenes o pega sus URLs.")
                    return
                if name.strip() and len(sources) > 1:
                    st.error("El nombre solo se puede fijar al añadir una única imagen.")
                    return
                cards, skipped = [], []
                for default_name, load in sources:
                    card_name = name.strip() or default_name
                    try:
                        cards.append(_custom_card(load(), card_name, int(quantity), foil, ctx))
                    except ValueError as exc:
                        skipped.append(f"{default_name}: {exc}")
                if not cards:
                    st.error("\n\n".join(skipped))
                    return
                _add_cards(ctx, deck_name, cards, skipped)
        st.divider()
        _render_sheet_splitter(ctx, deck_name)


def _render_sheet_splitter(ctx: _Context, deck_name: str) -> None:
    """Cut a sheet with several cards into one own image per grid cell.

    Not a form: the preview redraws the grid as rows, columns, margin and
    gap change. The widget keys carry a counter that is bumped after adding,
    which clears the inputs.
    """
    st.markdown("**✂️ Dividir una hoja con varias cartas**")
    form = st.session_state.get("sheet_form", 0)
    settings, preview = st.columns([1, 2])
    with settings:
        uploaded = st.file_uploader("Hoja de cartas", type=CUSTOM_IMAGE_TYPES, key=f"sheet-file-{form}")
        url = st.text_input("…o URL de la hoja", key=f"sheet-url-{form}").strip()
        rows_col, cols_col = st.columns(2)
        rows = int(rows_col.number_input(
            "Filas", min_value=1, max_value=SHEET_MAX_CELLS_PER_SIDE, value=3, key="sheet-rows"
        ))
        cols = int(cols_col.number_input(
            "Columnas", min_value=1, max_value=SHEET_MAX_CELLS_PER_SIDE, value=3, key="sheet-cols"
        ))
        margin_col, gap_col = st.columns(2)
        margin = int(margin_col.number_input(
            "Margen (px)", min_value=0, value=0, key="sheet-margin",
            help="Borde exterior de la hoja que se descarta.",
        ))
        gap = int(gap_col.number_input(
            "Separación (px)", min_value=0, value=0, key="sheet-gap",
            help="Espacio entre cartas vecinas que se descarta.",
        ))
        skipped_cells = st.multiselect(
            "Descartar celdas",
            list(range(1, rows * cols + 1)),
            key=f"sheet-skip-{form}",
            help="Huecos vacíos o cartas que no quieras añadir (números de la vista previa).",
        )
        base_name = st.text_input(
            "Nombre base",
            placeholder="Por defecto, el del fichero",
            help="Cada carta se llama «<nombre base> <número de celda>».",
            key=f"sheet-name-{form}",
        ).strip()
        quantity = int(st.number_input("Copias de cada carta", min_value=1, value=1, key="sheet-qty"))
        foil = st.checkbox("Foil", key="sheet-foil")

    if uploaded is None and not url:
        preview.caption("Sube una hoja o pega su URL para ver cómo se corta.")
        return
    try:
        data = uploaded.getvalue() if uploaded is not None else _download_sheet(url)
        preview.image(
            _sheet_preview(data, rows, cols, margin, gap, tuple(skipped_cells)),
            caption=f"{rows} × {cols} = {rows * cols} celdas",
            width="stretch",
        )
    except ValueError as exc:
        preview.error(str(exc))
        return

    if not settings.button("✂️ Dividir y añadir", type="primary", key="sheet-add"):
        return
    default_name = (
        Path(uploaded.name).stem if uploaded is not None
        else Path(unquote(urlparse(url).path)).stem
    )
    base_name = base_name or default_name or "Carta"
    cards, problems = [], []
    for number, cell in enumerate(_split_sheet(data, rows, cols, margin, gap), start=1):
        if number in skipped_cells:
            continue
        try:
            cards.append(_custom_card(cell, f"{base_name} {number}", quantity, foil, ctx))
        except ValueError as exc:
            problems.append(f"{number}: {exc}")
    if not cards:
        settings.error("\n\n".join(problems) or "Todas las celdas están descartadas.")
        return
    st.session_state.sheet_form = form + 1
    _add_cards(ctx, deck_name, cards, problems)


def _add_cards(
    ctx: _Context, deck_name: str, cards: list[DeckCard], skipped: list[str] | None = None
) -> None:
    with st.spinner("Buscando imágenes…"):
        added = _get_exporter(ctx).resolve_images(deck_name, cards)
    st.session_state.resolved.extend(added)
    st.session_state.pdfs = []
    missing = sum(1 for item in added if item.front_path is None)
    names = added[0].card.name if len(added) == 1 else f"{len(added)} cartas"
    st.session_state.flash = f"Añadido: {names}"
    warnings = [f"{missing} no encontrada(s): revisa «{FAILED_TAB}»."] if missing else []
    if skipped:
        warnings.append(f"{len(skipped)} imagen(es) descartada(s): " + "; ".join(skipped))
    if warnings:
        st.session_state.flash_warning = " ".join(warnings)
    st.rerun()


def _render_pdf_actions(
    dpi: int | None, foil_tone: ToneAdjustment | None, has_cards: bool
) -> None:
    resolved: list[ResolvedCard] = st.session_state.resolved
    ctx: _Context = st.session_state.context
    deck_name: str = st.session_state.deck_name

    generate_col, zip_col = st.columns(2)
    if generate_col.button("🖨️ Generar PDFs", type="primary", disabled=not has_cards):
        try:
            quality = "calidad original" if dpi is None else f"{dpi} DPI"
            with st.spinner(f"Generando PDFs ({quality})…"):
                pdfs = _get_exporter(ctx, dpi, foil_tone).render_pdfs(deck_name, resolved)
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
    text = format_deck((item.card for item in resolved), default_tcg=ctx.tcg)
    try:
        unchanged = path.read_text(encoding="utf-8") == text
    except OSError:
        unchanged = False
    if not unchanged:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return path


def _changed_lines(original: str, current: str, default_tcg: str | None = None) -> int:
    """Entries added or modified in ``current`` (formatting-only edits ignored)."""
    try:
        before = format_deck(parse_deck_text(original), default_tcg).splitlines()
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
    current = format_deck((item.card for item in resolved), default_tcg=ctx.tcg)
    try:
        path = _save_decklist(resolved, ctx, deck_name)
    except OSError as exc:
        logger.warning("Could not save the decklist: %s", exc)
        path = None
    changed = _changed_lines(original, current, ctx.tcg)

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

    ctx, dpi, foil_tone = _render_sidebar()
    _render_deck_input(ctx)
    if "resolved" in st.session_state:
        _render_preview(ctx, dpi, foil_tone)
        art_index = st.session_state.get("art_dialog")
        if art_index is not None and art_index < len(st.session_state.resolved):
            _art_dialog(art_index)


if __name__ == "__main__":
    main()
