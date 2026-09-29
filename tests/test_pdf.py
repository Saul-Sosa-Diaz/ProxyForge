"""Tests for the vector PDF writer (source-quality image embedding)."""
from __future__ import annotations

import re
import zlib

import pytest
from PIL import Image

from mocks import CARD_PNG, IMAGES_DIR, FakeStrategy
from src.exporter import Exporter
from src.models import DeckCard
from src.pdf import PdfWriter, _png_idat, draw_image_ops


def _write(tmp_path, image_path, size=None):
    writer = PdfWriter()
    name = writer.add_image(image_path, size)
    writer.add_page(595.0, 842.0, draw_image_ops(name, (178.6, 0, 0, 249.4, 20, 20)))
    pdf = tmp_path / "out.pdf"
    writer.save(pdf)
    return pdf.read_bytes()


def test_jpeg_is_embedded_byte_for_byte(tmp_path):
    source = (IMAGES_DIR / "Charizard.jpg").read_bytes()

    pdf = _write(tmp_path, IMAGES_DIR / "Charizard.jpg")

    assert b"/Filter /DCTDecode" in pdf
    assert source in pdf


def test_rgb_png_reuses_its_compressed_data(tmp_path):
    pdf = _write(tmp_path, CARD_PNG)

    idat = _png_idat(CARD_PNG.read_bytes())
    assert b"/Predictor 15" in pdf
    assert idat and idat in pdf


def test_rgba_png_is_converted_losslessly(tmp_path):
    source = tmp_path / "rgba.png"
    Image.new("RGBA", (5, 7), (10, 20, 30, 0)).save(source)

    pdf = _write(tmp_path, source)

    stream = re.search(rb"/Columns 5 >> /Length (\d+) >>\nstream\n", pdf)
    raw = zlib.decompress(pdf[stream.end() : stream.end() + int(stream.group(1))])
    assert len(raw) == 7 * (1 + 5 * 3)  # one filter byte per row, RGB only


def test_explicit_size_resamples(tmp_path):
    pdf = _write(tmp_path, CARD_PNG, size=(20, 28))

    assert b"/Width 20 /Height 28" in pdf
    assert b"/Filter /DCTDecode" in pdf


def test_xref_offsets_point_at_objects(tmp_path):
    pdf = _write(tmp_path, CARD_PNG)

    xref = int(re.search(rb"startxref\n(\d+)", pdf).group(1))
    assert pdf[xref:].startswith(b"xref")
    offsets = [int(o) for o in re.findall(rb"(\d{10}) 00000 n", pdf)]
    for number, offset in enumerate(offsets, start=1):
        assert pdf[offset:].startswith(f"{number} 0 obj".encode())


def test_same_image_is_embedded_once(tmp_path):
    writer = PdfWriter()
    assert writer.add_image(CARD_PNG) == writer.add_image(CARD_PNG)


def test_empty_document_raises(tmp_path):
    with pytest.raises(RuntimeError):
        PdfWriter().save(tmp_path / "empty.pdf")


def test_exporter_embeds_source_pixels_by_default(tmp_path):
    pdf = Exporter(FakeStrategy(), str(tmp_path)).export_deck(
        "deck", [DeckCard(quantity=9, name="Bolt")]
    ).read_bytes()

    assert pdf.count(b"/Subtype /Image") == 1  # nine copies share one image
    assert b"/Width 63 /Height 88" in pdf
    assert b"/MediaBox [0 0 595.2756 841.8898]" in pdf  # true A4 size
