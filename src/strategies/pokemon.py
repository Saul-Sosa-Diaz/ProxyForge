"""Pokémon image-fetching strategy.

Resolution order (both sources live on https://pkmncards.com):
    1. Site search (``https://pkmncards.com/?s=<card name>``). Pokémon prints
       the same character across many sets (there are dozens of Pikachus), so
       the search page is the primary source: every result carries the full
       title (``Name · Set (CODE) #number``), the card-page URL and the image
       URL. The best-matching title wins.
    2. Direct card-page fallback (``https://pkmncards.com/card/<slug>/``) built
       by slugifying the decklist name, in case the search yields nothing.

Decklist names should match the ones used on pkmncards.com as closely as
possible (e.g. ``Pikachu ex · Ascended Heroes (ASC) #276`` or at least
``Pikachu ex Ascended Heroes``) so the correct printing is selected.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

import requests
from bs4 import BeautifulSoup

from .base import TCGStrategy

logger = logging.getLogger(__name__)


class PokemonStrategy(TCGStrategy):
    """Fetch Pokémon card images by scraping https://pkmncards.com."""

    name = "pokemon"

    BASE_URL = "https://pkmncards.com"
    DEFAULT_TIMEOUT = 30

    def __init__(self, timeout: int = DEFAULT_TIMEOUT) -> None:
        self.timeout = timeout
        self._session = requests.Session()
        self._session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/123.0 Safari/537.36"
                )
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
        url = self._scrape_search_page(card_name)
        if url:
            return url
        logger.debug(
            "Card '%s' not found via pkmncards.com search; trying direct card page",
            card_name,
        )
        slug = _slugify(card_name)
        if not slug:
            return None
        return self._scrape_card_page(slug)

    # ------------------------------------------------------------------
    # Source 1: pkmncards.com search
    # ------------------------------------------------------------------
    def _scrape_search_page(self, card_name: str) -> str | None:
        search_url = f"{self.BASE_URL}/?s={requests.utils.quote(card_name)}"
        html = self._fetch_html(search_url)
        if html is None:
            return None
        results = self._parse_search_results(html)
        if not results:
            return None
        title, card_url, image_url = self._select_result(results, card_name)
        logger.debug("Search for '%s' matched '%s' (%s)", card_name, title, card_url)
        if image_url:
            return image_url
        # The matched result had no usable <img>; open the card page instead.
        if not card_url:
            return None
        html = self._fetch_html(card_url)
        if html is None:
            return None
        return self._extract_image_from_html(html)

    @staticmethod
    def _parse_search_results(html: str) -> list[tuple[str, str, str | None]]:
        """Extract ``(title, card_url, image_url)`` for every search result.

        Multi-result pages list cards as ``<a class="card-image-link"
        title="Name · Set (CODE) #n" href="/card/<slug>/">`` wrapping the card
        ``<img>``. When the search hits exactly one card, pkmncards.com
        renders the full card page instead: there ``card-image-link`` wraps
        the image file itself (lightbox) and the title lives in the first
        ``/card/<slug>/`` anchor, so both layouts are handled.
        """
        soup = BeautifulSoup(html, "html.parser")
        results: list[tuple[str, str, str | None]] = []
        for article in soup.find_all("article", class_=re.compile(r"\btype-pkmn_card\b")):
            img = article.find("img", class_="card-image", src=True)
            image_url = img["src"] if img else None
            card_url: str | None = None
            title = ""
            link = article.find("a", class_="card-image-link", href=True)
            if link is not None and "/card/" in link["href"]:
                card_url = link["href"]
                title = link.get("title", "")
            if card_url is None:
                card_link = article.find("a", href=re.compile(r"/card/[a-z0-9-]+/?$"))
                if card_link is not None:
                    card_url = card_link["href"]
                    title = card_link.get_text(strip=True)
            if card_url is None and image_url is None:
                continue
            results.append((title, card_url or "", image_url))
        return results

    @staticmethod
    def _select_result(
        results: list[tuple[str, str, str | None]],
        card_name: str,
    ) -> tuple[str, str, str | None]:
        """Pick the search result whose title best matches ``card_name``.

        Titles look like ``Pikachu ex · Ascended Heroes (ASC) #276``; the
        decklist name is compared after normalization (lowercase, punctuation
        stripped, whitespace collapsed) so partial names such as
        ``Pikachu ex Ascended Heroes`` still match the right printing.
        """
        key = _normalize(card_name)
        # 1. Exact normalized title match.
        for result in results:
            if _normalize(result[0]) == key:
                return result
        # 2. Title starts with the requested name (missing set/number suffix).
        for result in results:
            if _normalize(result[0]).startswith(key):
                return result
        # 3. Requested name contains the title (extra tokens typed by the user).
        for result in results:
            title_key = _normalize(result[0])
            if title_key and title_key in key:
                return result
        # 4. Nothing matched: fall back to the first search hit.
        if len(results) > 1:
            logger.warning(
                "Card '%s' is ambiguous on pkmncards.com (%d hits, e.g. '%s', '%s'); "
                "using the first hit '%s'. Add the set name/number to disambiguate.",
                card_name,
                len(results),
                results[0][0],
                results[1][0],
                results[0][0],
            )
        return results[0]

    # ------------------------------------------------------------------
    # Source 2: direct card-page fallback
    # ------------------------------------------------------------------
    def _scrape_card_page(self, slug: str) -> str | None:
        page_url = f"{self.BASE_URL}/card/{slug}/"
        html = self._fetch_html(page_url)
        if html is None:
            return None
        return self._extract_image_from_html(html)

    def _extract_image_from_html(self, html: str) -> str | None:
        soup = BeautifulSoup(html, "html.parser")
        img = soup.find("img", class_="card-image", src=True)
        if img and _looks_like_card_image(img["src"]):
            return _absolute_url(img["src"], self.BASE_URL)
        meta = soup.find("meta", property="og:image", content=True)
        if meta and _looks_like_card_image(meta["content"]):
            return meta["content"]
        return None

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------
    def _fetch_html(self, url: str) -> str | None:
        try:
            resp = self._session.get(url, timeout=self.timeout)
        except requests.RequestException:
            return None
        if resp.status_code != 200:
            return None
        return resp.text

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
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _normalize(name: str) -> str:
    """Lowercase key with all punctuation/symbols stripped (``·``, ``#``,
    parentheses...) so titles and decklist names compare cleanly."""
    return _NON_ALNUM.sub(" ", name.strip().lower()).strip()


def _slugify(name: str) -> str:
    slug = name.strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    return slug.strip("-")


def _looks_like_card_image(url: str) -> bool:
    lowered = url.lower()
    return "wp-content/uploads" in lowered and any(
        ext in lowered for ext in (".jpg", ".jpeg", ".png", ".webp")
    )


def _absolute_url(url: str, base: str) -> str:
    if url.startswith(("http://", "https://", "//")):
        return url if url.startswith("http") else f"https:{url}"
    if url.startswith("/"):
        return f"{base}{url}"
    return f"{base}/{url}"
