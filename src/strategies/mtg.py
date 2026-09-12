"""MTG image-fetching strategy.

Resolution order (Scryfall API, https://scryfall.com/docs/api, then
Moxfield, https://moxfield.com):
    1. Scryfall collector lookup (``GET /cards/<set>/<collector>``) when the
       decklist pins an exact printing (``Lightning Bolt (2x2) 117`` or the
       ``[2x2:117]`` art marker).
    2. Scryfall prints search (``GET /cards/search`` with
       ``unique:prints``) when the ``[art]`` marker requests a variant
       (``[borderless]``, ``[showcase]``, ``[fullart]``, ``[extended]``,
       ``[retro]``, ``[promo]``) or ``[base]``, optionally narrowed to a
       set (``[m21 borderless]``, ``[2x2:117 showcase]``).
    3. Scryfall card name lookup (``GET /cards/named``). The decklist name
       is tried with an exact match first and a fuzzy match second; set
       annotations from the decklist (``Lightning Bolt (2x2) 117``) or the
       ``[art]`` marker (``[m21]``, ``[2x2]``) are forwarded as the ``set``
       parameter. Every card is resolved live against the API on each run.
    4. Scryfall card search fallback (``GET /cards/search?q=<name>``) for
       names the named-lookup endpoint cannot resolve (typos, extra
       tokens...).
    5. Moxfield fallback (``GET https://api.moxfield.com/v2/cards/search``)
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

# Art markers that only make sense for Lorcana; when used with --tcg mtg a
# warning is logged and the default art is used.
_LORCANA_ONLY_ART = ("enchanted", "iconic", "epic", "special")

# Canonical MTG variant keywords honored by the prints search.
MTG_VARIANT_CHOICES = ("fullart", "borderless", "showcase", "extended", "retro", "promo")
MTG_ART_MODES = ("best", "base")


class MTGStrategy(TCGStrategy):
    """Fetch MTG card images via the Scryfall API (no local cache)."""

    name = "mtg"
    supports_art = True

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
        api_url: str | None = None,
        timeout: int = DEFAULT_TIMEOUT,
        image_format: str = "png",
        min_request_interval: float = DEFAULT_MIN_REQUEST_INTERVAL,
    ) -> None:
        self.api_url = (api_url or self.SCRYFALL_API_URL).rstrip("/")
        self.timeout = timeout
        self.image_format = image_format
        self.min_request_interval = max(0.0, min_request_interval)
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
    def fetch_card_image(
        self,
        card_name: str,
        output_path: str,
        art: str | None = None,
    ) -> bool:
        art = _effective_art(card_name, art)
        image_url = self._resolve_image_url(card_name, art)
        if not image_url:
            return False
        return self._download_image(image_url, output_path)

    # ------------------------------------------------------------------
    # Resolution helpers
    # ------------------------------------------------------------------
    def _resolve_image_url(self, card_name: str, art: str | None = None) -> str | None:
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
            url = self._lookup_scryfall_collector(set_code, collector_number, clean_name)
            if url:
                return url
            logger.debug(
                "Card '%s' not found at %s:%s; falling back to name lookup",
                card_name,
                set_code,
                collector_number,
            )

        # 2. Variant / base requests need the full prints list.
        if art_variant is not None or art_mode == "base":
            url = self._lookup_scryfall_prints(
                clean_name, set_code, art_variant, art_mode, card_name
            )
            if url:
                return url
            logger.info(
                "Card '%s' has no matching '%s' printing; using default art",
                card_name,
                art_variant or art_mode,
            )

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
            url = self._lookup_moxfield(card_name, set_code)
        return url

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
    # Source 1a: Scryfall collector endpoint (exact printing)
    # ------------------------------------------------------------------
    def _lookup_scryfall_collector(
        self, set_code: str, collector_number: str, expected_name: str | None = None
    ) -> str | None:
        """Fetch an exact printing via ``GET /cards/<set>/<collector>``.

        The endpoint returns whatever lives at that slot, so when
        ``expected_name`` is given the returned card is checked against it
        (front face included for double-faced cards). On a mismatch ``None``
        is returned and the caller falls back to the name lookup instead of
        silently downloading the wrong card.
        """
        endpoint = (
            f"/cards/{requests.utils.quote(set_code.lower())}"
            f"/{requests.utils.quote(collector_number.lower())}"
        )
        logger.debug("Scryfall collector lookup for '%s:%s'", set_code, collector_number)
        card = self._api_get(endpoint, {})
        if not card:
            return None
        if expected_name is not None and not _names_match(card.get("name"), expected_name):
            logger.warning(
                "Scryfall %s:%s is '%s', not '%s'; ignoring the collector number and "
                "falling back to name lookup.",
                set_code.lower(),
                collector_number.lower(),
                card.get("name"),
                expected_name,
            )
            return None
        return self._extract_image_url(card)

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
    ) -> str | None:
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
        return self._extract_image_url(picked)

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


def _names_match(returned_name: Any, expected_name: str) -> bool:
    """Check a Scryfall card name against the requested one (face-aware).

    Double-faced cards report ``"Front // Back"``; a request for either face
    counts as a match.
    """
    if not isinstance(returned_name, str) or not returned_name:
        return False
    expected = _normalize(expected_name)
    faces = [_normalize(face) for face in returned_name.split("//")]
    return expected in faces


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
