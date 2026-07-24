# Application Specification: TCG Card Image Downloader (`spec.md`)

## 1. Project Overview & Objectives

A containerized Python application designed to parse standard TCG (Trading Card Game) decklists via command-line arguments, fetch high-resolution card images using a **Strategy Pattern**, de-duplicate the collection, and output assets into a dedicated deck subfolder alongside a print-ready PDF configured to exact physical card dimensions (64x89 mm) with crop marks.

---

## 2. Tech Stack & Architecture

- **Language:** Python 3.11+
- **Containerization:** Docker & Docker Compose
- **Design Pattern:** Strategy Pattern for multi-TCG image fetching (Lorcana initial implementation, MTG ready).
- **Key Libraries:**
  - `requests` / `httpx` for API & HTTP fetching
  - `BeautifulSoup4` for web scraping fallback
  - `Pillow` (PIL) for precise mm-to-pixel image processing and PDF generation
  - `pydantic` for data validation and configuration management
  - `argparse` for command-line interface handling

---

## 3. Project Structure

```text
.
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── README.md
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

## 4. Input Format (`deck.txt`)

Standard TCG decklist format consisting of card quantity followed by the full card name:

```text
2 Mickey Mouse - Steamboat Pilot
4 Develop Your Brain
4 Mickey Mouse - Detective
4 Scrooge McDuck - Reformed Ebenezer
```

---

## 5. Command-Line Interface (`main.py`)

The application is controlled entirely via CLI arguments executed inside the container:

```bash
python src/main.py --input /app/input/my_awesome_deck.txt --tcg lorcana
```

### Arguments

- `--input`, `-i`: Path to the standard `.txt` deck file. The base file name (without extension) automatically dictates the output subfolder name.
- `--output`, `-o`: Base output directory (defaults to `/app/output`).
- `--tcg`, `-t`: Specifies the TCG strategy to apply (`lorcana`, `mtg`).

---

## 6. Core Components & Implementation Details

### 6.1. Parser (`parser.py`)

Reads the standard `.txt` file, extracts the deck name from the filename, strips quantities, and maps items to structured card objects.

```python
import re
from pathlib import Path
from pydantic import BaseModel

class DeckCard(BaseModel):
    quantity: int
    name: str

def parse_deck_file(file_path: str):
    path = Path(file_path)
    deck_name = path.stem
    cards = []

    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            match = re.match(r"^(\d+)\s+(.+)$", line)
            if match:
                quantity, name = match.groups()
                cards.append(
                    DeckCard(
                        quantity=int(quantity),
                        name=name.strip()
                    )
                )

    return deck_name, cards
```

### 6.2. Strategy Pattern (`strategies/`)

#### Base Strategy (`strategies/base.py`)

```python
from abc import ABC, abstractmethod

class TCGStrategy(ABC):
    @abstractmethod
    def fetch_card_image(self, card_name: str, output_path: str) -> bool:
        pass
```

#### Lorcana Strategy (`strategies/lorcana.py`)

Implements a dual-source resolution strategy:

1. **Local JSON Database (`lorcana.json`)**
   - Query local assets first for high-speed lookup.

2. **Web Scraping Fallback (`lorcana.gg`)**
   - If missing locally, construct a slug search query to `https://lorcana.gg/cards/`.
   - Parse target page using BeautifulSoup.
   - Extract image URL from target `div` style attribute or embedded `img` element.

#### MTG Strategy Placeholder (`strategies/mtg.py`)

Stubbed implementation complying with `TCGStrategy` ready for Scryfall API integration.

---

## 7. Output Generation & Physical Dimensions (`exporter.py`)

### Subfolder Generation

Creates:

```text
/app/output/{deck_name}/images/
```

dynamically based on the input filename.

### De-duplication

Downloads unique card assets once into the images directory, avoiding redundant network calls for quantities greater than 1.

### Print Layout & Dimensions (64×89 mm)

- Dimensions calculated precisely at target print DPI (e.g., **300 DPI** converts **64×89 mm** to approximately **755×1051 pixels** per card).
- Arranges cards into a multi-page grid layout on standard paper sizes (A4/Letter).
- Automatically embeds subtle cutting guide lines (crop marks) around each **64×89 mm** card slot to facilitate clean physical trimming.

---

## 8. Docker Configuration

### Dockerfile

```dockerfile
FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libgl1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENTRYPOINT ["python"]
```

### docker-compose.yml

```yaml
version: '3.8'

services:
  tcg-downloader:
    build: .
    volumes:
      - ./input:/app/input
      - ./output:/app/output
    command:
      [
        "src/main.py",
        "--input",
        "/app/input/deck.txt",
        "--tcg",
        "lorcana"
      ]
```