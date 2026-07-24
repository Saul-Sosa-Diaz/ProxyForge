"""Lorcana image-fetching strategy.

Resolution order:
    1. Local JSON database (``lorcana.json``) for high-speed lookup.
    2. Web scraping fallback on https://lorcana.gg/cards/.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup

from .base import TCGStrategy


class LorcanaStrategy(TCGStrategy):
    """Fetch Lorcana card images using a local JSON DB and a lorcana.gg fallback."""

    name = "lorcana"
    CARDS_BASE_URL = "https://lorcana.gg/cards"
    DEFAULT_TIMEOUT = 30

    def __init__(
        self,
        local_db_path: str | None = None,
        timeout: int = DEFAULT_TIMEOUT,
    ) -> None:
        self.local_db_path = Path(local_db_path) if local_db_path else Path("data/lorcana.json")
        self.timeout = timeout
        self._local_db: list[dict[str, Any]] | None = None
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
        url = self._lookup_local(card_name)
        if url:
            return url
        return self._scrape_lorcana_gg(card_name)

    def _lookup_local(self, card_name: str) -> str | None:
        db = self._load_local_db()
        if db is None:
            return None
        target = _normalize(card_name)
        for entry in db:
            entry_name = entry.get("name") or entry.get("card_name")
            if entry_name and _normalize(entry_name) == target:
                return entry.get("image_url") or entry.get("image")
        return None

    def _load_local_db(self) -> list[dict[str, Any]] | None:
        if self._local_db is not None:
            return self._local_db
        if not self.local_db_path.exists():
            return None
        try:
            with self.local_db_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            return None
        if isinstance(data, dict):
            data = data.get("cards") or data.get("data") or []
        if not isinstance(data, list):
            return None
        self._local_db = data
        return data

    # ------------------------------------------------------------------
    # Web scraping fallback
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
        return self._extract_image_from_html(html, card_name)

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
        return self._extract_image_from_html(html, card_name)

    def _extract_image_from_html(self, html: str, card_name: str) -> str | None:
        soup = BeautifulSoup(html, "html.parser")

        # 1. Image div whose ``style`` attribute embeds a background-image URL.
        for div in soup.find_all("div", style=True):
            url = _extract_bg_image(div.get("style", ""))
            if url and _looks_like_card_image(url):
                return _absolute_url(url, self.CARDS_BASE_URL)

        # 2. Embedded ``<img>`` element inside the card region.
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
def _slugify(name: str) -> str:
    slug = name.strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    return slug.strip("-")


def _normalize(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip().lower())


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