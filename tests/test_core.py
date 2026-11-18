"""Tests for ScanDesk's non-GUI helpers (no scanner, printer or display needed)."""
import re

import pytest
from PIL import Image

import scandesk

SCANIMAGE_A = """\
All options specific to device `pixma:04A9176C_123456':
  Scan mode:
    --resolution auto|75|150|300|600|1200dpi [75]
        Sets the resolution of the scanned image.
    --mode auto|Color|Gray|Lineart [Color]
        Selects the scan mode (e.g., lineart, monochrome, or color).
  Geometry:
    -l 0..216.07mm [0]
        Top-left x position of scan area.
    -x 0..216.07mm [216.07]
        Width of scan-area.
    -y 0..297.18mm (in steps of 0.1) [297.18]
        Height of scan-area.
"""


def test_parse_options_lists_and_ranges():
    opts = scandesk.parse_options(SCANIMAGE_A)
    assert opts["--resolution"] == (["75", "150", "300", "600", "1200"], "75")
    assert opts["--mode"] == (["Color", "Gray", "Lineart"], "Color")
    assert opts["-x"] == (("range", 0.0, 216.07), "216.07")
    assert opts["-y"][0] == ("range", 0.0, 297.18)


def test_parse_options_ignores_noise():
    assert scandesk.parse_options("garbage\n\nnothing here\n") == {}


def _page_boxes(pdf_bytes):
    return [tuple(float(v) for v in m) for m in
            re.findall(rb"/MediaBox \[0 0 ([\d.]+) ([\d.]+)\]", pdf_bytes)]


@pytest.mark.parametrize("mode", ["RGB", "L", "RGBA", "1", "P"])
def test_write_pdf_true_physical_size(tmp_path, mode):
    # A Letter page scanned at 150 dpi must become an 8.5 x 11 in (612 x 792 pt) PDF page.
    img = tmp_path / "page.png"
    Image.new(mode, (1275, 1650)).save(img)
    out = tmp_path / "out.pdf"
    scandesk.write_pdf([(str(img), 150)], out)
    data = out.read_bytes()
    assert data.startswith(b"%PDF-1.4")
    assert data.rstrip().endswith(b"%%EOF")
    assert _page_boxes(data) == [(612.0, 792.0)]


def test_write_pdf_multiple_pages_in_order(tmp_path):
    pages = []
    for i, (w, h, dpi) in enumerate([(300, 300, 100), (600, 300, 300), (200, 400, 0)]):
        p = tmp_path / f"p{i}.png"
        Image.new("RGB", (w, h), "white").save(p)
        pages.append((str(p), dpi))
    out = tmp_path / "multi.pdf"
    scandesk.write_pdf(pages, out)
    data = out.read_bytes()
    assert b"/Count 3" in data
    # dpi 0 falls back to 300
    assert _page_boxes(data) == [(216.0, 216.0), (144.0, 72.0), (48.0, 96.0)]
    assert not (tmp_path / "multi.pdf.part").exists()


def test_write_pdf_xref_offsets_are_valid(tmp_path):
    img = tmp_path / "a.png"
    Image.new("RGB", (50, 50)).save(img)
    out = tmp_path / "x.pdf"
    scandesk.write_pdf([(str(img), 72), (str(img), 72)], out)
    data = out.read_bytes()
    xref_at = int(re.search(rb"startxref\n(\d+)", data).group(1))
    assert data[xref_at:].startswith(b"xref")
    offsets = [int(o) for o in re.findall(rb"(\d{10}) 00000 n", data)]
    for num, off in enumerate(offsets, start=1):
        assert data[off:].startswith(f"{num} 0 obj".encode())
