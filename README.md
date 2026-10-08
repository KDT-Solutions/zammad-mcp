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
| `ATTACHMENT_TEXT_MAX_BYTES` | nein | `26214400` (25 MB) | Maximale Anhangsgrösse für `get_attachment_text` / `find_in_attachments` |
| `ATTACHMENT_TEXT_MAX_PDF_PAGES` | nein | `200` | Maximale Seitenzahl pro PDF |
| `ATTACHMENT_TEXT_TIMEOUT` | nein | `30` | Timeout pro Datei in Sekunden (Extraktion wird danach hart beendet) |
| `ATTACHMENT_TEXT_MAX_MEMORY_MB` | nein | `1024` | Speicherlimit des Extraktionsprozesses (nur Linux/macOS) |
| `FIND_IN_ATTACHMENTS_TIMEOUT` | nein | `240` | Gesamtzeit für `find_in_attachments`, danach werden restliche Anhänge als `not_checked` gemeldet |

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
| `get_version` | Version des laufenden MCP-Servers abfragen |
| `search_tickets` | Beliebige Ticket-Suche (Ergebnis inkl. Gruppe, Priorität, Kunde, Owner) |
| `get_ticket` | Ticket mit allen Artikeln und Anhängen abrufen (inkl. Gruppe, Priorität, Kunde, Owner, pending_time) |
| `list_recent_tickets` | Neueste Tickets auflisten (inkl. Gruppe, Priorität, Kunde, Owner) |
| `get_open_tickets` | Alle offenen Tickets (new/open), inkl. Gruppe, Priorität, Kunde, Owner |
| `get_pending_reached_tickets` | Pending-Reminder-Tickets, deren Wartezeit abgelaufen ist |
| `list_overviews` | Ticket-Übersichten/Kategorien auflisten |
| `download_attachment` | Anhang herunterladen (als base64) |
| `get_attachment_text` | Text eines Anhangs (PDF, XLSX, DOCX, CSV, TXT) serverseitig extrahieren, mit Paging über `offset`/`max_chars` |
| `find_in_attachments` | Tickets per Suche finden und alle Anhänge nach einem Begriff (oder Regex) durchsuchen |
| `find_invoice_aggregation_tickets` | Tickets mit Invoice-Aggregation-Excel-Anhängen |
| `create_ticket` | Neues Ticket erstellen (Gruppe ist Pflicht, unbekannte Gruppen werden abgelehnt) |
| `list_groups` | Alle aktiven Gruppen auflisten |
| `set_ticket_group` | Ticket in eine andere Gruppe verschieben |
| `set_ticket_owner` | Ticket einem Agent zuweisen (E-Mail, Login oder Name; `none` entfernt die Zuweisung) |
| `update_customer_name` | Vor- und/oder Nachname eines Kunden korrigieren (per E-Mail oder Ticket) |
| `add_ticket_note` | Interne Notiz hinzufügen |
| `update_ticket_state` | Ticket-Status ändern |
| `set_ticket_pending` | Ticket auf "pending reminder" setzen |
| `merge_ticket` | Zwei Tickets zusammenführen |
| `delete_ticket_article` | Ticket-Artikel löschen |
| `set_article_internal` | Bestehenden Ticket-Artikel auf intern (oder öffentlich) stellen |
| `forward_ticket` | Ticket-Artikel per E-Mail weiterleiten; mit `attachment_urls` werden PDFs, die nur als Link in der Mail stehen (z.B. Online-Rechnungen), serverseitig geladen und im Original angehaengt (nur oeffentliche http/https-Adressen, max. 20 MB, via `URL_ATTACHMENT_MAX_BYTES` anpassbar) |

## Textextraktion aus Anhängen

`download_attachment` liefert Base64, das bei grösseren PDFs im Client abgeschnitten wird. `get_attachment_text` und `find_in_attachments` extrahieren den Inhalt deshalb auf dem Server und geben nur Klartext zurück.

**get_attachment_text(ticket_id, article_id, attachment_id, offset=0, max_chars=20000)**

- Rückgabe: `filename`, `content_type`, `pages` (PDF), `extraction_method`, `total_chars`, `offset`, `returned_chars`, `truncated`, `next_offset` (falls `truncated`), `text`
- `max_chars` ist auf 50000 begrenzt. Lange Dokumente mit `offset=next_offset` weiter abrufen, bis `truncated=false`. Das extrahierte Ergebnis wird im Prozess zwischengespeichert, Folgeaufrufe laden die Datei nicht erneut.
- PDF: pro Seite mit Trenner `--- Seite N ---`, Zeilen nach y gruppiert und nach x sortiert, Tabellenspalten durch drei Leerzeichen getrennt. XLSX: pro Blatt `--- Blatt: Name ---`, Zellen tab-getrennt. DOCX: Absätze und Tabellen in Dokumentreihenfolge, Kopf-/Fusszeilen am Ende.
- Andere Dateitypen (Bilder, .doc, .xls, ...) werden mit einer klaren Fehlermeldung abgelehnt.

**PDF-Extraktion (`extraction_method`)**

1. `pdfminer` – Standardweg über pdfminer.six.
2. `font_cmap` – Fallback, wenn der Text Zeichensalat ist (Anteil an `(cid:..)`, Steuerzeichen, Private-Use-Zeichen oder U+FFFD über 20 %). Typisch bei PDFs aus WPS Office: eingebettete TrueType-Subset-Fonts ohne brauchbare ToUnicode-CMap, der Text steht als Glyph-IDs im Content-Stream (`<0027>Tj`). Die eingebetteten Fonts (FontFile2) werden mit fontTools geladen, aus der cmap-Tabelle (bzw. den Glyph-Namen) wird eine Reverse-Map GID → Unicode gebaut und die Content-Streams werden damit dekodiert.
3. `gid_offset_heuristic` – nur wenn Fallback 1 nichts Brauchbares liefert: GID + 29 = ASCII (Standard-Glyph-Reihenfolge, z.B. `0x0027` → `D`, `0x0003` → Leerzeichen, `0x0013` → `0`). Das ist geraten, das Ergebnis also prüfen.

Die Entscheidung fällt pro Seite. `extraction_method` nennt die unsicherste Methode, die im Dokument nötig war; bei gemischten Dokumenten zeigt `pages_by_method`, welche Seite wie extrahiert wurde.

**find_in_attachments(ticket_query, pattern, regex=false, limit_tickets=50, context_chars=150)**

- Sucht Tickets über die normale Zammad-Ticketsuche und extrahiert alle unterstützten Anhänge aller Artikel. Gleiche Anhänge (identische `attachment_id`) werden nur einmal verarbeitet.
- Standard: einfacher Substring, Gross-/Kleinschreibung egal (`52.5` sucht wörtlich nach `52.5`). Mit `regex=true` als regulärer Ausdruck (ebenfalls case-insensitive, max. 200 Zeichen, Timeout gegen ReDoS über das Paket `regex`).
- Rückgabe: `matches` (Ticketnummer, Titel, Artikel, Anhang, Dateiname, Seite bzw. Blatt, Treffer mit Kontext, `extraction_method`), `attachments_without_match`, `errors` (z.B. Limits überschritten), `skipped_unsupported` und gegebenenfalls `not_checked`, damit klar ist, was tatsächlich geprüft wurde.
- `limit_tickets` ist auf 200 begrenzt, Treffer auf 50 pro Anhang und 500 insgesamt (`match_count` zählt trotzdem alle).

**Sicherheit**

- Beide Tools sind strikt read-only (nur GET-Requests an Zammad).
- Limits pro Datei: 25 MB, 200 PDF-Seiten, 30 s. Bei Überschreitung wird abgebrochen und das in der Rückgabe gemeldet (`limit_exceeded`: `size`, `pages`, `timeout`, `memory`).
- Die Extraktion läuft in einem eigenen Python-Prozess mit Timeout und Speicherlimit, damit defekte oder präparierte Dateien den Server nicht blockieren. XLSX/DOCX werden vorher auf die entpackte Grösse geprüft (Zip-Bomben).

## Typischer Workflow (Invoice Aggregation)

1. Claude ruft `find_invoice_aggregation_tickets` auf
2. Findet alle Excel-Files nach Datum sortiert
3. Lädt die relevanten Files via `download_attachment` herunter
4. Vergleicht mit dem Rebill-Dump → kein manuelles Hochladen nötig
