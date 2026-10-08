"""Erzeugt Test-PDFs zur Laufzeit (keine Binaerdateien im Repo).

wps_like_pdf() bildet das Problem der WPS-Office-PDFs nach: eingebetteter
TrueType-Subset-Font als Type0/Identity-H ohne ToUnicode-CMap, Text als
Glyph-IDs in Hex-Strings (<0027>Tj ...), Glyph-Reihenfolge = Mac-Standard.
"""

import io
import zlib

from fontTools.fontBuilder import FontBuilder
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib.standardGlyphOrder import standardGlyphOrder


def _pdf(objects: list[bytes]) -> bytes:
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for i, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(f"{i} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for off in offsets:
        out.write(f"{off:010d} 00000 n \n".encode())
    out.write(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return out.getvalue()


def _stream(data: bytes, extra: str = "") -> bytes:
    comp = zlib.compress(data)
    return f"<< /Length {len(comp)} /Filter /FlateDecode {extra}>>\nstream\n".encode() + comp + b"\nendstream"


def _ttf(with_cmap: bool, post_names: bool) -> bytes:
    order = standardGlyphOrder[:120]
    fb = FontBuilder(1000, isTTF=True)
    fb.setupGlyphOrder(order)
    cmap = {}
    for name in order:
        if name == "space":
            cmap[0x20] = name
        elif len(name) == 1:
            cmap[ord(name)] = name
    digits = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine"]
    for i, d in enumerate(digits):
        cmap[0x30 + i] = d
    cmap[ord(".")] = "period"
    cmap[ord(",")] = "comma"
    cmap[ord(":")] = "colon"
    fb.setupCharacterMap(cmap)
    glyphs = {}
    for name in order:
        pen = TTGlyphPen(None)
        if name not in (".notdef", ".null", "nonmarkingreturn", "space"):
            pen.moveTo((0, 0)); pen.lineTo((0, 500)); pen.lineTo((400, 500)); pen.closePath()
        glyphs[name] = pen.glyph()
    fb.setupGlyf(glyphs)
    fb.setupHorizontalMetrics({n: (500, 0) for n in order})
    fb.setupHorizontalHeader(ascent=800, descent=-200)
    fb.setupNameTable({"familyName": "TestSubset", "styleName": "Regular"})
    fb.setupOS2()
    fb.setupPost()
    if not post_names:
        fb.font["post"].formatType = 3.0
    if not with_cmap:
        del fb.font["cmap"]
    buf = io.BytesIO()
    fb.font.save(buf)
    return buf.getvalue()


def _gid_hex(text: str) -> str:
    """Text -> Hex-String aus Glyph-IDs (Mac-Standard-Reihenfolge: GID = ASCII - 29)."""
    return "<" + "".join(f"{ord(c) - 29:04X}" for c in text) + ">"


def wps_like_pdf(with_cmap: bool = True, post_names: bool = True, lines: list[tuple[float, float, str]] | None = None) -> bytes:
    lines = lines or [
        (50, 750, "Delivery Note"),
        (50, 700, "Item"), (300, 700, "Qty"), (400, 700, "Price"),
        (50, 680, "Smart Lock"), (300, 680, "10"), (400, 680, "52.5"),
        (50, 660, "Cylinder"), (300, 660, "4"), (400, 660, "18.0"),
    ]
    font = _ttf(with_cmap, post_names)
    # Absichtlich durcheinander geschrieben (Spalten zuerst), Sortierung muss das richten
    ops = ["BT"]
    for x, y, text in sorted(lines, key=lambda l: (l[0], -l[1])):
        ops.append(f"/F1 12 Tf 1 0 0 1 {x} {y} Tm {_gid_hex(text)} Tj")
    ops.append("ET")
    content = "\n".join(ops).encode()
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        _stream(content),
        b"<< /Type /Font /Subtype /Type0 /BaseFont /ABCDEF+TestSubset /Encoding /Identity-H /DescendantFonts [6 0 R] >>",
        b"<< /Type /Font /Subtype /CIDFontType2 /BaseFont /ABCDEF+TestSubset "
        b"/CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) /Supplement 0 >> "
        b"/FontDescriptor 7 0 R /DW 500 /CIDToGIDMap /Identity >>",
        b"<< /Type /FontDescriptor /FontName /ABCDEF+TestSubset /Flags 32 /FontBBox [0 -200 1000 800] "
        b"/ItalicAngle 0 /Ascent 800 /Descent -200 /CapHeight 700 /StemV 80 /FontFile2 8 0 R >>",
        _stream(font, f"/Length1 {len(font)} "),
    ]
    return _pdf(objs)


def simple_pdf(pages: list[list[tuple[float, float, str]]]) -> bytes:
    """Normale PDF mit Helvetica (WinAnsi), eine Liste von (x, y, text) pro Seite."""
    n = len(pages)
    objs: list[bytes] = [b"<< /Type /Catalog /Pages 2 0 R >>", b""]
    kids = []
    font_id = 3
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    for lines in pages:
        content = ["BT"]
        for x, y, text in lines:
            esc = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            content.append(f"/F1 11 Tf 1 0 0 1 {x} {y} Tm ({esc}) Tj")
        content.append("ET")
        objs.append(_stream("\n".join(content).encode("cp1252")))
        content_id = len(objs)
        objs.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 {font_id} 0 R >> >> /Contents {content_id} 0 R >>".encode()
        )
        kids.append(len(objs))
    objs[1] = f"<< /Type /Pages /Kids [{' '.join(f'{k} 0 R' for k in kids)}] /Count {n} >>".encode()
    return _pdf(objs)
