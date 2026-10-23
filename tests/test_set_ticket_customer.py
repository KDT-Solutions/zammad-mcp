import httpx
import pytest

import zammad_mcp as zm

USERS = [
    {"id": 10, "firstname": "Anna", "lastname": "Muster", "email": "anna@kunde.example.com", "login": "anna@kunde.example.com", "organization_id": 5, "active": True},
    {"id": 11, "firstname": "Beat", "lastname": "Beispiel", "email": "beat@example.com", "login": "beat", "organization_id": None, "active": True},
    {"id": 12, "firstname": "Anna", "lastname": "Muster", "email": "anna.m@andere.example.com", "login": "annam", "active": True},
    {"id": 13, "firstname": "Inaktiv", "lastname": "User", "email": "alt@example.com", "login": "alt", "active": False},
]


class _Resp:
    def __init__(self, data, status=200):
        self._data, self.status_code = data, status
        self.is_success = status < 400
        self.text = str(data)

    def json(self):
        return self._data


@pytest.fixture
def api(monkeypatch):
    puts = []
    ticket = {"id": 7, "number": "31001", "customer_id": 11, "organization_id": None}

    def api_get(path, params=None):
        if path == "/tickets/7":
            return dict(ticket)
        if path == "/users/search":
            q = params["query"].lower()
            return [u for u in USERS if q in " ".join(str(v) for v in u.values()).lower()]
        if path.startswith("/users/"):
            return next(u for u in USERS if u["id"] == int(path.rsplit("/", 1)[1]))
        raise AssertionError(path)

    def put(url, headers=None, json=None, timeout=None):
        puts.append((url, json))
        return _Resp({**ticket, **json})

    monkeypatch.setattr(zm, "ZAMMAD_URL", "https://zammad.example.com")
    monkeypatch.setattr(zm, "api_get", api_get)
    monkeypatch.setattr(httpx, "put", put)
    zm._CACHE.clear()
    return puts


def test_change_customer_by_email_sets_organization(api):
    r = zm.set_ticket_customer(7, "ANNA@kunde.example.com", confirm=True)
    assert r["success"] and r["old_customer"] == "Beat Beispiel <beat@example.com>"
    assert r["new_customer"] == "Anna Muster <anna@kunde.example.com>"
    assert api == [("https://zammad.example.com/api/v1/tickets/7", {"customer_id": 10, "organization_id": 5})]


def test_change_customer_by_login_without_organization(api, monkeypatch):
    zm.set_ticket_customer(7, "annam", confirm=True)
    assert api[-1][1] == {"customer_id": 12, "organization_id": None}


def test_ambiguous_name_is_rejected(api):
    with pytest.raises(ValueError, match="mehrdeutig"):
        zm.set_ticket_customer(7, "Anna Muster")
    assert api == []


def test_unknown_or_inactive_is_rejected(api):
    with pytest.raises(ValueError, match="nicht gefunden"):
        zm.set_ticket_customer(7, "niemand@example.com")
    with pytest.raises(ValueError, match="nicht gefunden"):
        zm.set_ticket_customer(7, "alt@example.com")
    with pytest.raises(ValueError, match="fehlt"):
        zm.set_ticket_customer(7, " ")
    assert api == []


def test_same_customer_is_noop(api):
    r = zm.set_ticket_customer(7, "beat", confirm=True)
    assert r["unchanged"] is True and api == []


def test_preview_without_confirm_changes_nothing(api):
    r = zm.set_ticket_customer(7, "anna@kunde.example.com")
    assert r["preview"] is True and r["changed"] is False
    assert r["old_customer"] == "Beat Beispiel <beat@example.com>"
    assert r["new_customer"] == "Anna Muster <anna@kunde.example.com>"
    assert api == []


def test_tool_registered():
    import asyncio
    assert "set_ticket_customer" in {t.name for t in asyncio.run(zm.list_tools())}
    assert zm.TOOL_FUNCS["set_ticket_customer"] is zm.set_ticket_customer
