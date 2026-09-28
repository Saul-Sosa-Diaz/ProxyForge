"""Shared test doubles: fake HTTP session/response, fake strategy and fixture loaders."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from src.strategies.base import TCGStrategy

FIXTURES = Path(__file__).resolve().parent / "fixtures"
DECKS_DIR = FIXTURES / "decks"
IMAGES_DIR = FIXTURES / "images"
RESPONSES_DIR = FIXTURES / "responses"
CARD_PNG = FIXTURES / "card.png"
CARD_BACK_PNG = FIXTURES / "card_back.png"


def deck(name: str) -> str:
    """Path to a fixture decklist (``tests/fixtures/decks/<name>.txt``)."""
    return str(DECKS_DIR / f"{name}.txt")


def response_text(name: str) -> str:
    return (RESPONSES_DIR / name).read_text(encoding="utf-8")


def response_json(name: str):
    return json.loads(response_text(name))


class FakeResponse:
    """Minimal stand-in for ``requests.Response``."""

    def __init__(
        self,
        status_code: int = 200,
        text: str = "",
        content: bytes = b"",
        json_data=None,
    ) -> None:
        self.status_code = status_code
        self.text = text
        self.content = content
        self._json = json_data
        self.headers: dict[str, str] = {}
        self.reason = "" if status_code == 200 else "Not Found"

    def json(self):
        if self._json is None:
            raise ValueError("No JSON body")
        return self._json

    def iter_content(self, chunk_size: int = 8192):
        yield self.content


def image_response() -> FakeResponse:
    """A 200 response whose body is the fixture card image."""
    return FakeResponse(content=CARD_PNG.read_bytes())


class FakeSession:
    """Fake ``requests.Session`` that answers from a ``{substring: response}`` map.

    The request key is the URL plus its query params (``url?k=v&...``); the
    first route whose substring appears in the key wins. Unmatched requests
    get a 404. Every requested key is recorded in ``calls``.
    """

    def __init__(self, routes: dict[str, FakeResponse] | None = None) -> None:
        self.routes = routes or {}
        self.calls: list[str] = []
        self.headers: dict[str, str] = {}

    def get(self, url: str, params=None, **kwargs) -> FakeResponse:
        key = url
        if params:
            key += "?" + "&".join(f"{k}={v}" for k, v in params.items())
        self.calls.append(key)
        for pattern, response in self.routes.items():
            if pattern in key:
                return response
        return FakeResponse(status_code=404)

    head = get


class FakeStrategy(TCGStrategy):
    """Strategy that copies the fixture card image for every known card."""

    name = "fake"

    def __init__(self, known: set[str] | None = None, backs: set[str] | None = None) -> None:
        self.known = known  # None = every card is known
        self.backs = backs or set()
        self.calls: list[str] = []

    def fetch_card_image(self, card_name: str, output_path: str, art: str | None = None) -> bool:
        self.calls.append(card_name)
        if self.known is not None and card_name not in self.known:
            return False
        shutil.copyfile(CARD_PNG, output_path)
        return True

    def fetch_card_back_image(self, card_name: str, output_path: str, art: str | None = None) -> bool:
        if card_name not in self.backs:
            return False
        shutil.copyfile(CARD_BACK_PNG, output_path)
        return True
