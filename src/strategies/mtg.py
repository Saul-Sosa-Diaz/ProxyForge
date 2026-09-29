"""MTG image-fetching strategy.

Resolution order (MPC Autofill, https://mpcfill.com, only for explicit
``[mpc:<identifier>]`` markers; otherwise Scryfall API,
https://scryfall.com/docs/api, then Moxfield, https://moxfield.com):
    1. MPC Autofill community database (``POST /2/cards/``) when the
       decklist line carries ``[mpc:<identifier>]`` (picked in the web art
       picker, which lists every MPC image of a card via
       ``POST /2/editorSearch/``): that exact full-resolution image. Lines
       without an MPC marker never touch MPC; an MPC image that was removed
       falls back to Scryfall.
    2. Scryfall collector lookup (``GET /cards/<set>/<collector>``) when the
       decklist pins an exact printing (``Lightning Bolt (2x2) 117`` or the
       ``[2x2:117]`` art marker).
    3. Scryfall prints search (``GET /cards/search`` with
       ``unique:prints``) when the ``[art]`` marker requests a variant
       (``[borderless]``, ``[showcase]``, ``[fullart]``, ``[extended]``,
       ``[retro]``, ``[promo]``) or ``[base]``, optionally narrowed to a
       set (``[m21 borderless]``, ``[2x2:117 showcase]``).
    4. Scryfall card name lookup (``GET /cards/named``). The decklist name
       is tried with an exact match first and a fuzzy match second; set
       annotations from the decklist (``Lightning Bolt (2x2) 117``) or the
       ``[art]`` marker (``[m21]``, ``[2x2]``) are forwarded as the ``set``
       parameter. Every card is resolved live against the API on each run.
    5. Scryfall card search fallback (``GET /cards/search?q=<name>``) for
       names the named-lookup endpoint cannot resolve (typos, extra
       tokens...).
    6. Moxfield fallback (``GET https://api.moxfield.com/v2/cards/search``)
       when Scryfall cannot resolve the card at all. The matched card's
       internal Moxfield id feeds the assets CDN image that the
       "Download Image" button on ``https://moxfield.com/cards/<id>-<slug>``
       pages points to
       (``https://assets.moxfield.net/cards/card-<id>-normal.jpg``).

Art selection (per-card ``[art]`` decklist marker, like Lorcana):
``[best]`` (default), ``[base]`` (standard non-premium printing),
``[<set>]`` (e.g. ``[m21]``), ``[<set>:<collector>]`` (e.g.
``[2x2:117]``), a variant (``[fullart]``, ``[borderless]``,
``[showcase]``, ``[extended]``, ``[retro]``, ``[promo]``) or a set
(+collector) + variant combo (``[m21 borderless]``,
``[2x2:117 showcase]``). Cards without a marker use Scryfall's default
printing.

MPC images are served at full resolution from the Google Drive
``downloadLink`` of the chosen community image (often 1200 DPI scans).
Scryfall images use its image CDN
(``cards.scryfall.io``) in the versions documented at
https://scryfall.com/docs/api/images. By default the ``png`` version is
used (744x1040, highest quality); the remaining versions (``large``,
``normal``, ``border_crop``, ``small``...) serve as fallbacks.

Double-faced cards (transform, modal DFC...): backs of MPC fronts resolve
via ``GET /2/DFCPairs/`` while Scryfall backs use ``card_faces[1]``, so a
single decklist line prints both sides without naming the back
explicitly. ``fetch_card_image`` saves the front face while
``fetch_card_back_image`` saves the back face. Split / flip / adventure
cards expose a single top-level image and have no automatic back.

Rate limits (https://scryfall.com/docs/api/rate-limits): the ``/cards/*``
endpoints are limited to 2 requests/second, so a minimum interval is
enforced between Scryfall API calls and HTTP 429 responses are honored via
the ``Retry-After`` header. The image CDNs have no rate limits.
"""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any

import requests

from ..models import ArtOption, DeckCard
from .base import TCGStrategy

logger = logging.getLogger(__name__)

# Art markers that only make sense for Lorcana; when used with --tcg mtg a
# warning is logged and the default art is used.
_LORCANA_ONLY_ART = ("enchanted", "iconic", "epic", "special")

# Canonical MTG variant keywords honored by the prints search.
MTG_VARIANT_CHOICES = ("fullart", "borderless", "showcase", "extended", "retro", "promo")
MTG_ART_MODES = ("best", "base")
# '[mpc:<identifier>]' pins one MPC Autofill image (case-sensitive id).
MPC_ART_PREFIX = "mpc:"
# Legacy '[scryfall:<art>]' marker (Scryfall is now the default): same as '[<art>]'.
SCRYFALL_ART_PREFIX = "scryfall:"
# MPC images that stand in for a card rather than show it.
_MPC_STAND_IN = re.compile(r"\b(checklist|placeholder)\b", re.IGNORECASE)


class MTGStrategy(TCGStrategy):
    """Fetch MTG card images via MPC Autofill, Scryfall and Moxfield (no local cache)."""

    name = "mtg"
    supports_art = True

    SCRYFALL_API_URL = "https://api.scryfall.com"
    NAMED_ENDPOINT = "/cards/named"
    SEARCH_ENDPOINT = "/cards/search"
    MOXFIELD_SEARCH_API_URL = "https://api.moxfield.com/v2/cards/search"
    MOXFIELD_ASSETS_URL = "https://assets.moxfield.net/cards"
    MPC_DEFAULT_URL = "https://mpcfill.com"
    MPC_SOURCES_ENDPOINT = "/2/sources/"
    MPC_EDITOR_SEARCH_ENDPOINT = "/2/editorSearch/"
    MPC_CARDS_ENDPOINT = "/2/cards/"
    MPC_DFC_PAIRS_ENDPOINT = "/2/DFCPairs/"
    # Card documents per ``POST /2/cards/`` call (the server rejects 3000).
    MPC_CARDS_BATCH_SIZE = 1000
    # Names per ``POST /2/editorSearch/`` call (a 112-card deck fits in one).
    MPC_SEARCH_BATCH_SIZE = 100
    # mpcfill.com (Cloudflare) answers ~10 calls in a few seconds with HTTP
    # 429 + Retry-After: 10; one call every 1.5 s stays clear of it.
    MPC_DEFAULT_MIN_REQUEST_INTERVAL = 1.5
    MPC_RATE_LIMIT_RETRIES = 3
    MPC_RATE_LIMIT_RETRY_SECONDS = 10.0
    MPC_MAX_RETRY_SECONDS = 60.0
    DEFAULT_TIMEOUT = 30
    # Scryfall limits /cards/* to 2 requests/second (500 ms between calls).
    DEFAULT_MIN_REQUEST_INTERVAL = 0.5
    IMAGE_FORMAT_FALLBACKS = ("png", "large", "normal", "border_crop", "small")
    # Scryfall blocks access for 30 seconds after an HTTP 429.
    RATE_LIMIT_RETRY_SECONDS = 30.0

    def __init__(
        self,
        api_url: str | None = None,
        timeout: int = DEFAULT_TIMEOUT,
        image_format: str = "png",
        min_request_interval: float = DEFAULT_MIN_REQUEST_INTERVAL,
        mpc_url: str | None = None,
        mpc_min_request_interval: float = MPC_DEFAULT_MIN_REQUEST_INTERVAL,
    ) -> None:
        self.api_url = (api_url or self.SCRYFALL_API_URL).rstrip("/")
        self.mpc_url = (mpc_url or self.MPC_DEFAULT_URL).rstrip("/")
        self.timeout = timeout
        self.image_format = image_format
        self.min_request_interval = max(0.0, min_request_interval)
        self._last_request_at = 0.0
        self.mpc_min_request_interval = max(0.0, mpc_min_request_interval)
        self._mpc_last_request_at = 0.0
        # Resolved Scryfall card objects, keyed by (normalized name,
        # effective art): shared by the front and back fetches so a
        # double-faced card costs no extra API calls for its back face.
        self._card_cache: dict[tuple[str, str | None], dict[str, Any] | None] = {}
        # MPC Autofill caches, keyed by normalized name / identifier. The
        # sources list and the DFC pairs are fetched once per run. Only
        # successful responses are cached (a rate-limited call retries).
        self._mpc_sources_cache: list[list[Any]] | None = None
        self._mpc_search_cache: dict[str, list[str]] = {}
        self._mpc_card_cache: dict[str, dict[str, Any] | None] = {}
        self._mpc_dfc_pairs: dict[str, str] | None = None
        self._session = requests.Session()
        self._session.headers.update(
            {
                # Scryfall asks for an identifying User-Agent and an Accept header.
                "User-Agent": "tgc-card-image-downloader/1.0",
                "Accept": "application/json",
            }
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def fetch_card_image(
        self,
        card_name: str,
        output_path: str,
        art: str | None = None,
    ) -> bool:
        # 1. MPC Autofill only for an explicit '[mpc:<identifier>]' marker
        # (chosen in the web art picker): always that exact image.
        identifier = _mpc_identifier(art)
        if identifier is not None:
            card = self._mpc_card(identifier)
            pinned_url = self._mpc_image_url(card) if card is not None else None
            if pinned_url:
                _log_mpc_choice(card_name, card)
                return self._download_image(pinned_url, output_path)
            logger.warning(
                "MPC Autofill card '%s' for '%s' is unavailable; using Scryfall.",
                identifier,
                card_name,
            )
            art = None
        # 2. Everything else: Scryfall (then Moxfield).
        art = _effective_art(card_name, _scryfall_art(art))
        image_url = self._resolve_image_url(card_name, art)
        if not image_url:
            return False
        logger.info("Card '%s' -> Scryfall (%s, ~300 DPI)", card_name, self.image_format)
        return self._download_image(image_url, output_path)

    def fetch_card_back_image(
        self,
        card_name: str,
        output_path: str,
        art: str | None = None,
    ) -> bool:
        """Save the back face of a double-faced card (same source as the front)."""
        if _mpc_identifier(art) is not None:
            # MPC front: its back comes from the MPC DFC pairs when possible.
            clean_name, _, _ = _parse_card_reference(card_name)
            mpc_back_url, mpc_back_name = self._lookup_mpc_back(clean_name)
            if mpc_back_url:
                logger.info(
                    "Card '%s' is double-faced (back: '%s'); downloading its back face.",
                    card_name,
                    mpc_back_name,
                )
                return self._download_image(mpc_back_url, output_path)
            # A pinned MPC front says nothing about the Scryfall printing.
            art = None
        else:
            art = _effective_art(card_name, _scryfall_art(art))
        card = self._resolve_card(card_name, art)
        if card is None:
            return False
        back_url = self._extract_back_image_url(card)
        if not back_url:
            return False
        faces = card.get("card_faces")
        back_name = (
            faces[1].get("name")
            if isinstance(faces, list)
            and len(faces) > 1
            and isinstance(faces[1], dict)
            else None
        )
        logger.info(
            "Card '%s' is double-faced%s; downloading its back face.",
            card_name,
            f" (back: '{back_name}')" if back_name else "",
        )
        return self._download_image(back_url, output_path)

    def pin_back(self, card_name: str, art: str | None = None) -> tuple[str, str | None] | None:
        """Pin the MPC ``DFCPairs`` back of an MPC front as ``/ Back [mpc:<id>]``.

        The back image is the highest-DPI MPC hit for the back name, written
        into the saved decklist so later runs reuse it. Scryfall fronts keep
        their automatic Scryfall back.
        """
        if _mpc_identifier(art) is None:
            return None
        clean_name, _, _ = _parse_card_reference(card_name)
        back_name = self._mpc_back_name(clean_name)
        if not back_name:
            return None
        card = self._mpc_best_card(back_name)
        if card is None:
            return None
        return back_name, f"{MPC_ART_PREFIX}{card['identifier']}"

    def prefetch(self, cards: list[DeckCard]) -> None:
        """Batch the MPC Autofill lookups of a deck's ``[mpc:<id>]`` entries.

        Only entries with an MPC marker touch MPC: their card documents are
        fetched ``MPC_CARDS_BATCH_SIZE`` at a time, and the back names of
        MPC fronts without an explicit back are searched in one batched
        ``editorSearch`` call, so the rate limit is never hit by a deck.
        """
        identifiers: list[str] = []
        back_names: list[str] = []
        for card in cards:
            for art in (card.art, card.back_art if card.back_name else None):
                identifier = _mpc_identifier(art)
                if identifier is not None:
                    identifiers.append(identifier)
            if not card.back_name and _mpc_identifier(card.art) is not None:
                back_name = self._mpc_back_name(_parse_card_reference(card.name)[0])
                if back_name:
                    back_names.append(back_name)
        if back_names:
            self._mpc_search_many(back_names)
            for name in back_names:
                identifiers.extend(self._mpc_search_cache.get(_normalize(name), []))
        if identifiers:
            self._mpc_cards(identifiers)

    def list_art_options(self, card_name: str) -> list[ArtOption]:
        """List every MPC Autofill art, then every Scryfall printing.

        MPC arts (``mpc:<identifier>``, the only way a card uses MPC) come
        first, highest DPI first, labeled with source, DPI and file size.
        Scryfall printings follow (newest first) as ``set:collector`` arts
        (~300 DPI scans). Printings whose set/collector cannot be written as
        an ``[art]`` marker (e.g. The List's ``plst:BLC-129``) are skipped.
        Tokens and other extras are only searched when the card has no
        regular printing (``Bird (teoc)``).
        """
        clean_name, _, _ = _parse_card_reference(card_name)
        options = [_mpc_art_option(card) for card in self._mpc_ranked_cards(clean_name)]
        prints = self._search_all_prints(f'!"{clean_name}"')
        if not prints:
            prints = self._search_all_prints(f'!"{clean_name}" include:extras')
        options.extend(_art_option(card) for card in prints)
        return [option for option in options if option is not None]

    # ------------------------------------------------------------------
    # MPC Autofill (explicit '[mpc:<identifier>]' arts and the art picker)
    # ------------------------------------------------------------------
    def _mpc_best_card(self, clean_name: str) -> dict[str, Any] | None:
        """Highest-DPI MPC Autofill hit for a card name (``None`` if none).

        Searches the community database by name only (decklist set
        annotations and ``[art]`` markers are ignored). Returns ``None``
        when MPC has no hit so the caller falls back to Scryfall.
        """
        ranked = self._mpc_ranked_cards(clean_name)
        return ranked[0] if ranked else None

    def _mpc_ranked_cards(self, clean_name: str) -> list[dict[str, Any]]:
        """Every MPC hit for a name, highest DPI first (ties keep MPC order).

        Checklist / placeholder stand-ins go last so they are never the
        default art.
        """
        identifiers = self._mpc_search_identifiers(clean_name)
        cards = [card for card in self._mpc_cards(identifiers) if self._mpc_image_url(card)]
        return sorted(cards, key=lambda card: (_is_mpc_stand_in(card), -_mpc_dpi(card)))

    def _lookup_mpc_back(self, clean_name: str) -> tuple[str | None, str | None]:
        """Resolve a double-faced back via MPC ``DFCPairs``.

        Returns ``(image_url, back_name)`` (``(None, None)`` when the card
        has no MPC back pair) so the caller falls back to Scryfall backs.
        """
        back_name = self._mpc_back_name(clean_name)
        if not back_name:
            return None, None
        card = self._mpc_best_card(back_name)
        if card is None:
            return None, back_name
        _log_mpc_choice(back_name, card)
        return self._mpc_image_url(card), back_name

    def _mpc_back_name(self, clean_name: str) -> str | None:
        """Back-face name of an MPC double-faced card (case-insensitive)."""
        pairs = self._mpc_dfc_pairs_map()
        back_name = pairs.get(clean_name)
        if back_name is None:
            lowered = clean_name.lower()
            for front, back in pairs.items():
                if front.lower() == lowered:
                    back_name = back
                    break
        return back_name or None

    def _mpc_search_identifiers(self, clean_name: str) -> list[str]:
        """Search MPC Autofill for a card name (cached per run).

        Failed requests (rate limit, network) are not cached, so the next
        lookup tries again instead of treating the card as missing.
        """
        key = _normalize(clean_name)
        if key not in self._mpc_search_cache:
            self._mpc_search_many([clean_name])
        return self._mpc_search_cache.get(key, [])

    def _mpc_search_many(self, clean_names: list[str]) -> None:
        """Search many names with batched ``editorSearch`` calls (fills the cache)."""
        queries = list(
            dict.fromkeys(
                name.strip()
                for name in clean_names
                if name.strip() and _normalize(name) not in self._mpc_search_cache
            )
        )
        for start in range(0, len(queries), self.MPC_SEARCH_BATCH_SIZE):
            batch = queries[start : start + self.MPC_SEARCH_BATCH_SIZE]
            hits = self._mpc_editor_search(batch)
            if hits is None:
                continue  # failed: leave uncached so a later lookup retries
            for query in batch:
                self._mpc_search_cache[_normalize(query)] = hits.get(_normalize(query), [])

    def _mpc_editor_search(self, queries: list[str]) -> dict[str, list[str]] | None:
        """Run one MPC ``editorSearch`` call for several names.

        Returns ``{normalized name: identifiers}`` (names without hits are
        absent), or ``None`` when the request failed.
        """
        sources = self._mpc_source_settings()
        if sources is None:
            return None
        payload: dict[str, Any] = {
            "searchSettings": {
                "searchTypeSettings": {"fuzzySearch": True, "filterCardbacks": False},
                "sourceSettings": {"sources": sources},
                "filterSettings": {
                    "minimumDPI": 0,
                    "maximumDPI": 1500,
                    "maximumSize": 30,
                    "languages": [],
                    "includesTags": [],
                    "excludesTags": [],
                },
            },
            "queries": [{"query": query, "cardType": "CARD"} for query in queries],
        }
        logger.debug("MPC Autofill search for %d name(s)", len(queries))
        data = self._mpc_json(self._mpc_post(self.MPC_EDITOR_SEARCH_ENDPOINT, payload))
        results = data.get("results") if data is not None else None
        if not isinstance(results, dict):
            return None
        hits: dict[str, list[str]] = {}
        for raw_key, per_type in results.items():
            if isinstance(per_type, dict) and isinstance(per_type.get("CARD"), list):
                hits[_normalize(str(raw_key))] = [
                    h for h in per_type["CARD"] if isinstance(h, str) and h
                ]
        return hits

    def _mpc_source_settings(self) -> list[list[Any]] | None:
        """MPC ``[[source_pk, True], ...]`` search scope (fetched once per run)."""
        if self._mpc_sources_cache is None:
            self._mpc_sources_cache = self._fetch_mpc_sources()
        return self._mpc_sources_cache

    def _fetch_mpc_sources(self) -> list[list[Any]] | None:
        data = self._mpc_json(self._mpc_get(self.MPC_SOURCES_ENDPOINT))
        results = data.get("results") if data is not None else None
        if not isinstance(results, dict) or not results:
            logger.debug("MPC Autofill sources unavailable; skipping MPC Autofill for now")
            return None
        settings = [
            [entry["pk"], True]
            for entry in results.values()
            if isinstance(entry, dict) and isinstance(entry.get("pk"), int)
        ]
        return settings or None

    def _mpc_card(self, identifier: str) -> dict[str, Any] | None:
        """Fetch one MPC card document (cached per run)."""
        cards = self._mpc_cards([identifier])
        return cards[0] if cards else None

    def _mpc_cards(self, identifiers: list[str]) -> list[dict[str, Any]]:
        """Fetch MPC card documents in batches (cached per run, input order)."""
        missing = [i for i in dict.fromkeys(identifiers) if i not in self._mpc_card_cache]
        for start in range(0, len(missing), self.MPC_CARDS_BATCH_SIZE):
            batch = missing[start : start + self.MPC_CARDS_BATCH_SIZE]
            found = self._fetch_mpc_cards(batch)
            if found is None:
                continue  # failed: leave uncached so a later lookup retries
            for identifier in batch:
                self._mpc_card_cache[identifier] = found.get(identifier)
        cards = (self._mpc_card_cache.get(i) for i in identifiers)
        return [card for card in cards if card is not None]

    def _fetch_mpc_cards(self, identifiers: list[str]) -> dict[str, dict[str, Any]] | None:
        """``{identifier: card}`` for one ``/2/cards/`` call (``None`` if it failed)."""
        data = self._mpc_json(self._mpc_post(self.MPC_CARDS_ENDPOINT, {"cardIdentifiers": identifiers}))
        results = data.get("results") if data is not None else None
        if not isinstance(results, dict):
            return None
        wanted = set(identifiers)
        return {
            identifier: card
            for identifier, card in results.items()
            if identifier in wanted and isinstance(card, dict)
        }

    @staticmethod
    def _mpc_image_url(card: dict[str, Any]) -> str | None:
        """Best MPC image URL: full-res ``downloadLink``, then thumbnails."""
        for key in ("downloadLink", "mediumThumbnailUrl", "smallThumbnailUrl"):
            url = card.get(key)
            if isinstance(url, str) and url:
                return url
        return None

    def _mpc_dfc_pairs_map(self) -> dict[str, str]:
        """MPC double-faced ``{front: back}`` pairs (fetched once per run)."""
        if self._mpc_dfc_pairs is None:
            pairs = self._fetch_mpc_dfc_pairs()
            if pairs is None:
                return {}  # failed: retry on the next lookup
            self._mpc_dfc_pairs = pairs
        return self._mpc_dfc_pairs

    def _fetch_mpc_dfc_pairs(self) -> dict[str, str] | None:
        data = self._mpc_json(self._mpc_get(self.MPC_DFC_PAIRS_ENDPOINT))
        pairs = data.get("dfcPairs") if data is not None else None
        if not isinstance(pairs, dict):
            return None
        return {
            str(front): str(back)
            for front, back in pairs.items()
            if isinstance(front, str) and isinstance(back, str)
        }

    def _mpc_get(self, endpoint: str) -> requests.Response | None:
        return self._mpc_request("GET", endpoint)

    def _mpc_post(self, endpoint: str, payload: dict[str, Any]) -> requests.Response | None:
        return self._mpc_request("POST", endpoint, payload)

    def _mpc_request(
        self, method: str, endpoint: str, payload: dict[str, Any] | None = None
    ) -> requests.Response | None:
        """Send one MPC Autofill request, paced and retried on HTTP 429.

        mpcfill.com sits behind Cloudflare, which answers bursts (~10 calls
        in a few seconds) with HTTP 429 + ``Retry-After``: requests are
        spaced by ``mpc_min_request_interval`` and a 429 waits for the
        advertised delay before retrying.
        """
        url = f"{self.mpc_url}{endpoint}"
        for attempt in range(self.MPC_RATE_LIMIT_RETRIES + 1):
            self._mpc_throttle()
            try:
                if method == "POST":
                    resp = self._session.post(url, json=payload, timeout=self.timeout)
                else:
                    resp = self._session.get(url, timeout=self.timeout)
            except requests.RequestException as exc:
                logger.warning("MPC Autofill request to %s failed: %s", endpoint, exc)
                return None
            if resp.status_code != 429 or attempt == self.MPC_RATE_LIMIT_RETRIES:
                return resp
            retry_after = min(
                _parse_retry_after(resp.headers.get("Retry-After"), self.MPC_RATE_LIMIT_RETRY_SECONDS),
                self.MPC_MAX_RETRY_SECONDS,
            )
            logger.warning(
                "MPC Autofill rate limit hit for %s; retrying in %.0fs", endpoint, retry_after
            )
            time.sleep(retry_after)
        return None

    def _mpc_throttle(self) -> None:
        """Enforce the minimum interval between MPC Autofill requests."""
        if self.mpc_min_request_interval <= 0:
            return
        wait = self.mpc_min_request_interval - (time.monotonic() - self._mpc_last_request_at)
        if wait > 0:
            time.sleep(wait)
        self._mpc_last_request_at = time.monotonic()

    def _mpc_json(self, resp: requests.Response | None) -> dict[str, Any] | None:
        """JSON body of a successful MPC response (``None`` otherwise)."""
        if resp is None or resp.status_code != 200:
            if resp is not None:
                logger.debug("MPC Autofill returned HTTP %s", resp.status_code)
            return None
        try:
            data = resp.json()
        except (json.JSONDecodeError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    # ------------------------------------------------------------------
    # Resolution helpers (Scryfall + Moxfield fallbacks)
    # ------------------------------------------------------------------
    def _resolve_image_url(self, card_name: str, art: str | None = None) -> str | None:
        card = self._resolve_card(card_name, art)
        if card is not None:
            return self._extract_image_url(card)
        # Moxfield fallback (its results carry no back-face imagery).
        art_set, _, _, _ = _parse_mtg_art(art)
        clean_name, name_set, _ = _parse_card_reference(card_name)
        set_code = art_set if art_set is not None else name_set
        logger.debug("Card '%s' not found on Scryfall; falling back to Moxfield", card_name)
        return self._lookup_moxfield(card_name, set_code)

    def _resolve_card(
        self, card_name: str, art: str | None = None
    ) -> dict[str, Any] | None:
        """Resolve a Scryfall card object (cached per run).

        ``art`` must already be the effective marker (see
        :func:`_effective_art`). Returns ``None`` when Scryfall cannot
        resolve the card (the caller then tries Moxfield, front only).
        """
        key = (_normalize(card_name), art)
        if key not in self._card_cache:
            self._card_cache[key] = self._lookup_scryfall_card(card_name, art)
        return self._card_cache[key]

    def _lookup_scryfall_card(
        self, card_name: str, art: str | None = None
    ) -> dict[str, Any] | None:
        """Run the Scryfall resolution steps, returning the card object."""
        art_set, art_collector, art_variant, art_mode = _parse_mtg_art(art)
        clean_name, name_set, name_collector = _parse_card_reference(card_name)
        # An [art] set pins the printing: its collector (if any) wins and the
        # decklist collector number must not leak into another set.
        if art_set is not None:
            set_code = art_set
            collector_number: str | None = art_collector
        else:
            set_code = name_set
            collector_number = name_collector

        # 1. Exact printing via the collector endpoint.
        if set_code is not None and collector_number is not None:
            card = self._lookup_scryfall_collector(set_code, collector_number, clean_name)
            if card:
                return card
            logger.debug(
                "Card '%s' not found at %s:%s; falling back to name lookup",
                card_name,
                set_code,
                collector_number,
            )

        # 2. Variant / base requests need the full prints list.
        if art_variant is not None or art_mode == "base":
            card = self._lookup_scryfall_prints(
                clean_name, set_code, art_variant, art_mode, card_name
            )
            if card:
                return card
            logger.info(
                "Card '%s' has no matching '%s' printing; using default art",
                card_name,
                art_variant or art_mode,
            )

        card = self._lookup_scryfall_named(clean_name, set_code)
        if not card:
            logger.debug(
                "Card '%s' not found via /cards/named; falling back to /cards/search",
                card_name,
            )
            card = self._lookup_scryfall_search(clean_name)
        return card

    # ------------------------------------------------------------------
    # Source 1: Scryfall /cards/named (exact + fuzzy, optional set filter)
    # ------------------------------------------------------------------
    def _lookup_scryfall_named(
        self, card_name: str, set_code: str | None
    ) -> dict[str, Any] | None:
        attempts: list[dict[str, str]] = []
        if set_code:
            attempts.append({"exact": card_name, "set": set_code})
            attempts.append({"fuzzy": card_name, "set": set_code})
        attempts.append({"exact": card_name})
        attempts.append({"fuzzy": card_name})
        for params in attempts:
            match_type = "exact" if "exact" in params else "fuzzy"
            logger.debug("Scryfall named lookup for '%s' (%s)", card_name, match_type)
            card = self._api_get(self.NAMED_ENDPOINT, params)
            if card:
                return card
        return None

    # ------------------------------------------------------------------
    # Source 1a: Scryfall collector endpoint (exact printing)
    # ------------------------------------------------------------------
    def _lookup_scryfall_collector(
        self, set_code: str, collector_number: str, expected_name: str | None = None
    ) -> dict[str, Any] | None:
        """Fetch an exact printing via ``GET /cards/<set>/<collector>``.

        The endpoint returns whatever lives at that slot, so when
        ``expected_name`` is given the returned card is checked against it
        (front face included for double-faced cards, and the printing's
        alternate ``flavor_name`` too: Secret Lair / Universes Beyond decks
        list ``Chaos Emerald (SLD) 7037`` for that Lotus Petal). On a
        mismatch ``None`` is returned and the caller falls back to the name
        lookup instead of silently downloading the wrong card.
        """
        endpoint = (
            f"/cards/{requests.utils.quote(set_code.lower())}"
            f"/{requests.utils.quote(collector_number.lower())}"
        )
        logger.debug("Scryfall collector lookup for '%s:%s'", set_code, collector_number)
        card = self._api_get(endpoint, {})
        if not card:
            return None
        if expected_name is not None and not _names_match(card, expected_name):
            logger.warning(
                "Scryfall %s:%s is '%s', not '%s'; ignoring the collector number and "
                "falling back to name lookup.",
                set_code.lower(),
                collector_number.lower(),
                card.get("name"),
                expected_name,
            )
            return None
        return card

    # ------------------------------------------------------------------
    # Source 1b: Scryfall prints search (variant / base art selection)
    # ------------------------------------------------------------------
    def _lookup_scryfall_prints(
        self,
        card_name: str,
        set_code: str | None,
        variant: str | None,
        mode: str | None,
        original_name: str,
    ) -> dict[str, Any] | None:
        """Resolve a variant/base request via ``/cards/search`` ``unique:prints``.

        All printings of ``card_name`` are fetched (newest first) and filtered
        client-side by set and by Scryfall card fields (``full_art``,
        ``border_color``, ``frame_effects``, ``promo``...). Returns ``None``
        when nothing matches so the caller can fall back to default art.
        """
        query = f'!"{card_name}"'
        if set_code:
            query += f" e:{set_code.lower()}"
        logger.debug("Scryfall prints search for '%s' (variant=%s mode=%s)", query, variant, mode)
        prints = self._search_all_prints(query)
        if not prints:
            return None
        if set_code:
            in_set = [c for c in prints if str(c.get("set", "")).lower() == set_code.lower()]
            if in_set:
                prints = in_set
            else:
                logger.debug("No '%s' printings in set '%s'", card_name, set_code)
                return None
        if variant is not None:
            matching = [c for c in prints if _matches_variant(c, variant)]
            if not matching:
                logger.debug("No '%s' variant for '%s'", variant, original_name)
                return None
            prints = matching
        elif mode == "base":
            base_prints = [c for c in prints if _is_base_print(c)]
            if not base_prints:
                logger.debug("No base printing for '%s'", original_name)
                return None
            prints = base_prints
        picked = prints[0]
        logger.debug(
            "Card '%s' art matched '%s' (%s #%s variant=%s)",
            original_name,
            picked.get("name"),
            picked.get("set"),
            picked.get("collector_number"),
            variant or mode,
        )
        return picked

    def _search_all_prints(self, query: str) -> list[dict[str, Any]]:
        """Fetch every page of a ``unique:prints`` search (newest first)."""
        prints: list[dict[str, Any]] = []
        params: dict[str, str] = {"q": query, "unique": "prints", "order": "released"}
        next_page: str | None = None
        for _ in range(5):  # safety cap: 5 x 175 prints is plenty
            if next_page:
                page = self._api_get_page(next_page)
            else:
                page = self._api_get(self.SEARCH_ENDPOINT, params)
            if not page:
                break
            data = page.get("data")
            if isinstance(data, list):
                prints.extend(c for c in data if isinstance(c, dict))
            if page.get("has_more") and isinstance(page.get("next_page"), str):
                next_page = page["next_page"]
            else:
                break
        return prints

    def _api_get_page(self, url: str) -> dict[str, Any] | None:
        """GET an absolute Scryfall pagination URL (rate-limited)."""
        self._throttle()
        try:
            resp = self._session.get(url, timeout=self.timeout)
        except requests.RequestException as exc:
            logger.warning("Scryfall request to %s failed: %s", url, exc)
            return None
        if resp.status_code != 200:
            return None
        try:
            data = resp.json()
        except (json.JSONDecodeError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    # ------------------------------------------------------------------
    # Source 2: Scryfall /cards/search fallback
    # ------------------------------------------------------------------
    def _lookup_scryfall_search(self, card_name: str) -> dict[str, Any] | None:
        card = self._api_get(self.SEARCH_ENDPOINT, {"q": card_name})
        if not card:
            return None
        data = card.get("data")
        if not isinstance(data, list) or not data or not isinstance(data[0], dict):
            return None
        return data[0]

    # ------------------------------------------------------------------
    # Source 3: Moxfield fallback
    # ------------------------------------------------------------------
    def _lookup_moxfield(self, card_name: str, set_code: str | None = None) -> str | None:
        """Resolve a card via Moxfield when Scryfall fails.

        Moxfield's card search API (the JSON backend of
        ``https://moxfield.com/cards/search``) returns each match with its
        internal Moxfield ``id``, which is both the slug of the card page
        (``/cards/<id>-<name>``) and the key of its CDN images
        (``assets.moxfield.net/cards/card-<id>-normal.jpg``, the URL behind
        the page's "Download Image" button).

        ``set_code`` is the effective set (decklist annotation and/or ``[art]``
        marker); variant requests cannot be honored here because Moxfield
        search results carry no frame/variant fields.
        """
        clean_name, name_set, _collector = _parse_card_reference(card_name)
        effective_set = set_code or name_set
        results = self._moxfield_search(clean_name)
        if not results:
            return None
        card = self._select_moxfield_result(results, clean_name, effective_set)
        if card is None:
            return None
        logger.debug(
            "Card '%s' matched '%s' (%s #%s) on Moxfield",
            card_name,
            card.get("name"),
            card.get("set_name"),
            card.get("cn"),
        )
        return self._moxfield_image_url(card.get("id"))

    def _moxfield_search(self, card_name: str) -> list[dict[str, Any]]:
        try:
            resp = self._session.get(
                self.MOXFIELD_SEARCH_API_URL, params={"q": card_name}, timeout=self.timeout
            )
        except requests.RequestException as exc:
            logger.warning("Moxfield search request failed: %s", exc)
            return []
        if resp.status_code != 200:
            logger.debug("Moxfield search returned HTTP %s", resp.status_code)
            return []
        try:
            data = resp.json()
        except (json.JSONDecodeError, ValueError):
            return []
        results = data.get("data")
        if not isinstance(results, list):
            return []
        return [r for r in results if isinstance(r, dict)]

    @staticmethod
    def _select_moxfield_result(
        results: list[dict[str, Any]],
        card_name: str,
        set_code: str | None,
    ) -> dict[str, Any] | None:
        """Pick the Moxfield search result that best matches ``card_name``.

        Exact (normalized) name matches win, preferring the printing whose
        set code matches the decklist annotation. Without an exact match the
        first hit is used, with a warning when several candidates exist.
        """
        key = _normalize(card_name)
        exact = [r for r in results if _normalize(str(r.get("name", ""))) == key]
        if exact:
            if set_code:
                for r in exact:
                    if str(r.get("set", "")).lower() == set_code:
                        return r
            return exact[0]
        if len(results) > 1:
            logger.warning(
                "Card '%s' is ambiguous on Moxfield (%d hits, e.g. '%s', '%s'); "
                "using the first hit '%s'. Add the set code to disambiguate.",
                card_name,
                len(results),
                results[0].get("name"),
                results[1].get("name"),
                results[0].get("name"),
            )
        return results[0]

    def _moxfield_image_url(self, card_id: Any) -> str | None:
        """Build the Moxfield CDN image URL for a card id.

        ``normal`` is the full-card image used by the "Download Image"
        button; ``art_crop`` is the artwork-only fallback.
        """
        if not isinstance(card_id, str) or not card_id:
            return None
        for variant in ("normal", "art_crop"):
            url = f"{self.MOXFIELD_ASSETS_URL}/card-{card_id}-{variant}.jpg"
            if self._asset_exists(url):
                return url
        return None

    def _asset_exists(self, url: str) -> bool:
        try:
            resp = self._session.head(url, timeout=self.timeout, allow_redirects=True)
        except requests.RequestException:
            return False
        return resp.status_code == 200

    # ------------------------------------------------------------------
    # Card object -> image URL
    # ------------------------------------------------------------------
    def _extract_image_url(self, card: dict[str, Any]) -> str | None:
        """Extract the preferred image URL from a Scryfall Card object.

        Double-faced cards (transform, modal DFC...) carry ``image_uris`` on
        each entry of ``card_faces`` instead of the top level, so the front
        face is used in that case. Cards whose ``image_status`` is
        ``missing`` have no usable imagery yet.
        """
        if card.get("image_status") == "missing":
            return None
        image_uris = card.get("image_uris")
        if not isinstance(image_uris, dict):
            faces = card.get("card_faces")
            if isinstance(faces, list) and faces and isinstance(faces[0], dict):
                image_uris = faces[0].get("image_uris")
        if not isinstance(image_uris, dict):
            return None
        for fmt in self._image_format_candidates():
            url = image_uris.get(fmt)
            if isinstance(url, str) and url:
                return url
        return None

    def _extract_back_image_url(self, card: dict[str, Any]) -> str | None:
        """Extract the back-face image URL from a Scryfall Card object.

        Only faces carrying their own imagery count (transform, modal
        DFC... via ``card_faces[1].image_uris``). Split / flip / adventure
        cards expose a single top-level image — their faces have no
        ``image_uris`` — so they correctly yield ``None`` here.
        """
        if card.get("image_status") == "missing":
            return None
        faces = card.get("card_faces")
        if (
            not isinstance(faces, list)
            or len(faces) < 2
            or not isinstance(faces[1], dict)
        ):
            return None
        image_uris = faces[1].get("image_uris")
        if not isinstance(image_uris, dict):
            return None
        for fmt in self._image_format_candidates():
            url = image_uris.get(fmt)
            if isinstance(url, str) and url:
                return url
        return None

    def _image_format_candidates(self) -> list[str]:
        candidates = [self.image_format]
        candidates.extend(f for f in self.IMAGE_FORMAT_FALLBACKS if f != self.image_format)
        return candidates

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------
    def _throttle(self) -> None:
        """Enforce the minimum interval between Scryfall API requests."""
        if self.min_request_interval <= 0:
            return
        wait = self.min_request_interval - (time.monotonic() - self._last_request_at)
        if wait > 0:
            time.sleep(wait)
        self._last_request_at = time.monotonic()

    def _api_get(self, endpoint: str, params: dict[str, str]) -> dict[str, Any] | None:
        """GET a Scryfall API endpoint, honoring rate limits and 429s."""
        url = f"{self.api_url}{endpoint}"
        resp = self._send_api_request(url, params)
        if resp is None:
            return None
        if resp.status_code == 429:
            retry_after = _parse_retry_after(
                resp.headers.get("Retry-After"), self.RATE_LIMIT_RETRY_SECONDS
            )
            logger.warning(
                "Scryfall rate limit hit for %s; retrying in %.1fs", endpoint, retry_after
            )
            time.sleep(retry_after)
            resp = self._send_api_request(url, params)
            if resp is None:
                return None
        if resp.status_code != 200:
            logger.debug(
                "Scryfall %s returned HTTP %s: %s",
                endpoint,
                resp.status_code,
                _error_details(resp),
            )
            return None
        try:
            data = resp.json()
        except (json.JSONDecodeError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def _send_api_request(self, url: str, params: dict[str, str]) -> requests.Response | None:
        self._throttle()
        try:
            return self._session.get(url, params=params, timeout=self.timeout)
        except requests.RequestException as exc:
            logger.warning("Scryfall request to %s failed: %s", url, exc)
            return None

    def _download_image(self, image_url: str, output_path: str) -> bool:
        try:
            resp = self._session.get(image_url, timeout=self.timeout, stream=True)
        except requests.RequestException:
            return False
        if resp.status_code != 200:
            return False
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
        return out.exists() and out.stat().st_size > 0


# ----------------------------------------------------------------------
# Module-level helpers
# ----------------------------------------------------------------------
_SET_ANNOTATION = re.compile(
    r"\s*(?:\*+\s*[A-Za-z0-9]{0,3}\s*\*+\s*)?"                  # premium marker, e.g. *F*
    r"(?:[\(\[]\s*(?P<set>[A-Za-z0-9]{1,8})\s*[\)\]]\s*)?"     # set code: (2x2) or [MH2]
    r"(?:#?\s*(?P<collector>(?:\d{1,4}[A-Za-z\u2605]{0,2}|\u2605))\s*)?"  # collector number, e.g. 253s or 181a
    r"(?:\*+\s*[A-Za-z0-9]{0,3}\s*\*+\s*)?"                    # premium marker, e.g. *F*
    r"$"
)


def _normalize(name: str) -> str:
    """Lowercase + whitespace-normalized name for comparisons."""
    return re.sub(r"\s+", " ", name.strip().lower())


def _normalize_art_key(art: str) -> str:
    """Normalized art marker (lowercase, single spaces)."""
    return re.sub(r"\s+", " ", art.strip().lower())


def _mpc_identifier(art: str | None) -> str | None:
    """MPC Autofill identifier of a pinned ``mpc:<identifier>`` marker."""
    if art and art[: len(MPC_ART_PREFIX)].lower() == MPC_ART_PREFIX:
        return art[len(MPC_ART_PREFIX) :].strip() or None
    return None


def _scryfall_art(art: str | None) -> str | None:
    """Scryfall ``[art]`` marker, dropping a legacy ``scryfall:`` prefix."""
    if art and art[: len(SCRYFALL_ART_PREFIX)].lower() == SCRYFALL_ART_PREFIX:
        return art[len(SCRYFALL_ART_PREFIX) :].strip() or None
    return art


def _mpc_dpi(card: dict[str, Any]) -> int:
    dpi = card.get("dpi")
    return dpi if isinstance(dpi, int) else 0


def _is_mpc_stand_in(card: dict[str, Any]) -> bool:
    """True for MPC checklist / placeholder images (not the real card art)."""
    return bool(_MPC_STAND_IN.search(str(card.get("name") or "")))


def _mpc_quality(card: dict[str, Any]) -> str:
    """``"1200 DPI · 3.7 MB · MrTeferi"``-style summary of an MPC image."""
    parts = [f"{_mpc_dpi(card)} DPI" if _mpc_dpi(card) else "DPI ?"]
    size = card.get("size")
    if isinstance(size, int) and size > 0:
        parts.append(f"{size / 1_000_000:.1f} MB")
    source = card.get("sourceName") or card.get("source")
    if source:
        parts.append(str(source))
    return " · ".join(parts)


def _log_mpc_choice(card_name: str, card: dict[str, Any]) -> None:
    logger.info(
        "Card '%s' -> MPC Autofill '%s' (%s)",
        card_name,
        card.get("name"),
        _mpc_quality(card),
    )


def _mpc_art_option(card: dict[str, Any]) -> ArtOption | None:
    """Build the art-picker option of one MPC Autofill image."""
    identifier = card.get("identifier")
    # w400 previews: plenty for the picker grid and ~4x lighter than w800.
    image_url = card.get("smallThumbnailUrl") or card.get("mediumThumbnailUrl")
    if not isinstance(identifier, str) or not identifier or not isinstance(image_url, str):
        return None
    label = f"MPC · {_mpc_quality(card)} · {card.get('name')}"
    return ArtOption(value=f"{MPC_ART_PREFIX}{identifier}", label=label, image_url=image_url)


def _effective_art(card_name: str, art: str | None) -> str | None:
    """Validate an ``[art]`` marker for MTG, warning on Lorcana-only values."""
    if art is None:
        return None
    normalized = _normalize_art_key(art)
    # Split off a possible "set[:collector] variant" combo and check the
    # variant/mode token for Lorcana-only keywords.
    tokens = normalized.split(" ")
    for tok in tokens:
        if tok in _LORCANA_ONLY_ART:
            logger.warning(
                "Art marker '[%s]' on card '%s' is Lorcana-only; using default MTG art.",
                art,
                card_name,
            )
            return None
    if normalized in MTG_VARIANT_CHOICES or normalized in MTG_ART_MODES:
        return normalized
    # Set / set:collector / set + variant combos (validated by the parser,
    # re-validated here for direct API calls).
    parsed = _parse_mtg_art(normalized)
    if parsed == (None, None, None, None) and normalized not in ("best", None):
        logger.warning(
            "Unknown art option '[%s]' on card '%s'; using default MTG art.",
            art,
            card_name,
        )
        return None
    return normalized


def _parse_mtg_art(art: str | None) -> tuple[str | None, str | None, str | None, str | None]:
    """Split a normalized ``[art]`` marker into ``(set, collector, variant, mode)``.

    Accepted (already lowercased by the parser):
        ``best`` / ``base`` -> mode
        ``fullart`` / ``borderless`` / ``showcase`` / ``extended`` /
        ``retro`` / ``promo`` -> variant
        ``m21`` -> set
        ``2x2:117`` -> set + collector
        ``m21 borderless`` / ``2x2:117 showcase`` -> set (+collector) + variant
    Returns ``(None, None, None, None)`` for ``None``/``best``/unrecognized.
    """
    if art is None:
        return None, None, None, None
    text = _normalize_art_key(art)
    if text in ("", "best"):
        return None, None, None, "best"
    if text == "base":
        return None, None, None, "base"
    if text in MTG_VARIANT_CHOICES:
        return None, None, text, None

    parts = text.split(" ")
    if len(parts) == 2:
        first, second = parts
        # "set variant" or "variant set" or "set:collector variant" ...
        if first in MTG_VARIANT_CHOICES and _SET_ONLY.match(second):
            return second, None, first, None
        if second in MTG_VARIANT_CHOICES and _SET_ONLY.match(first):
            return first, None, second, None
        if first in MTG_VARIANT_CHOICES and _SET_COLLECTOR_JOINED.match(second):
            m = _SET_COLLECTOR_JOINED.match(second)
            assert m is not None
            return m.group("set").lower(), m.group("collector").lower(), first, None
        if second in MTG_VARIANT_CHOICES and _SET_COLLECTOR_JOINED.match(first):
            m = _SET_COLLECTOR_JOINED.match(first)
            assert m is not None
            return m.group("set").lower(), m.group("collector").lower(), second, None
        if second in MTG_ART_MODES and _SET_ONLY.match(first):
            return first, None, None, second
        if first in MTG_ART_MODES and _SET_ONLY.match(second):
            return second, None, None, first
        if second in MTG_ART_MODES and _SET_COLLECTOR_JOINED.match(first):
            m = _SET_COLLECTOR_JOINED.match(first)
            assert m is not None
            return m.group("set").lower(), m.group("collector").lower(), None, second
        if first in MTG_ART_MODES and _SET_COLLECTOR_JOINED.match(second):
            m = _SET_COLLECTOR_JOINED.match(second)
            assert m is not None
            return m.group("set").lower(), m.group("collector").lower(), None, first
    if len(parts) == 3:
        # Defensive: "variant set collector" with spaces (the parser already
        # normalizes these to "set:collector variant").
        for i, tok in enumerate(parts):
            if tok in MTG_VARIANT_CHOICES or tok in MTG_ART_MODES:
                rest = [parts[j] for j in range(3) if j != i]
                if _SET_ONLY.match(rest[0]) and _COLLECTOR_ONLY.match(rest[1]):
                    if tok in MTG_VARIANT_CHOICES:
                        return rest[0].lower(), rest[1].lower(), tok, None
                    return rest[0].lower(), rest[1].lower(), None, tok
                if _SET_ONLY.match(rest[1]) and _COLLECTOR_ONLY.match(rest[0]):
                    if tok in MTG_VARIANT_CHOICES:
                        return rest[1].lower(), rest[0].lower(), tok, None
                    return rest[1].lower(), rest[0].lower(), None, tok
    if len(parts) == 1:
        if _SET_ONLY.match(text):
            return text, None, None, None
        m = _SET_COLLECTOR_JOINED.match(text)
        if m:
            return m.group("set").lower(), m.group("collector").lower(), None, None
        if " " not in text and ":" in text:
            # Defensive: "set:collector" with odd spacing already handled.
            left, _, right = text.partition(":")
            if _SET_ONLY.match(left.strip()) and _COLLECTOR_ONLY.match(right.strip()):
                return left.strip(), right.strip(), None, None
    # Three-token "variant set collector" combos are normalized by the parser
    # to "set:collector variant", so reaching here means unrecognized.
    return None, None, None, None


def _matches_variant(card: dict[str, Any], variant: str) -> bool:
    """Check a Scryfall Card object against a variant keyword."""
    frame_effects = [str(e).lower() for e in (card.get("frame_effects") or []) if isinstance(e, str)]
    border = str(card.get("border_color") or "").lower()
    frame = str(card.get("frame") or "")
    if variant == "fullart":
        return bool(card.get("full_art"))
    if variant == "borderless":
        return border == "borderless"
    if variant == "showcase":
        return "showcase" in frame_effects
    if variant == "extended":
        return "extendedart" in frame_effects
    if variant == "retro":
        return frame in ("1993", "1997")
    if variant == "promo":
        return bool(card.get("promo"))
    return False


def _art_option(card: dict[str, Any]) -> ArtOption | None:
    """Build the art-picker option of one Scryfall printing (``None`` if unusable)."""
    set_code = str(card.get("set") or "").lower()
    collector = str(card.get("collector_number") or "").lower()
    value = f"{set_code}:{collector}"
    if not _SET_COLLECTOR_JOINED.match(value) or card.get("image_status") == "missing":
        return None
    image_uris = card.get("image_uris")
    if not isinstance(image_uris, dict):
        faces = card.get("card_faces")
        if isinstance(faces, list) and faces and isinstance(faces[0], dict):
            image_uris = faces[0].get("image_uris")
    if not isinstance(image_uris, dict):
        return None
    image_url = image_uris.get("normal") or image_uris.get("small")
    if not isinstance(image_url, str) or not image_url:
        return None
    label = f"Scryfall · ~300 DPI · {card.get('set_name') or set_code.upper()} ({set_code.upper()}) #{collector}"
    variants = [v for v in MTG_VARIANT_CHOICES if _matches_variant(card, v)]
    if variants:
        label += " · " + ", ".join(variants)
    if card.get("flavor_name") and card.get("name"):
        # Alternate-name printing (e.g. SLD "Chaos Emerald"): show the real card.
        label += f" · {card['name']}"
    return ArtOption(value=value, label=label, image_url=image_url)


def _is_base_print(card: dict[str, Any]) -> bool:
    """Standard (non-premium) printing: no promo, no alt-art treatments."""
    if card.get("promo"):
        return False
    if card.get("full_art"):
        return False
    if str(card.get("border_color") or "").lower() not in ("black", "white"):
        return False
    frame_effects = [str(e).lower() for e in (card.get("frame_effects") or []) if isinstance(e, str)]
    return "showcase" not in frame_effects and "extendedart" not in frame_effects


def _parse_card_reference(card_name: str) -> tuple[str, str | None, str | None]:
    """Split a decklist card name into ``(clean_name, set_code, collector)``.

    Handles the trailing annotations found in MTG deck exports
    (Manabox, MTGO, Arena...):
        ``Barad-dûr (PLTR) 253s *F*``  ->  ``("Barad-dûr", "pltr", "253s")``
        ``Lightning Bolt (2x2) 117``   ->  ``("Lightning Bolt", "2x2", "117")``
        ``Counterspell [MH2]``         ->  ``("Counterspell", "mh2", None)``
        ``Lightning Bolt``             ->  ``("Lightning Bolt", None, None)``

    where ``*F*``/``*G*``/``*S*`` are foil/premium markers and collector
    numbers may carry letter suffixes (``253s``, ``181a``) or a star.
    """
    name = card_name.strip()
    match = _SET_ANNOTATION.search(name)
    if not match:
        return name, None, None
    set_code = match.group("set")
    collector = match.group("collector")
    clean = name[: match.start()].strip()
    if not clean:
        return name, None, None
    return (
        clean,
        set_code.lower() if set_code else None,
        collector.lower() if collector else None,
    )


_SET_CODE = r"[a-z0-9]{2,5}"
_COLLECTOR = r"(?:\d{1,4}[a-z\u2605]{0,2}|\u2605)"
_SET_COLLECTOR_JOINED = re.compile(
    rf"^(?P<set>{_SET_CODE})\s*[:\-#]\s*(?P<collector>{_COLLECTOR})$",
    re.IGNORECASE,
)
_SET_ONLY = re.compile(rf"^(?P<set>{_SET_CODE})$", re.IGNORECASE)
_COLLECTOR_ONLY = re.compile(rf"^(?P<collector>{_COLLECTOR})$", re.IGNORECASE)


def _names_match(card: dict[str, Any], expected_name: str) -> bool:
    """Check a Scryfall card against the requested name.

    Accepted: the Oracle name or either face of a double-faced card
    (``"Front // Back"``), and the printing's alternate ``flavor_name``
    (card-level or per face), e.g. ``Chaos Emerald`` for SLD's Lotus Petal.
    """
    return _normalize(expected_name) in _card_names(card)


def _card_names(card: dict[str, Any]) -> set[str]:
    """Normalized names a printing answers to (Oracle, faces, flavor names)."""
    sources = [card]
    faces = card.get("card_faces")
    if isinstance(faces, list):
        sources.extend(face for face in faces if isinstance(face, dict))
    names: set[str] = set()
    for source in sources:
        for key in ("name", "flavor_name"):
            value = source.get(key)
            if isinstance(value, str):
                names.update(_normalize(part) for part in value.split("//"))
    names.discard("")
    return names


def _parse_retry_after(value: str | None, default: float) -> float:
    """Parse a ``Retry-After`` header (seconds); fall back to ``default``."""
    if value is None:
        return default
    try:
        return max(0.0, float(value))
    except ValueError:
        return default


def _error_details(resp: requests.Response) -> str:
    try:
        data = resp.json()
    except (json.JSONDecodeError, ValueError):
        return resp.reason or ""
    return data.get("details") or resp.reason or ""
