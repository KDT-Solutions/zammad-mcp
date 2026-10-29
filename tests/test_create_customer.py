import httpx
import pytest

import zammad_mcp as zm

USERS = [
    {"id": 10, "firstname": "Anna", "lastname": "Muster", "email": "anna@kunde.example.com", "login": "anna@kunde.example.com", "organization_id": 5, "active": True},
    {"id": 11, "firstname": "Beat", "lastname": "Beispiel", "email": "beat@example.com", "login": "beat", "organization_id": None, "active": True},
    {"id": 13, "firstname": "Inaktiv", "lastname": "User", "email": "alt@example.com", "login": "alt", "active": False},
]
ORGS = [
    {"id": 5, "name": "Kunde AG", "active": True},
    {"id": 6, "name": "Kunde AG Filiale", "active": True},
    {"id": 7, "name": "Doppelt GmbH", "active": True},
    {"id": 8, "name": "doppelt gmbh", "active": True},
]


class _Resp:
    def __init__(self, data, status=200):
        self._data, self.status_code = data, status
        self.is_success = status < 400
        self.text = str(data)
        self.request = None

    def json(self):
        return self._data


@pytest.fixture
def api(monkeypatch):
    calls = []
    ticket = {"id": 7, "number": "31001", "customer_id": 11, "organization_id": None}

    def api_get(path, params=None):
        if path == "/tickets/7":
            return dict(ticket)
        if path == "/users/search":
            q = params["query"].lower()
            return [u for u in USERS if q in " ".join(str(v) for v in u.values()).lower()]
        if path == "/organizations/search":
            q = params["query"].lower()
            return [o for o in ORGS if q in o["name"].lower()]
        if path.startswith("/users/"):
            return next(u for u in USERS if u["id"] == int(path.rsplit("/", 1)[1]))
        raise AssertionError(path)

    def post(url, headers=None, json=None, timeout=None):
        calls.append(("POST", url, json))
        return _Resp({"id": 99, "active": True, **json})

    def put(url, headers=None, json=None, timeout=None):
        calls.append(("PUT", url, json))
        return _Resp({**ticket, **json})

    monkeypatch.setattr(zm, "ZAMMAD_URL", "https://zammad.example.com")
    monkeypatch.setattr(zm, "api_get", api_get)
    monkeypatch.setattr(httpx, "post", post)
    monkeypatch.setattr(httpx, "put", put)
    zm._CACHE.clear()
    return calls


def test_preview_without_confirm_creates_nothing(api):
    r = zm.create_customer("Neu@Example.com", firstname="Nina", lastname="Neu", organization="kunde ag",
                           phone="044 123 45 67", note="Testnotiz")
    assert r["preview"] is True and r["created"] is False
    assert r["new_customer"] == {
        "email": "neu@example.com", "firstname": "Nina", "lastname": "Neu", "organization": "Kunde AG",
        "organization_id": 5, "phone": "+41441234567", "note": "Testnotiz", "roles": ["Customer"],
    }
    assert api == []


def test_confirm_creates_customer_role_only(api):
    r = zm.create_customer("neu@example.com", firstname="Nina", phone="079 123 45 67", confirm=True)
    assert r == {"success": True, "created": True, "already_existed": False, "id": 99,
                 "name": "Nina", "email": "neu@example.com", "active": True}
    method, url, payload = api[0]
    assert (method, url) == ("POST", "https://zammad.example.com/api/v1/users")
    assert payload["roles"] == ["Customer"] and payload["phone"] == "+41791234567"
    assert "organization_id" not in payload and len(api) == 1


def test_existing_email_returns_existing(api):
    r = zm.create_customer("ANNA@kunde.example.com", confirm=True)
    assert r == {"success": True, "created": False, "already_existed": True, "id": 10,
                 "name": "Anna Muster", "email": "anna@kunde.example.com", "active": True}
    r = zm.create_customer("alt@example.com", confirm=True)
    assert r["already_existed"] is True and r["active"] is False
    assert api == []


def test_no_extra_roles_possible(api):
    with pytest.raises(TypeError):
        zm.create_customer("neu@example.com", roles=["Admin"], confirm=True)
    import asyncio
    tool = next(t for t in asyncio.run(zm.list_tools()) if t.name == "create_customer")
    assert tool.inputSchema["additionalProperties"] is False
    assert "roles" not in tool.inputSchema["properties"]
    assert api == []


@pytest.mark.parametrize("email", ["", "kein-at", "a@b", "a b@example.com", "a@example.com, b@example.com", "Name <a@example.com>", "a..b@example.com"])
def test_invalid_email_rejected(api, email):
    with pytest.raises(ValueError):
        zm.create_customer(email, confirm=True)
    assert api == []


def test_unknown_or_ambiguous_organization_rejected(api):
    with pytest.raises(ValueError, match="nicht gefunden"):
        zm.create_customer("neu@example.com", organization="Gibts AG", confirm=True)
    with pytest.raises(ValueError, match="mehrdeutig"):
        zm.create_customer("neu@example.com", organization="Doppelt GmbH", confirm=True)
    assert api == []


def test_invalid_phone_rejected(api):
    with pytest.raises(ValueError):
        zm.create_customer("neu@example.com", phone="12345", confirm=True)
    assert api == []


def test_sms_normalize_still_rejects_landline():
    with pytest.raises(ValueError):
        zm._normalize_phone("044 123 45 67")
    assert zm._normalize_phone("044 123 45 67", landline=True) == "+41441234567"


# --- set_ticket_customer mit create_if_missing ---------------------------------

def test_set_customer_unknown_without_flag_still_rejected(api):
    with pytest.raises(ValueError, match="nicht gefunden"):
        zm.set_ticket_customer(7, "neu@example.com", confirm=True)
    assert api == []


def test_set_customer_create_preview(api):
    r = zm.set_ticket_customer(7, "neu@example.com", create_if_missing=True, firstname="Nina", lastname="Neu")
    assert r["preview"] is True and r["changed"] is False
    assert r["new_customer_will_be_created"] is True
    assert r["new_customer"] == "Nina Neu <neu@example.com>"
    assert r["customer_to_create"]["roles"] == ["Customer"]
    assert "neu angelegt" in r["hinweis"]
    assert api == []


def test_set_customer_create_and_assign(api):
    r = zm.set_ticket_customer(7, "neu@example.com", create_if_missing=True, firstname="Nina", lastname="Neu", confirm=True)
    assert r["success"] and r["customer_created"]["id"] == 99
    assert r["new_customer"] == "Nina Neu <neu@example.com>"
    assert api[0][0] == "POST" and api[0][2]["roles"] == ["Customer"]
    assert api[1] == ("PUT", "https://zammad.example.com/api/v1/tickets/7", {"customer_id": 99, "organization_id": None})


def test_set_customer_create_needs_email_and_skips_inactive(api):
    with pytest.raises(ValueError, match="E-Mail"):
        zm.set_ticket_customer(7, "Nina Neu", create_if_missing=True, confirm=True)
    with pytest.raises(ValueError, match="inaktiv"):
        zm.set_ticket_customer(7, "alt@example.com", create_if_missing=True, confirm=True)
    assert api == []


def test_set_customer_existing_with_flag_behaves_as_before(api):
    r = zm.set_ticket_customer(7, "anna@kunde.example.com", create_if_missing=True, confirm=True)
    assert "customer_created" not in r
    assert api == [("PUT", "https://zammad.example.com/api/v1/tickets/7", {"customer_id": 10, "organization_id": 5})]


def test_tool_registered():
    import asyncio
    assert "create_customer" in {t.name for t in asyncio.run(zm.list_tools())}
    assert zm.TOOL_FUNCS["create_customer"] is zm.create_customer
