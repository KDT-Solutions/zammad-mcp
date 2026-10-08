"""
Zammad MCP Server
Ermöglicht Claude direkten Zugriff auf Zammad Tickets und Anhänge.

Transport wird über die Umgebungsvariable MCP_TRANSPORT gesteuert:
- "stdio" (Standard) - für die lokale Nutzung via uvx/Claude Desktop, unveraendert.
- "http" - startet den Server als HTTP-Dienst (Streamable HTTP) für den Cloud-Einsatz,
  z.B. hinter einem Reverse-Proxy. Erfordert MCP_AUTH_TOKEN.

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
import ipaddress
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, wait
from urllib.parse import unquote, urlparse
from typing import Any

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
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

_EXTRACT_WORKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "attachment_text.py")
_TEXT_CACHE: "OrderedDict[tuple, dict]" = OrderedDict()
_TEXT_CACHE_MAX = 16
_TEXT_CACHE_LOCK = threading.Lock()


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
    cmd = [sys.executable, "-I", _EXTRACT_WORKER, kind, str(ATTACHMENT_TEXT_MAX_PDF_PAGES), str(ATTACHMENT_TEXT_MAX_MEMORY_MB)]
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
    if result.get("ok"):
        with _TEXT_CACHE_LOCK:
            _TEXT_CACHE[key] = result
            while len(_TEXT_CACHE) > _TEXT_CACHE_MAX:
                _TEXT_CACHE.popitem(last=False)
    return result


def _unsupported_msg(info: dict) -> str:
    return (
        f"Dateityp nicht unterstuetzt: {info.get('filename')} ({info.get('content_type') or 'unbekannt'}). "
        f"Unterstuetzt: PDF, XLSX, DOCX, CSV, TXT"
    )


def get_attachment_text(ticket_id: int, article_id: int, attachment_id: int, offset: int = 0, max_chars: int = 20000) -> dict:
    """Text eines Anhangs serverseitig extrahieren und seitenweise (offset/max_chars) zurueckgeben."""
    res = _extract_attachment(ticket_id, article_id, attachment_id)
    if not res.get("ok"):
        out = {k: res.get(k) for k in ("filename", "content_type", "size_bytes", "pages") if res.get(k) is not None}
        out["error"] = res.get("error")
        if res.get("limit_exceeded"):
            out["limit_exceeded"] = res["limit_exceeded"]
        return out
    text = res["text"]
    offset = max(0, int(offset or 0))
    max_chars = min(max(1, int(max_chars or 20000)), ATTACHMENT_TEXT_MAX_CHARS)
    part = text[offset:offset + max_chars]
    truncated = offset + len(part) < len(text)
    out = {
        "filename": res.get("filename"),
        "content_type": res.get("content_type"),
        "extraction_method": res.get("extraction_method"),
    }
    if res.get("kind") == "pdf":
        out["pages"] = res.get("pages")
        if len(res.get("pages_by_method") or {}) > 1:
            out["pages_by_method"] = res["pages_by_method"]
    out.update({
        "total_chars": len(text),
        "offset": offset,
        "returned_chars": len(part),
        "truncated": truncated,
    })
    if truncated:
        out["next_offset"] = offset + len(part)
    if res.get("text_limit_reached"):
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


def find_in_attachments(ticket_query: str, pattern: str, regex: bool = False, limit_tickets: int = 50, context_chars: int = 150) -> dict:
    """Tickets per Ticketsuche finden und alle unterstuetzten Anhaenge nach pattern durchsuchen."""
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
                if _attachment_text.detect_kind(att.get("filename"), ctype) is None:
                    skipped_count += 1
                    if len(skipped) < FIND_MAX_LISTED_SKIPPED:
                        skipped.append(entry)
                    continue
                jobs.append({"entry": entry, "meta": att})

    def work(job):
        e = job["entry"]
        return _extract_attachment(e["ticket_id"], e["article_id"], e["attachment_id"], job["meta"])

    results: list = [None] * len(jobs)
    pool = ThreadPoolExecutor(max_workers=FIND_WORKERS)
    try:
        futures = {pool.submit(work, job): i for i, job in enumerate(jobs)}
        done, _ = wait(futures, timeout=max(0.0, deadline - time.monotonic()))
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
        text = res["text"]
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
                section = _section_at(res.get("sections") or [], m.start())
                if section and section.get("page"):
                    hit["page"] = section["page"]
                if section and section.get("sheet"):
                    hit["sheet"] = section["sheet"]
                hit["context"] = text[max(0, m.start() - context_chars):m.end() + context_chars]
                hit["extraction_method"] = res.get("extraction_method")
                matches.append(hit)
        except TimeoutError:
            errors.append({**entry, "error": f"Regex-Suche nach {REGEX_TIMEOUT:g} s abgebrochen (Timeout)", "limit_exceeded": "regex_timeout"})
            continue
        if hits == 0:
            without_match.append({**entry, "extraction_method": res.get("extraction_method")})

    out = {
        "ticket_query": ticket_query,
        "pattern": pattern,
        "regex": is_regex,
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
    "update_customer_name": update_customer_name,
    "add_ticket_note": add_ticket_note,
    "update_ticket_state": update_ticket_state,
    "update_ticket_title": update_ticket_title,
    "set_ticket_pending": set_ticket_pending,
    "merge_ticket": merge_ticket,
    "delete_ticket_article": delete_ticket_article,
    "set_article_internal": set_article_internal,
    "forward_ticket": forward_ticket,
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
                "funktioniert auch bei grossen PDFs). Unterstuetzt PDF, XLSX, DOCX, CSV, TXT. Read-only. "
                "PDF-Text pro Seite mit Trenner '--- Seite N ---', Tabellenzeilen zusammengehalten. "
                "Lange Dokumente seitenweise abrufen: solange truncated=true, mit offset=next_offset erneut aufrufen. "
                "extraction_method zeigt, wie der Text gewonnen wurde (pdfminer | font_cmap | gid_offset_heuristic; "
                "gid_offset_heuristic = geraten, Inhalt pruefen). Limits: 25 MB, 200 PDF-Seiten, 30 s pro Datei."
            ),
            inputSchema={"type": "object", "properties": {
                "ticket_id": {"type": "integer"},
                "article_id": {"type": "integer"},
                "attachment_id": {"type": "integer"},
                "offset": {"type": "integer", "default": 0, "description": "Startposition im extrahierten Text (Zeichen)"},
                "max_chars": {"type": "integer", "default": 20000, "description": "Maximal zurueckgegebene Zeichen (hoechstens 50000)"},
            }, "required": ["ticket_id", "article_id", "attachment_id"]},
        ),
        Tool(
            name="find_in_attachments",
            description=(
                "Tickets ueber die Ticketsuche (ticket_query) finden und den Text aller unterstuetzten Anhaenge "
                "(PDF, XLSX, DOCX, CSV, TXT) nach pattern durchsuchen. Standard: Substring, Gross-/Kleinschreibung egal; "
                "regex=true fuer regulaere Ausdruecke (max. 200 Zeichen, mit Timeout). Read-only. "
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
# MCP_TRANSPORT=http aktiviert den Streamable-HTTP-Modus fuer den Cloud-Einsatz
# (z.B. via Docker/Portainer). Ein statisches Bearer-Token (MCP_AUTH_TOKEN)
# schuetzt den Endpoint, da er oeffentlich erreichbar ist. Ohne gesetztes
# MCP_AUTH_TOKEN startet der HTTP-Modus NICHT (fail-safe, kein offener Endpoint).

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


async def run_http_server():
    """Streamable-HTTP-Transport für Cloud-/Docker-Betrieb (MCP_TRANSPORT=http),
    handverdrahtet identisch zu bexio-mcp (siehe Modul-Docstring)."""
    from starlette.applications import Starlette
    from starlette.routing import Route
    from starlette.middleware.cors import CORSMiddleware
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from mcp.server.transport_security import TransportSecuritySettings
    import uvicorn

    host = os.environ.get("MCP_HOST", "0.0.0.0")
    port = int(os.environ.get("MCP_PORT", "8000"))

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
    secured_app = BearerAuthMiddleware(app, MCP_AUTH_TOKEN)
    # CORS aussen um die Auth-Middleware: Browser-basierte MCP-Clients (z.B.
    # Claude.ai) rufen den Endpoint per Cross-Origin-JS-Fetch auf. Ohne CORS-
    # Header blockt der Browser die Antwort, bevor der Client sie ueberhaupt
    # sieht. Preflight-OPTIONS-Requests (ohne Authorization-Header) werden von
    # CORSMiddleware direkt beantwortet, bevor sie die Bearer-Pruefung erreichen.
    cors_app = CORSMiddleware(
        secured_app,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["mcp-session-id"],
    )

    config = uvicorn.Config(cors_app, host=host, port=port, log_level="info")
    srv = uvicorn.Server(config)
    print(f"Zammad MCP HTTP server running on {host}:{port}", flush=True)
    await srv.serve()


def main():
    transport = os.environ.get("MCP_TRANSPORT", "stdio").lower()

    if transport in ("http", "streamable-http"):
        if not MCP_AUTH_TOKEN:
            raise RuntimeError(
                "MCP_TRANSPORT=http erfordert MCP_AUTH_TOKEN (statisches Bearer-Token) - "
                "aus Sicherheitsgruenden kein Start ohne Token."
            )
        asyncio.run(run_http_server())
    else:
        async def _run_stdio():
            async with stdio_server() as (read_stream, write_stream):
                await server.run(read_stream, write_stream, server.create_initialization_options())
        asyncio.run(_run_stdio())


if __name__ == "__main__":
    main()
