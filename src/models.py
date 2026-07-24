"""Data models for the TCG Card Image Downloader."""
from __future__ import annotations

from pydantic import BaseModel, Field


class DeckCard(BaseModel):
    """A single card entry from a decklist."""

    quantity: int = Field(gt=0, description="Number of copies of the card.")
    name: str = Field(min_length=1, description="Full card name.")


class DownloadResult(BaseModel):
    """Result of a single card image fetch operation."""

    card_name: str
    success: bool
    image_path: str | None = None
    source: str | None = None
    error: str | None = None