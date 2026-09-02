<div align="center">

# 🃏 ProxyForge

**Stop paying $5 for a piece of cardboard. Print your own deck.**

[![Python](https://img.shields.io/badge/Python-3.11+-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Docker](https://img.shields.io/badge/Docker-ready-2496ED?logo=docker&logoColor=white)](https://www.docker.com/)
[![License](https://img.shields.io/badge/license-MIT-green)](#license)
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen)](#contributing)

</div>

---

> **⚠️ Disclaimer:** TCGs are stupidly expensive, and this project was born out of poverty, not spite toward publishers. Use it for playtest proxies, not for selling counterfeits. **Don't judge me.** 🥲

A containerized Python application that parses standard **Trading Card Game**
decklists, fetches high-resolution card images using a **Strategy Pattern**,
de-duplicates the collection, and emits assets into a dedicated deck subfolder
alongside a print-ready PDF configured to exact physical card dimensions
(**64 x 89 mm**) with crop marks.

---

## 📚 Table of Contents

- [Features](#-features)
- [Input Format](#-input-format)
- [Usage](#-usage)
- [CLI Arguments](#-cli-arguments)
- [Output](#-output)
- [LorcanaJSON Database & Cache](#-lorcanajson-database--cache-lorcana)
- [PkmnCards Naming](#-pkmncards-naming-pokémon)
- [Local Images](#-local-images-local)
- [Print Dimensions](#-print-dimensions)
- [Contributing](#-contributing)
- [License](#-license)

---

## ✨ Features

| | |
|---|---|
| 📝 **Standard parsing** | Reads decklists in `<quantity> <full card name>` format |
| 🔌 **Strategy Pattern** | Extensible architecture to support multiple TCGs |
| 🃏 **Lorcana** | Queries the **LorcanaJSON** API, caches locally, and falls back to scraping `lorcana.gg` |
| ⚡ **Pokémon** | Scrapes [`pkmncards.com`](https://pkmncards.com) search to pick the right printing among many reprints |
| 🔮 **Magic: The Gathering** | Queries the [Scryfall API](https://scryfall.com/docs/api) (exact + fuzzy name lookup, high-res `png` imagery) with a local cache and search fallback |
| 💾 **Local** | Resolves cards from your own image files on disk; the card name is the local file name |
| ♻️ **De-duplication** | Each unique card is downloaded only once, regardless of `quantity` |
| 📁 **Auto-organization** | Output subfolder named after the deck file |
| 🖨️ **Print-ready PDF** | Multi-page A4 grid at **800 DPI** with crop marks around every 64 x 89 mm card slot |

---

## 📋 Input Format

Each non-empty line of the deck file must follow:

```text
<quantity> <full card name>
```

**Example** (`input/my_awesome_deck.txt`):

```text
4 Daisy Duck - Donald's Date
2 Emerald Chromicon
4 Lilo - Escape Artist
4 Tramp - Enterprising Dog
4 Tramp - Street-Smart Dog
4 Lady - Decisive Dog
4 Bobby Zimuruski - Spray Cheese Kid
2 Under the Sea
4 Mowgli - Man Cub
4 Grandmother Willow - Ancient Advisor
4 Mulan - Resourceful Recruit
2 Pluto - Friendly Pooch
4 Lilo - Snow Artist
4 Pudge - Controls the Weather
4 Stitch - Naughty Experiment
2 Lilo - Rock Star
4 Stitch - Rock Star
```

> 💡 The deck file's base name (without extension) automatically becomes the
> output subfolder name.
> E.g. `my_awesome_deck.txt` → `output/my_awesome_deck/`

---

## 🚀 Usage

### Option A — Docker Compose *(recommended)*

```bash
docker compose run --rm tcg-downloader
```

This runs the default command bundled in `docker-compose.yml`, which processes
`input/my_awesome_deck.txt` with the `lorcana` strategy.

To process a different deck, override the command:

```bash
docker compose run --rm tcg-downloader \
  src/main.py --input ./input/my_deck.txt --tcg lorcana
```

### Option B — Local Python

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python src/main.py --input input/my_awesome_deck.txt --output output --tcg lorcana
```

---

## ⚙️ CLI Arguments

| Flag | Short | Default | Description |
| --- | --- | --- | --- |
| `--input` | `-i` | _required_ | Path to the standard `.txt` deck file. |
| `--output` | `-o` | `/app/output` | Base output directory. |
| `--tcg` | `-t` | _required_ | TCG strategy to apply (`lorcana`, `pokemon`, `mtg`, `local`). |
| `--db-cache` | | per strategy | Path to the strategy cache file (Lorcana/MTG). Auto-created on first run. Defaults: `data/lorcana_cache.json`, `data/mtg_cache.json`. |
| `--local-dir` | | `input/images` | Directory with local card images (Local only). Card names in the decklist must match the local file names. |
| `--refresh-db` | | off | Force re-resolution of cards, ignoring the local cache (Lorcana/MTG). |
| `--verbose` | `-v` | off | Enable verbose logging. |

---

## 📦 Output

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

## 🗄️ LorcanaJSON Database & Cache (Lorcana)

On the first run the Lorcana strategy downloads the full card database from
**LorcanaJSON** (`https://lorcanajson.org/files/current/en/allCards.json.zip`)
and saves it to `data/lorcana_cache.json` (override with `--db-cache`).
Subsequent runs load the cache instantly instead of re-downloading.

Each card in the cache follows the LorcanaJSON schema; the strategy reads the
`fullName` / `simpleName` fields for matching and the `images.full` URL
(usually 1468 x 2048 px) for downloads.

```json
{
  "cards": [
    {
      "fullName": "Mickey Mouse - Steamboat Pilot",
      "simpleName": "mickey mouse steamboat pilot",
      "images": { "full": "https://.../mickey-steamboat.png" }
    }
  ]
}
```

Force a refresh of the cached database with `--refresh-db`. When a card is
not present in the LorcanaJSON database, the strategy falls back to scraping
`https://lorcana.gg/cards/`.

---

## 🔮 Scryfall Lookup & Cache (Magic: The Gathering)

The MTG strategy resolves each card through the **Scryfall API**
(`https://api.scryfall.com`):

1. `GET /cards/named` — exact match first, fuzzy match second, so typos and
   accent variations still resolve. Decklist set annotations are understood
   and forwarded: `Lightning Bolt (2x2) 117` → `exact=Lightning Bolt` +
   `set=2x2`.
2. `GET /cards/search` — last-resort search for names the named lookup
   cannot resolve.

Images are downloaded from the Scryfall image CDN in the highest-quality
`png` version (744 x 1040, transparent rounded corners); the remaining
versions (`large`, `normal`, `border_crop`, `small`) act as fallbacks.
Double-faced cards use the front face (`card_faces[0].image_uris`).

Resolved names are cached in `data/mtg_cache.json` (override with
`--db-cache`, force re-resolution with `--refresh-db`) so subsequent runs
skip the API entirely, following Scryfall's caching guidelines. Scryfall's
rate limits are honored: a 500 ms minimum interval between API requests and
`Retry-After` handling on HTTP 429 (see
[Rate Limits](https://scryfall.com/docs/api/rate-limits)).

---

## ⚡ PkmnCards Naming (Pokémon)

The Pokémon strategy fetches images from **[pkmncards.com](https://pkmncards.com)**,
so card names in the decklist should match the ones used on that site.

Pokémon reprints the same character in many different sets (there are dozens
of Pikachus), so the strategy always queries the **site search**
(`https://pkmncards.com/?s=<card name>`) instead of guessing a card URL. Each
search result carries its full title — `Name · Set (CODE) #number`, e.g.
`Pikachu ex · Ascended Heroes (ASC) #276` — and the strategy picks the best
match:

1. Exact title match (punctuation like `·`, `#` or parentheses is ignored).
2. Title starting with the given name (e.g. `Pikachu ex Ascended Heroes`).
3. Otherwise, the **first search hit** is used and a warning is logged.

> ⚠️ To get the exact printing you want, include the set name (and ideally
> the set code and collector number) as written on pkmncards.com:
>
> ```text
> 4 Pikachu ex · Ascended Heroes (ASC) #276
> 4 Boss's Orders · Paldea Evolved (PAL) #172
> ```
>
> A bare `4 Pikachu` will resolve to an arbitrary printing.

---

## 💾 Local Images (Local)

The `local` strategy skips every remote source and resolves cards from a
directory on disk (`--local-dir`, default `input/images`). **The card name in
the decklist is the local file name** (extension optional):

```text
4 Mickey_Mouse_-_Steamboat_Pilot
2 Brawl.png
```

```text
input/images/
├── Mickey_Mouse_-_Steamboat_Pilot.png   # matches "Mickey_Mouse_-_Steamboat_Pilot"
└── Brawl.png                            # matches "Brawl" or "Brawl.png"
```

Resolution order:

1. Exact file name match (`.png`, `.jpg`, `.jpeg` and `.webp` supported).
2. Case-insensitive match by file name or stem.

```bash
python src/main.py --input input/my_deck.txt --output output --tcg local \
  --local-dir input/images
```

> 💡 This is the fastest way to re-print a previously downloaded deck: point
> `--local-dir` at an existing `output/<tcg>/<deck>/images` folder.

---

## 🖨️ Print Dimensions

| Property | Value |
| --- | --- |
| Card size | 64 x 89 mm |
| Print DPI | 800 |
| Card pixels (at 800 DPI) | ~2016 x 2800 px |
| Page size | A4 (210 x 297 mm) |
| Grid | 3 x 3 (9 cards per page) |

---

## 🤝 Contributing

PRs are welcome. If you want to add support for a new TCG, implement a new
strategy following the existing interface in `src/strategies/` and open a
pull request.

---

## 📄 License

This project is distributed under the MIT License. Use it, modify it, and
share it freely — but remember to respect each publisher's image rights.