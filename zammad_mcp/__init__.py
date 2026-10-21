"""
Zammad MCP Server
Ermöglicht Claude direkten Zugriff auf Zammad Tickets und Anhänge.

Laeuft ausschliesslich als HTTP-Dienst (Streamable HTTP) im Docker-Container hinter
einem Reverse-Proxy und erfordert MCP_AUTH_TOKEN. Ein lokaler Betrieb (stdio/uvx) ist
nicht vorgesehen.

Alle Instanz-spezifischen Werte (Zammad-URL, Token, Auth-Token) werden ausschliesslich
über Umgebungsvariablen gesetzt - es sind keine echten Adressen oder Zugangsdaten
in diesem Code hinterlegt.

HTTP-Transport-Hinweis (2026-09-16):
Der HTTP-Modus laeuft NICHT mehr ueber FastMCP.streamable_http_app(), sondern - wie
bexio-mcp, wo dasselbe Setup nachweislich stabil laeuft - ueber den rohen
mcp.server.Server + StreamableHTTPSessionManager, von Hand in eine Starlette-App
verdrahtet. Grund: mit identischem NPM-Reverse-Proxy, identischem json_response=True
und identischem stateful-Modus blieb der FastMCP-Pfad "Missing session ID (-32600)"
liefern, waehrend der handverdrahtete SessionManager-Pfad bei bexio zuverlaessig
funktioniert. FastMCPs eigene interne Verdrahtung von streamable_http_app() ist damit
als Unterschied ausgeschlossen.
"""

import asyncio
import base64
import bisect
import hashlib
import ipaddress
import json
import logging
import os
import queue
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, wait
from urllib.parse import unquote, urlparse
from typing import Any

import httpx
from mcp.server import Server
from mcp.types import Tool, TextContent

from . import attachment_text as _attachment_text



# Version = <Major.Minor>.<Patch>. Major.Minor kommt aus pyproject.toml (von Hand
# gepflegt), der Patch-Teil zaehlt automatisch: Anzahl Commits, die eine der
# build-relevanten Dateien (Paket, pyproject.toml, Dockerfile, requirements.txt,
# Workflow) geaendert haben. Im Docker-Image setzt GitHub Actions die fertige
# Version als APP_VERSION, lokal (Git-Checkout) wird sie aus der Git-Historie berechnet.
_REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_VERSION_PATHS = ['Dockerfile', 'requirements.txt', 'pyproject.toml', 'zammad_mcp/', '.github/workflows/docker-publish.yml']


def _version_base() -> str:
    """Major.Minor aus pyproject.toml (Checkout/Docker), sonst aus den installierten Paket-Metadaten."""
    raw = ""
    try:
        import tomllib
        with open(os.path.join(_REPO_DIR, "pyproject.toml"), "rb") as f:
            raw = tomllib.load(f)["project"]["version"]
    except Exception:
        try:
            from importlib.metadata import version
            raw = version("zammad-mcp")
        except Exception:
            pass
    return ".".join(raw.split(".")[:2]) if raw else "0.0"


def _read_version() -> str:
    env_version = os.environ.get("APP_VERSION", "").strip()
    if env_version:
        return env_version
    base = _version_base()
    try:
        import subprocess
        count = subprocess.run(
            ["git", "rev-list", "--count", "HEAD", "--", *_VERSION_PATHS],
            cwd=_REPO_DIR, capture_output=True, text=True, timeout=5, check=True,
        ).stdout.strip()
        if count.isdigit():
            return f"{base}.{count}"
    except Exception:
        pass
    return f"{base}.0-dev"


__version__ = _read_version()

ZAMMAD_URL = os.environ.get("ZAMMAD_URL", "")
ZAMMAD_TOKEN = os.environ.get("ZAMMAD_TOKEN", "")


def get_headers() -> dict:
    return {
        "Authorization": f"Token token={ZAMMAD_TOKEN}",
        "Content-Type": "application/json",
    }


def api_get(path: str, params: dict = None) -> Any:
    if not ZAMMAD_URL:
        raise RuntimeError("ZAMMAD_URL ist nicht gesetzt (Umgebungsvariable fehlt)")
    url = f"{ZAMMAD_URL}/api/v1{path}"
    response = httpx.get(url, headers=get_headers(), params=params, timeout=30)
    response.raise_for_status()
    return response.json()


_CACHE: dict = {}


def _name_map(path: str) -> dict:
    """Id -> Name (z.B. Gruppen, Prioritaeten), pro Prozess gecacht. Bei Fehler leer (Fallback auf ID)."""
    if path not in _CACHE:
        try:
            _CACHE[path] = {str(x["id"]): x["name"] for x in api_get(path)}
        except Exception:
            return {}
    return _CACHE[path]


def _user_label(user_id, assets: dict = None):
    """'Vorname Nachname <mail>' zu einer User-ID, gecacht. Nutzt vorhandene Search-Assets vor einem API-Call."""
    if not user_id:
        return None
    key = f"user:{user_id}"
    if key in _CACHE:
        return _CACHE[key]
    u = ((assets or {}).get("User") or {}).get(str(user_id))
    if u is None:
        try:
            u = api_get(f"/users/{user_id}")
        except Exception:
            return f"User {user_id}"
    name = " ".join(x for x in (u.get("firstname"), u.get("lastname")) if x).strip()
    mail = u.get("email") or u.get("login")
    label = f"{name} <{mail}>" if name and mail else (mail or name or f"User {user_id}")
    _CACHE[key] = label
    return label


def _ticket_meta(t: dict, assets: dict = None) -> dict:
    """Gruppe, Prioritaet, Kunde und Owner eines Tickets (Owner-ID 1 = System, wird weggelassen)."""
    owner_id = t.get("owner_id")
    return {
        "group": _name_map("/groups").get(str(t.get("group_id")), t.get("group_id")),
        "priority": _name_map("/ticket_priorities").get(str(t.get("priority_id")), t.get("priority_id")),
        "customer": _user_label(t.get("customer_id"), assets),
        "owner": _user_label(owner_id, assets) if owner_id and owner_id != 1 else None,
    }


def search_tickets(query: str, limit: int = 20) -> list[dict]:
    """Tickets in Zammad suchen."""
    result = api_get("/tickets/search", params={"query": query, "limit": limit})
    # Zammad returns either a list or {assets: {Ticket: {}}, ticket_ids: [...]}
    all_assets: dict = {}
    if isinstance(result, list):
        tickets = result
    else:
        ticket_ids = result.get("ticket_ids", [])
        all_assets = result.get("assets", {}) or {}
        assets = all_assets.get("Ticket", {})
        tickets = [assets[str(tid)] for tid in ticket_ids if str(tid) in assets]

    # Fetch state names if not already available
    try:
        states = {str(s["id"]): s["name"] for s in api_get("/ticket_states")}
    except Exception:
        states = {}

    return [
        {
            "id": t["id"],
            "number": t.get("number"),
            "title": t.get("title"),
            "state": states.get(str(t.get("state_id")), t.get("state_id")),
            "created_at": t.get("created_at"),
            "updated_at": t.get("updated_at"),
            **_ticket_meta(t, all_assets),
        }
        for t in tickets
    ]


def get_ticket(ticket_id: int) -> dict:
    """Ein einzelnes Ticket mit allen Artikeln und Anhängen abrufen."""
    ticket = api_get(f"/tickets/{ticket_id}", params={"expand": "true"})
    articles = api_get(f"/ticket_articles/by_ticket/{ticket_id}")
    try:
        states = {str(s["id"]): s["name"] for s in api_get("/ticket_states")}
    except Exception:
        states = {}

    return {
        "id": ticket["id"],
        "number": ticket.get("number"),
        "title": ticket.get("title"),
        "state": states.get(str(ticket.get("state_id")), ticket.get("state_id")),
        "created_at": ticket.get("created_at"),
        "updated_at": ticket.get("updated_at"),
        "pending_time": ticket.get("pending_time"),
        **_ticket_meta(ticket),
        "articles": [
            {
                "id": a["id"],
                "from": a.get("from"),
                "subject": a.get("subject"),
                "body": a.get("body", ""),
                "created_at": a.get("created_at"),
                "attachments": [
                    {
                        "id": att["id"],
                        "filename": att["filename"],
                        "size": att.get("size"),
                        "content_type": att.get("preferences", {}).get("Content-Type", ""),
                    }
                    for att in a.get("attachments", [])
                ],
            }
            for a in articles
        ],
    }


def list_recent_tickets(limit: int = 25) -> list[dict]:
    """Die neuesten Tickets auflisten."""
    tickets = api_get("/tickets", params={"per_page": limit, "page": 1, "sort_by": "created_at", "order_by": "desc"})
    if isinstance(tickets, list):
        return [
            {
                "id": t["id"],
                "number": t.get("number"),
                "title": t.get("title"),
                "created_at": t.get("created_at"),
                **_ticket_meta(t),
            }
            for t in tickets
        ]
    return []


def download_attachment(ticket_id: int, article_id: int, attachment_id: int) -> dict:
    """Einen Anhang aus einem Ticket-Artikel herunterladen (base64-kodiert)."""
    if not ZAMMAD_URL:
        raise RuntimeError("ZAMMAD_URL ist nicht gesetzt (Umgebungsvariable fehlt)")
    url = f"{ZAMMAD_URL}/api/v1/ticket_attachment/{ticket_id}/{article_id}/{attachment_id}"
    response = httpx.get(url, headers=get_headers(), timeout=60)
    response.raise_for_status()
    return {
        "content_type": response.headers.get("Content-Type", ""),
        "size_bytes": len(response.content),
        "content_base64": base64.b64encode(response.content).decode("utf-8"),
    }


def find_invoice_aggregation_tickets(limit: int = 50) -> list[dict]:
    """
    Tickets mit Invoice Aggregation Excel-Anhängen suchen.
    Gibt eine Liste sortiert nach Dateiname (= Datum) zurück.
    """
    result = api_get("/tickets/search", params={"query": "from:noreply@marketplace.also.ch", "limit": limit})

    if isinstance(result, list):
        tickets = result
    else:
        ticket_ids = result.get("ticket_ids", [])
        assets = result.get("assets", {}).get("Ticket", {})
        tickets = [assets[str(tid)] for tid in ticket_ids if str(tid) in assets]

    found = []
    for t in tickets:
        ticket_id = t["id"]
        try:
            articles = api_get(f"/ticket_articles/by_ticket/{ticket_id}")
            for article in articles:
                for att in article.get("attachments", []):
                    fname = att.get("filename", "")
                    if fname.endswith(".xlsx"):
                        found.append({
                            "ticket_id": ticket_id,
                            "ticket_number": t.get("number"),
                            "ticket_title": t.get("title"),
                            "article_id": article["id"],
                            "attachment_id": att["id"],
                            "filename": fname,
                            "size": att.get("size"),
                        })
        except Exception:
            continue

    found.sort(key=lambda x: x["filename"], reverse=True)
    return found


def list_overviews() -> list[dict]:
    """Alle Ticket-Übersichten (Kategorien) in Zammad auflisten, z.B. Offen, Wartend, etc."""
    overviews = api_get("/ticket_overviews")
    return [{"id": o["id"], "name": o["name"], "link": o.get("link", ""), "count": o.get("count", 0)} for o in overviews]


def get_open_tickets(limit: int = 100) -> list[dict]:
    """Alle offenen Tickets abrufen (state: new oder open)."""
    return _get_tickets_by_states(["new", "open"], limit)


def get_pending_reached_tickets(limit: int = 100) -> list[dict]:
    """Alle 'Warten erreicht' Tickets abrufen — pending reminder Tickets wo die Wartezeit abgelaufen ist."""
    from datetime import datetime, timezone
    tickets = _get_tickets_by_states(["pending reminder"], limit * 2)
    now = datetime.now(timezone.utc)
    reached = []
    for t in tickets:
        # Fetch full ticket to get pending_time
        try:
            full = api_get(f"/tickets/{t['id']}")
            pt = full.get("pending_time")
            if pt:
                pending_dt = datetime.fromisoformat(pt.replace("Z", "+00:00"))
                if pending_dt <= now:
                    t["pending_time"] = pt
                    reached.append(t)
            else:
                reached.append(t)
        except Exception:
            continue
    return reached[:limit]


def _get_tickets_by_states(state_names: list[str], limit: int) -> list[dict]:
    try:
        states = api_get("/ticket_states")
        state_ids = {str(s["id"]): s["name"] for s in states}
        target_ids = [str(s["id"]) for s in states if s["name"] in state_names]
    except Exception:
        state_ids = {}
        target_ids = []

    query = " OR ".join(f"state_id:{sid}" for sid in target_ids) if target_ids else "*"
    result = api_get("/tickets/search", params={"query": query, "limit": limit, "sort_by": "updated_at", "order_by": "desc"})

    all_assets: dict = {}
    if isinstance(result, list):
        tickets = result
    else:
        ticket_ids = result.get("ticket_ids", [])
        all_assets = result.get("assets", {}) or {}
        assets = all_assets.get("Ticket", {})
        tickets = [assets[str(tid)] for tid in ticket_ids if str(tid) in assets]

    return [
        {
            "id": t["id"],
            "number": t.get("number"),
            "title": t.get("title"),
            "state": state_ids.get(str(t.get("state_id")), str(t.get("state_id"))),
            "updated_at": t.get("updated_at"),
            **_ticket_meta(t, all_assets),
        }
        for t in tickets
        if not state_names or state_ids.get(str(t.get("state_id"))) in state_names
    ]


def _resolve_group(group: str) -> tuple[int, str]:
    """Gruppenname -> (id, name). Kein stiller Fallback: unbekannt/leer ist ein Fehler mit Liste der gueltigen Gruppen."""
    groups = api_get("/groups")
    valid = ", ".join(sorted(g["name"] for g in groups if g.get("active", True)))
    if not group or not group.strip():
        raise ValueError(f"Gruppe fehlt. Gueltige Gruppen: {valid}")
    match = next((g for g in groups if g["name"].lower() == group.strip().lower()), None)
    if not match:
        raise ValueError(f"Gruppe '{group}' nicht gefunden. Gueltige Gruppen: {valid}")
    return match["id"], match["name"]


def get_version() -> dict:
    """Version des laufenden Zammad-MCP-Servers."""
    return {"name": "zammad-mcp", "version": __version__, "commit": os.environ.get("GIT_SHA", "unbekannt")}


def list_groups() -> list[dict]:
    """Alle aktiven Zammad-Gruppen (id, name) auflisten."""
    return [{"id": g["id"], "name": g["name"]} for g in api_get("/groups") if g.get("active", True)]


def set_ticket_group(ticket_id: int, group: str) -> dict:
    """Ticket in eine andere Gruppe verschieben (exakter Gruppenname, siehe list_groups)."""
    group_id, group_name = _resolve_group(group)
    url = f"{ZAMMAD_URL}/api/v1/tickets/{ticket_id}"
    response = httpx.put(url, headers=get_headers(), json={"group_id": group_id}, timeout=30)
    response.raise_for_status()
    return {"success": True, "ticket_id": ticket_id, "group": group_name}


def _resolve_agent(owner: str) -> tuple[int, str]:
    """E-Mail, Login oder 'Vorname Nachname' -> (id, label). Nur aktive Agents/Admins, exakter Treffer,
    kein stiller Fallback: nicht gefunden oder mehrdeutig ist ein Fehler mit Liste der gefundenen Agents."""
    query = (owner or "").strip()
    if not query:
        raise ValueError("Owner fehlt. Erwartet: E-Mail, Login oder 'Vorname Nachname' eines aktiven Agents")
    users = api_get("/users/search", params={"query": query, "limit": 20, "expand": "true"})
    agents = [
        u for u in users
        if u.get("active") and {"Agent", "Admin"} & set(u.get("roles") or [])
    ]
    q = query.lower()
    matches = []
    for u in agents:
        full_name = " ".join(x for x in (u.get("firstname"), u.get("lastname")) if x).strip()
        candidates = {(u.get("email") or "").lower(), (u.get("login") or "").lower(), full_name.lower()}
        if q in candidates:
            matches.append(u)
    found = ", ".join(_user_label(u["id"], {"User": {str(u["id"]): u}}) for u in agents) or "keine"
    if not matches:
        raise ValueError(f"Agent '{owner}' nicht gefunden. Gefundene Agents: {found}")
    if len(matches) > 1:
        raise ValueError(f"Agent '{owner}' mehrdeutig. Gefundene Agents: {found}")
    u = matches[0]
    return u["id"], _user_label(u["id"], {"User": {str(u["id"]): u}})


def set_ticket_owner(ticket_id: int, owner: str) -> dict:
    """Ticket einem Agent zuweisen. owner = E-Mail, Login oder 'Vorname Nachname'; 'none' entfernt die Zuweisung."""
    if (owner or "").strip().lower() in ("none", "niemand", "-"):
        owner_id, label = 1, None
    else:
        owner_id, label = _resolve_agent(owner)
    url = f"{ZAMMAD_URL}/api/v1/tickets/{ticket_id}"
    response = httpx.put(url, headers=get_headers(), json={"owner_id": owner_id}, timeout=30)
    if not response.is_success:
        return {"success": False, "status_code": response.status_code, "error": response.text}
    return {"success": True, "ticket_id": ticket_id, "owner": label}


def _resolve_customer(customer: str) -> dict:
    """E-Mail, Login oder 'Vorname Nachname' -> User-Dict. Nur aktive User, exakter Treffer,
    kein stiller Fallback und keine automatische Neuanlage: nicht gefunden oder mehrdeutig
    ist ein Fehler mit Liste der gefundenen User."""
    query = (customer or "").strip()
    if not query:
        raise ValueError("Kunde fehlt. Erwartet: E-Mail, Login oder 'Vorname Nachname' eines bestehenden Kunden")
    users = [u for u in api_get("/users/search", params={"query": query, "limit": 20}) if u.get("active", True)]
    q = query.lower()
    matches = []
    for u in users:
        full_name = " ".join(x for x in (u.get("firstname"), u.get("lastname")) if x).strip()
        if q in {(u.get("email") or "").lower(), (u.get("login") or "").lower(), full_name.lower()}:
            matches.append(u)
    found = ", ".join(_user_label(u["id"], {"User": {str(u["id"]): u}}) for u in users) or "keine"
    if not matches:
        raise ValueError(f"Kunde '{customer}' nicht gefunden. Gefundene User: {found}")
    if len(matches) > 1:
        raise ValueError(f"Kunde '{customer}' mehrdeutig. Gefundene User: {found}")
    return matches[0]


def set_ticket_customer(ticket_id: int, customer: str) -> dict:
    """Kunde eines Tickets wechseln. customer = E-Mail, Login oder 'Vorname Nachname' eines bestehenden,
    aktiven Users. Die Organisation des Tickets wird wie in der Zammad-Oberflaeche auf die Organisation
    des neuen Kunden gesetzt (bzw. geleert, wenn er keine hat)."""
    ticket = api_get(f"/tickets/{ticket_id}")
    new = _resolve_customer(customer)
    old_id = ticket.get("customer_id")
    old_label = _user_label(old_id) if old_id and old_id != 1 else None
    new_label = _user_label(new["id"], {"User": {str(new["id"]): new}})
    if old_id == new["id"]:
        return {"success": True, "ticket_id": ticket_id, "unchanged": True, "customer": new_label}
    payload = {"customer_id": new["id"], "organization_id": new.get("organization_id")}
    url = f"{ZAMMAD_URL}/api/v1/tickets/{ticket_id}"
    response = httpx.put(url, headers=get_headers(), json=payload, timeout=30)
    if not response.is_success:
        return {"success": False, "status_code": response.status_code, "error": response.text}
    updated = response.json()
    result = {
        "success": True,
        "ticket_id": ticket_id,
        "ticket_number": updated.get("number", ticket.get("number")),
        "old_customer": old_label,
        "new_customer": new_label,
    }
    if updated.get("customer_id") != new["id"]:
        result["success"] = False
        result["error"] = f"Zammad hat den Kunden nicht uebernommen (customer_id ist {updated.get('customer_id')})"
    return result


def update_customer_name(firstname: str = None, lastname: str = None, email: str = "", ticket_id: int = None) -> dict:
    """Vor- und/oder Nachname eines Kunden korrigieren. Kunde wird ueber exakte E-Mail oder ueber ticket_id
    (Kunde des Tickets) bestimmt, genau eines von beiden. Nicht uebergebene Namensfelder bleiben unveraendert."""
    if bool((email or "").strip()) == bool(ticket_id):
        raise ValueError("Genau eines von 'email' oder 'ticket_id' angeben")
    if firstname is None and lastname is None:
        raise ValueError("Mindestens 'firstname' oder 'lastname' angeben")
    if ticket_id:
        customer_id = api_get(f"/tickets/{ticket_id}").get("customer_id")
        if not customer_id or customer_id == 1:
            raise ValueError(f"Ticket {ticket_id} hat keinen Kunden")
    else:
        q = email.strip().lower()
        users = api_get("/users/search", params={"query": q, "limit": 20})
        matches = [u for u in users if (u.get("email") or "").lower() == q]
        if not matches:
            raise ValueError(f"Kunde mit E-Mail '{email}' nicht gefunden")
        if len(matches) > 1:
            raise ValueError(f"E-Mail '{email}' mehrdeutig ({len(matches)} User gefunden)")
        customer_id = matches[0]["id"]
    old = _user_label(customer_id)
    data = {}
    if firstname is not None:
        data["firstname"] = firstname.strip()
    if lastname is not None:
        data["lastname"] = lastname.strip()
    url = f"{ZAMMAD_URL}/api/v1/users/{customer_id}"
    response = httpx.put(url, headers=get_headers(), json=data, timeout=30)
    if not response.is_success:
        return {"success": False, "status_code": response.status_code, "error": response.text}
    _CACHE.pop(f"user:{customer_id}", None)
    return {"success": True, "customer_id": customer_id, "old": old, "new": _user_label(customer_id, {"User": {str(customer_id): response.json()}})}


def create_ticket(
    title: str,
    body: str,
    customer_email: str,
    group: str,
    state: str = "new",
    priority: str = "2 normal",
) -> dict:
    """
    Neues Ticket in Zammad erstellen.
    Pflichtfelder: title, body, customer_email, group (exakter Gruppenname, siehe list_groups).
    Optionale Felder: state ('new', 'open', etc.), priority ('1 low', '2 normal', '3 high').
    """
    group_id, group_name = _resolve_group(group)
    # Resolve state_id
    try:
        states = api_get("/ticket_states")
        state_id = next((s["id"] for s in states if s["name"].lower() == state.lower()), None)
        if not state_id:
            state_id = next((s["id"] for s in states if s["name"] == "new"), 1)
    except Exception:
        state_id = 1

    # Resolve priority_id
    try:
        priorities = api_get("/ticket_priorities")
        priority_id = next((p["id"] for p in priorities if p["name"].lower() == priority.lower()), None)
        if not priority_id:
            priority_id = 2
    except Exception:
        priority_id = 2

    # Resolve or create customer by email
    customer_id = None
    try:
        users = api_get("/users/search", params={"query": customer_email, "limit": 5})
        if isinstance(users, list):
            for u in users:
                if u.get("email", "").lower() == customer_email.lower():
                    customer_id = u["id"]
                    break
    except Exception:
        pass

    if not customer_id:
        # Create customer
        try:
            new_user = httpx.post(
                f"{ZAMMAD_URL}/api/v1/users",
                headers=get_headers(),
                json={"email": customer_email, "roles": ["Customer"]},
                timeout=30,
            )
            new_user.raise_for_status()
            customer_id = new_user.json().get("id")
        except Exception:
            customer_id = None

    url = f"{ZAMMAD_URL}/api/v1/tickets"
    payload = {
        "title": title,
        "group": group_name,
        "state_id": state_id,
        "priority_id": priority_id,
        "article": {
            "body": body,
            "type": "phone",
            "sender": "Customer",
            "internal": False,
        },
    }
    if customer_id:
        payload["customer_id"] = customer_id
    else:
        payload["customer"] = customer_email

    response = httpx.post(url, headers=get_headers(), json=payload, timeout=30)
    if not response.is_success:
        return {
            "success": False,
            "status_code": response.status_code,
            "error": response.text,
            "payload_sent": payload,
        }
    ticket = response.json()
    return {
        "success": True,
        "ticket_id": ticket.get("id"),
        "ticket_number": ticket.get("number"),
        "title": ticket.get("title"),
    }


def add_ticket_note(ticket_id: int, body: str) -> dict:
    """
    Interne Notiz zu einem Ticket hinzufügen (nur intern sichtbar, nicht an Kunde).
    """
    url = f"{ZAMMAD_URL}/api/v1/ticket_articles"
    payload = {
        "ticket_id": ticket_id,
        "body": body,
        "type": "note",
        "internal": True,
        "sender": "Agent",
    }
    response = httpx.post(url, headers=get_headers(), json=payload, timeout=30)
    response.raise_for_status()
    return {"success": True, "article_id": response.json().get("id")}


def update_ticket_state(ticket_id: int, state: str) -> dict:
    """
    Ticket-Status ändern. Mögliche Werte: 'new', 'open', 'closed', 'pending reminder', 'pending close'
    """
    try:
        states = api_get("/ticket_states")
        state_map = {s["name"].lower(): s["id"] for s in states}
    except Exception:
        return {"success": False, "error": "Konnte States nicht laden"}

    state_id = state_map.get(state.lower())
    if not state_id:
        return {"success": False, "error": f"Unbekannter State: {state}. Verfügbar: {list(state_map.keys())}"}

    url = f"{ZAMMAD_URL}/api/v1/tickets/{ticket_id}"
    response = httpx.put(url, headers=get_headers(), json={"state_id": state_id}, timeout=30)
    response.raise_for_status()
    return {"success": True, "ticket_id": ticket_id, "new_state": state}


def update_ticket_title(ticket_id: int, title: str) -> dict:
    """
    Ticket-Titel ändern, z.B. um einen generischen Titel (wie 'Dodolock Kontaktformular')
    durch einen zum tatsächlichen Inhalt passenden Titel zu ersetzen.
    """
    url = f"{ZAMMAD_URL}/api/v1/tickets/{ticket_id}"
    response = httpx.put(url, headers=get_headers(), json={"title": title}, timeout=30)
    response.raise_for_status()
    updated = response.json()
    return {"success": True, "ticket_id": ticket_id, "new_title": updated.get("title", title)}


def set_ticket_pending(ticket_id: int, pending_date: str, note: str = "") -> dict:
    """
    Ticket auf 'pending reminder' setzen mit Datum (Format: YYYY-MM-DD).
    Optional: interne Notiz hinterlegen.
    Beispiel: pending_date='2026-06-21'
    """
    try:
        states = api_get("/ticket_states")
        state_id = next((s["id"] for s in states if s["name"] == "pending reminder"), None)
    except Exception:
        return {"success": False, "error": "Konnte States nicht laden"}

    if not state_id:
        return {"success": False, "error": "State 'pending reminder' nicht gefunden"}

    pending_time = f"{pending_date}T08:00:00.000Z"
    url = f"{ZAMMAD_URL}/api/v1/tickets/{ticket_id}"
    response = httpx.put(url, headers=get_headers(), json={
        "state_id": state_id,
        "pending_time": pending_time,
    }, timeout=30)
    response.raise_for_status()

    if note:
        add_ticket_note(ticket_id, note)

    return {"success": True, "ticket_id": ticket_id, "pending_until": pending_date}


def merge_ticket(ticket_id: int, master_ticket_number: str) -> dict:
    """
    Zwei Tickets zusammenfuehren (Zammad Merge-Funktion, native Zammad-Funktion,
    entspricht dem 'Merge'-Button in der Zammad-Oberflaeche).

    Alle Artikel von ticket_id wandern in das Ziel-Ticket (master_ticket_number).
    Das Quellticket (ticket_id) wird danach automatisch geschlossen und in Zammad
    als "merged into #<master_ticket_number>" markiert - es existiert als
    eigenstaendiges Ticket danach nicht mehr sinnvoll separat weiter.

    Args:
        ticket_id: Interne Zammad Ticket-ID des Tickets, das gemergt werden soll
            (dieses Ticket verschwindet als eigenstaendiges Ticket)
        master_ticket_number: Ticket-NUMMER (das kundenseitige "number"-Feld,
            NICHT die interne ID!) des Ziel-Tickets, in das gemergt wird
    """
    url = f"{ZAMMAD_URL}/api/v1/ticket_merge/{ticket_id}/{master_ticket_number}"
    response = httpx.put(url, headers=get_headers(), timeout=30)
    if not response.is_success:
        return {"success": False, "status_code": response.status_code, "error": response.text}
    return {
        "success": True,
        "merged_ticket_id": ticket_id,
        "master_ticket_number": master_ticket_number,
        "result": response.json() if response.content else None,
    }


def set_article_internal(article_id: int, internal: bool = True) -> dict:
    """Einen bestehenden Ticket-Artikel auf intern (oder wieder oeffentlich) stellen."""
    url = f"{ZAMMAD_URL}/api/v1/ticket_articles/{article_id}"
    response = httpx.put(url, headers=get_headers(), json={"internal": internal}, timeout=30)
    response.raise_for_status()
    data = response.json()
    return {"success": True, "article_id": article_id, "internal": data.get("internal")}


def delete_ticket_article(article_id: int) -> dict:
    """Einen Ticket-Artikel (z.B. falsche Notiz) löschen."""
    url = f"{ZAMMAD_URL}/api/v1/ticket_articles/{article_id}"
    response = httpx.delete(url, headers=get_headers(), timeout=30)
    response.raise_for_status()
    return {"success": True, "deleted_article_id": article_id}


# ---------------------------------------------------------------------------
# Dateien von externen URLs laden (z.B. Rechnungs-PDF hinter einem Link in der Mail)
# ---------------------------------------------------------------------------
# Der Server laedt die Datei selbst herunter, damit der Inhalt nie durch das
# Sprachmodell muss (kein Base64-Abtippen, Original-PDF bleibt unveraendert).
# Schutz gegen SSRF: nur http/https, keine privaten/internen Zieladressen
# (auch nicht nach Redirects), Groessenlimit.

URL_ATTACHMENT_MAX_BYTES = int(os.environ.get("URL_ATTACHMENT_MAX_BYTES", str(20 * 1024 * 1024)))
URL_ATTACHMENT_MAX_REDIRECTS = 10


def _assert_public_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError(f"Nur http/https-URLs erlaubt: {url}")
    try:
        infos = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
    except socket.gaierror as e:
        raise ValueError(f"Host nicht aufloesbar: {parsed.hostname} ({e})")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise ValueError(f"Interne/private Zieladresse nicht erlaubt: {parsed.hostname} -> {ip}")


def _filename_from_response(resp: httpx.Response, fallback: str) -> str:
    cd = resp.headers.get("content-disposition", "")
    m = re.search(r"filename\*\s*=\s*[^']*''([^;]+)", cd, re.I)
    if m:
        return unquote(m.group(1).strip().strip('"'))
    m = re.search(r'filename\s*=\s*"?([^";]+)"?', cd, re.I)
    if m:
        return m.group(1).strip()
    name = os.path.basename(urlparse(str(resp.url)).path)
    return name or fallback


def fetch_url_attachment(url: str, filename: str | None = None, require_pdf: bool = True) -> dict:
    """
    Datei von einer oeffentlichen URL laden (Redirects werden einzeln geprueft).
    Gibt ein Zammad-Attachment-Dict zurueck: filename, data (base64), mime-type.
    """
    current = url
    with httpx.Client(timeout=60, follow_redirects=False) as client:
        for _ in range(URL_ATTACHMENT_MAX_REDIRECTS + 1):
            _assert_public_url(current)
            with client.stream("GET", current, headers={"User-Agent": "Mozilla/5.0 (zammad-mcp)"}) as resp:
                if resp.is_redirect:
                    location = resp.headers.get("location")
                    if not location:
                        raise ValueError(f"Redirect ohne Location: {current}")
                    current = str(resp.url.join(location))
                    continue
                resp.raise_for_status()
                chunks, size = [], 0
                for chunk in resp.iter_bytes():
                    size += len(chunk)
                    if size > URL_ATTACHMENT_MAX_BYTES:
                        raise ValueError(f"Datei zu gross (> {URL_ATTACHMENT_MAX_BYTES} Bytes): {url}")
                    chunks.append(chunk)
                content = b"".join(chunks)
                mime = resp.headers.get("content-type", "application/octet-stream").split(";")[0].strip()
                is_pdf = content.startswith(b"%PDF")
                if require_pdf and not is_pdf:
                    raise ValueError(f"Keine PDF unter {url} (Content-Type: {mime}, {size} Bytes)")
                if is_pdf:
                    mime = "application/pdf"
                name = filename or _filename_from_response(resp, "dokument.pdf" if is_pdf else "dokument")
                if is_pdf and not name.lower().endswith(".pdf"):
                    name += ".pdf"
                return {
                    "filename": name,
                    "data": base64.b64encode(content).decode("utf-8"),
                    "mime-type": mime,
                    "_size": size,
                }
    raise ValueError(f"Zu viele Redirects: {url}")


def forward_ticket(
    ticket_id: int,
    article_id: int,
    to: str,
    body: str = "",
    include_attachments: bool = True,
    attachment_filenames: list[str] | None = None,
    attachment_urls: list[str] | None = None,
    attachment_url_filenames: list[str] | None = None,
) -> dict:
    """
    Einen Ticket-Artikel per E-Mail weiterleiten.
    Erstellt einen neuen Email-Artikel im Ticket mit dem Original als Weiterleitung.

    Args:
        ticket_id: Interne Zammad Ticket-ID
        article_id: ID des weiterzuleitenden Artikels
        to: Empfänger E-Mail-Adresse
        body: Optionaler Text vor dem weitergeleiteten Inhalt
        include_attachments: Anhänge mitschicken (Standard: True)
        attachment_filenames: Optionale Liste von Dateinamen - wenn gesetzt, werden NUR
            Anhänge mit passendem Dateinamen mitgeschickt (z.B. nur eine bestimmte PDF,
            ohne weitere Anhänge). Wenn None, gilt include_attachments wie bisher
            (alle oder keine Anhänge).
        attachment_urls: Optionale Liste von URLs (z.B. Rechnungs-Link aus der Mail).
            Der Server laedt jede Datei selbst herunter (Redirects/Tracking-Links werden
            verfolgt) und haengt sie im Original an. Nur PDFs, nur oeffentliche Adressen.
            Schlaegt ein Download fehl, wird NICHT gesendet.
        attachment_url_filenames: Optionale Dateinamen zu attachment_urls (gleiche
            Reihenfolge); sonst Name aus Content-Disposition bzw. URL.
    """
    # Original-Artikel laden
    articles = api_get(f"/ticket_articles/by_ticket/{ticket_id}")
    original = next((a for a in articles if a["id"] == article_id), None)
    if not original:
        return {"success": False, "error": f"Artikel {article_id} nicht gefunden"}

    original_body = original.get("body", "")
    original_subject = original.get("subject", "")
    original_from = original.get("from", "")
    original_date = original.get("created_at", "")

    # Weiterleitungs-Body aufbauen
    forward_intro = f"{body}<br><br>" if body else ""
    forwarded_body = (
        f"{forward_intro}"
        f"---------- Weitergeleitete Nachricht ----------<br>"
        f"Von: {original_from}<br>"
        f"Datum: {original_date}<br>"
        f"Betreff: {original_subject}<br><br>"
        f"{original_body}"
    )

    # Anhänge laden
    attachments = []
    if include_attachments:
        for att in original.get("attachments", []):
            if attachment_filenames is not None and att["filename"] not in attachment_filenames:
                continue
            try:
                att_url = f"{ZAMMAD_URL}/api/v1/ticket_attachment/{ticket_id}/{article_id}/{att['id']}"
                att_resp = httpx.get(att_url, headers=get_headers(), timeout=60)
                att_resp.raise_for_status()
                attachments.append({
                    "filename": att["filename"],
                    "data": base64.b64encode(att_resp.content).decode("utf-8"),
                    "mime-type": att.get("preferences", {}).get("Content-Type", "application/octet-stream"),
                })
            except Exception:
                continue

    # Anhänge von URLs laden (Fehler -> nicht senden, damit nichts ohne Beleg rausgeht)
    url_files = []
    for i, att_url in enumerate(attachment_urls or []):
        name = None
        if attachment_url_filenames and i < len(attachment_url_filenames):
            name = attachment_url_filenames[i] or None
        try:
            fetched = fetch_url_attachment(att_url, filename=name)
        except Exception as e:
            return {"success": False, "error": f"Download fehlgeschlagen, nichts gesendet: {e}"}
        url_files.append({"filename": fetched["filename"], "size": fetched.pop("_size")})
        attachments.append(fetched)

    payload = {
        "ticket_id": ticket_id,
        "to": to,
        "subject": f"Fwd: {original_subject}",
        "body": forwarded_body,
        "type": "email",
        "sender": "Agent",
        "internal": False,
        "content_type": "text/html",
    }
    if attachments:
        payload["attachments"] = attachments

    url = f"{ZAMMAD_URL}/api/v1/ticket_articles"
    response = httpx.post(url, headers=get_headers(), json=payload, timeout=30)
    if not response.is_success:
        return {"success": False, "status_code": response.status_code, "error": response.text}

    result = {"success": True, "article_id": response.json().get("id"), "forwarded_to": to}
    if url_files:
        result["url_attachments"] = url_files
    return result


# ---------------------------------------------------------------------------
# SMS ueber das SMS-Gateway von SMTP2GO
# ---------------------------------------------------------------------------
# Versand per SMTP2GO-API (POST /sms/send), nicht ueber einen Zammad-SMS-Kanal.
# Antworten der Empfaenger kommen nicht ueber diesen Server zurueck, sondern ueber
# die SMTP2GO-Einstellung "Forward replies" als E-Mail in ein Zammad-Postfach.
# Der Versand ist im Ticket als interne Notiz dokumentiert (Typ "note", damit ein
# eventuell konfigurierter Zammad-SMS-Kanal die Nachricht nicht ein zweites Mal sendet).
# Ohne confirm=true wird nichts gesendet, es kommt nur eine Vorschau zurueck.

SMTP2GO_API_URL = os.environ.get("SMTP2GO_API_URL", "").strip().rstrip("/") or "https://api.smtp2go.com/v3"
SMTP2GO_API_KEY = os.environ.get("SMTP2GO_API_KEY", "").strip()
SMTP2GO_SMS_SENDER = os.environ.get("SMTP2GO_SMS_SENDER", "").strip()

# Muss zum "Message size limit" in den SMTP2GO-SMS-Einstellungen passen: ist dort
# "Message truncation" aktiv, wuerde ein laengerer Text sonst still abgeschnitten.
# SMTP2GO selbst erlaubt hoechstens 10 Teile.
try:
    SMS_MAX_UNITS = max(1, min(int(os.environ.get("SMS_MAX_UNITS", "").strip() or 3), 10))
except ValueError:
    SMS_MAX_UNITS = 3

# GSM 03.38: Basiszeichensatz (1 Zeichen) und Erweiterung (2 Zeichen, Escape)
_GSM_BASIC = set(
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
)
_GSM_EXT = set("^{}\\[~]|€\f")


def _normalize_phone(number: str) -> str:
    """Nummer ins E.164-Format (+<Ziffern>) bringen. Internationales Format (+41... oder 0041...)
    oder Schweizer Mobilnummer 07x 123 45 67 (wird zu +417x...). Andere nationale Nummern werden abgelehnt."""
    raw = (number or "").strip()
    if not raw:
        raise ValueError("Telefonnummer fehlt")
    cleaned = re.sub(r"[\s\-./()]", "", raw)
    if cleaned.startswith("00"):
        cleaned = "+" + cleaned[2:]
    elif re.fullmatch(r"07\d{8}", cleaned):
        cleaned = "+41" + cleaned[1:]  # Schweizer Mobilnummer
    if not cleaned.startswith("+"):
        raise ValueError(
            f"Telefonnummer '{number}' nicht erkannt. Erwartet Schweizer Mobilnummer (079 123 45 67) "
            f"oder internationales Format (+41791234567, 0041791234567)"
        )
    digits = cleaned[1:]
    if not digits.isdigit() or not 8 <= len(digits) <= 15 or digits.startswith("0"):
        raise ValueError(f"Ungueltige Telefonnummer: '{number}'")
    return "+" + digits


def _sms_units(text: str) -> dict:
    """Kodierung und Anzahl SMS-Teile schaetzen (GSM-7: 160/153, sonst UCS-2: 70/67).
    SMTP2GO kann mit 'Smart encoding' Sonderzeichen ersetzen - dann wird es eher weniger."""
    if all(c in _GSM_BASIC or c in _GSM_EXT for c in text):
        length = sum(2 if c in _GSM_EXT else 1 for c in text)
        single, multi, encoding = 160, 153, "GSM-7"
    else:
        length = len(text.encode("utf-16-le")) // 2
        single, multi, encoding = 70, 67, "UCS-2"
    units = 1 if length <= single else -(-length // multi)
    return {"encoding": encoding, "length": length, "units": units}


def _smtp2go_post(path: str, payload: dict) -> dict:
    if not SMTP2GO_API_KEY:
        raise RuntimeError("SMTP2GO_API_KEY ist nicht gesetzt (Umgebungsvariable fehlt)")
    response = httpx.post(
        f"{SMTP2GO_API_URL}{path}",
        headers={"X-Smtp2go-Api-Key": SMTP2GO_API_KEY, "Content-Type": "application/json", "Accept": "application/json"},
        json=payload,
        timeout=30,
    )
    try:
        data = response.json()
    except ValueError:
        data = {"raw": response.text}
    if not response.is_success:
        raise RuntimeError(f"SMTP2GO API Fehler {response.status_code}: {json.dumps(data, ensure_ascii=False)}")
    return data


def _ticket_customer_mobile(ticket_id: int) -> str:
    customer_id = api_get(f"/tickets/{ticket_id}").get("customer_id")
    if not customer_id or customer_id == 1:
        raise ValueError(f"Ticket {ticket_id} hat keinen Kunden - Empfaenger 'to' angeben")
    mobile = (api_get(f"/users/{customer_id}").get("mobile") or "").strip()
    if not mobile:
        raise ValueError(
            f"Beim Kunden von Ticket {ticket_id} ({_user_label(customer_id)}) ist keine Mobilnummer hinterlegt - 'to' angeben"
        )
    return mobile


def send_sms(text: str, to: str = "", ticket_id: int = None, sender: str = "", confirm: bool = False) -> dict:
    """SMS ueber SMTP2GO senden. Ohne confirm=true nur Vorschau. Mit ticket_id wird der Versand
    als interne Notiz im Ticket dokumentiert; ohne 'to' wird die Mobilnummer des Ticket-Kunden verwendet."""
    text = (text or "").strip()
    if not text:
        raise ValueError("Text fehlt")
    if not (to or "").strip():
        if not ticket_id:
            raise ValueError("Empfaenger fehlt: 'to' angeben oder 'ticket_id' (Mobilnummer des Ticket-Kunden)")
        to = _ticket_customer_mobile(ticket_id)
    destination = _normalize_phone(to)
    sender_number = _normalize_phone(sender) if (sender or "").strip() else (
        _normalize_phone(SMTP2GO_SMS_SENDER) if SMTP2GO_SMS_SENDER else ""
    )
    stats = _sms_units(text)
    if stats["units"] > SMS_MAX_UNITS:
        raise ValueError(
            f"Text zu lang: {stats['units']} SMS-Teile ({stats['encoding']}, {stats['length']} Zeichen), "
            f"erlaubt sind hoechstens {SMS_MAX_UNITS} (SMS_MAX_UNITS / Message size limit bei SMTP2GO)"
        )
    if ticket_id:
        api_get(f"/tickets/{ticket_id}")  # Ticket muss existieren, bevor etwas gesendet wird

    preview = {
        "to": destination,
        "sender": sender_number or "SMTP2GO-Standardabsender",
        "ticket_id": ticket_id,
        "text": text,
        **stats,
    }
    if not confirm:
        return {"preview": True, "sent": False, "hinweis": "Nichts gesendet. Zum Senden mit confirm=true erneut aufrufen.", **preview}

    payload = {"destination": [destination], "content": text}
    if sender_number:
        payload["sender"] = sender_number
    api_result = _smtp2go_post("/sms/send", payload)

    result = {"success": True, "sent": True, **preview, "smtp2go": api_result}
    if ticket_id:
        note = (
            f"SMS gesendet an {destination} (Absender: {preview['sender']}, {stats['units']} Teil(e))\n\n{text}"
        )
        try:
            result["note_article_id"] = add_ticket_note(ticket_id, note)["article_id"]
        except Exception as e:
            result["note_error"] = f"SMS wurde gesendet, Notiz im Ticket fehlgeschlagen: {e}"
    return result


def list_received_sms(start_date: str = "", end_date: str = "") -> dict:
    """Bei SMTP2GO eingegangene SMS (Antworten) abfragen. Read-only. Datumsangaben ISO-8601 (UTC);
    ohne Angabe liefert SMTP2GO die letzten 7 Tage."""
    payload = {}
    if (start_date or "").strip():
        payload["start_date"] = start_date.strip()
    if (end_date or "").strip():
        payload["end_date"] = end_date.strip()
    return _smtp2go_post("/sms/view-received", payload)


# ---------------------------------------------------------------------------
# Textextraktion aus Anhaengen (get_attachment_text, find_in_attachments)
# ---------------------------------------------------------------------------
# download_attachment liefert Base64, das bei groesseren PDFs im Client abgeschnitten
# wird. Diese Tools extrahieren den Text serverseitig und geben nur Klartext zurueck.
# Strikt read-only: es werden ausschliesslich GET-Requests an Zammad geschickt.
# Die eigentliche Extraktion laeuft in einem eigenen Prozess (attachment_text.py) mit
# Timeout und Speicherlimit, damit kaputte oder boesartige Dateien den Server nicht
# blockieren koennen.

ATTACHMENT_TEXT_MAX_BYTES = int(os.environ.get("ATTACHMENT_TEXT_MAX_BYTES", str(25 * 1024 * 1024)))
ATTACHMENT_TEXT_MAX_PDF_PAGES = int(os.environ.get("ATTACHMENT_TEXT_MAX_PDF_PAGES", "200"))
ATTACHMENT_TEXT_TIMEOUT = float(os.environ.get("ATTACHMENT_TEXT_TIMEOUT", "30"))
ATTACHMENT_TEXT_MAX_MEMORY_MB = int(os.environ.get("ATTACHMENT_TEXT_MAX_MEMORY_MB", "1024"))
ATTACHMENT_TEXT_MAX_CHARS = 50000
FIND_MAX_TICKETS = 200
FIND_TOTAL_TIMEOUT = float(os.environ.get("FIND_IN_ATTACHMENTS_TIMEOUT", "240"))
FIND_WORKERS = 4
FIND_MAX_MATCHES_PER_ATTACHMENT = 50
FIND_MAX_MATCHES_TOTAL = 500
FIND_MAX_LISTED_SKIPPED = 100
REGEX_MAX_LEN = 200
REGEX_TIMEOUT = 2.0

# OCR (Tesseract) fuer Scans und Bilder. OCR_TIMEOUT_SECONDS ist das Zeitbudget pro
# Tool-Aufruf; ist es aufgebraucht, wird mit den fertigen Seiten zurueckgegeben und der
# naechste Aufruf setzt ueber next_page/next_offset fort (fertige Seiten sind gecacht).
def _env_num(name: str, default, cast=float):
    """Zahl aus der Umgebung; leer oder ungueltig -> Standardwert (z.B. leere Portainer-Variable)."""
    try:
        value = cast(os.environ.get(name, "").strip())
        return value if value > 0 else default
    except ValueError:
        return default


OCR_TIMEOUT_SECONDS = _env_num("OCR_TIMEOUT_SECONDS", 50.0)
OCR_MAX_PAGES = _env_num("OCR_MAX_PAGES", 60, int)
OCR_DPI = min(_env_num("OCR_DPI", 300, int), 600)
OCR_LANGS = os.environ.get("OCR_LANGS", "").strip() or "deu+fra+eng+ita"
if not re.match(r"^[A-Za-z_]+(\+[A-Za-z_]+)*$", OCR_LANGS):
    OCR_LANGS = "deu+fra+eng+ita"
# Pixel-Limit gegen Decompression-Bombs (A4 bei 300 dpi sind ca. 8.7 Mio. Pixel)
OCR_MAX_IMAGE_PIXELS = _env_num("OCR_MAX_IMAGE_PIXELS", 100_000_000, int)
OCR_MAX_MEMORY_MB = _env_num("OCR_MAX_MEMORY_MB", 2048, int)
OCR_MAX_PARALLEL = _env_num("OCR_MAX_PARALLEL", 2, int)
OCR_CACHE_DIR = os.environ.get("OCR_CACHE_DIR", "").strip()
OCR_CACHE_TTL_HOURS = _env_num("OCR_CACHE_TTL_HOURS", 24.0)
OCR_MODES = ("auto", "force", "off")

_EXTRACT_WORKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "attachment_text.py")
_TEXT_CACHE: "OrderedDict[tuple, dict]" = OrderedDict()
_TEXT_CACHE_MAX = 16
_TEXT_CACHE_LOCK = threading.Lock()
# Rohdaten der zuletzt geladenen Anhaenge, damit OCR-Folgeaufrufe nicht neu herunterladen
_DATA_CACHE: "OrderedDict[tuple, bytes]" = OrderedDict()
_DATA_CACHE_MAX_BYTES = 64 * 1024 * 1024
# OCR-Text pro Seite (In-Memory-LRU, optional zusaetzlich auf Disk)
_OCR_MEM: "OrderedDict[tuple, str]" = OrderedDict()
_OCR_MEM_MAX_CHARS = 20_000_000
_OCR_MEM_CHARS = 0
_OCR_LOCK = threading.Lock()
_OCR_SEMAPHORE = threading.BoundedSemaphore(max(1, OCR_MAX_PARALLEL))
_OCR_LAST_PRUNE = 0.0

# Log: nie OCR-Text, nur Seitenzahl, Dauer und Zeichenanzahl. Ausgabe nach stderr
# (landet im Container-Log).
_log = logging.getLogger("zammad_mcp.ocr")
if not _log.handlers:
    _handler = logging.StreamHandler(sys.stderr)
    _handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
    _log.addHandler(_handler)
    _log.setLevel(logging.INFO)
    _log.propagate = False


def _attachment_meta(ticket_id: int, article_id: int, attachment_id: int) -> dict:
    """Dateiname, Groesse und Content-Type eines Anhangs aus dem Artikel lesen."""
    article = api_get(f"/ticket_articles/{article_id}")
    if article.get("ticket_id") is not None and int(article["ticket_id"]) != int(ticket_id):
        raise ValueError(f"Artikel {article_id} gehoert nicht zu Ticket {ticket_id}")
    att = next((a for a in article.get("attachments", []) if int(a.get("id", 0)) == int(attachment_id)), None)
    if att is None:
        raise ValueError(f"Anhang {attachment_id} nicht in Artikel {article_id} gefunden")
    return att


def _download_attachment_limited(ticket_id: int, article_id: int, attachment_id: int) -> tuple[bytes, str]:
    """Anhang per GET laden, Abbruch sobald ATTACHMENT_TEXT_MAX_BYTES ueberschritten wird."""
    if not ZAMMAD_URL:
        raise RuntimeError("ZAMMAD_URL ist nicht gesetzt (Umgebungsvariable fehlt)")
    url = f"{ZAMMAD_URL}/api/v1/ticket_attachment/{ticket_id}/{article_id}/{attachment_id}"
    with httpx.stream("GET", url, headers=get_headers(), timeout=60) as resp:
        resp.raise_for_status()
        declared = resp.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > ATTACHMENT_TEXT_MAX_BYTES:
            raise _AttachmentLimit("size", f"Anhang ist {int(declared)} Bytes gross, erlaubt sind maximal {ATTACHMENT_TEXT_MAX_BYTES}")
        chunks, size = [], 0
        for chunk in resp.iter_bytes():
            size += len(chunk)
            if size > ATTACHMENT_TEXT_MAX_BYTES:
                raise _AttachmentLimit("size", f"Anhang ist groesser als {ATTACHMENT_TEXT_MAX_BYTES} Bytes")
            chunks.append(chunk)
        return b"".join(chunks), resp.headers.get("content-type", "")


class _AttachmentLimit(Exception):
    def __init__(self, limit: str, message: str):
        super().__init__(message)
        self.limit = limit


def _run_extraction(data: bytes, kind: str) -> dict:
    """Extraktion im eigenen Prozess mit hartem Timeout (Prozess wird dann beendet)."""
    cmd = [sys.executable, "-I", _EXTRACT_WORKER, kind, str(ATTACHMENT_TEXT_MAX_PDF_PAGES),
           str(ATTACHMENT_TEXT_MAX_MEMORY_MB), str(OCR_MAX_IMAGE_PIXELS)]
    try:
        proc = subprocess.run(cmd, input=data, capture_output=True, timeout=ATTACHMENT_TEXT_TIMEOUT)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"Extraktion nach {ATTACHMENT_TEXT_TIMEOUT:g} s abgebrochen (Timeout)", "limit_exceeded": "timeout"}
    try:
        return json.loads(proc.stdout.decode("utf-8"))
    except Exception:
        stderr = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        return {"ok": False, "error": f"Extraktion fehlgeschlagen (Exit-Code {proc.returncode}): {stderr[-1] if stderr else 'keine Ausgabe'}"}


def _extract_attachment(ticket_id: int, article_id: int, attachment_id: int, meta: dict | None = None) -> dict:
    """Anhang laden und Text extrahieren. Ergebnis immer als Dict mit ok/error, nie Exception
    wegen der Datei selbst (Zammad-/Netzwerkfehler werden ebenfalls als error gemeldet)."""
    key = (int(ticket_id), int(article_id), int(attachment_id))
    with _TEXT_CACHE_LOCK:
        if key in _TEXT_CACHE:
            _TEXT_CACHE.move_to_end(key)
            return _TEXT_CACHE[key]

    info: dict = {"filename": None, "content_type": None}
    try:
        if meta is None:
            meta = _attachment_meta(ticket_id, article_id, attachment_id)
        info["filename"] = meta.get("filename")
        info["content_type"] = (meta.get("preferences") or {}).get("Content-Type") or (meta.get("preferences") or {}).get("Mime-Type")
        size = int(meta.get("size") or 0)
        if size > ATTACHMENT_TEXT_MAX_BYTES:
            return {**info, "ok": False, "size_bytes": size, "limit_exceeded": "size",
                    "error": f"Anhang ist {size} Bytes gross, erlaubt sind maximal {ATTACHMENT_TEXT_MAX_BYTES}"}
        if _attachment_text.detect_kind(info["filename"], info["content_type"]) is None:
            return {**info, "ok": False, "error": _unsupported_msg(info)}
        data, resp_type = _download_attachment_limited(ticket_id, article_id, attachment_id)
        info["content_type"] = info["content_type"] or resp_type.split(";")[0].strip()
        kind = _attachment_text.detect_kind(info["filename"], info["content_type"], data)
        if kind is None:
            return {**info, "ok": False, "size_bytes": len(data), "error": _unsupported_msg(info)}
    except _AttachmentLimit as e:
        return {**info, "ok": False, "limit_exceeded": e.limit, "error": str(e)}
    except httpx.HTTPStatusError as e:
        return {**info, "ok": False, "error": f"Zammad API Fehler {e.response.status_code} @ {e.request.url}"}
    except Exception as e:
        return {**info, "ok": False, "error": f"{type(e).__name__}: {e}"}

    result = {**info, "size_bytes": len(data), "kind": kind, **_run_extraction(data, kind)}
    if result.get("ok") and kind in _attachment_text.PAGED_KINDS:
        _data_cache_put(key, data)
    if result.get("ok"):
        with _TEXT_CACHE_LOCK:
            _TEXT_CACHE[key] = result
            while len(_TEXT_CACHE) > _TEXT_CACHE_MAX:
                _TEXT_CACHE.popitem(last=False)
    return result


def _unsupported_msg(info: dict) -> str:
    return (
        f"Dateityp nicht unterstuetzt: {info.get('filename')} ({info.get('content_type') or 'unbekannt'}). "
        f"Unterstuetzt: PDF, XLSX, DOCX, CSV, TXT sowie PNG, JPEG, TIFF (per OCR)"
    )


def _data_cache_put(key: tuple, data: bytes) -> None:
    if len(data) > _DATA_CACHE_MAX_BYTES:
        return
    with _TEXT_CACHE_LOCK:
        _DATA_CACHE[key] = data
        _DATA_CACHE.move_to_end(key)
        while sum(len(v) for v in _DATA_CACHE.values()) > _DATA_CACHE_MAX_BYTES:
            _DATA_CACHE.popitem(last=False)


def _attachment_bytes(ticket_id: int, article_id: int, attachment_id: int) -> bytes:
    key = (int(ticket_id), int(article_id), int(attachment_id))
    with _TEXT_CACHE_LOCK:
        if key in _DATA_CACHE:
            _DATA_CACHE.move_to_end(key)
            return _DATA_CACHE[key]
    data, _ = _download_attachment_limited(ticket_id, article_id, attachment_id)
    _data_cache_put(key, data)
    return data


# --- OCR-Cache -------------------------------------------------------------
# Schluessel = IDs + Seite + OCR-Einstellungen. Auf Disk nur als SHA-256 dieses
# Schluessels (nie Anhangnamen), Dateien mit 0600, Verzeichnis mit 0700.

def _ocr_key(ticket_id, article_id, attachment_id, page) -> tuple:
    return (int(ticket_id), int(article_id), int(attachment_id), int(page), OCR_DPI, OCR_LANGS)


def _ocr_cache_path(key: tuple) -> str:
    digest = hashlib.sha256(":".join(str(k) for k in key).encode("utf-8")).hexdigest()
    return os.path.join(OCR_CACHE_DIR, f"{digest}.json")


def _ocr_mem_put(key: tuple, text: str) -> None:
    global _OCR_MEM_CHARS
    with _OCR_LOCK:
        if key in _OCR_MEM:
            _OCR_MEM_CHARS -= len(_OCR_MEM.pop(key))
        _OCR_MEM[key] = text
        _OCR_MEM_CHARS += len(text)
        while _OCR_MEM_CHARS > _OCR_MEM_MAX_CHARS and len(_OCR_MEM) > 1:
            _, old = _OCR_MEM.popitem(last=False)
            _OCR_MEM_CHARS -= len(old)


def _ocr_cache_get(key: tuple) -> str | None:
    with _OCR_LOCK:
        if key in _OCR_MEM:
            _OCR_MEM.move_to_end(key)
            return _OCR_MEM[key]
    if not OCR_CACHE_DIR:
        return None
    path = _ocr_cache_path(key)
    try:
        if time.time() - os.stat(path).st_mtime > OCR_CACHE_TTL_HOURS * 3600:
            os.remove(path)
            return None
        with open(path, "r", encoding="utf-8") as f:
            text = json.load(f)["text"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    _ocr_mem_put(key, text)
    return text


def _ocr_cache_prune() -> None:
    """Abgelaufene Disk-Cache-Dateien entfernen (hoechstens alle 10 Minuten)."""
    global _OCR_LAST_PRUNE
    now = time.time()
    if now - _OCR_LAST_PRUNE < 600:
        return
    _OCR_LAST_PRUNE = now
    try:
        for entry in os.scandir(OCR_CACHE_DIR):
            if entry.name.endswith((".json", ".tmp")) and now - entry.stat().st_mtime > OCR_CACHE_TTL_HOURS * 3600:
                os.remove(entry.path)
    except OSError:
        pass


def _ocr_cache_put(key: tuple, text: str) -> None:
    _ocr_mem_put(key, text)
    if not OCR_CACHE_DIR:
        return
    path = _ocr_cache_path(key)
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        os.makedirs(OCR_CACHE_DIR, mode=0o700, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(tmp, flags, 0o600)
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(fd, 0o600)
            os.write(fd, json.dumps({"text": text}, ensure_ascii=False).encode("utf-8"))
        finally:
            os.close(fd)
        os.replace(tmp, path)
    except OSError as e:
        _log.warning("OCR-Cache nicht schreibbar: %s", type(e).__name__)
        try:
            os.remove(tmp)
        except OSError:
            pass
    _ocr_cache_prune()


# --- OCR-Lauf --------------------------------------------------------------

def _kill_process_tree(proc: subprocess.Popen) -> None:
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except (OSError, ProcessLookupError):
        pass


def _run_ocr(data: bytes, kind: str, pages: list[int], deadline: float, on_page) -> dict:
    """OCR-Worker starten und Seitenergebnisse bis zur deadline einsammeln.
    on_page(msg) wird pro fertiger Seite aufgerufen; gibt sie True zurueck, wird abgebrochen.
    Rueckgabe: {"timed_out": bool, "fatal": str|None}."""
    remaining = deadline - time.monotonic()
    if remaining <= 0 or not _OCR_SEMAPHORE.acquire(timeout=remaining):
        return {"timed_out": True, "fatal": None}
    tmpdir = None
    proc = None
    try:
        tmpdir = tempfile.mkdtemp(prefix="zammad-ocr-")
        cmd = [sys.executable, "-I", _EXTRACT_WORKER, "ocr", kind, str(OCR_DPI), OCR_LANGS,
               str(OCR_MAX_IMAGE_PIXELS), str(OCR_MAX_MEMORY_MB), tmpdir, ",".join(str(p) for p in pages)]
        # OMP_THREAD_LIMIT=1: Tesseract-OpenMP-Threads in parallelen Workern bremsen sich
        # gegenseitig massiv aus (gemessen: 0.4 s -> 30 s pro Seite bei 4 parallelen Jobs)
        env = {**os.environ, "OMP_THREAD_LIMIT": "1"}
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                env=env, start_new_session=(os.name == "posix"))
        lines: queue.Queue = queue.Queue()

        def feed():
            try:
                proc.stdin.write(data)
                proc.stdin.close()
            except OSError:
                pass

        def read():
            try:
                for line in proc.stdout:
                    lines.put(line)
            finally:
                lines.put(None)

        threading.Thread(target=feed, daemon=True).start()
        threading.Thread(target=read, daemon=True).start()
        fatal = None
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return {"timed_out": True, "fatal": fatal}
            try:
                line = lines.get(timeout=remaining)
            except queue.Empty:
                return {"timed_out": True, "fatal": fatal}
            if line is None:
                return {"timed_out": False, "fatal": fatal or "OCR-Prozess unerwartet beendet"}
            try:
                msg = json.loads(line.decode("utf-8"))
            except ValueError:
                continue
            if "fatal" in msg:
                fatal = msg["fatal"]
            elif msg.get("done"):
                return {"timed_out": False, "fatal": fatal}
            elif "page" in msg and on_page(msg):
                return {"timed_out": False, "fatal": None}
    finally:
        if proc is not None:
            if proc.poll() is None:
                _kill_process_tree(proc)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            for stream in (proc.stdin, proc.stdout):
                try:
                    if stream:
                        stream.close()
                except OSError:
                    pass
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)
        _OCR_SEMAPHORE.release()


_METHOD_RANK = {"none": 0, "pdfminer": 1, "font_cmap": 2, "gid_offset_heuristic": 3}


def _load_document(ticket_id: int, article_id: int, attachment_id: int, meta: dict | None = None,
                   ocr: str = "auto", page_from: int | None = None, page_to: int | None = None,
                   deadline: float | None = None, stop_after_chars: int | None = None) -> dict:
    """Normale Extraktion plus seitenweise OCR (Scans, Bilder) innerhalb des Zeitbudgets.
    Fuer PDFs/Bilder enthaelt das Ergebnis page_texts {Seite: Text} fuer page_from bis zur
    letzten fertigen Seite; next_page ist gesetzt, wenn noch Seiten fehlen."""
    ocr = (ocr or "auto").strip().lower()
    if ocr not in OCR_MODES:
        raise ValueError(f"ocr muss 'auto', 'force' oder 'off' sein, nicht '{ocr}'")
    if deadline is None:
        deadline = time.monotonic() + OCR_TIMEOUT_SECONDS
    started = time.monotonic()
    base = _extract_attachment(ticket_id, article_id, attachment_id, meta)
    if not base.get("ok") or base.get("kind") not in _attachment_text.PAGED_KINDS:
        return {**base, "paged": False}

    n = int(base["pages"])
    pf = 1 if page_from is None else int(page_from)
    if pf < 1 or pf > n:
        return {"ok": False, "filename": base.get("filename"), "content_type": base.get("content_type"),
                "pages": n, "error": f"page_from={pf} liegt ausserhalb von 1..{n}"}
    pt = n if page_to is None else min(max(int(page_to), pf), n)

    base_texts = base["page_texts"]
    base_methods = base["page_methods"]

    def needs_ocr(p: int) -> bool:
        if ocr == "off":
            return False
        if ocr == "force":
            return True
        return _attachment_text.meaningful_chars(base_texts[p - 1]) < _attachment_text.OCR_MIN_CHARS

    candidates = [p for p in range(pf, pt + 1) if needs_ocr(p)]
    allowed = [p for p in candidates if p <= OCR_MAX_PAGES]
    doc = {
        "ok": True, "paged": True, "kind": base["kind"], "filename": base.get("filename"),
        "content_type": base.get("content_type"), "pages": n, "page_from": pf, "page_to": pt,
        "base_method": base.get("extraction_method"), "page_texts": {}, "page_methods": {},
        "ocr_pages": [], "ocr_failed_pages": [], "ocr_skipped_pages": [p for p in candidates if p > OCR_MAX_PAGES],
        "ocr_error": None, "next_page": None,
    }
    if base["kind"] == "image" and ocr == "off":
        doc["ocr_error"] = "OCR ist ausgeschaltet (ocr='off'), Bilder haben keine Textebene"

    ocr_text: dict[int, str] = {}
    for p in allowed:
        cached = _ocr_cache_get(_ocr_key(ticket_id, article_id, attachment_id, p))
        if cached is not None:
            ocr_text[p] = cached
    waiting = {p for p in allowed if p not in ocr_text}
    failed: set = set()
    state = {"next": pf, "chars": 0}

    def advance() -> bool:
        """Fertige Seiten in Reihenfolge uebernehmen; True, wenn genug Text vorliegt."""
        while state["next"] <= pt:
            p = state["next"]
            if p in waiting and p not in ocr_text and p not in failed:
                break
            if p in ocr_text:
                text, method = ocr_text[p], "ocr"
                doc["ocr_pages"].append(p)
            else:
                text, method = base_texts[p - 1], base_methods[p - 1]
                if p in failed:
                    doc["ocr_failed_pages"].append(p)
            doc["page_texts"][p] = text
            doc["page_methods"][p] = method
            state["chars"] += len(f"--- Seite {p} ---\n") + len(text) + 2
            state["next"] += 1
        return stop_after_chars is not None and state["chars"] > stop_after_chars

    if not advance() and state["next"] <= pt and waiting:
        try:
            data = _attachment_bytes(ticket_id, article_id, attachment_id)
        except Exception as e:
            data = None
            doc["ocr_error"] = f"Anhang fuer OCR nicht ladbar: {type(e).__name__}"
        if data is not None:
            done_this_call = []

            def on_page(msg: dict) -> bool:
                p = int(msg["page"])
                if "error" in msg:
                    failed.add(p)
                    _log.info("OCR Seite %d: Fehler nach %.1f s", p, float(msg.get("seconds", 0)))
                else:
                    ocr_text[p] = msg.get("text", "")
                    _ocr_cache_put(_ocr_key(ticket_id, article_id, attachment_id, p), ocr_text[p])
                    _log.info("OCR Seite %d: %.1f s, %d Zeichen", p, float(msg.get("seconds", 0)), len(ocr_text[p]))
                done_this_call.append(p)
                return advance()

            ocr_start = time.monotonic()
            pending = [p for p in allowed if p in waiting and p >= state["next"]]
            res = _run_ocr(data, base["kind"], pending, deadline, on_page)
            if res.get("fatal"):
                doc["ocr_error"] = res["fatal"]
                waiting.clear()  # restliche Seiten ohne OCR uebernehmen
                advance()
            elif res.get("timed_out") and not done_this_call and state["next"] <= pt:
                # Eine einzelne Seite hat das ganze Budget verbraucht: als fehlgeschlagen
                # markieren, damit der naechste Aufruf nicht wieder an ihr haengen bleibt.
                if time.monotonic() - ocr_start >= 0.8 * OCR_TIMEOUT_SECONDS:
                    failed.add(state["next"])
                    advance()
    if state["next"] <= pt:
        doc["next_page"] = state["next"]

    methods = list(doc["page_methods"].values())
    if methods and all(m == "ocr" for m in methods):
        doc["extraction_method"] = "ocr"
    elif "ocr" in methods:
        doc["extraction_method"] = "mixed"
    else:
        non_ocr = [m for m in methods if m != "ocr"] or [base.get("extraction_method") or "none"]
        doc["extraction_method"] = max(non_ocr, key=lambda m: _METHOD_RANK.get(m, 0))
    by_method: dict[str, list[int]] = {}
    for p, m in doc["page_methods"].items():
        by_method.setdefault(m, []).append(p)
    doc["pages_by_method"] = by_method
    doc["seconds"] = round(time.monotonic() - started, 1)
    return doc


def get_attachment_text(ticket_id: int, article_id: int, attachment_id: int, offset: int = 0, max_chars: int = 20000,
                        ocr: str = "auto", page_from: int | None = None, page_to: int | None = None) -> dict:
    """Text eines Anhangs serverseitig extrahieren (bei Scans/Bildern per OCR) und
    seitenweise (page_from/page_to) bzw. zeichenweise (offset/max_chars) zurueckgeben."""
    deadline = time.monotonic() + OCR_TIMEOUT_SECONDS
    offset = max(0, int(offset or 0))
    max_chars = min(max(1, int(max_chars or 20000)), ATTACHMENT_TEXT_MAX_CHARS)
    doc = _load_document(ticket_id, article_id, attachment_id, ocr=ocr, page_from=page_from, page_to=page_to,
                         deadline=deadline, stop_after_chars=offset + max_chars + 2)
    if not doc.get("ok"):
        out = {k: doc.get(k) for k in ("filename", "content_type", "size_bytes", "pages") if doc.get(k) is not None}
        out["error"] = doc.get("error")
        if doc.get("limit_exceeded"):
            out["limit_exceeded"] = doc["limit_exceeded"]
        return out

    out = {
        "filename": doc.get("filename"),
        "content_type": doc.get("content_type"),
        "extraction_method": doc.get("extraction_method"),
    }
    if doc["paged"]:
        text, _ = _attachment_text.compose_pages(doc["page_texts"])
        out["pages"] = doc["pages"]
        out["page_from"] = doc["page_from"]
        out["page_to"] = doc["page_to"]
        out["ocr_pages"] = doc["ocr_pages"]
        if len(doc["pages_by_method"]) > 1:
            out["pages_by_method"] = doc["pages_by_method"]
        for k in ("ocr_failed_pages", "ocr_skipped_pages"):
            if doc[k]:
                out[k] = doc[k]
        if doc["ocr_skipped_pages"]:
            out["ocr_skipped_reason"] = f"OCR ist auf die ersten {OCR_MAX_PAGES} Seiten begrenzt (OCR_MAX_PAGES)"
        if doc["ocr_error"]:
            out["ocr_error"] = doc["ocr_error"]
    else:
        text = doc["text"]
        if page_from is not None or page_to is not None:
            out["note"] = "page_from/page_to gelten nur fuer PDFs und Bilder, hier ignoriert"

    part = text[offset:offset + max_chars]
    more_chars = offset + len(part) < len(text)
    next_page = doc.get("next_page") if doc["paged"] else None
    out.update({
        "total_chars": len(text),
        "offset": offset,
        "returned_chars": len(part),
        "truncated": bool(more_chars or next_page),
    })
    if more_chars:
        out["next_offset"] = offset + len(part)
    elif next_page:
        # Text bis hier vollstaendig; restliche Seiten mit page_from=next_page abrufen
        out["next_page"] = next_page
        out["ocr_incomplete"] = True
        out["hint"] = f"OCR-Zeitbudget ({OCR_TIMEOUT_SECONDS:g} s) aufgebraucht, mit page_from={next_page} fortsetzen"
    if doc.get("text_limit_reached"):
        out["text_limit_reached"] = True
    out["text"] = part
    return out


def _compile_pattern(pattern: str, use_regex: bool):
    if not pattern:
        raise ValueError("pattern darf nicht leer sein")
    if not use_regex:
        if len(pattern) > 1000:
            raise ValueError("pattern zu lang (max. 1000 Zeichen)")
        return re.compile(re.escape(pattern), re.IGNORECASE), False
    if len(pattern) > REGEX_MAX_LEN:
        raise ValueError(f"Regex zu lang (max. {REGEX_MAX_LEN} Zeichen)")
    try:
        import regex as regex_mod
    except ImportError:
        raise ValueError("Regex-Suche nicht verfuegbar (Paket 'regex' fehlt), bitte regex=false verwenden")
    try:
        return regex_mod.compile(pattern, regex_mod.IGNORECASE | regex_mod.VERSION0), True
    except regex_mod.error as e:
        raise ValueError(f"Ungueltige Regex: {e}")


def _section_at(sections: list[dict], pos: int) -> dict | None:
    if not sections:
        return None
    idx = bisect.bisect_right([s["offset"] for s in sections], pos) - 1
    return sections[idx] if idx >= 0 else None


def find_in_attachments(ticket_query: str, pattern: str, regex: bool = False, limit_tickets: int = 50,
                        context_chars: int = 150, ocr: str = "off") -> dict:
    """Tickets per Ticketsuche finden und alle unterstuetzten Anhaenge nach pattern durchsuchen.
    ocr='auto' durchsucht auch Scans und Bilder per OCR (teuer, nutzt den OCR-Cache)."""
    ocr = (ocr or "off").strip().lower()
    if ocr not in OCR_MODES:
        raise ValueError(f"ocr muss 'auto', 'force' oder 'off' sein, nicht '{ocr}'")
    compiled, is_regex = _compile_pattern(pattern, bool(regex))
    limit_tickets = min(max(1, int(limit_tickets or 50)), FIND_MAX_TICKETS)
    context_chars = min(max(0, int(context_chars if context_chars is not None else 150)), 1000)
    deadline = time.monotonic() + FIND_TOTAL_TIMEOUT

    result = api_get("/tickets/search", params={"query": ticket_query, "limit": limit_tickets})
    if isinstance(result, list):
        tickets = result
    else:
        assets = (result.get("assets") or {}).get("Ticket", {})
        tickets = [assets[str(tid)] for tid in result.get("ticket_ids", []) if str(tid) in assets]
    tickets = tickets[:limit_tickets]

    jobs: list[dict] = []
    seen: set = set()
    skipped: list[dict] = []
    skipped_count = 0
    errors: list[dict] = []
    for t in tickets:
        tinfo = {"ticket_id": t["id"], "ticket_number": t.get("number"), "ticket_title": t.get("title")}
        try:
            articles = api_get(f"/ticket_articles/by_ticket/{t['id']}")
        except Exception as e:
            errors.append({**tinfo, "error": f"Artikel konnten nicht geladen werden: {e}"})
            continue
        for a in articles:
            for att in a.get("attachments", []) or []:
                att_id = att.get("id")
                if att_id in seen:
                    continue
                seen.add(att_id)
                entry = {**tinfo, "article_id": a["id"], "attachment_id": att_id, "filename": att.get("filename")}
                ctype = (att.get("preferences") or {}).get("Content-Type", "")
                kind = _attachment_text.detect_kind(att.get("filename"), ctype)
                if kind is None or (kind == "image" and ocr == "off"):
                    skipped_count += 1
                    if len(skipped) < FIND_MAX_LISTED_SKIPPED:
                        reason = "Dateityp nicht unterstuetzt" if kind is None else "Bild, nur mit ocr='auto' durchsuchbar"
                        skipped.append({**entry, "reason": reason})
                    continue
                jobs.append({"entry": entry, "meta": att})

    def work(job):
        e = job["entry"]
        ocr_deadline = min(deadline, time.monotonic() + OCR_TIMEOUT_SECONDS)
        return _load_document(e["ticket_id"], e["article_id"], e["attachment_id"], job["meta"],
                              ocr=ocr, deadline=ocr_deadline)

    results: list = [None] * len(jobs)
    pool = ThreadPoolExecutor(max_workers=FIND_WORKERS)
    try:
        futures = {pool.submit(work, job): i for i, job in enumerate(jobs)}
        # kleine Reserve: laufende OCR-Jobs geben spaetestens an der deadline zurueck
        done, _ = wait(futures, timeout=max(0.0, deadline - time.monotonic()) + 5)
        for f in done:
            try:
                results[futures[f]] = f.result()
            except Exception as e:
                results[futures[f]] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    matches: list[dict] = []
    without_match: list[dict] = []
    not_checked: list[dict] = []
    ocr_incomplete: list[dict] = []
    total_hits = 0
    for job, res in zip(jobs, results):
        entry = job["entry"]
        if res is None:
            not_checked.append(entry)
            continue
        if not res.get("ok"):
            err = {**entry, "error": res.get("error")}
            if res.get("limit_exceeded"):
                err["limit_exceeded"] = res["limit_exceeded"]
            errors.append(err)
            continue
        if res.get("paged"):
            text, sections = _attachment_text.compose_pages(res["page_texts"])
            ocr_pages = set(res["ocr_pages"])
            if res.get("next_page"):
                ocr_incomplete.append({**entry, "next_page": res["next_page"], "pages": res["pages"]})
        else:
            text, sections, ocr_pages = res["text"], res.get("sections") or [], set()
        hits = 0
        try:
            it = compiled.finditer(text, timeout=REGEX_TIMEOUT) if is_regex else compiled.finditer(text)
            for m in it:
                if m.start() == m.end():
                    continue
                hits += 1
                total_hits += 1
                if hits > FIND_MAX_MATCHES_PER_ATTACHMENT or len(matches) >= FIND_MAX_MATCHES_TOTAL:
                    continue
                hit = {**entry, "match": m.group(0)}
                section = _section_at(sections, m.start())
                if section and section.get("page"):
                    hit["page"] = section["page"]
                    if section["page"] in ocr_pages:
                        hit["ocr"] = True
                if section and section.get("sheet"):
                    hit["sheet"] = section["sheet"]
                hit["context"] = text[max(0, m.start() - context_chars):m.end() + context_chars]
                hit["extraction_method"] = res.get("extraction_method")
                matches.append(hit)
        except TimeoutError:
            errors.append({**entry, "error": f"Regex-Suche nach {REGEX_TIMEOUT:g} s abgebrochen (Timeout)", "limit_exceeded": "regex_timeout"})
            continue
        if hits == 0:
            miss = {**entry, "extraction_method": res.get("extraction_method")}
            if res.get("paged"):
                empty = [p for p, t in res["page_texts"].items()
                         if _attachment_text.meaningful_chars(t) < _attachment_text.OCR_MIN_CHARS]
                if empty and ocr == "off":
                    miss["pages_without_text"] = empty[:50]
                    miss["hint"] = "Seiten ohne Textebene (Scan?), mit ocr='auto' durchsuchbar"
            without_match.append(miss)

    out = {
        "ticket_query": ticket_query,
        "pattern": pattern,
        "regex": is_regex,
        "ocr": ocr,
        "tickets_searched": len(tickets),
        "attachments_checked": sum(1 for r in results if r is not None and r.get("ok")),
        "match_count": total_hits,
        "matches": matches,
        "attachments_without_match": without_match,
    }
    if total_hits > len(matches):
        out["matches_truncated"] = True
    if errors:
        out["errors"] = errors
    if ocr_incomplete:
        out["ocr_incomplete"] = ocr_incomplete
        out["ocr_incomplete_hint"] = ("OCR-Zeitbudget pro Anhang aufgebraucht; fertige Seiten sind gecacht, "
                                      "ein erneuter Aufruf macht weiter (oder get_attachment_text mit page_from=next_page)")
    if not_checked:
        out["not_checked"] = not_checked
        out["not_checked_reason"] = f"Gesamtzeit von {FIND_TOTAL_TIMEOUT:g} s ueberschritten"
    if skipped_count:
        out["skipped_unsupported_count"] = skipped_count
        out["skipped_unsupported"] = skipped
    return out


# ---------------------------------------------------------------------------
# MCP-Server (low-level): Tool-Liste + Dispatch
# ---------------------------------------------------------------------------
# Bewusst der rohe mcp.server.Server statt FastMCP - identisch zu bexio-mcp,
# das mit exakt diesem Unterbau stabil laeuft (siehe Modul-Docstring oben).

server = Server("zammad-connector", version=__version__)

TOOL_FUNCS = {
    "get_version": get_version,
    "search_tickets": search_tickets,
    "get_ticket": get_ticket,
    "list_recent_tickets": list_recent_tickets,
    "download_attachment": download_attachment,
    "get_attachment_text": get_attachment_text,
    "find_in_attachments": find_in_attachments,
    "find_invoice_aggregation_tickets": find_invoice_aggregation_tickets,
    "list_overviews": list_overviews,
    "get_open_tickets": get_open_tickets,
    "get_pending_reached_tickets": get_pending_reached_tickets,
    "create_ticket": create_ticket,
    "list_groups": list_groups,
    "set_ticket_group": set_ticket_group,
    "set_ticket_owner": set_ticket_owner,
    "set_ticket_customer": set_ticket_customer,
    "update_customer_name": update_customer_name,
    "add_ticket_note": add_ticket_note,
    "update_ticket_state": update_ticket_state,
    "update_ticket_title": update_ticket_title,
    "set_ticket_pending": set_ticket_pending,
    "merge_ticket": merge_ticket,
    "delete_ticket_article": delete_ticket_article,
    "set_article_internal": set_article_internal,
    "forward_ticket": forward_ticket,
    "send_sms": send_sms,
    "list_received_sms": list_received_sms,
}


@server.list_tools()
async def list_tools():
    return [
        Tool(
            name="get_version",
            description="Version des laufenden Zammad-MCP-Servers abfragen.",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="search_tickets",
            description="Tickets in Zammad suchen.",
            inputSchema={"type": "object", "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "default": 20},
            }, "required": ["query"]},
        ),
        Tool(
            name="get_ticket",
            description="Ein einzelnes Ticket mit allen Artikeln und Anhängen abrufen.",
            inputSchema={"type": "object", "properties": {
                "ticket_id": {"type": "integer"},
            }, "required": ["ticket_id"]},
        ),
        Tool(
            name="list_recent_tickets",
            description="Die neuesten Tickets auflisten.",
            inputSchema={"type": "object", "properties": {
                "limit": {"type": "integer", "default": 25},
            }},
        ),
        Tool(
            name="download_attachment",
            description="Einen Anhang aus einem Ticket-Artikel herunterladen (base64-kodiert).",
            inputSchema={"type": "object", "properties": {
                "ticket_id": {"type": "integer"},
                "article_id": {"type": "integer"},
                "attachment_id": {"type": "integer"},
            }, "required": ["ticket_id", "article_id", "attachment_id"]},
        ),
        Tool(
            name="get_attachment_text",
            description=(
                "Text eines Anhangs serverseitig extrahieren und als Klartext zurueckgeben (statt Base64, "
                "funktioniert auch bei grossen PDFs). Unterstuetzt PDF, XLSX, DOCX, CSV, TXT sowie Scans und Bilder "
                "(PNG, JPEG, mehrseitiges TIFF) per OCR. Read-only. "
                "PDF-Text pro Seite mit Trenner '--- Seite N ---', Tabellenzeilen zusammengehalten. "
                "ocr='auto' (Standard) liest Seiten ohne Textebene per OCR, 'force' alle Seiten, 'off' keine. "
                "OCR ist langsam (einige Sekunden pro Seite) und laeuft mit Zeitbudget: solange truncated=true, "
                "mit offset=next_offset bzw. page_from=next_page erneut aufrufen, fertige Seiten sind gecacht. "
                "page_from/page_to waehlen einzelne Seiten. extraction_method: pdfminer | font_cmap | "
                "gid_offset_heuristic (geraten, Inhalt pruefen) | ocr | mixed; ocr_pages listet die OCR-Seiten. "
                "Limits: 25 MB, 200 Seiten, 30 s fuer die normale Extraktion."
            ),
            inputSchema={"type": "object", "properties": {
                "ticket_id": {"type": "integer"},
                "article_id": {"type": "integer"},
                "attachment_id": {"type": "integer"},
                "offset": {"type": "integer", "default": 0, "description": "Startposition im extrahierten Text (Zeichen)"},
                "max_chars": {"type": "integer", "default": 20000, "description": "Maximal zurueckgegebene Zeichen (hoechstens 50000)"},
                "ocr": {"type": "string", "enum": ["auto", "force", "off"], "default": "auto", "description": "auto = nur Seiten ohne Textebene per OCR, force = alle Seiten, off = kein OCR"},
                "page_from": {"type": "integer", "description": "Erste Seite (PDF/TIFF), Standard 1"},
                "page_to": {"type": "integer", "description": "Letzte Seite (PDF/TIFF), Standard letzte Seite"},
            }, "required": ["ticket_id", "article_id", "attachment_id"]},
        ),
        Tool(
            name="find_in_attachments",
            description=(
                "Tickets ueber die Ticketsuche (ticket_query) finden und den Text aller unterstuetzten Anhaenge "
                "(PDF, XLSX, DOCX, CSV, TXT) nach pattern durchsuchen. Standard: Substring, Gross-/Kleinschreibung egal; "
                "regex=true fuer regulaere Ausdruecke (max. 200 Zeichen, mit Timeout). Read-only. "
                "ocr='auto' durchsucht auch Scans und Bilder per OCR (langsam, Standard 'off'). "
                "Liefert Treffer mit Ticket, Artikel, Anhang, Seite (PDF) bzw. Blatt (XLSX) und Kontext sowie die "
                "durchsuchten Anhaenge ohne Treffer, Fehler und uebersprungene Dateien. "
                "Beispiel: ticket_query='QSD Smart Lock PO', pattern='52.5'."
            ),
            inputSchema={"type": "object", "properties": {
                "ticket_query": {"type": "string", "description": "Zammad-Suchanfrage wie bei search_tickets"},
                "pattern": {"type": "string", "description": "Suchbegriff (Substring) oder Regex bei regex=true"},
                "regex": {"type": "boolean", "default": False},
                "limit_tickets": {"type": "integer", "default": 50, "description": "Maximale Anzahl Tickets (hoechstens 200)"},
                "context_chars": {"type": "integer", "default": 150, "description": "Kontext vor und nach dem Treffer (hoechstens 1000)"},
                "ocr": {"type": "string", "enum": ["auto", "force", "off"], "default": "off", "description": "OCR fuer Scans und Bilder (teuer); 'auto' = nur Seiten ohne Textebene"},
            }, "required": ["ticket_query", "pattern"]},
        ),
        Tool(
            name="find_invoice_aggregation_tickets",
            description="Tickets mit Invoice Aggregation Excel-Anhängen suchen. Gibt eine Liste sortiert nach Dateiname (= Datum) zurück.",
            inputSchema={"type": "object", "properties": {
                "limit": {"type": "integer", "default": 50},
            }},
        ),
        Tool(
            name="list_overviews",
            description="Alle Ticket-Übersichten (Kategorien) in Zammad auflisten, z.B. Offen, Wartend, etc.",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="get_open_tickets",
            description="Alle offenen Tickets abrufen (state: new oder open).",
            inputSchema={"type": "object", "properties": {
                "limit": {"type": "integer", "default": 100},
            }},
        ),
        Tool(
            name="get_pending_reached_tickets",
            description="Alle 'Warten erreicht' Tickets abrufen — pending reminder Tickets wo die Wartezeit abgelaufen ist.",
            inputSchema={"type": "object", "properties": {
                "limit": {"type": "integer", "default": 100},
            }},
        ),
        Tool(
            name="create_ticket",
            description="Neues Ticket in Zammad erstellen. Pflichtfelder: title, body, customer_email, group (exakter Gruppenname, siehe list_groups; unbekannte Gruppen werden abgelehnt). Optionale Felder: state ('new', 'open', etc.), priority ('1 low', '2 normal', '3 high').",
            inputSchema={"type": "object", "properties": {
                "title": {"type": "string"},
                "body": {"type": "string"},
                "customer_email": {"type": "string"},
                "group": {"type": "string", "description": "Exakter Gruppenname, siehe list_groups"},
                "state": {"type": "string", "default": "new"},
                "priority": {"type": "string", "default": "2 normal"},
            }, "required": ["title", "body", "customer_email", "group"]},
        ),
        Tool(
            name="list_groups",
            description="Alle aktiven Zammad-Gruppen (id, name) auflisten.",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="set_ticket_group",
            description="Ticket in eine andere Gruppe verschieben (exakter Gruppenname, siehe list_groups).",
            inputSchema={"type": "object", "properties": {
                "ticket_id": {"type": "integer"},
                "group": {"type": "string"},
            }, "required": ["ticket_id", "group"]},
        ),
        Tool(
            name="set_ticket_owner",
            description="Ticket einem Agent zuweisen (Owner setzen). owner = E-Mail, Login oder 'Vorname Nachname' eines aktiven Agents; unbekannte oder mehrdeutige Angaben werden abgelehnt. owner='none' entfernt die Zuweisung. Der Agent braucht Zugriff auf die Gruppe des Tickets.",
            inputSchema={"type": "object", "properties": {
                "ticket_id": {"type": "integer"},
                "owner": {"type": "string"},
            }, "required": ["ticket_id", "owner"]},
        ),
        Tool(
            name="set_ticket_customer",
            description="Kunde eines Tickets wechseln. customer = E-Mail, Login oder 'Vorname Nachname' eines bestehenden, aktiven Users; unbekannte oder mehrdeutige Angaben werden abgelehnt, es wird kein neuer Kunde angelegt. Die Organisation des Tickets wird auf die des neuen Kunden gesetzt. Bestehende Artikel bleiben unveraendert.",
            inputSchema={"type": "object", "properties": {
                "ticket_id": {"type": "integer"},
                "customer": {"type": "string", "description": "E-Mail, Login oder 'Vorname Nachname' des neuen Kunden"},
            }, "required": ["ticket_id", "customer"]},
        ),
        Tool(
            name="update_customer_name",
            description="Vor- und/oder Nachname eines Kunden korrigieren. Kunde ueber exakte 'email' ODER ueber 'ticket_id' (Kunde des Tickets) bestimmen, genau eines von beiden. Nur uebergebene Namensfelder werden geaendert.",
            inputSchema={"type": "object", "properties": {
                "firstname": {"type": "string"},
                "lastname": {"type": "string"},
                "email": {"type": "string"},
                "ticket_id": {"type": "integer"},
            }},
        ),
        Tool(
            name="add_ticket_note",
            description="Interne Notiz zu einem Ticket hinzufügen (nur intern sichtbar, nicht an Kunde).",
            inputSchema={"type": "object", "properties": {
                "ticket_id": {"type": "integer"},
                "body": {"type": "string"},
            }, "required": ["ticket_id", "body"]},
        ),
        Tool(
            name="update_ticket_state",
            description="Ticket-Status ändern. Mögliche Werte: 'new', 'open', 'closed', 'pending reminder', 'pending close'",
            inputSchema={"type": "object", "properties": {
                "ticket_id": {"type": "integer"},
                "state": {"type": "string"},
            }, "required": ["ticket_id", "state"]},
        ),
        Tool(
            name="update_ticket_title",
            description="Ticket-Titel ändern, z.B. um einen generischen Titel (wie 'Dodolock Kontaktformular') durch einen zum tatsächlichen Inhalt passenden Titel zu ersetzen.",
            inputSchema={"type": "object", "properties": {
                "ticket_id": {"type": "integer"},
                "title": {"type": "string"},
            }, "required": ["ticket_id", "title"]},
        ),
        Tool(
            name="set_ticket_pending",
            description="Ticket auf 'pending reminder' setzen mit Datum (Format: YYYY-MM-DD). Optional: interne Notiz hinterlegen. Beispiel: pending_date='2026-06-21'",
            inputSchema={"type": "object", "properties": {
                "ticket_id": {"type": "integer"},
                "pending_date": {"type": "string"},
                "note": {"type": "string", "default": ""},
            }, "required": ["ticket_id", "pending_date"]},
        ),
        Tool(
            name="merge_ticket",
            description="Zwei Tickets zusammenfuehren (Zammad Merge-Funktion). Alle Artikel von ticket_id wandern in das Ziel-Ticket (master_ticket_number); das Quellticket wird danach automatisch geschlossen.",
            inputSchema={"type": "object", "properties": {
                "ticket_id": {"type": "integer", "description": "Interne Zammad Ticket-ID des Tickets, das gemergt werden soll"},
                "master_ticket_number": {"type": "string", "description": "Ticket-NUMMER (kundenseitiges 'number'-Feld, NICHT die interne ID!) des Ziel-Tickets"},
            }, "required": ["ticket_id", "master_ticket_number"]},
        ),
        Tool(
            name="delete_ticket_article",
            description="Einen Ticket-Artikel (z.B. falsche Notiz) löschen.",
            inputSchema={"type": "object", "properties": {
                "article_id": {"type": "integer"},
            }, "required": ["article_id"]},
        ),
        Tool(
            name="set_article_internal",
            description="Einen bestehenden Ticket-Artikel auf intern stellen (internal=true, Standard) oder wieder oeffentlich (internal=false). Aendert nur das Flag, sendet nichts.",
            inputSchema={"type": "object", "properties": {
                "article_id": {"type": "integer"},
                "internal": {"type": "boolean", "default": True},
            }, "required": ["article_id"]},
        ),
        Tool(
            name="forward_ticket",
            description="Einen Ticket-Artikel per E-Mail weiterleiten. Erstellt einen neuen Email-Artikel im Ticket mit dem Original als Weiterleitung. Mit attachment_urls koennen PDFs, die nur als Link in der Mail stehen (z.B. Online-Rechnungen), serverseitig geladen und im Original angehaengt werden.",
            inputSchema={"type": "object", "properties": {
                "ticket_id": {"type": "integer"},
                "article_id": {"type": "integer", "description": "ID des weiterzuleitenden Artikels"},
                "to": {"type": "string", "description": "Empfänger E-Mail-Adresse"},
                "body": {"type": "string", "default": "", "description": "Optionaler Text vor dem weitergeleiteten Inhalt"},
                "include_attachments": {"type": "boolean", "default": True},
                "attachment_filenames": {"type": "array", "items": {"type": "string"}, "description": "Optional: nur Anhänge mit passendem Dateinamen mitschicken"},
                "attachment_urls": {"type": "array", "items": {"type": "string"}, "description": "Optional: PDFs von diesen URLs (z.B. Rechnungs-Link in der Mail) serverseitig herunterladen und im Original anhaengen. Redirects werden verfolgt; nur oeffentliche http/https-Adressen. Bei Download-Fehler wird nichts gesendet."},
                "attachment_url_filenames": {"type": "array", "items": {"type": "string"}, "description": "Optional: Dateinamen zu attachment_urls (gleiche Reihenfolge)"},
            }, "required": ["ticket_id", "article_id", "to"]},
        ),
        Tool(
            name="send_sms",
            description=(
                "SMS ueber das SMS-Gateway von SMTP2GO senden (nicht ueber Zammad). Ohne confirm=true wird NICHTS "
                "gesendet, es kommt nur eine Vorschau (Empfaenger, Absender, Kodierung, Anzahl SMS-Teile). "
                "Nur auf ausdruecklichen Auftrag senden. Empfaenger als Schweizer Mobilnummer (079 123 45 67, wird zu +4179...) "
                "oder international (+49..., 0049...); "
                "fehlt 'to', wird die Mobilnummer des Kunden von ticket_id verwendet. Mit ticket_id wird der Versand "
                "als interne Notiz im Ticket dokumentiert. Antworten kommen ueber SMTP2GO 'Forward replies' als "
                "E-Mail in Zammad an, nicht ueber dieses Tool. Max. 3 SMS-Teile (einstellbar ueber SMS_MAX_UNITS; GSM-7: 160/153 Zeichen, "
                "mit Zeichen ausserhalb GSM-7, z.B. Emojis, UCS-2: 70/67)."
            ),
            inputSchema={"type": "object", "properties": {
                "text": {"type": "string", "description": "SMS-Text (Klartext)"},
                "to": {"type": "string", "description": "Empfaengernummer, z.B. 079 123 45 67 oder +41791234567. Leer = Mobilnummer des Ticket-Kunden"},
                "ticket_id": {"type": "integer", "description": "Interne Ticket-ID; Versand wird dort als interne Notiz dokumentiert"},
                "sender": {"type": "string", "description": "Optionale Absendernummer (E.164); Standard aus SMTP2GO_SMS_SENDER bzw. SMTP2GO-Konto"},
                "confirm": {"type": "boolean", "default": False, "description": "true = wirklich senden, sonst nur Vorschau"},
            }, "required": ["text"]},
        ),
        Tool(
            name="list_received_sms",
            description="Bei SMTP2GO eingegangene SMS (Antworten auf gesendete SMS) abfragen. Read-only. Ohne Datum: letzte 7 Tage. Datumsangaben ISO-8601 in UTC (end_date exklusiv).",
            inputSchema={"type": "object", "properties": {
                "start_date": {"type": "string", "description": "z.B. 2026-10-01T00:00:00Z"},
                "end_date": {"type": "string", "description": "z.B. 2026-10-20T23:59:59Z"},
            }},
        ),
    ]


@server.call_tool()
async def call_tool(name, arguments):
    func = TOOL_FUNCS.get(name)
    if not func:
        return [TextContent(type="text", text=f"Unbekanntes Tool: {name}")]
    try:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, lambda: func(**(arguments or {})))
        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]
    except httpx.HTTPStatusError as e:
        return [TextContent(type="text", text=f"Zammad API Fehler {e.response.status_code} @ {e.request.url}: {e.response.text}")]
    except Exception as e:
        return [TextContent(type="text", text=f"Fehler: {str(e)}")]


# ---------------------------------------------------------------------------
# Cloud/HTTP-Betrieb
# ---------------------------------------------------------------------------
# Streamable-HTTP-Betrieb fuer den Cloud-Einsatz (Docker/Portainer). Ein
# statisches Bearer-Token (MCP_AUTH_TOKEN) schuetzt den Endpoint, da er
# oeffentlich erreichbar ist. Ohne gesetztes MCP_AUTH_TOKEN startet der Server
# NICHT (fail-safe, kein offener Endpoint).

MCP_AUTH_TOKEN = os.environ.get("MCP_AUTH_TOKEN", "")


class _StreamableHTTPASGIApp:
    """Minimaler ASGI-Wrapper um den StreamableHTTPSessionManager (wie bexio-mcp)."""

    def __init__(self, session_manager):
        self.session_manager = session_manager

    async def __call__(self, scope, receive, send):
        await self.session_manager.handle_request(scope, receive, send)


class BearerAuthMiddleware:
    """Minimalistische ASGI-Middleware: prueft 'Authorization: Bearer <token>'."""

    def __init__(self, app, token: str):
        self.app = app
        self.token = token

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers", []))
        auth_header = headers.get(b"authorization", b"").decode("latin-1")
        expected = f"Bearer {self.token}"
        if auth_header != expected:
            from starlette.responses import JSONResponse
            response = JSONResponse({"error": "unauthorized"}, status_code=401)
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)


def with_healthz(app):
    """GET /healthz fuer den Docker-Healthcheck, ohne Auth. Liefert nur {"status": "ok"} -
    keine Version, keine Konfiguration. Prueft nur, ob der Prozess antwortet, nicht die
    Zammad-API (sonst wuerde ein Ausfall bei Zammad den Container unhealthy machen).
    Alle anderen Pfade laufen unveraendert durch die Bearer-Pruefung."""
    from starlette.responses import JSONResponse

    async def router(scope, receive, send):
        if scope["type"] == "http" and scope["path"] == "/healthz" and scope["method"] in ("GET", "HEAD"):
            await JSONResponse({"status": "ok"})(scope, receive, send)
            return
        await app(scope, receive, send)

    return router


def build_http_app(token: str):
    """Starlette-App mit /mcp hinter der Bearer-Pruefung, /healthz davor, CORS aussen."""
    from starlette.applications import Starlette
    from starlette.routing import Route
    from starlette.middleware.cors import CORSMiddleware
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from mcp.server.transport_security import TransportSecuritySettings

    # security_settings explizit gesetzt statt der SDK-eigenen Auto-Erkennung
    # zu ueberlassen - siehe bexio-mcp fuer die ausfuehrliche Begruendung
    # (DNS-Rebinding-Schutz wuerde sonst jeden Request mit oeffentlichem
    # Host-Header blocken; die eigentliche Absicherung uebernimmt ohnehin
    # MCP_AUTH_TOKEN/BearerAuthMiddleware).
    session_manager = StreamableHTTPSessionManager(
        app=server,
        json_response=True,
        security_settings=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    mcp_asgi_app = _StreamableHTTPASGIApp(session_manager)

    app = Starlette(
        routes=[Route("/mcp", endpoint=mcp_asgi_app)],
        lifespan=lambda app: session_manager.run(),
    )
    secured_app = with_healthz(BearerAuthMiddleware(app, token))
    # CORS aussen um die Auth-Middleware: Browser-basierte MCP-Clients (z.B.
    # Claude.ai) rufen den Endpoint per Cross-Origin-JS-Fetch auf. Ohne CORS-
    # Header blockt der Browser die Antwort, bevor der Client sie ueberhaupt
    # sieht. Preflight-OPTIONS-Requests (ohne Authorization-Header) werden von
    # CORSMiddleware direkt beantwortet, bevor sie die Bearer-Pruefung erreichen.
    return CORSMiddleware(
        secured_app,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["mcp-session-id"],
    )


async def run_http_server():
    """Streamable-HTTP-Transport für den Cloud-/Docker-Betrieb,
    handverdrahtet identisch zu bexio-mcp (siehe Modul-Docstring)."""
    import uvicorn

    host = os.environ.get("MCP_HOST", "0.0.0.0")
    port = int(os.environ.get("MCP_PORT", "8000"))
    cors_app = build_http_app(MCP_AUTH_TOKEN)

    config = uvicorn.Config(cors_app, host=host, port=port, log_level="info")
    srv = uvicorn.Server(config)
    print(f"Zammad MCP HTTP server running on {host}:{port}", flush=True)
    await srv.serve()


def main():
    if not MCP_AUTH_TOKEN:
        raise RuntimeError(
            "MCP_AUTH_TOKEN (statisches Bearer-Token) fehlt - aus Sicherheitsgruenden kein Start ohne Token."
        )
    asyncio.run(run_http_server())


if __name__ == "__main__":
    main()
