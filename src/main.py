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
from src.pdf import ToneAdjustment
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
        help=(
            "TCG strategy to apply for image fetching. Decklist lines under a "
            "'[mtg]' / '[lorcana]' / '[local]' / '[pokemon]' section use that "
            "game instead, so one deck can mix games."
        ),
    )
    parser.add_argument(
        "--local-dir",
        default=LocalStrategy.DEFAULT_IMAGES_DIR,
        help=(
            "Directory with local card images ('local' cards and '[file:<name>]' "
            "arts of any game). Card names in the decklist must match the local "
            "file names."
        ),
    )
    parser.add_argument(
        "--mpc-url",
        default=MTGStrategy.MPC_DEFAULT_URL,
        help=(
            "MPC Autofill backend URL (mtg strategy only, primary source). "
            f"Default: {MTGStrategy.MPC_DEFAULT_URL}."
        ),
    )
    parser.add_argument(
        "--dpi",
        type=int,
        default=None,
        help=(
            "Resample every card to this resolution (e.g. 800, 300 for drafts). "
            "Default: embed the downloaded images at their original quality."
        ),
    )
    parser.add_argument(
        "--foil-lighten",
        action="store_true",
        help=(
            "Lighten the images of the foil PDFs so the foil shows through: "
            "more exposure, lifted shadows and lower opacity over white. "
            "Tune it with --foil-exposure / --foil-shadows / --foil-opacity."
        ),
    )
    defaults = ToneAdjustment()
    parser.add_argument(
        "--foil-exposure",
        type=float,
        default=None,
        metavar="PCT",
        help=f"Foil exposure / brightness boost in %% (default {defaults.exposure:.0%}; implies --foil-lighten).",
    )
    parser.add_argument(
        "--foil-shadows",
        type=float,
        default=None,
        metavar="PCT",
        help=(
            f"Foil shadow lift in %%: black becomes this grey (default {defaults.shadows:.0%}, "
            "max 24; implies --foil-lighten)."
        ),
    )
    parser.add_argument(
        "--foil-opacity",
        type=float,
        default=None,
        metavar="PCT",
        help=f"Foil image opacity over white in %% (default {defaults.opacity:.0%}; implies --foil-lighten).",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable verbose logging.",
    )
    return parser


def _foil_tone(args: argparse.Namespace) -> ToneAdjustment | None:
    """Tone curve for the foil PDFs from the ``--foil-*`` options (``None`` = off)."""
    values = {
        "exposure": args.foil_exposure,
        "shadows": args.foil_shadows,
        "opacity": args.foil_opacity,
    }
    if not args.foil_lighten and all(v is None for v in values.values()):
        return None
    return ToneAdjustment(**{key: v / 100 for key, v in values.items() if v is not None})


_STRATEGY_REGISTRY: dict[str, type[TCGStrategy]] = {
    "local": LocalStrategy,
    "lorcana": LorcanaStrategy,
    "mtg": MTGStrategy,
    "pokemon": PokemonStrategy,
}


def _make_strategy(
    name: str,
    local_dir: str | None,
    mpc_url: str | None = None,
) -> TCGStrategy:
    cls = _STRATEGY_REGISTRY.get(name)
    if cls is None:
        raise ValueError(f"Unknown TCG strategy: {name}")
    if cls is LorcanaStrategy:
        return LorcanaStrategy()
    if cls is MTGStrategy:
        return MTGStrategy(mpc_url=mpc_url)
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

    unknown = sorted({card.tcg for card in cards if card.tcg} - set(_STRATEGY_REGISTRY))
    if unknown:
        logger.error(
            "Unknown game section(s) in '%s': %s (valid: %s).",
            args.input,
            ", ".join(f"[{tcg}]" for tcg in unknown),
            ", ".join(_STRATEGY_REGISTRY),
        )
        return 2

    try:
        strategy = _make_strategy(args.tcg, args.local_dir, args.mpc_url)
    except ValueError as exc:
        logger.error("%s", exc)
        return 2

    exporter = Exporter(
        strategy=strategy,
        output_base_dir=args.output + f"/{args.tcg}",
        target_dpi=args.dpi,
        foil_tone=_foil_tone(args),
        # Cards under a '[lorcana]' / '[local]' ... section use that game.
        strategy_factory=lambda tcg: _make_strategy(tcg, args.local_dir, args.mpc_url),
    )
    try:
        pdf_path = exporter.export_deck(deck_name, cards)
    except Exception as exc:  # noqa: BLE001 - top-level CLI guard
        logger.error("Export failed: %s", exc)
        return 1

    logger.info("Done. Printable PDF: %s", pdf_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())