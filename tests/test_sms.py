"""send_sms / list_received_sms: SMTP2GO-Aufrufe und Zammad-Notiz gemockt (respx)."""

import json
import os
import sys

import httpx
import pytest
import respx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import zammad_mcp as zm  # noqa: E402

ZAMMAD = "https://zammad.example.com"
SMTP2GO = "https://api.smtp2go.example.com/v3"


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setattr(zm, "ZAMMAD_URL", ZAMMAD)
    monkeypatch.setattr(zm, "ZAMMAD_TOKEN", "zt")
    monkeypatch.setattr(zm, "SMTP2GO_API_URL", SMTP2GO)
    monkeypatch.setattr(zm, "SMTP2GO_API_KEY", "api-test-key")
    monkeypatch.setattr(zm, "SMTP2GO_SMS_SENDER", "")
    zm._CACHE.clear()


@pytest.mark.parametrize("raw,expected", [
    ("+41 79 123 45 67", "+41791234567"),
    ("0041-79-123.45.67", "+41791234567"),
    ("+49 (151) 1234567", "+491511234567"),
    ("079 123 45 67", "+41791234567"),
    ("076-123.45.67", "+41761234567"),
])
def test_normalize_phone_ok(raw, expected):
    assert zm._normalize_phone(raw) == expected


@pytest.mark.parametrize("raw", ["061 123 45 67", "079 123 45 6", "0791234567 8", "", "+41abc", "+123", "+0791234567", "+1234567890123456"])
def test_normalize_phone_rejects(raw):
    with pytest.raises(ValueError):
        zm._normalize_phone(raw)


def test_sms_units():
    assert zm._sms_units("a" * 160) == {"encoding": "GSM-7", "length": 160, "units": 1}
    assert zm._sms_units("a" * 161)["units"] == 2
    assert zm._sms_units("Grüezi, Ihr Termin ist bestätigt")["encoding"] == "GSM-7"
    assert zm._sms_units("€" * 80) == {"encoding": "GSM-7", "length": 160, "units": 1}
    assert zm._sms_units("Hallo 🙂")["encoding"] == "UCS-2"
    assert zm._sms_units("ł" * 71)["units"] == 2


def test_preview_sends_nothing():
    with respx.mock(assert_all_mocked=True) as mock:
        r = zm.send_sms(text="Test", to="+41791234567")
    assert r["preview"] is True and r["sent"] is False
    assert r["to"] == "+41791234567"
    assert mock.calls.call_count == 0


def test_send_with_ticket_writes_note():
    with respx.mock(assert_all_mocked=True) as mock:
        mock.get(f"{ZAMMAD}/api/v1/tickets/5").respond(json={"id": 5, "customer_id": 9})
        sms = mock.post(f"{SMTP2GO}/sms/send").respond(json={"request_id": "r1", "data": {}})
        note = mock.post(f"{ZAMMAD}/api/v1/ticket_articles").respond(json={"id": 77})
        r = zm.send_sms(text="Hallo", to="0041791234567", ticket_id=5, sender="+447700900123", confirm=True)
    assert r["success"] is True and r["note_article_id"] == 77
    sent = json.loads(sms.calls[0].request.content)
    assert sent == {"destination": ["+41791234567"], "content": "Hallo", "sender": "+447700900123"}
    assert sms.calls[0].request.headers["X-Smtp2go-Api-Key"] == "api-test-key"
    article = json.loads(note.calls[0].request.content)
    assert article["internal"] is True and article["type"] == "note"
    assert "+41791234567" in article["body"] and "Hallo" in article["body"]


def test_recipient_from_ticket_customer_mobile():
    with respx.mock(assert_all_mocked=True) as mock:
        mock.get(f"{ZAMMAD}/api/v1/tickets/5").respond(json={"id": 5, "customer_id": 9})
        mock.get(f"{ZAMMAD}/api/v1/users/9").respond(json={"id": 9, "mobile": "+41 79 111 22 33"})
        r = zm.send_sms(text="Hallo", ticket_id=5)
    assert r["to"] == "+41791112233" and r["sent"] is False


def test_customer_without_mobile_is_error():
    with respx.mock(assert_all_mocked=True) as mock:
        mock.get(f"{ZAMMAD}/api/v1/tickets/5").respond(json={"id": 5, "customer_id": 9})
        mock.get(f"{ZAMMAD}/api/v1/users/9").respond(json={"id": 9, "mobile": "", "email": "kunde@example.com"})
        with pytest.raises(ValueError, match="keine Mobilnummer"):
            zm.send_sms(text="Hallo", ticket_id=5, confirm=True)


def test_missing_ticket_sends_nothing():
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        mock.get(f"{ZAMMAD}/api/v1/tickets/999").respond(status_code=404, json={"error": "not found"})
        sms = mock.post(f"{SMTP2GO}/sms/send").respond(json={})
        with pytest.raises(httpx.HTTPStatusError):
            zm.send_sms(text="Hallo", to="+41791234567", ticket_id=999, confirm=True)
    assert sms.calls.call_count == 0


def test_too_long_rejected():
    with pytest.raises(ValueError, match="zu lang"):
        zm.send_sms(text="a" * 1531, to="+41791234567", confirm=True)


def test_missing_api_key(monkeypatch):
    monkeypatch.setattr(zm, "SMTP2GO_API_KEY", "")
    with respx.mock(assert_all_mocked=True):
        with pytest.raises(RuntimeError, match="SMTP2GO_API_KEY"):
            zm.send_sms(text="Hallo", to="+41791234567", confirm=True)


def test_smtp2go_error_is_raised():
    with respx.mock(assert_all_mocked=True) as mock:
        mock.post(f"{SMTP2GO}/sms/send").respond(status_code=400, json={"data": {"error": "Invalid destination"}})
        with pytest.raises(RuntimeError, match="400"):
            zm.send_sms(text="Hallo", to="+41791234567", confirm=True)


def test_list_received_sms():
    with respx.mock(assert_all_mocked=True) as mock:
        route = mock.post(f"{SMTP2GO}/sms/view-received").respond(json={"data": {"messages": []}})
        r = zm.list_received_sms(start_date="2026-10-01T00:00:00Z")
    assert r == {"data": {"messages": []}}
    assert json.loads(route.calls[0].request.content) == {"start_date": "2026-10-01T00:00:00Z"}
