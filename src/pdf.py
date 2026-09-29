"""Minimal vector PDF writer that embeds card images at their source quality.

Pages are drawn with PDF operators in points (1/72 inch): each card image
is an image XObject embedded once per document and placed with a
transformation matrix, so the printer receives the original pixels instead
of a page raster resampled to a fixed DPI.

Image encoding (lossless with respect to the downloaded file):
    - Baseline JPEG (RGB / grayscale): the original file bytes are embedded
      as-is (``DCTDecode``), with no decode / re-encode round trip.
    - PNG and every other format: the PNG ``IDAT`` stream is embedded with
      ``FlateDecode`` + PNG predictors (8-bit RGB PNGs pass through
      untouched; other modes are converted to RGB and re-encoded as PNG
      in memory first).
    - Cards resampled to an explicit size (draft DPI) are re-encoded as
      high-quality JPEG to keep those files small.
"""
from __future__ import annotations

import io
import struct
import zlib
from pathlib import Path

from PIL import Image

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
# JPEG quality for cards resampled to an explicit DPI (drafts, smaller files).
RESAMPLED_JPEG_QUALITY = 95


class PdfWriter:
    """Accumulate pages and images, then write a PDF 1.4 file."""

    def __init__(self) -> None:
        # Object 1 is the catalog and object 2 the page tree (written on save).
        self._objects: list[bytes] = [b"", b""]
        self._page_refs: list[int] = []
        # Resource name -> object number, and source key -> resource name.
        self._image_refs: dict[str, int] = {}
        self._image_names: dict[tuple[str, tuple[int, int] | None], str] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def add_image(self, path: Path, size: tuple[int, int] | None = None) -> str:
        """Embed an image once and return its resource name (e.g. ``Im1``).

        Args:
            path: Image file on disk.
            size: Optional ``(width, height)`` in pixels to resample to;
                ``None`` keeps the source pixels untouched.

        Returns:
            The XObject name to draw with :func:`draw_image_ops`.
        """
        key = (str(path), size)
        if key not in self._image_names:
            name = f"Im{len(self._image_names) + 1}"
            self._image_refs[name] = self._add_object(_image_object(path, size))
            self._image_names[key] = name
        return self._image_names[key]

    def add_page(self, width_pt: float, height_pt: float, content: str) -> None:
        """Append a page whose content stream is ``content`` (PDF operators)."""
        stream = zlib.compress(content.encode("ascii"))
        content_ref = self._add_object(
            _stream(f"/Length {len(stream)} /Filter /FlateDecode".encode(), stream)
        )
        xobjects = " ".join(f"/{name} {ref} 0 R" for name, ref in self._image_refs.items())
        page = (
            f"<< /Type /Page /Parent 2 0 R "
            f"/MediaBox [0 0 {_num(width_pt)} {_num(height_pt)}] "
            f"/Resources << /XObject << {xobjects} >> >> "
            f"/Contents {content_ref} 0 R >>"
        )
        self._page_refs.append(self._add_object(page.encode("ascii")))

    def save(self, path: Path) -> None:
        """Write the document to ``path``.

        Raises:
            RuntimeError: If no page was added.
        """
        if not self._page_refs:
            raise RuntimeError("No pages available to build the PDF.")
        kids = " ".join(f"{ref} 0 R" for ref in self._page_refs)
        self._objects[0] = b"<< /Type /Catalog /Pages 2 0 R >>"
        self._objects[1] = (
            f"<< /Type /Pages /Kids [{kids}] /Count {len(self._page_refs)} >>".encode("ascii")
        )
        with open(path, "wb") as out:
            out.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
            offsets: list[int] = []
            for number, body in enumerate(self._objects, start=1):
                offsets.append(out.tell())
                out.write(f"{number} 0 obj\n".encode("ascii"))
                out.write(body)
                out.write(b"\nendobj\n")
            xref_offset = out.tell()
            out.write(f"xref\n0 {len(self._objects) + 1}\n".encode("ascii"))
            out.write(b"0000000000 65535 f \n")
            for offset in offsets:
                out.write(f"{offset:010d} 00000 n \n".encode("ascii"))
            out.write(
                f"trailer\n<< /Size {len(self._objects) + 1} /Root 1 0 R >>\n"
                f"startxref\n{xref_offset}\n%%EOF\n".encode("ascii")
            )

    def _add_object(self, body: bytes) -> int:
        self._objects.append(body)
        return len(self._objects)


# ----------------------------------------------------------------------
# Drawing operators
# ----------------------------------------------------------------------
def draw_image_ops(
    name: str,
    matrix: tuple[float, float, float, float, float, float],
    clip: tuple[float, float, float, float] | None = None,
) -> str:
    """Operators drawing image ``name`` through ``matrix`` (unit square -> page).

    ``clip`` is an optional ``(x, y, width, height)`` rectangle in points.
    """
    clip_ops = f"{_nums(clip)} re W n " if clip is not None else ""
    return f"q {clip_ops}{_nums(matrix)} cm /{name} Do Q"


def line_ops(x1: float, y1: float, x2: float, y2: float) -> str:
    """Operators stroking one straight line (current stroke settings)."""
    return f"{_nums((x1, y1))} m {_nums((x2, y2))} l S"


def _num(value: float) -> str:
    text = f"{value:.4f}".rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


def _nums(values: tuple[float, ...]) -> str:
    return " ".join(_num(v) for v in values)


# ----------------------------------------------------------------------
# Image encoding
# ----------------------------------------------------------------------
def _stream(header: bytes, data: bytes) -> bytes:
    return b"<< " + header + b" >>\nstream\n" + data + b"\nendstream"


def _image_object(path: Path, size: tuple[int, int] | None) -> bytes:
    """Image XObject for ``path``, reusing the file's own compressed data."""
    with Image.open(path) as src:
        src.load()
        if size is not None and src.size != size:
            # Resampled drafts favor size: high-quality JPEG, no chroma subsampling.
            image = src.convert("RGB").resize(size, Image.LANCZOS)
            buffer = io.BytesIO()
            image.save(buffer, "JPEG", quality=RESAMPLED_JPEG_QUALITY, subsampling=0)
            return _jpeg_image_object(buffer.getvalue(), size, "RGB")
        if src.format == "JPEG" and src.mode in ("RGB", "L"):
            return _jpeg_image_object(Path(path).read_bytes(), src.size, src.mode)
        if src.format == "PNG":
            png = Path(path).read_bytes()
            if _is_plain_rgb8_png(png):
                return _png_image_object(src, png)
        return _png_image_object(src.convert("RGB"))


def _jpeg_image_object(data: bytes, size: tuple[int, int], mode: str) -> bytes:
    """``DCTDecode`` XObject wrapping a JPEG file as-is."""
    colorspace = "DeviceRGB" if mode == "RGB" else "DeviceGray"
    width, height = size
    header = (
        f"/Type /XObject /Subtype /Image /Width {width} /Height {height} "
        f"/ColorSpace /{colorspace} /BitsPerComponent 8 "
        f"/Filter /DCTDecode /Length {len(data)}"
    )
    return _stream(header.encode("ascii"), data)


def _is_plain_rgb8_png(png: bytes) -> bool:
    """True for 8-bit truecolor (no alpha), non-interlaced PNGs."""
    if not png.startswith(_PNG_SIGNATURE) or png[12:16] != b"IHDR":
        return False
    bit_depth, color_type, _, _, interlace = png[24:29]
    return bit_depth == 8 and color_type == 2 and interlace == 0


def _png_image_object(image: Image.Image, png: bytes | None = None) -> bytes:
    """``FlateDecode`` XObject from the ``IDAT`` data of an 8-bit RGB PNG.

    ``png`` is the file to reuse; when omitted ``image`` is encoded as PNG
    in memory (lossless, with Pillow's adaptive row filters).
    """
    if png is None:
        buffer = io.BytesIO()
        image.save(buffer, "PNG", compress_level=6)
        png = buffer.getvalue()
    width, height = image.size
    data = _png_idat(png)
    header = (
        f"/Type /XObject /Subtype /Image /Width {width} /Height {height} "
        f"/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /FlateDecode "
        f"/DecodeParms << /Predictor 15 /Colors 3 /BitsPerComponent 8 /Columns {width} >> "
        f"/Length {len(data)}"
    )
    return _stream(header.encode("ascii"), data)


def _png_idat(png: bytes) -> bytes:
    """Concatenated ``IDAT`` chunk data (the zlib stream) of a PNG file."""
    if not png.startswith(_PNG_SIGNATURE):
        raise ValueError("Not a PNG file.")
    chunks: list[bytes] = []
    pos = len(_PNG_SIGNATURE)
    while pos + 8 <= len(png):
        length, kind = struct.unpack(">I4s", png[pos : pos + 8])
        if kind == b"IDAT":
            chunks.append(png[pos + 8 : pos + 8 + length])
        elif kind == b"IEND":
            break
        pos += 12 + length
    return b"".join(chunks)
