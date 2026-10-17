"""Synthetische "Scans": Text wird mit Pillow als Bild gerendert und ohne Textebene
gespeichert (PDF, PNG, mehrseitiges TIFF). Keine echten Scans im Repo."""

import io

from PIL import Image, ImageDraw, ImageFont


def page_image(lines: list[str], size=(1240, 1754)) -> Image.Image:
    """A4 bei 150 dpi, schwarzer Text auf weiss."""
    img = Image.new("L", size, 255)
    draw = ImageDraw.Draw(img)
    font = ImageFont.load_default(size=44)
    y = 120
    for line in lines:
        draw.text((100, y), line, fill=0, font=font)
        y += 80
    return img


def scan_pdf(pages: list[list[str]]) -> bytes:
    imgs = [page_image(p) for p in pages]
    buf = io.BytesIO()
    imgs[0].save(buf, "PDF", resolution=150, save_all=True, append_images=imgs[1:])
    return buf.getvalue()


def png(lines: list[str]) -> bytes:
    buf = io.BytesIO()
    page_image(lines).save(buf, "PNG")
    return buf.getvalue()


def tiff(pages: list[list[str]]) -> bytes:
    imgs = [page_image(p) for p in pages]
    buf = io.BytesIO()
    imgs[0].save(buf, "TIFF", save_all=True, append_images=imgs[1:], compression="tiff_deflate")
    return buf.getvalue()


def merge_pdfs(*pdfs: bytes) -> bytes:
    from pypdf import PdfReader, PdfWriter
    w = PdfWriter()
    for data in pdfs:
        for page in PdfReader(io.BytesIO(data)).pages:
            w.add_page(page)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()
