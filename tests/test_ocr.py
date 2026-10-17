import logging
import os
import shutil
import stat
import sys
import time

import httpx
import pytest

sys.path.insert(0, os.path.dirname(__file__))

import zammad_mcp as zm  # noqa: E402
from pdf_fixtures import simple_pdf  # noqa: E402
from scan_fixtures import merge_pdfs, png, scan_pdf, tiff  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("tesseract") is None, reason="Tesseract nicht installiert")

TEXT_PAGE = [(50, 750, "Lieferschein Musterfirma AG"), (50, 730, "Artikel Testschloss Modell Fantasia 52.5")]
SCAN_1 = ["Rechnung 4711", "Musterfirma AG Beispielstadt", "Total CHF 52.50"]
SCAN_2 = ["Seite zwei Fantasia", "Bestellnummer 0815"]
SCAN_3 = ["Seite drei Schluss", "Vielen Dank"]


@pytest.fixture
def zammad(monkeypatch):
    files = {
        201: ("lieferschein.pdf", simple_pdf([TEXT_PAGE]), "application/pdf"),
        202: ("scan.pdf", scan_pdf([SCAN_1]), "application/pdf"),
        203: ("gemischt.pdf", merge_pdfs(simple_pdf([TEXT_PAGE]), scan_pdf([SCAN_2])), "application/pdf"),
        204: ("scan3.pdf", scan_pdf([SCAN_1, SCAN_2, SCAN_3]), "application/pdf"),
        205: ("foto.png", png(SCAN_1), "image/png"),
        206: ("fax.tif", tiff([SCAN_1, SCAN_2]), "image/tiff"),
    }

    def att(i):
        name, data, ctype = files[i]
        return {"id": i, "filename": name, "size": str(len(data)), "preferences": {"Content-Type": ctype}}

    articles = {1: [{"id": 31, "ticket_id": 1, "attachments": [att(i) for i in files]}]}

    def api_get(path, params=None):
        if path == "/tickets/search":
            return {"ticket_ids": [1], "assets": {"Ticket": {"1": {"id": 1, "number": "4242", "title": "Fantasia Scan"}}}}
        if path.startswith("/ticket_articles/by_ticket/"):
            return articles[1]
        if path.startswith("/ticket_articles/"):
            return articles[1][0]
        raise AssertionError(path)

    downloads = []

    def download(ticket_id, article_id, attachment_id):
        downloads.append(attachment_id)
        return files[attachment_id][1], files[attachment_id][2]

    def forbidden(*a, **k):
        raise AssertionError("schreibender Request")

    runs = []
    real_run_ocr = zm._run_ocr

    def counting_run_ocr(data, kind, pages, deadline, on_page):
        runs.append(list(pages))
        return real_run_ocr(data, kind, pages, deadline, on_page)

    monkeypatch.setattr(zm, "api_get", api_get)
    monkeypatch.setattr(zm, "_download_attachment_limited", download)
    monkeypatch.setattr(zm, "_run_ocr", counting_run_ocr)
    monkeypatch.setattr(zm, "OCR_CACHE_DIR", "")
    for m in ("post", "put", "patch", "delete"):
        monkeypatch.setattr(httpx, m, forbidden)
    zm._TEXT_CACHE.clear()
    zm._DATA_CACHE.clear()
    zm._OCR_MEM.clear()
    return {"downloads": downloads, "runs": runs, "real_run_ocr": real_run_ocr}


def test_text_pdf_without_ocr(zammad):
    r = zm.get_attachment_text(1, 31, 201)
    assert r["extraction_method"] == "pdfminer" and r["ocr_pages"] == []
    assert "Testschloss" in r["text"] and zammad["runs"] == []


def test_scan_pdf_ocr(zammad):
    r = zm.get_attachment_text(1, 31, 202)
    assert r["extraction_method"] == "ocr" and r["ocr_pages"] == [1]
    assert r["text"].startswith("--- Seite 1 ---\n")
    assert "Rechnung 4711" in r["text"] and "52.50" in r["text"]
    assert r["truncated"] is False
    off = zm.get_attachment_text(1, 31, 202, ocr="off")
    assert off["ocr_pages"] == [] and "4711" not in off["text"]


def test_mixed_pdf(zammad):
    r = zm.get_attachment_text(1, 31, 203)
    assert r["extraction_method"] == "mixed" and r["ocr_pages"] == [2]
    assert r["pages_by_method"] == {"pdfminer": [1], "ocr": [2]}
    assert "Testschloss" in r["text"] and "0815" in r["text"]
    assert zammad["runs"] == [[2]]
    forced = zm.get_attachment_text(1, 31, 203, ocr="force", page_from=1, page_to=1)
    assert forced["ocr_pages"] == [1] and forced["extraction_method"] == "ocr"


def test_page_range(zammad):
    r = zm.get_attachment_text(1, 31, 204, page_from=2, page_to=2)
    assert (r["page_from"], r["page_to"], r["ocr_pages"]) == (2, 2, [2])
    assert "--- Seite 2 ---" in r["text"] and "Seite 1" not in r["text"] and "0815" in r["text"]
    assert zammad["runs"] == [[2]]
    assert "ausserhalb" in zm.get_attachment_text(1, 31, 204, page_from=9)["error"]


def test_timeout_continues_with_next_page(zammad, monkeypatch):
    real = zammad["real_run_ocr"]

    def one_page_then_budget_gone(data, kind, pages, deadline, on_page):
        zammad["runs"].append(list(pages))
        real(data, kind, pages[:1], deadline, on_page)
        return {"timed_out": True, "fatal": None}

    monkeypatch.setattr(zm, "_run_ocr", one_page_then_budget_gone)
    r1 = zm.get_attachment_text(1, 31, 204)
    assert r1["truncated"] is True and r1["next_page"] == 2 and r1["ocr_incomplete"] is True
    assert r1["ocr_pages"] == [1] and "4711" in r1["text"] and "0815" not in r1["text"]
    r2 = zm.get_attachment_text(1, 31, 204, page_from=r1["next_page"])
    assert r2["next_page"] == 3 and "0815" in r2["text"] and "4711" not in r2["text"]
    r3 = zm.get_attachment_text(1, 31, 204, page_from=r2["next_page"])
    assert r3["truncated"] is False and "Vielen Dank" in r3["text"]
    assert zammad["runs"] == [[1, 2, 3], [2, 3], [3]]
    # alles gecacht: kompletter Abruf ohne neuen OCR-Lauf
    full = zm.get_attachment_text(1, 31, 204)
    assert full["ocr_pages"] == [1, 2, 3] and len(zammad["runs"]) == 3


def test_page_using_whole_budget_is_skipped(zammad, monkeypatch):
    monkeypatch.setattr(zm, "OCR_TIMEOUT_SECONDS", 0.3)
    # erster Aufruf: Budget schon durch Download + normale Extraktion verbraucht
    r = zm.get_attachment_text(1, 31, 202)
    assert r["truncated"] is True and r["next_page"] == 1 and r["ocr_pages"] == []
    # zweiter Aufruf: die Seite braucht das ganze OCR-Budget -> als fehlgeschlagen
    # markiert und uebersprungen, kein Endlos-Retry an derselben Seite
    r = zm.get_attachment_text(1, 31, 202)
    assert r["ocr_failed_pages"] == [1] and r["truncated"] is False


def test_char_paging_with_ocr(zammad):
    r1 = zm.get_attachment_text(1, 31, 204, max_chars=40)
    assert r1["truncated"] is True and r1["next_offset"] == 40 and "next_page" not in r1
    # nur so viele Seiten OCR, wie fuer max_chars noetig
    assert r1["ocr_pages"] == [1]
    r2 = zm.get_attachment_text(1, 31, 204, offset=40, max_chars=50000)
    assert r2["truncated"] is False and r2["ocr_pages"] == [1, 2, 3]
    assert zammad["runs"] == [[1, 2, 3], [2, 3]]


def test_memory_and_disk_cache(zammad, monkeypatch, tmp_path):
    cache_dir = tmp_path / "ocr-cache"
    monkeypatch.setattr(zm, "OCR_CACHE_DIR", str(cache_dir))
    r = zm.get_attachment_text(1, 31, 202)
    assert r["ocr_pages"] == [1] and len(zammad["runs"]) == 1
    zm.get_attachment_text(1, 31, 202, max_chars=10)
    assert len(zammad["runs"]) == 1  # In-Memory-Treffer

    files = list(cache_dir.iterdir())
    assert len(files) == 1
    f = files[0]
    assert len(f.stem) == 64 and all(c in "0123456789abcdef" for c in f.stem)  # Hash, kein Anhangname
    assert stat.S_IMODE(f.stat().st_mode) == 0o600
    assert stat.S_IMODE(cache_dir.stat().st_mode) == 0o700

    zm._OCR_MEM.clear()
    zm._TEXT_CACHE.clear()
    r = zm.get_attachment_text(1, 31, 202)
    assert "4711" in r["text"] and len(zammad["runs"]) == 1  # Disk-Treffer

    zm._OCR_MEM.clear()
    old = time.time() - 25 * 3600
    os.utime(f, (old, old))
    zm.get_attachment_text(1, 31, 202)
    assert len(zammad["runs"]) == 2  # TTL abgelaufen -> neu OCR


def test_image_attachments(zammad):
    r = zm.get_attachment_text(1, 31, 205)
    assert r["extraction_method"] == "ocr" and r["pages"] == 1 and "Rechnung 4711" in r["text"]
    t = zm.get_attachment_text(1, 31, 206)
    assert t["pages"] == 2 and t["ocr_pages"] == [1, 2]
    assert "--- Seite 2 ---" in t["text"] and "0815" in t["text"]
    off = zm.get_attachment_text(1, 31, 205, ocr="off")
    assert "ausgeschaltet" in off["ocr_error"]
    assert "ocr muss" in str(pytest.raises(ValueError, zm.get_attachment_text, 1, 31, 205, ocr="x").value)


def test_pixel_limit(zammad, monkeypatch):
    monkeypatch.setattr(zm, "OCR_MAX_IMAGE_PIXELS", 1000)
    r = zm.get_attachment_text(1, 31, 205)
    assert r["limit_exceeded"] == "pixels"


def test_ocr_text_not_logged(zammad):
    records = []
    handler = logging.Handler()
    handler.emit = records.append
    zm._log.addHandler(handler)
    try:
        zm.get_attachment_text(1, 31, 202)
    finally:
        zm._log.removeHandler(handler)
    msgs = [r.getMessage() for r in records]
    assert msgs and all("4711" not in m and "Musterfirma" not in m for m in msgs)
    assert "Seite 1" in msgs[0] and "Zeichen" in msgs[0]


def test_temp_dir_removed(zammad, monkeypatch):
    import tempfile
    created = []
    real_mkdtemp = tempfile.mkdtemp

    def mkdtemp(*a, **k):
        d = real_mkdtemp(*a, **k)
        created.append(d)
        return d

    monkeypatch.setattr(tempfile, "mkdtemp", mkdtemp)
    zm.get_attachment_text(1, 31, 202)
    assert created and not any(os.path.exists(d) for d in created)


def test_find_with_ocr(zammad):
    off = zm.find_in_attachments("Fantasia", "4711")
    assert {m["attachment_id"] for m in off["matches"]} == set()
    scan_miss = next(a for a in off["attachments_without_match"] if a["attachment_id"] == 202)
    assert scan_miss["pages_without_text"] == [1]
    assert {s["attachment_id"] for s in off["skipped_unsupported"]} == {205, 206}
    assert zammad["runs"] == []

    on = zm.find_in_attachments("Fantasia", "4711", ocr="auto")
    hits = {(m["attachment_id"], m.get("page")) for m in on["matches"]}
    assert hits == {(202, 1), (204, 1), (205, 1), (206, 1)}
    assert all(m.get("ocr") for m in on["matches"])
    runs_after = len(zammad["runs"])
    zm.find_in_attachments("Fantasia", "0815", ocr="auto")  # alles aus dem Cache
    assert len(zammad["runs"]) == runs_after
