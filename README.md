# TCG Card Image Downloader

A containerized Python application that parses standard TCG (Trading Card Game)
decklists, fetches high-resolution card images using a **Strategy Pattern**,
de-duplicates the collection, and emits assets into a dedicated deck subfolder
alongside a print-ready PDF configured to exact physical card dimensions
(**64 x 89 mm**) with crop marks.

---

## Features

- Standard decklist parsing (`<quantity> <full card name>`).
- Pluggable **Strategy Pattern** for multi-TCG image fetching:
  - **Lorcana** — local JSON database (`data/lorcana.json`) with a
    `lorcana.gg` web-scraping fallback.
  - **MTG** — stub ready for Scryfall API integration.
- De-duplicated image downloads (each unique card is fetched once).
- Dynamic output subfolder named after the deck file.
- Print-ready multi-page PDF grid (A4) at **300 DPI** with crop marks around
  every 64 x 89 mm card slot.

---

## Project Structure

```text
.
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── README.md
├── data/
│   └── lorcana.json            # optional local card database
├── input/
│   └── my_awesome_deck.txt
├── output/
│   └── my_awesome_deck/
│       ├── images/
│       └── my_awesome_deck_printable.pdf
└── src/
    ├── __init__.py
    ├── main.py
    ├── parser.py
    ├── models.py
    ├── exporter.py
    └── strategies/
        ├── __init__.py
        ├── base.py
        ├── lorcana.py
        └── mtg.py
```

---

## Input Format

Each non-empty line of the deck file must follow:

```text
<quantity> <full card name>
```

Example (`input/my_awesome_deck.txt`):

```text
2 Mickey Mouse - Steamboat Pilot
4 Develop Your Brain
4 Mickey Mouse - Detective
4 Scrooge McDuck - Reformed Ebenezer
```

The deck file's base name (without extension) automatically becomes the output
subfolder name (e.g. `my_awesome_deck.txt` -> `output/my_awesome_deck/`).

---

## Usage

### Option A — Docker Compose (recommended)

```bash
docker compose run --rm tcg-downloader
```

This runs the default command bundled in `docker-compose.yml`, which processes
`input/my_awesome_deck.txt` with the `lorcana` strategy.

To process a different deck, override the command:

```bash
docker compose run --rm tcg-downloader \
  src/main.py --input /app/input/my_deck.txt --tcg lorcana
```

### Option B — Local Python

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python src/main.py --input input/my_awesome_deck.txt --output output --tcg lorcana
```

### CLI Arguments

| Flag | Short | Default | Description |
| --- | --- | --- | --- |
| `--input` | `-i` | _required_ | Path to the standard `.txt` deck file. |
| `--output` | `-o` | `/app/output` | Base output directory. |
| `--tcg` | `-t` | _required_ | TCG strategy to apply (`lorcana`, `mtg`). |
| `--local-db` | | _none_ | Optional path to the local JSON card DB (Lorcana). |
| `--verbose` | `-v` | off | Enable verbose logging. |

---

## Output

For a deck file named `my_awesome_deck.txt`, the application produces:

```text
output/my_awesome_deck/
├── images/                      # uniquely downloaded card images
└── my_awesome_deck_printable.pdf
```

The PDF arranges cards into a multi-page grid on A4 paper. Each card slot is
exactly **64 x 89 mm** with subtle black crop marks at the corners to allow
clean physical trimming after printing.

---

## Local JSON Database (Lorcana)

Place a `data/lorcana.json` file at the project root (or pass its path via
`--local-db`). The file may be either a JSON array of card objects or an
object with a `cards` (or `data`) array. Each entry must contain at least
`name` and an image URL field (`image_url` or `image`):

```json
{
  "cards": [
    {
      "name": "Mickey Mouse - Steamboat Pilot",
      "image_url": "https://example.com/images/mickey-steamboat.jpg"
    }
  ]
}
```

When a card is not present in the local database, the Lorcana strategy falls
back to scraping `https://lorcana.gg/cards/`.

---

## Print Dimensions

| Property | Value |
| --- | --- |
| Card size | 64 x 89 mm |
| Print DPI | 300 |
| Card pixels (at 300 DPI) | ~756 x 1051 px |
| Page size | A4 (210 x 297 mm) |
| Margins | 10 mm |
| Crop mark length | 3 mm |
| Crop mark offset from card | 2 mm |