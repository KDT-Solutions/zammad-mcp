# Zammad MCP Server

MCP-Server zur Anbindung eines selbst gehosteten Zammad-Systems an Claude über die Zammad REST API. Läuft in zwei Betriebsarten:

- **stdio (lokal)** — klassischer lokaler MCP-Server via uvx/Claude Desktop.
- **HTTP (Cloud)** — als Docker-Container mit Streamable-HTTP-Transport (z.B. via Portainer), für den Zugriff aus Cloud-Sessions ohne laufenden lokalen Rechner.

Alle Zugangsdaten und Einstellungen (inkl. der URL der eigenen Zammad-Instanz) werden ausschliesslich über Umgebungsvariablen konfiguriert - im Code oder Repo stehen keine Secrets oder Instanz-spezifischen Angaben. Das Repo ist damit öffentlich auf GitHub hostbar und für jede beliebige selbst gehostete Zammad-Instanz nutzbar.

## Lokale Installation (stdio)

```bash
cd zammad-mcp
pip install -r requirements.txt
```

### Konfiguration in Claude Desktop (claude_desktop_config.json)

```json
{
  "mcpServers": {
    "zammad": {
      "command": "uvx",
      "args": ["--from", "C:/Pfad/zu/zammad-mcp", "--python", "3.12", "zammad-mcp"],
      "env": {
        "ZAMMAD_URL": "https://deine-zammad-instanz.example.com",
        "ZAMMAD_TOKEN": "dein-api-token"
      }
    }
  }
}
```

Ohne gesetztes `MCP_TRANSPORT` (oder mit `MCP_TRANSPORT=stdio`) verhält sich der Server exakt wie bisher - die Cloud/HTTP-Erweiterung ändert am lokalen Betrieb nichts.

## Umgebungsvariablen

| Variable | Pflicht | Standard | Beschreibung |
|---|---|---|---|
| `ZAMMAD_URL` | ja | – (muss gesetzt werden) | Basis-URL der Zammad-Instanz |
| `ZAMMAD_TOKEN` | ja | – | Zammad API-Token (Bearer für die Zammad-REST-API) |
| `MCP_TRANSPORT` | nein | `stdio` | `stdio` = lokal (Standard) / `http` = Cloud-Modus (Streamable HTTP) |
| `MCP_AUTH_TOKEN` | ja, nur im HTTP-Modus | – | Statisches Bearer-Token zum Schutz des öffentlichen Endpoints. Ohne dieses Token startet der HTTP-Modus nicht (fail-safe) |
| `MCP_HOST` | nein | `0.0.0.0` | Bind-Adresse des HTTP-Servers *innerhalb* des Containers |
| `MCP_PORT` | nein | `8000` | Port des HTTP-Servers *innerhalb* des Containers |
| `MCP_BIND_ADDR` | nein | `127.0.0.1` | Bind-Adresse *auf dem Docker-Host* (docker-compose Port-Mapping) |
| `MCP_HOST_PORT` | nein | `8420` | Port *auf dem Docker-Host* (docker-compose Port-Mapping) |

`MCP_HOST`/`MCP_PORT` steuern den Server im Container, `MCP_BIND_ADDR`/`MCP_HOST_PORT` das Port-Mapping nach aussen (docker-compose `ports:`) - getrennt, damit man den Container z.B. nur auf localhost binden und den öffentlichen Zugriff über einen Reverse-Proxy führen kann.

## Image-Build (GitHub Actions → GitHub Container Registry)

Das Docker-Image wird **nicht** lokal aus dem Dockerfile gebaut, sondern bei jedem Push auf `main` automatisch per GitHub Actions gebaut und nach `ghcr.io/<repo>:latest` veröffentlicht (`.github/workflows/docker-publish.yml`). Grund: Portainer kann Images zuverlässig pullen, aber ein Dockerfile im Repo nicht zuverlässig automatisch neu bauen - "Pull and redeploy" funktioniert dadurch immer nur mit einer schon vorhandenen lokalen Kopie, nicht mit einem frischen Build. Mit einem fertigen Image aus einer Registry fällt dieses Problem weg.

**Einmalig nach dem ersten Push:** Das neu erstellte Package ist auf GitHub standardmässig privat, auch wenn das Repo öffentlich ist. Unter `github.com/KDT-Solutions/zammad-mcp` → Reiter **Packages** → `zammad-mcp` → **Package settings** → **Change visibility** → **Public** stellen, sonst kann Portainer das Image nicht ohne Zugangsdaten pullen.

## Cloud-Betrieb (Docker Compose, manuell)

```bash
git clone <repo-url> zammad-mcp
cd zammad-mcp
cp .env.example .env
# .env ausfüllen: ZAMMAD_URL, ZAMMAD_TOKEN, MCP_AUTH_TOKEN (langes zufälliges Token generieren)
docker compose up -d
```

Update auf eine neue Version (holt das neueste, per GitHub Actions gebaute Image):

```bash
docker compose pull
docker compose up -d
```

## Cloud-Betrieb via Portainer (empfohlen)

1. In Portainer **Stacks → Add stack**
2. **Build method: Repository** wählen, Repo-URL eintragen (Branch `main`), Compose-Pfad `docker-compose.yml`
3. Unter **Environment variables** die folgenden Werte setzen (aus Portainer-UI, landen NICHT im Repo):
   - `ZAMMAD_URL`
   - `ZAMMAD_TOKEN`
   - `MCP_AUTH_TOKEN`
   - optional `MCP_BIND_ADDR` / `MCP_HOST_PORT`, falls die Standardwerte nicht passen
4. **Deploy the stack**
5. Für ein Update später: im Stack auf **Pull and redeploy** klicken - das zieht jetzt zuverlässig das aktuelle Image aus der Registry (kein lokaler Build mehr nötig, siehe oben)

Da `docker-compose.yml` alle Werte ausschliesslich über `${VARIABLE}`-Platzhalter referenziert, funktioniert das identisch für lokales `docker compose` (mit `.env`-Datei) und für Portainer (mit den dort hinterlegten Stack-Variablen) - ohne Anpassungen am Repo.

## Netzwerk / Reverse-Proxy

Der Container bindet standardmässig nur auf `127.0.0.1:8420` auf dem Docker-Host - nicht direkt öffentlich erreichbar. Für den Zugriff aus einer Cloud-Claude-Session braucht es zusätzlich:

1. Eine eigene Subdomain (z.B. `zammad-mcp.deine-domain.ch`)
2. Einen Reverse-Proxy (Plesk/nginx) mit TLS-Zertifikat, der auf `127.0.0.1:8420` weiterleitet (Endpoint-Pfad: `/mcp`)
3. In der Cloud-Claude-Session wird der Server dann als Remote-MCP mit `https://zammad-mcp.deine-domain.ch/mcp` und dem `MCP_AUTH_TOKEN` als Bearer-Token eingebunden

## Sicherheit

- Der HTTP-Modus startet nur, wenn `MCP_AUTH_TOKEN` gesetzt ist - es gibt also nie einen ungeschützten öffentlichen Endpoint.
- Jeder HTTP-Request muss den Header `Authorization: Bearer <MCP_AUTH_TOKEN>` mitschicken, sonst Antwort `401 unauthorized`.
- `.env` ist in `.gitignore` und wird nie committet - Secrets landen nur als Umgebungsvariablen (lokal in `.env`, in der Cloud in der Portainer-Stack-Konfiguration).

## Verfügbare Tools

| Tool | Beschreibung |
|------|--------------|
| `search_tickets` | Beliebige Ticket-Suche |
| `get_ticket` | Ticket mit allen Artikeln und Anhängen abrufen |
| `list_recent_tickets` | Neueste Tickets auflisten |
| `get_open_tickets` | Alle offenen Tickets (new/open) |
| `get_pending_reached_tickets` | Pending-Reminder-Tickets, deren Wartezeit abgelaufen ist |
| `list_overviews` | Ticket-Übersichten/Kategorien auflisten |
| `download_attachment` | Anhang herunterladen (als base64) |
| `find_invoice_aggregation_tickets` | Tickets mit Invoice-Aggregation-Excel-Anhängen |
| `create_ticket` | Neues Ticket erstellen |
| `add_ticket_note` | Interne Notiz hinzufügen |
| `update_ticket_state` | Ticket-Status ändern |
| `set_ticket_pending` | Ticket auf "pending reminder" setzen |
| `merge_ticket` | Zwei Tickets zusammenführen |
| `delete_ticket_article` | Ticket-Artikel löschen |
| `forward_ticket` | Ticket-Artikel per E-Mail weiterleiten |

## Typischer Workflow (Invoice Aggregation)

1. Claude ruft `find_invoice_aggregation_tickets` auf
2. Findet alle Excel-Files nach Datum sortiert
3. Lädt die relevanten Files via `download_attachment` herunter
4. Vergleicht mit dem Rebill-Dump → kein manuelles Hochladen nötig
