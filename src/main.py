"""Command-line entry point for the TCG Card Image Downloader.

Supports both invocation styles:
    python src/main.py --input ... --tcg lorcana   # script mode (spec/Dockerfile)
    python -m src.main --input ... --tcg lorcana   # module mode
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# Bootstrap the project root onto sys.path so the ``src`` package is importable
# when this file is launched directly as a script (`python src/main.py`).
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.exporter import Exporter
from src.parser import parse_deck_file
from src.strategies.base import TCGStrategy
from src.strategies.local import LocalStrategy
from src.strategies.lorcana import LorcanaStrategy
from src.strategies.mtg import MTGStrategy
from src.strategies.pokemon import PokemonStrategy

logger = logging.getLogger("tcg-downloader")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tcg-card-image-downloader",
        description=(
            "Parse a TCG decklist, fetch high-resolution card images, "
            "de-duplicate the collection, and emit a print-ready PDF "
            "(63x88 mm cards with gutter, bleed and crop marks)."
        ),
    )
    parser.add_argument(
        "--input",
        "-i",
        required=True,
        help="Path to the standard .txt deck file. The base file name dictates the output subfolder.",
    )
    parser.add_argument(
        "--output",
        "-o",
        default="/app/output",
        help="Base output directory (default: /app/output).",
    )
    parser.add_argument(
        "--tcg",
        "-t",
        required=True,
        choices=["local", "lorcana", "mtg", "pokemon"],
        help="TCG strategy to apply for image fetching.",
    )
    parser.add_argument(
        "--local-dir",
        default=LocalStrategy.DEFAULT_IMAGES_DIR,
        help=(
            "Directory with local card images (local strategy only). "
            "Card names in the decklist must match the local file names."
        ),
    )
    parser.add_argument(
        "--db-cache",
        default=None,
        help=(
            "Path to the strategy cache file (Lorcana/MTG). Auto-created on first run. "
            "Defaults: data/lorcana_cache.json (Lorcana), data/mtg_cache.json (MTG)."
        ),
    )
    parser.add_argument(
        "--refresh-db",
        action="store_true",
        help="Force re-resolution of cards, ignoring the local cache (Lorcana/MTG).",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable verbose logging.",
    )
    return parser


_STRATEGY_REGISTRY: dict[str, type[TCGStrategy]] = {
    "local": LocalStrategy,
    "lorcana": LorcanaStrategy,
    "mtg": MTGStrategy,
    "pokemon": PokemonStrategy,
}


def _make_strategy(
    name: str,
    db_cache: str | None,
    refresh_db: bool,
    local_dir: str | None,
) -> TCGStrategy:
    cls = _STRATEGY_REGISTRY.get(name)
    if cls is None:
        raise ValueError(f"Unknown TCG strategy: {name}")
    if cls is LorcanaStrategy:
        return LorcanaStrategy(
            cache_path=db_cache,
            refresh_db=refresh_db,
        )
    if cls is MTGStrategy:
        return MTGStrategy(
            cache_path=db_cache,
            refresh_db=refresh_db,
        )
    if cls is LocalStrategy:
        return LocalStrategy(images_dir=local_dir)
    return cls()


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    try:
        deck_name, cards = parse_deck_file(args.input)
    except (OSError, ValueError) as exc:
        logger.error("Failed to parse deck file '%s': %s", args.input, exc)
        return 2

    if not cards:
        logger.error("Deck file '%s' contains no cards.", args.input)
        return 2

    logger.info("Parsed deck '%s' with %d unique card entries.", deck_name, len(cards))

    try:
        strategy = _make_strategy(args.tcg, args.db_cache, args.refresh_db, args.local_dir)
    except ValueError as exc:
        logger.error("%s", exc)
        return 2

    exporter = Exporter(strategy=strategy, output_base_dir=args.output + f"/{args.tcg}")
    try:
        pdf_path = exporter.export_deck(deck_name, cards)
    except Exception as exc:  # noqa: BLE001 - top-level CLI guard
        logger.error("Export failed: %s", exc)
        return 1

    logger.info("Done. Printable PDF: %s", pdf_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())