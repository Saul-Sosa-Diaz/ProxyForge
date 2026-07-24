"""Lorcana image-fetching strategy.

Resolution order:
    1. LorcanaJSON API (``https://lorcanajson.org/files/current/en/allCards.json.zip``)
       Downloaded once, cached locally to avoid repeated large downloads, and
       indexed by card ``fullName`` / ``simpleName`` for high-speed lookup.
    2. Web scraping fallback on https://lorcana.gg/cards/ when a card is not
       present in the LorcanaJSON database.
"""
from __future__ import annotations

import io
import json
import logging
import re
import time
import zipfile
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup

from .base import TCGStrategy

logger = logging.getLogger(__name__)


class LorcanaStrategy(TCGStrategy):
    """Fetch Lorcana card images via LorcanaJSON with a lorcana.gg fallback."""

    name = "lorcana"

    LORCANAJSON_ZIP_URL = "https://lorcanajson.org/files/current/en/allCards.json.zip"
    LORCANAJSON_JSON_URL = "https://lorcanajson.org/files/current/en/allCards.json"
    CARDS_BASE_URL = "https://lorcana.gg/cards"
    DEFAULT_TIMEOUT = 30

    def __init__(
        self,
        cache_path: str | None = None,
        api_url: str | None = None,
        timeout: int = DEFAULT_TIMEOUT,
        refresh_db: bool = False,
        cache_ttl_seconds: float | None = None,
    ) -> None:
        self.cache_path = Path(cache_path) if cache_path else Path("data/lorcana_cache.json")
        self.api_url = api_url or self.LORCANAJSON_ZIP_URL
        self.timeout = timeout
        self.refresh_db = refresh_db
        self.cache_ttl_seconds = cache_ttl_seconds
        self._db_index: dict[str, str] | None = None
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
        url = self._lookup_lorcanajson(card_name)
        if url:
            return url
        logger.debug("Card '%s' not found in LorcanaJSON; falling back to lorcana.gg", card_name)
        return self._scrape_lorcana_gg(card_name)

    # ------------------------------------------------------------------
    # Source 1: LorcanaJSON API (+ local cache)
    # ------------------------------------------------------------------
    def _lookup_lorcanajson(self, card_name: str) -> str | None:
        index = self._get_db_index()
        if index is None:
            return None
        for key in _candidate_keys(card_name):
            if key in index:
                return index[key]
        return None

    def _get_db_index(self) -> dict[str, str] | None:
        if self._db_index is not None:
            return self._db_index
        cards = self._load_all_cards()
        if not cards:
            return None
        index: dict[str, str] = {}
        for card in cards:
            if not isinstance(card, dict):
                continue
            images = card.get("images") or {}
            img_url = images.get("full") or images.get("thumbnail")
            if not img_url:
                continue
            full_name = card.get("fullName")
            simple_name = card.get("simpleName")
            if full_name:
                index.setdefault(_normalize(full_name), img_url)
            if simple_name:
                index.setdefault(simple_name.lower().strip(), img_url)
                index.setdefault(_to_simple_key(simple_name), img_url)
        self._db_index = index
        logger.info("LorcanaJSON index built with %d cards", len(index))
        return index

    def _load_all_cards(self) -> list[dict[str, Any]] | None:
        data = self._load_cache()
        if data is None:
            data = self._download_all_cards()
            if data is None:
                return None
            self._save_cache(data)
        if isinstance(data, dict):
            cards = data.get("cards") or data.get("data") or []
        else:
            cards = data
        if not isinstance(cards, list):
            return None
        return cards

    def _load_cache(self) -> dict[str, Any] | None:
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
            logger.warning("Failed to read LorcanaJSON cache '%s': %s", self.cache_path, exc)
            return None
        logger.info("Loaded LorcanaJSON cache from %s", self.cache_path)
        return data

    def _save_cache(self, data: dict[str, Any]) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self.cache_path.open("w", encoding="utf-8") as f:
                json.dump(data, f)
            logger.info("Saved LorcanaJSON cache to %s", self.cache_path)
        except OSError as exc:
            logger.warning("Failed to write LorcanaJSON cache '%s': %s", self.cache_path, exc)

    def _download_all_cards(self) -> dict[str, Any] | None:
        logger.info("Downloading LorcanaJSON database from %s", self.api_url)
        try:
            resp = self._session.get(self.api_url, timeout=self.timeout)
        except requests.RequestException as exc:
            logger.warning("LorcanaJSON request failed: %s", exc)
            return None
        if resp.status_code != 200:
            logger.warning("LorcanaJSON returned HTTP %s", resp.status_code)
            return None

        if self.api_url.endswith(".zip"):
            data = self._parse_zip(resp.content)
            if data is not None:
                return data
            logger.info("Zip parse failed; retrying with plain JSON endpoint")
            resp = self._session.get(self.LORCANAJSON_JSON_URL, timeout=self.timeout)
            if resp.status_code != 200:
                return None
        try:
            return resp.json()
        except json.JSONDecodeError as exc:
            logger.warning("LorcanaJSON decode failed: %s", exc)
            return None

    @staticmethod
    def _parse_zip(content: bytes) -> dict[str, Any] | None:
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as zf:
                json_names = [n for n in zf.namelist() if n.endswith(".json")]
                if not json_names:
                    return None
                with zf.open(json_names[0]) as member:
                    return json.loads(member.read())
        except (zipfile.BadZipFile, json.JSONDecodeError, KeyError) as exc:
            logger.warning("LorcanaJSON zip parse failed: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Source 2: lorcana.gg web scraping fallback
    # ------------------------------------------------------------------
    def _scrape_lorcana_gg(self, card_name: str) -> str | None:
        slug = _slugify(card_name)
        if not slug:
            return None
        url = self._scrape_card_page(slug, card_name)
        if url:
            return url
        return self._scrape_search_page(card_name)

    def _scrape_card_page(self, slug: str, card_name: str) -> str | None:
        page_url = f"{self.CARDS_BASE_URL}/{slug}"
        html = self._fetch_html(page_url)
        if html is None:
            return None
        return self._extract_image_from_html(html)

    def _scrape_search_page(self, card_name: str) -> str | None:
        search_url = f"{self.CARDS_BASE_URL}/?q={requests.utils.quote(card_name)}"
        html = self._fetch_html(search_url)
        if html is None:
            return None
        soup = BeautifulSoup(html, "html.parser")
        link = soup.find("a", href=re.compile(r"/cards/[^?]+"))
        if link is None or not link.get("href"):
            return None
        href = link["href"]
        card_url = href if href.startswith("http") else f"https://lorcana.gg{href}"
        html = self._fetch_html(card_url)
        if html is None:
            return None
        return self._extract_image_from_html(html)

    def _extract_image_from_html(self, html: str) -> str | None:
        soup = BeautifulSoup(html, "html.parser")
        for div in soup.find_all("div", style=True):
            url = _extract_bg_image(div.get("style", ""))
            if url and _looks_like_card_image(url):
                return _absolute_url(url, self.CARDS_BASE_URL)
        for img in soup.find_all("img"):
            src = img.get("src") or img.get("data-src") or ""
            if src and _looks_like_card_image(src):
                return _absolute_url(src, self.CARDS_BASE_URL)
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
_SPECIAL_CHARS = re.compile(r"[.,!?]")


def _normalize(name: str) -> str:
    """Lowercase + whitespace-normalized key (preserves the ' - ' version dash)."""
    return re.sub(r"\s+", " ", name.strip().lower())


def _to_simple_key(name: str) -> str:
    """Mimic LorcanaJSON's ``simpleName`` form: lowercase, version separator
    removed, common punctuation stripped, whitespace collapsed."""
    s = name.strip().lower()
    s = s.replace(" - ", " ")
    s = _SPECIAL_CHARS.sub("", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _candidate_keys(card_name: str) -> list[str]:
    """Ordered lookup keys for a decklist card name."""
    keys = [_normalize(card_name), _to_simple_key(card_name)]
    seen: set[str] = set()
    unique: list[str] = []
    for k in keys:
        if k and k not in seen:
            seen.add(k)
            unique.append(k)
    return unique


def _slugify(name: str) -> str:
    slug = name.strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    return slug.strip("-")


def _extract_bg_image(style: str) -> str | None:
    match = re.search(r"background-image\s*:\s*url\(['\"]?([^'\")]+)['\"]?\)", style)
    return match.group(1) if match else None


def _looks_like_card_image(url: str) -> bool:
    lowered = url.lower()
    return any(ext in lowered for ext in (".jpg", ".jpeg", ".png", ".webp", "image", "card"))


def _absolute_url(url: str, base: str) -> str:
    if url.startswith(("http://", "https://", "//")):
        return url if url.startswith("http") else f"https:{url}"
    if url.startswith("/"):
        return f"https://lorcana.gg{url}"
    return f"{base}/{url}"