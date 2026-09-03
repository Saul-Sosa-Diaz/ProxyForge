"""MTG image-fetching strategy.

Resolution order (Scryfall API, https://scryfall.com/docs/api, then
Moxfield, https://moxfield.com):
    1. Scryfall card name lookup (``GET /cards/named``). The decklist name
       is tried with an exact match first and a fuzzy match second; set
       annotations from the decklist (``Lightning Bolt (2x2) 117``,
       ``Counterspell [MH2]``) are parsed and forwarded as the ``set``
       parameter. Resolved names are cached locally (name -> image URL) to
       avoid repeated API calls on subsequent runs, as recommended by
       Scryfall's guidelines.
    2. Scryfall card search fallback (``GET /cards/search?q=<name>``) for
       names the named-lookup endpoint cannot resolve (typos, extra
       tokens...).
    3. Moxfield fallback (``GET https://api.moxfield.com/v2/cards/search``)
       when Scryfall cannot resolve the card at all. The matched card's
       internal Moxfield id feeds the assets CDN image that the
       "Download Image" button on ``https://moxfield.com/cards/<id>-<slug>``
       pages points to
       (``https://assets.moxfield.net/cards/card-<id>-normal.jpg``).

Images are served by the Scryfall image CDN (``cards.scryfall.io``) in the
versions documented at https://scryfall.com/docs/api/images. By default the
``png`` version is used (744x1040, highest quality); the remaining versions
(``large``, ``normal``, ``border_crop``, ``small``...) serve as fallbacks.

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

from .base import TCGStrategy

logger = logging.getLogger(__name__)


class MTGStrategy(TCGStrategy):
    """Fetch MTG card images via the Scryfall API with a local URL cache."""

    name = "mtg"

    SCRYFALL_API_URL = "https://api.scryfall.com"
    NAMED_ENDPOINT = "/cards/named"
    SEARCH_ENDPOINT = "/cards/search"
    MOXFIELD_SEARCH_API_URL = "https://api.moxfield.com/v2/cards/search"
    MOXFIELD_ASSETS_URL = "https://assets.moxfield.net/cards"
    DEFAULT_TIMEOUT = 30
    # Scryfall limits /cards/* to 2 requests/second (500 ms between calls).
    DEFAULT_MIN_REQUEST_INTERVAL = 0.5
    IMAGE_FORMAT_FALLBACKS = ("png", "large", "normal", "border_crop", "small")
    # Scryfall blocks access for 30 seconds after an HTTP 429.
    RATE_LIMIT_RETRY_SECONDS = 30.0

    def __init__(
        self,
        cache_path: str | None = None,
        api_url: str | None = None,
        timeout: int = DEFAULT_TIMEOUT,
        refresh_db: bool = False,
        cache_ttl_seconds: float | None = None,
        image_format: str = "png",
        min_request_interval: float = DEFAULT_MIN_REQUEST_INTERVAL,
    ) -> None:
        self.cache_path = Path(cache_path) if cache_path else Path("data/mtg_cache.json")
        self.api_url = (api_url or self.SCRYFALL_API_URL).rstrip("/")
        self.timeout = timeout
        self.refresh_db = refresh_db
        self.cache_ttl_seconds = cache_ttl_seconds
        self.image_format = image_format
        self.min_request_interval = max(0.0, min_request_interval)
        self._cache: dict[str, str] | None = None
        self._last_request_at = 0.0
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
    def fetch_card_image(self, card_name: str, output_path: str) -> bool:
        image_url = self._resolve_image_url(card_name)
        if not image_url:
            return False
        return self._download_image(image_url, output_path)

    # ------------------------------------------------------------------
    # Resolution helpers
    # ------------------------------------------------------------------
    def _resolve_image_url(self, card_name: str) -> str | None:
        url = self._lookup_cache(card_name)
        if url:
            return url
        clean_name, set_code = _parse_card_reference(card_name)
        url = self._lookup_scryfall_named(clean_name, set_code)
        if not url:
            logger.debug(
                "Card '%s' not found via /cards/named; falling back to /cards/search",
                card_name,
            )
            url = self._lookup_scryfall_search(clean_name)
        if not url:
            logger.debug(
                "Card '%s' not found on Scryfall; falling back to Moxfield", card_name
            )
            url = self._lookup_moxfield(card_name)
        if url:
            self._store_cache(card_name, url)
        return url

    # ------------------------------------------------------------------
    # Local cache (resolved name -> image URL)
    # ------------------------------------------------------------------
    def _lookup_cache(self, card_name: str) -> str | None:
        cache = self._get_cache()
        for key in _candidate_keys(card_name):
            if key in cache:
                return cache[key]
        return None

    def _store_cache(self, card_name: str, url: str) -> None:
        cache = self._get_cache()
        added = False
        for key in _candidate_keys(card_name):
            if key not in cache:
                cache[key] = url
                added = True
        if added:
            self._save_cache()

    def _get_cache(self) -> dict[str, str]:
        if self._cache is None:
            self._cache = self._load_cache() or {}
        return self._cache

    def _load_cache(self) -> dict[str, str] | None:
        if self.refresh_db or not self.cache_path.exists():
            return None
        if self.cache_ttl_seconds is not None:
            age = time.time() - self.cache_path.stat().st_mtime
            if age > self.cache_ttl_seconds:
                return None
        try:
            with self.cache_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to read Scryfall cache '%s': %s", self.cache_path, exc)
            return None
        if not isinstance(data, dict):
            return None
        logger.info("Loaded Scryfall cache from %s (%d entries)", self.cache_path, len(data))
        return {str(k): str(v) for k, v in data.items() if isinstance(v, str)}

    def _save_cache(self) -> None:
        if self._cache is None:
            return
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self.cache_path.open("w", encoding="utf-8") as f:
                json.dump(self._cache, f, indent=2)
            logger.info("Saved Scryfall cache to %s", self.cache_path)
        except OSError as exc:
            logger.warning("Failed to write Scryfall cache '%s': %s", self.cache_path, exc)

    # ------------------------------------------------------------------
    # Source 1: Scryfall /cards/named (exact + fuzzy, optional set filter)
    # ------------------------------------------------------------------
    def _lookup_scryfall_named(self, card_name: str, set_code: str | None) -> str | None:
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
                return self._extract_image_url(card)
        return None

    # ------------------------------------------------------------------
    # Source 2: Scryfall /cards/search fallback
    # ------------------------------------------------------------------
    def _lookup_scryfall_search(self, card_name: str) -> str | None:
        card = self._api_get(self.SEARCH_ENDPOINT, {"q": card_name})
        if not card:
            return None
        data = card.get("data")
        if not isinstance(data, list) or not data or not isinstance(data[0], dict):
            return None
        return self._extract_image_url(data[0])

    # ------------------------------------------------------------------
    # Source 3: Moxfield fallback
    # ------------------------------------------------------------------
    def _lookup_moxfield(self, card_name: str) -> str | None:
        """Resolve a card via Moxfield when Scryfall fails.

        Moxfield's card search API (the JSON backend of
        ``https://moxfield.com/cards/search``) returns each match with its
        internal Moxfield ``id``, which is both the slug of the card page
        (``/cards/<id>-<name>``) and the key of its CDN images
        (``assets.moxfield.net/cards/card-<id>-normal.jpg``, the URL behind
        the page's "Download Image" button).
        """
        clean_name, set_code = _parse_card_reference(card_name)
        results = self._moxfield_search(clean_name)
        if not results:
            return None
        card = self._select_moxfield_result(results, clean_name, set_code)
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
    """Lowercase + whitespace-normalized cache key."""
    return re.sub(r"\s+", " ", name.strip().lower())


def _parse_card_reference(card_name: str) -> tuple[str, str | None]:
    """Split a decklist card name into ``(clean_name, set_code)``.

    Handles the trailing annotations found in MTG deck exports
    (Manabox, MTGO, Arena...):
        ``Barad-dûr (PLTR) 253s *F*``  ->  ``("Barad-dûr", "pltr")``
        ``Lightning Bolt (2x2) 117``   ->  ``("Lightning Bolt", "2x2")``
        ``Counterspell [MH2]``         ->  ``("Counterspell", "mh2")``
        ``Lightning Bolt``             ->  ``("Lightning Bolt", None)``

    where ``*F*``/``*G*``/``*S*`` are foil/premium markers and collector
    numbers may carry letter suffixes (``253s``, ``181a``) or a star.
    """
    name = card_name.strip()
    match = _SET_ANNOTATION.search(name)
    if not match:
        return name, None
    set_code = match.group("set")
    clean = name[: match.start()].strip()
    if not clean:
        return name, None
    return clean, set_code.lower() if set_code else None


def _candidate_keys(card_name: str) -> list[str]:
    """Ordered cache keys for a decklist card name.

    Both the raw name and the annotation-free name are indexed so that
    ``Lightning Bolt (2x2) 117`` and ``Lightning Bolt`` share cache entries.
    """
    clean, _set_code = _parse_card_reference(card_name)
    keys = [_normalize(card_name), _normalize(clean)]
    seen: set[str] = set()
    unique: list[str] = []
    for k in keys:
        if k and k not in seen:
            seen.add(k)
            unique.append(k)
    return unique


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
