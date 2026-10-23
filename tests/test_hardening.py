"""Haertung: Token-Pruefung (compare_digest, Mindestlaenge, Rate-Limit), Groessenlimits,
ID-Validierung, Fehlermeldungen ohne interne Details, SSRF-Pinning beim URL-Download."""

import asyncio
import hmac
import json
import os
import sys

import httpx
import pytest
import respx
from starlette.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import zammad_mcp as zm  # noqa: E402

TOKEN = "test-mcp-auth-token-0123456789abcdef"
HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
ZAMMAD = "https://zammad.example.com"


def _init():
    return {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}}


@pytest.fixture
def http_client():
    with TestClient(zm.build_http_app(TOKEN), base_url="https://mcp.example.com") as client:
        yield client


# --- Token --------------------------------------------------------------------

@pytest.mark.parametrize("token", ["", " ", "a", "kurz-" * 5, "x" * 31, " " + "x" * 32 + " "])
def test_short_token_refused(token):
    with pytest.raises(RuntimeError, match="MCP_AUTH_TOKEN"):
        zm._validate_auth_token(token)
    with pytest.raises(RuntimeError, match="MCP_AUTH_TOKEN"):
        zm.build_http_app(token)


def test_main_refuses_short_token(monkeypatch):
    monkeypatch.setattr(zm, "MCP_AUTH_TOKEN", "zu-kurz")
    monkeypatch.setattr(zm.asyncio, "run", lambda coro: (coro.close(), pytest.fail("Server gestartet"))[1])
    with pytest.raises(RuntimeError, match="zu kurz"):
        zm.main()


def test_token_compared_with_compare_digest(monkeypatch, http_client):
    calls = []
    real = hmac.compare_digest

    def spy(a, b):
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(zm.hmac, "compare_digest", spy)
    assert http_client.post("/mcp", json=_init(), headers={**HEADERS, "Authorization": "Bearer falsch"}).status_code == 401
    assert calls and calls[0] == (b"Bearer falsch", f"Bearer {TOKEN}".encode())


# --- Rate-Limit -------------------------------------------------------------------

def test_rate_limit_after_failures(http_client, monkeypatch):
    bad = {**HEADERS, "Authorization": "Bearer falsch"}
    good = {**HEADERS, "Authorization": f"Bearer {TOKEN}"}
    for _ in range(zm.AUTH_MAX_FAILURES):
        assert http_client.post("/mcp", json=_init(), headers=bad).status_code == 401
    resp = http_client.post("/mcp", json=_init(), headers=bad)
    assert resp.status_code == 429 and resp.headers["Retry-After"] == str(int(zm.AUTH_FAILURE_WINDOW))
    assert http_client.get("/").status_code == 429  # ohne Token ebenfalls gebremst
    # gueltiges Token wird nie gebremst, /healthz bleibt frei
    assert http_client.post("/mcp", json=_init(), headers=good).status_code == 200
    assert http_client.get("/healthz").status_code == 200
    # nach Ablauf des Fensters wieder 401
    now = zm.time.monotonic()
    monkeypatch.setattr(zm.time, "monotonic", lambda: now + zm.AUTH_FAILURE_WINDOW + 1)
    assert http_client.post("/mcp", json=_init(), headers=bad).status_code == 401


def test_failure_limiter_window():
    lim = zm._AuthFailureLimiter(max_failures=2, window=100)
    assert not lim.blocked()
    lim._failures.extend([0.0, 1.0])
    assert lim.blocked() is (zm.time.monotonic() - 1.0 <= 100)


# --- Anhang-Groessenlimit ------------------------------------------------------------

@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setattr(zm, "ZAMMAD_URL", ZAMMAD)
    monkeypatch.setattr(zm, "ZAMMAD_TOKEN", "zt")
    zm._CACHE.clear()


def test_download_attachment_limit(monkeypatch):
    monkeypatch.setattr(zm, "ATTACHMENT_TEXT_MAX_BYTES", 10)
    with respx.mock(assert_all_mocked=True) as mock:
        mock.get(f"{ZAMMAD}/api/v1/ticket_attachment/1/2/3").respond(content=b"x" * 11, headers={"Content-Length": "11"})
        r = zm.download_attachment(1, 2, 3)
        assert r["limit_exceeded"] == "size" and "get_attachment_text" in r["hint"] and "content_base64" not in r
        mock.get(f"{ZAMMAD}/api/v1/ticket_attachment/1/2/4").respond(content=b"x" * 11)
        r = zm.download_attachment(1, 2, 4)  # ohne Content-Length: Abbruch beim Streamen
        assert r["limit_exceeded"] == "size"
        mock.get(f"{ZAMMAD}/api/v1/ticket_attachment/1/2/5").respond(content=b"ok", headers={"Content-Type": "text/plain"})
        r = zm.download_attachment(1, 2, 5)
    assert r["size_bytes"] == 2 and r["content_base64"] == "b2s=" and r["content_type"].startswith("text/plain")
    assert r["content_notice"] == zm.UNTRUSTED_CONTENT_NOTE


# --- ID-Validierung ---------------------------------------------------------------

@pytest.mark.parametrize("bad", ["1/../users/7", "7?x", "", "-1", 0, -5, True, None, 1.5, "abc"])
def test_int_id_rejects(bad):
    with pytest.raises(ValueError):
        zm._int_id(bad, "ticket_id")


def test_int_id_accepts():
    assert zm._int_id(7, "x") == 7 and zm._int_id("42", "x") == 42


@pytest.mark.parametrize("func,args", [
    (zm.get_ticket, ("1/../users/1",)),
    (zm.download_attachment, (1, 2, "3/../x")),
    (zm.add_ticket_note, ("x", "n")),
    (zm.update_ticket_state, ("x", "open")),
    (zm.update_ticket_title, ("x", "t")),
    (zm.set_ticket_pending, ("x", "2026-01-01")),
    (zm.set_ticket_group, ("x", "g")),
    (zm.set_ticket_owner, ("x", "o")),
    (zm.set_ticket_customer, ("x", "c")),
    (zm.delete_ticket_article, ("x",)),
    (zm.set_article_internal, ("x",)),
    (zm.forward_ticket, ("x", 1, "a@example.com")),
    (zm.get_attachment_text, (1, "x", 1)),
])
def test_tools_validate_ids_before_any_request(func, args):
    with respx.mock(assert_all_mocked=True) as mock:
        try:
            result = func(*args)
        except ValueError as e:
            assert "ganze Zahl" in str(e)
        else:
            assert "ganze Zahl" in json.dumps(result)  # get_attachment_text meldet als error-Dict
    assert mock.calls.call_count == 0


def test_pending_date_validated():
    with respx.mock(assert_all_mocked=True) as mock:
        with pytest.raises(ValueError, match="YYYY-MM-DD"):
            zm.set_ticket_pending(1, "morgen")
    assert mock.calls.call_count == 0


# --- Fehlermeldungen ---------------------------------------------------------------

def test_call_tool_hides_url_and_response_text():
    with respx.mock(assert_all_mocked=True) as mock:
        mock.get(f"{ZAMMAD}/api/v1/tickets/5").respond(status_code=404, text='{"error":"geheim"}')
        out = asyncio.run(zm.call_tool("get_ticket", {"ticket_id": 5}))
    text = out[0].text
    assert "404" in text and "nicht gefunden" in text
    assert "zammad.example.com" not in text and "geheim" not in text


def test_call_tool_connection_error_without_details():
    with respx.mock(assert_all_mocked=True) as mock:
        mock.get(f"{ZAMMAD}/api/v1/tickets/5").mock(side_effect=httpx.ConnectError("boom zammad.example.com"))
        out = asyncio.run(zm.call_tool("get_ticket", {"ticket_id": 5}))
    assert out[0].text == "Verbindungsfehler (ConnectError)"


def test_error_results_without_response_text():
    with respx.mock(assert_all_mocked=True) as mock:
        mock.get(f"{ZAMMAD}/api/v1/groups").respond(json=[{"id": 1, "name": "Users", "active": True}])
        mock.get(f"{ZAMMAD}/api/v1/ticket_states").respond(json=[{"id": 1, "name": "new"}])
        mock.get(f"{ZAMMAD}/api/v1/ticket_priorities").respond(json=[{"id": 2, "name": "2 normal"}])
        mock.get(f"{ZAMMAD}/api/v1/users/search").respond(json=[{"id": 3, "email": "k@example.com"}])
        mock.post(f"{ZAMMAD}/api/v1/tickets").respond(status_code=422, text="Validation failed: geheim")
        r = zm.create_ticket("T", "B", "k@example.com", "Users")
    assert r["success"] is False and r["status_code"] == 422
    assert "geheim" not in json.dumps(r) and "payload_sent" not in r


# --- SSRF: Verbindung an die geprueften IP gepinnt -------------------------------------

@pytest.mark.parametrize("ip,public", [
    ("8.8.8.8", True), ("10.0.0.5", False), ("127.0.0.1", False), ("169.254.169.254", False),
    ("192.168.100.2", False), ("0.0.0.0", False), ("::1", False), ("::ffff:10.0.0.5", False),
    ("2001:db8::1", False), ("2606:4700::1111", True), ("224.0.0.1", False),
])
def test_ip_is_public(ip, public):
    assert zm._ip_is_public(ip) is public


def test_fetch_pins_connection_and_rechecks_redirect(monkeypatch):
    dns = {"files.example.com": ["8.8.8.8"], "internal.example.com": ["10.0.0.5"]}
    monkeypatch.setattr(zm, "_resolve_host", lambda h, p: dns[h])
    seen = []

    def handler(request):
        seen.append((request.url.host, request.headers["host"], request.extensions.get("sni_hostname")))
        return httpx.Response(302, headers={"location": "https://internal.example.com/x.pdf"})

    monkeypatch.setattr(zm, "_fetch_client",
                        lambda: httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False))
    with pytest.raises(ValueError, match="SSRF"):
        zm.fetch_url_attachment("https://files.example.com/r.pdf?id=1")
    # erster Hop an die geprueften IP, Host/SNI = Originalname; zweiter Hop (intern) nie verbunden
    assert seen == [("8.8.8.8", "files.example.com", "files.example.com")]


def test_fetch_ok_filename_and_size_limit(monkeypatch):
    monkeypatch.setattr(zm, "_resolve_host", lambda h, p: ["8.8.8.8"])
    monkeypatch.setattr(zm, "URL_ATTACHMENT_MAX_BYTES", 20)

    def handler(request):
        if request.url.path == "/go":
            return httpx.Response(302, headers={"location": "/dl/rechnung-4711.pdf"})
        if request.url.path == "/big":
            return httpx.Response(200, content=b"%PDF" + b"x" * 100)
        if request.url.path == "/declared":
            return httpx.Response(200, headers={"content-length": "999"}, content=b"%PDF")
        if request.url.path == "/html":
            return httpx.Response(200, content=b"<html>", headers={"content-type": "text/html"})
        return httpx.Response(200, content=b"%PDF-1.4", headers={"content-disposition": 'attachment; filename="../../x/r.pdf"'})

    monkeypatch.setattr(zm, "_fetch_client",
                        lambda: httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False))
    r = zm.fetch_url_attachment("https://files.example.com/go")
    assert r["filename"] == "r.pdf" and r["mime-type"] == "application/pdf" and r["_size"] == 8
    with pytest.raises(ValueError, match="zu gross"):
        zm.fetch_url_attachment("https://files.example.com/big")
    with pytest.raises(ValueError, match="zu gross"):
        zm.fetch_url_attachment("https://files.example.com/declared")
    with pytest.raises(ValueError) as e:
        zm.fetch_url_attachment("https://files.example.com/html")
    assert str(e.value) == "Keine PDF unter https://files.example.com/html"  # kein Content-Type/Groessen-Orakel


def test_fetch_rejects_private_and_credentials(monkeypatch):
    monkeypatch.setattr(zm, "_resolve_host", lambda h, p: ["192.168.1.1"])
    with pytest.raises(ValueError, match="SSRF"):
        zm.fetch_url_attachment("https://intern.example.com/x.pdf")
    with pytest.raises(ValueError, match="Zugangsdaten"):
        zm.fetch_url_attachment("https://user:pw@files.example.com/x.pdf")
    with pytest.raises(ValueError, match="http/https"):
        zm.fetch_url_attachment("ftp://files.example.com/x.pdf")
