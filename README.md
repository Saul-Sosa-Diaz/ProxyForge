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
(**63 x 88 mm**) with gutter, bleed and crop marks.

---

## 📚 Table of Contents

- [Features](#-features)
- [Input Format](#-input-format)
- [Usage](#-usage)
- [CLI Arguments](#-cli-arguments)
- [Output](#-output)
- [LorcanaJSON Database](#-lorcanajson-database-lorcana)
- [PkmnCards Naming](#-pkmncards-naming-pokémon)
- [Local Images](#-local-images-local)
- [Print Dimensions](#-print-dimensions)
- [Contributing](#-contributing)
- [License](#-license)

---

## ✨ Features

| | |
|---|---|
| 📝 **Standard parsing** | Reads decklists in `<quantity> <full card name>` format with optional `[art]` and foil markers |
| 🔌 **Strategy Pattern** | Extensible architecture to support multiple TCGs |
| 🃏 **Lorcana** | Queries the **LorcanaJSON** API on every run, and falls back to scraping `lorcana.gg`; `[art]` decklist markers select the card art (default: most premium available) |
| ⚡ **Pokémon** | Scrapes [`pkmncards.com`](https://pkmncards.com) search to pick the right printing among many reprints |
| 🔮 **Magic: The Gathering** | Queries the [Scryfall API](https://scryfall.com/docs/api) live on every run (exact + fuzzy name lookup, collector-number lookup, variant/base art search, high-res `png` imagery) with a search fallback and a final [Moxfield](https://moxfield.com) fallback; `[art]` decklist markers select set / variant / printing |
| 💾 **Local** | Resolves cards from your own image files on disk; the card name is the local file name |
| ♻️ **De-duplication** | Each unique card is downloaded only once, regardless of `quantity` |
| 📁 **Auto-organization** | Output subfolder named after the deck file |
| 🖨️ **Print-ready PDF** | Multi-page A4 grid at **800 DPI** with gutter, bleed and crop marks around every 63 x 88 mm card slot |

---

## 📋 Input Format

Each non-empty line of the deck file must follow:

```text
<quantity> <full card name> [art] [*F*]
```

**Example** (`input/my_awesome_deck.txt`):

```text
4 Daisy Duck - Donald's Date
2 Emerald Chromicon
2 Hades - King of Olympus [enchanted]
4 Lilo - Escape Artist
4 Tramp - Enterprising Dog
4 Tramp - Street-Smart Dog
4 Lady - Decisive Dog
4 Bobby Zimuruski - Spray Cheese Kid
2 Under the Sea
2 A Whole New World [base]
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

**Optional per-card markers** (may appear in either order at the end of the line):

- `[art]` — requests a specific art variant for that card (Lorcana and MTG;
  see [Art Selection](#-art-selection-lorcana--mtg)): Lorcana accepts `best`,
  `enchanted`, `iconic`, `epic`, `special` or `base`; MTG accepts a set code
  (`[m21]`), a set + collector number (`[2x2:117]`), a variant (`[fullart]`,
  `[borderless]`, `[showcase]`, `[extended]`, `[retro]`, `[promo]`,
  `[base]`/`[best]`) or a set (+collector) + variant combo
  (`[m21 borderless]`, `[2x2:117 showcase]`). Cards without the marker use
  the strategy default (Lorcana: most premium art available; MTG: Scryfall's
  default printing), and only recognized art expressions are stripped, so
  bracketed text belonging to a card name is left untouched.
- `*F*` / `*G*` — foil/premium marker (see [Output](#-output)).

**MTG example** (`input/mtg/sneaky.txt` with art picks):

```text
4 Lightning Bolt [m21]
2 Counterspell [mh2] *F*
4 Llanowar Elves [dmu:169]
2 Sol Ring [borderless]
4 Ragavan, Nimble Pilferer [mh2 showcase]
```

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
  src/main.py --input ./input/mtg/tmnt.txt --tcg mtg
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
| `--local-dir` | | `input/images` | Directory with local card images (Local only). Card names in the decklist must match the local file names. |
| `--verbose` | `-v` | off | Enable verbose logging. |

---

## 📦 Output

For a deck file named `my_awesome_deck.txt`, the application produces:

```text
output/my_awesome_deck/
├── images/                              # uniquely downloaded card images
├── my_awesome_deck_printable.pdf        # regular cards
└── my_awesome_deck_printable_foil.pdf   # foil entries only (when present)
```

Decklist entries marked as foil/premium with a trailing `*F*` (Manabox) or
`*G*` (MTGO) are rendered into their own `_printable_foil.pdf` so they can
be printed on separate (e.g. holographic) paper stock; the marker is
stripped from the card name before fetching. Decks without foil entries
only produce the regular PDF.

The PDFs arrange cards into a multi-page grid on A4 paper. Each card slot is
exactly **63 x 88 mm**, separated by a 3 mm gutter with 1 mm artwork bleed into it,
plus crop marks in the outer margins for clean physical trimming after printing.

---

## 🗄️ LorcanaJSON Database (Lorcana)

On every run the Lorcana strategy downloads the full card database from
**LorcanaJSON** (`https://lorcanajson.org/files/current/en/allCards.json.zip`)
— nothing is cached on disk.

Each card in the database follows the LorcanaJSON schema; the strategy reads the
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

When a card is not present in the LorcanaJSON database, the strategy falls
back to scraping `https://lorcana.gg/cards/`.

### 🎨 Art Selection (`[art]` decklist marker, Lorcana + MTG)

Most Lorcana cards exist in several art variants that share the same name:
the standard printing plus alternate-art premium versions (Enchanted, Iconic,
Epic, and Special/promo). LorcanaJSON lists each variant as its own entry,
ranked here from most to least premium:

```text
Enchanted > Iconic > Epic > Special (promo) > Legendary > Super Rare > Rare > Uncommon > Common
```

A trailing `[art]` marker on a decklist line requests a specific variant for
that card. **Lorcana cards without a marker always download the most premium art
available** (usually Enchanted — the most expensive and typically the
prettiest):

```text
4 Hades - King of Olympus [enchanted]   <- always the Enchanted art
2 A Whole New World [base]              <- always the standard art
4 Lilo - Escape Artist                  <- best available art (default)
```

| Value | Behaviour (Lorcana) |
| --- | --- |
| *(no marker)* — default | Picks the most premium art available for the card. |
| `best` | Same as no marker. |
| `enchanted` / `iconic` / `epic` / `special` | Requests that specific art; if the card has no such version, falls back to the default and logs it. |
| `base` | Picks the standard (non-premium) printing. |

MTG cards work the same way: a trailing `[art]` marker picks the printing.
The marker may be a set code, a set + collector number, a frame variant, or
a set (+collector) + variant combo (the `[art]` set always wins over a
`(SET)` annotation in the card name):

```text
4 Lightning Bolt [m21]                 <- that set's default printing
4 Lightning Bolt [2x2:117]             <- exact printing (collector endpoint)
2 Sol Ring [borderless]                <- newest borderless printing
4 Ragavan, Nimble Pilferer [mh2 showcase]  <- showcase within MH2
2 Counterspell [base]                  <- newest standard printing
4 Llanowar Elves                       <- Scryfall default (no marker / [best])
```

| Value | Behaviour (MTG) |
| --- | --- |
| *(no marker)* / `best` — default | Scryfall's default printing (`/cards/named`). |
| `<set>` (e.g. `m21`, `2x2`) | That set's default printing (`/cards/named?set=...`). |
| `<set>:<collector>` (e.g. `2x2:117`, `2x2-117`, `pltr 253s`) | Exact printing (`/cards/<set>/<collector>`). |
| `fullart` / `borderless` / `showcase` / `extended` / `retro` / `promo` | Newest printing matching that treatment (`/cards/search` `unique:prints`, filtered by `full_art` / `border_color` / `frame_effects` / `frame` / `promo`). Falls back to default with a log when nothing matches. |
| `base` (`normal` / `standard` aliases) | Newest standard printing (no promo, no full-art/borderless/showcase/extended). |
| `<set> <variant>` / `<set>:<collector> <variant>` (e.g. `m21 borderless`, `2x2:117 showcase`) | Variant filtered within that set/printing. |

> ⚠️ Lorcana-only markers (`[enchanted]`, `[iconic]`, `[epic]`, `[special]`)
> are ignored with a warning when used with `--tcg mtg` (and vice versa:
> MTG set/variant markers are ignored with a warning under `--tcg lorcana`).
> Pokémon and Local ignore `[art]` markers entirely with a warning.

---

## 🔮 Scryfall Lookup (Magic: The Gathering)

The MTG strategy resolves each card live through the **Scryfall API**
(`https://api.scryfall.com`) — nothing is cached on disk:

1. `GET /cards/<set>/<collector>` — exact printing when the decklist pins
   one (`Lightning Bolt (2x2) 117`) or the `[art]` marker does (`[2x2:117]`).
2. `GET /cards/search` with `unique:prints` — variant / base art selection
   (`[borderless]`, `[showcase]`, `[fullart]`, `[extended]`, `[retro]`,
   `[promo]`, `[base]`), optionally narrowed to a set (`[m21 borderless]`,
   `[2x2:117 showcase]`). Prints are filtered client-side by Scryfall card
   fields and the newest match wins; with no match the default art is used.
3. `GET /cards/named` — exact match first, fuzzy match second, so typos and
   accent variations still resolve. Decklist annotations and `[art]` sets are
   understood and forwarded: set code, collector number and foil/premium markers
   (`Lightning Bolt (2x2) 117`, `Barad-dûr (PLTR) 253s *F*`,
   `Lightning Bolt [m21]` → `exact=Lightning Bolt` + `set=m21`; the `[art]`
   set wins over a `(SET)` annotation).
4. `GET /cards/search` — last-resort search for names the named lookup
   cannot resolve.
5. [Moxfield](https://moxfield.com) — final fallback when Scryfall cannot
   resolve the card at all. The card is looked up via Moxfield's search API
   (the JSON backend of `moxfield.com/cards/search`) and the image is taken
   from their assets CDN (`assets.moxfield.net/cards/card-<id>-normal.jpg`),
   the same URL served by the "Download Image" button on
   `moxfield.com/cards/<id>-<name>` pages.

Images are downloaded from the Scryfall image CDN in the highest-quality
`png` version (744 x 1040, transparent rounded corners); the remaining
versions (`large`, `normal`, `border_crop`, `small`) act as fallbacks.
Double-faced cards use the front face (`card_faces[0].image_uris`).

Every card is resolved live on each run. Scryfall's
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
| Card size | 63 x 88 mm |
| Print DPI | 800 |
| Card pixels (at 800 DPI) | ~1984 x 2772 px |
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