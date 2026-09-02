"""MTG image-fetching strategy.

Resolution order (both sources live on the Scryfall API,
https://scryfall.com/docs/api):
    1. Card name lookup (``GET /cards/named``). The decklist name is tried
       with an exact match first and a fuzzy match second; set annotations
       from the decklist (``Lightning Bolt (2x2) 117``, ``Counterspell
       [MH2]``) are parsed and forwarded as the ``set`` parameter. Resolved
       names are cached locally (name -> image URL) to avoid repeated API
       calls on subsequent runs, as recommended by Scryfall's guidelines.
    2. Card search fallback (``GET /cards/search?q=<name>``) for names the
       named-lookup endpoint cannot resolve (typos, extra tokens...).

Images are served by the Scryfall image CDN (``cards.scryfall.io``) in the
versions documented at https://scryfall.com/docs/api/images. By default the
``png`` version is used (744x1040, highest quality); the remaining versions
(``large``, ``normal``, ``border_crop``, ``small``...) serve as fallbacks.

Rate limits (https://scryfall.com/docs/api/rate-limits): the ``/cards/*``
endpoints are limited to 2 requests/second, so a minimum interval is
enforced between API calls and HTTP 429 responses are honored via the
``Retry-After`` header. The image CDN has no rate limits.
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
    r"\s*(?:\[(?P<brackets>[A-Za-z0-9]{2,8})\]|\((?P<parens>[A-Za-z0-9]{2,8})\))"
    r"(?:\s+(?P<collector>\d{1,4}))?\s*$"
)


def _normalize(name: str) -> str:
    """Lowercase + whitespace-normalized cache key."""
    return re.sub(r"\s+", " ", name.strip().lower())


def _parse_card_reference(card_name: str) -> tuple[str, str | None]:
    """Split a decklist card name into ``(clean_name, set_code)``.

    Handles the common MTG decklist set/collector annotations:
        ``Lightning Bolt (2x2) 117`` -> ``("Lightning Bolt", "2x2")``
        ``Counterspell [MH2]``       -> ``("Counterspell", "mh2")``
        ``Lightning Bolt``           -> ``("Lightning Bolt", None)``
    """
    name = card_name.strip()
    match = _SET_ANNOTATION.search(name)
    if not match:
        return name, None
    set_code = match.group("brackets") or match.group("parens")
    clean = name[: match.start()].strip()
    if not clean:
        return name, None
    return clean, set_code.lower()


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
