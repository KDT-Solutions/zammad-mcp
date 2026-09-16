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
import json
import os
from typing import Any

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

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


def search_tickets(query: str, limit: int = 20) -> list[dict]:
    """Tickets in Zammad suchen."""
    result = api_get("/tickets/search", params={"query": query, "limit": limit})
    # Zammad returns either a list or {assets: {Ticket: {}}, ticket_ids: [...]}
    if isinstance(result, list):
        tickets = result
    else:
        ticket_ids = result.get("ticket_ids", [])
        assets = result.get("assets", {}).get("Ticket", {})
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
        }
        for t in tickets
    ]


def get_ticket(ticket_id: int) -> dict:
    """Ein einzelnes Ticket mit allen Artikeln und Anhängen abrufen."""
    ticket = api_get(f"/tickets/{ticket_id}")
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

    if isinstance(result, list):
        tickets = result
    else:
        ticket_ids = result.get("ticket_ids", [])
        assets = result.get("assets", {}).get("Ticket", {})
        tickets = [assets[str(tid)] for tid in ticket_ids if str(tid) in assets]

    return [
        {
            "id": t["id"],
            "number": t.get("number"),
            "title": t.get("title"),
            "state": state_ids.get(str(t.get("state_id")), str(t.get("state_id"))),
            "updated_at": t.get("updated_at"),
        }
        for t in tickets
        if not state_names or state_ids.get(str(t.get("state_id"))) in state_names
    ]


def create_ticket(
    title: str,
    body: str,
    customer_email: str,
    group: str = "",
    state: str = "new",
    priority: str = "2 normal",
) -> dict:
    """
    Neues Ticket in Zammad erstellen.
    Pflichtfelder: title, body, customer_email.
    Optionale Felder: group (leer = erste verfügbare Gruppe), state ('new', 'open', etc.), priority ('1 low', '2 normal', '3 high').
    """
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

    # Resolve group_id
    try:
        groups = api_get("/groups")
        if group:
            group_id = next((g["id"] for g in groups if g["name"].lower() == group.lower()), None)
        else:
            group_id = None
        if not group_id:
            group_id = groups[0]["id"] if groups else 1
        group_name = next((g["name"] for g in groups if g["id"] == group_id), "")
    except Exception:
        group_id = 1
        group_name = ""

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


def delete_ticket_article(article_id: int) -> dict:
    """Einen Ticket-Artikel (z.B. falsche Notiz) löschen."""
    url = f"{ZAMMAD_URL}/api/v1/ticket_articles/{article_id}"
    response = httpx.delete(url, headers=get_headers(), timeout=30)
    response.raise_for_status()
    return {"success": True, "deleted_article_id": article_id}


def forward_ticket(
    ticket_id: int,
    article_id: int,
    to: str,
    body: str = "",
    include_attachments: bool = True,
    attachment_filenames: list[str] | None = None,
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

    return {"success": True, "article_id": response.json().get("id"), "forwarded_to": to}


# ---------------------------------------------------------------------------
# MCP-Server (low-level): Tool-Liste + Dispatch
# ---------------------------------------------------------------------------
# Bewusst der rohe mcp.server.Server statt FastMCP - identisch zu bexio-mcp,
# das mit exakt diesem Unterbau stabil laeuft (siehe Modul-Docstring oben).

server = Server("zammad-connector")

TOOL_FUNCS = {
    "search_tickets": search_tickets,
    "get_ticket": get_ticket,
    "list_recent_tickets": list_recent_tickets,
    "download_attachment": download_attachment,
    "find_invoice_aggregation_tickets": find_invoice_aggregation_tickets,
    "list_overviews": list_overviews,
    "get_open_tickets": get_open_tickets,
    "get_pending_reached_tickets": get_pending_reached_tickets,
    "create_ticket": create_ticket,
    "add_ticket_note": add_ticket_note,
    "update_ticket_state": update_ticket_state,
    "update_ticket_title": update_ticket_title,
    "set_ticket_pending": set_ticket_pending,
    "merge_ticket": merge_ticket,
    "delete_ticket_article": delete_ticket_article,
    "forward_ticket": forward_ticket,
}


@server.list_tools()
async def list_tools():
    return [
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
            description="Neues Ticket in Zammad erstellen. Pflichtfelder: title, body, customer_email. Optionale Felder: group (leer = erste verfügbare Gruppe), state ('new', 'open', etc.), priority ('1 low', '2 normal', '3 high').",
            inputSchema={"type": "object", "properties": {
                "title": {"type": "string"},
                "body": {"type": "string"},
                "customer_email": {"type": "string"},
                "group": {"type": "string", "default": ""},
                "state": {"type": "string", "default": "new"},
                "priority": {"type": "string", "default": "2 normal"},
            }, "required": ["title", "body", "customer_email"]},
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
            name="forward_ticket",
            description="Einen Ticket-Artikel per E-Mail weiterleiten. Erstellt einen neuen Email-Artikel im Ticket mit dem Original als Weiterleitung.",
            inputSchema={"type": "object", "properties": {
                "ticket_id": {"type": "integer"},
                "article_id": {"type": "integer", "description": "ID des weiterzuleitenden Artikels"},
                "to": {"type": "string", "description": "Empfänger E-Mail-Adresse"},
                "body": {"type": "string", "default": "", "description": "Optionaler Text vor dem weitergeleiteten Inhalt"},
                "include_attachments": {"type": "boolean", "default": True},
                "attachment_filenames": {"type": "array", "items": {"type": "string"}, "description": "Optional: nur Anhänge mit passendem Dateinamen mitschicken"},
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
