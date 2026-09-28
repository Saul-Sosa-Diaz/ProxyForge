# ProxyForge — guía del proyecto

App CLI en Python que lee una decklist de TCG, descarga las imágenes de las cartas y genera PDFs listos para imprimir (63x88 mm, A4, 800 DPI, gutter, bleed y marcas de corte).

## Esquema de carpetas

```
tgc-card-image-downloader/
├── Dockerfile · docker-compose.yml   # python:3.11-slim; monta input/ output/ data/ src/
├── requirements.txt                  # requests, bs4, Pillow, pydantic
├── requirements-web.txt              # + streamlit (lo instalan Dockerfile y requirements-dev)
├── requirements-dev.txt · pytest.ini # pytest; pythonpath = . tests
├── input/                # decklists .txt (+ input/images para la estrategia local)
├── output/<tcg>/<mazo>/  # images/*.png + *.pdf
├── data/                 # caché (volumen)
├── src/
│   ├── main.py       # CLI: argparse, registro de estrategias, códigos de salida
│   ├── app.py        # UI web Streamlit: pestañas por PDF, selector de arte, descargas
│   ├── parser.py     # .txt / texto ↔ list[DeckCard] (parse_deck_* y format_deck)
│   ├── models.py     # pydantic: DeckCard, ResolvedCard (DownloadResult no se usa)
│   ├── exporter.py   # descarga sin duplicados + cuadrícula PDF
│   └── strategies/
│       ├── base.py   # clase abstracta TCGStrategy + warn_unsupported_art
│       ├── local.py · lorcana.py · mtg.py · pokemon.py
│       └── __init__.py   # reexporta las estrategias + __all__
└── tests/
    ├── mocks.py      # FakeSession/FakeResponse, FakeStrategy, cargadores de fixtures
    ├── test_local.py · test_lorcana.py · test_mtg.py · test_pokemon.py
    ├── test_exporter.py  # generación de PDFs · test_parser.py · test_app.py (AppTest)
    └── fixtures/     # decks/*.txt, images/, responses/*.html|json, card.png, card_back.png
```

## Arquitectura

- App de línea de comandos: procesa un mazo por ejecución, no hay servidor. UI web opcional con Streamlit (`src/app.py`) que reutiliza el mismo núcleo.
- Patrón Strategy: `TCGStrategy` (`src/strategies/base.py`) define `fetch_card_image` (abstracto), `fetch_card_back_image` (opcional, devuelve `False` por defecto) y `list_art_options` (opcional, `[]` por defecto; MTG y Lorcana devuelven `ArtOption(value=<marcador [art]>, label, image_url)` para el selector de arte de la web), más los atributos `name` y `supports_art`.
- Una estrategia por juego o fuente. `Exporter` recibe la estrategia inyectada y no depende del juego.
- Arranque: `python src/main.py ...` o `python -m src.main ...`. Web: `streamlit run src/app.py` o `docker compose up web` (puerto 8501).

## Flujo de una ejecución

1. `main.main()` → `_build_parser()` lee `--input --tcg --output --local-dir -v` y configura el logging.
2. `parser.parse_deck_file()` → `(deck_name = nombre del fichero, list[DeckCard])`.
3. `_make_strategy()` busca en `_STRATEGY_REGISTRY` e instancia la estrategia.
4. `Exporter(strategy, output/<tcg>).export_deck()` = `resolve_images()` (fase de descarga → `list[ResolvedCard]`, incluye fallidas con `front_path=None`) + `render_pdfs()` (fase de render → lista de PDFs). La web llama a las dos fases por separado. `resolve_images` → `_resolve_card` → `_fetch_single_image` (clave `_image_key(name, art)`; si el `.png` existe no se vuelve a pedir) → `strategy.fetch_card_image`. Sin reverso explícito → `_fetch_automatic_back` → `strategy.fetch_card_back_image`.
5. `render_pdfs` separa normales/foil y una cara/dos caras → `_build_pdf` / `_emit_dual_pdfs` (`_build_front_pages`, `_build_back_pages` con columnas en espejo, `_paste_card`, `_draw_crop_marks`, `_save_pages`).
6. PDFs: `<mazo>.pdf`, `foil_<mazo>.pdf`, `front_/back_<mazo>.pdf`, `foil_front_/foil_back_<mazo>.pdf`.
7. Código de salida: 0 bien, 1 fallo al exportar, 2 fallo al leer el mazo o la estrategia.

## Convenciones

- Cabecera: docstring del módulo y `from __future__ import annotations`.
- Importaciones: estándar → terceros → locales. Relativas dentro del paquete (`from .base import ...`); solo `main.py` usa `src.`.
- Logging: `logger = logging.getLogger(__name__)`, argumentos estilo `%`. `debug` para fallbacks, `warning` para fallos recuperables, `error` solo en `main`.
- Nombres: clases `XxxStrategy` con `name = "xxx"`; constantes en MAYÚSCULAS (`BASE_URL`, `DEFAULT_TIMEOUT`, `DEFAULT_IMAGES_DIR`); métodos privados `_resolve_*`, `_lookup_*`, `_scrape_*`, `_download_image`; helpers de módulo privados (`_normalize`, `_slugify`).
- Clases divididas con comentarios separadores `# --- Public API / Resolution helpers / ... ---`.
- Tipado moderno (`str | None`, `list[...]`) y docstrings estilo Google (Args/Returns).
- Errores:
  - Las estrategias nunca lanzan: capturan `requests.RequestException`, `OSError` y errores JSON, lo registran y devuelven `False`/`None`.
  - El parser lanza `ValueError` incluyendo `raw_line!r`.
  - El exporter lanza `RuntimeError` si no hay nada que renderizar.
  - `main` captura todo y lo traduce a código de salida.
- Cada estrategia encadena fuentes: principal → respaldo.
- MTG: el número de colección se valida contra el nombre oficial, las caras y el `flavor_name` (nombres alternativos de Secret Lair / Universes Beyond, p. ej. `Chaos Emerald (SLD) 7037` = Lotus Petal).
- Las estrategias que no soportan `[art]` llaman primero a `warn_unsupported_art(card_name, art)`.

## Tests

- Ejecutar: `pip install -r requirements-dev.txt` y `python -m pytest`.
- UI web: `streamlit.testing.v1.AppTest` con la estrategia `local` y `tests/fixtures/images` (sin red).
- Streamlit reejecuta `app.py` en cada interacción: lo que se guarde en `session_state` y se compare debe ser por valor (tuplas/modelos de `src.models`), no clases definidas en `app.py`. En `AppTest` las pestañas ocultas no se pintan: cambiar con `session_state["preview-tab"]`.
- Sin red: cada test sustituye `strategy._session` por `FakeSession({subcadena_url: FakeResponse})`; la primera ruta contenida en `url?k=v` gana y el resto devuelve 404.
- Los mocks usan solo ficheros estáticos de `tests/fixtures/` (nunca `input/`): mazos en `decks/`, imágenes locales en `images/` y respuestas HTTP en `responses/`, cargadas con `deck()`, `response_text()`, `response_json()` e `image_response()`.
- PDFs: `Exporter(..., target_dpi=50)` para que sean rápidos (la geometría escala con el DPI); `FakeStrategy` copia `card.png`/`card_back.png` y simula fallos (`known`) y reversos automáticos (`backs`).
- MTG: usar `MTGStrategy(min_request_interval=0)` para evitar la espera entre peticiones.
- Lorcana se prueba con `api_url=LORCANAJSON_JSON_URL`; la ruta del zip no está cubierta.

## Añadir una estrategia nueva (p. ej. módulo de enlaces / URLs)

- Crear `src/strategies/<nombre>.py` con `XxxStrategy(TCGStrategy)`, `name = "xxx"`, `requests.Session`, y `fetch_card_image` que devuelva `bool` sin lanzar excepciones.
- Registrar en `src/strategies/__init__.py` (import + `__all__`).
- Registrar en `src/main.py`: `choices` de `--tcg`, `_STRATEGY_REGISTRY` y `_make_strategy` si necesita argumentos.
- Para URLs, revisar:
  - `src/parser.py`: la `/` se interpreta como separador anverso/reverso y rompería las URLs (exceptuar `://` o cambiar el formato de entrada).
  - `_sanitize_filename` en `src/exporter.py`: con URLs genera nombres muy largos (considerar un hash).
- Añadir `tests/test_<nombre>.py` con sus respuestas simuladas en `tests/fixtures/responses/` y un mazo en `tests/fixtures/decks/`.
- Opcional: README y un mazo de ejemplo en `input/<nombre>/`.
