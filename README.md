# Zammad MCP Server

MCP-Server zur Anbindung eines selbst gehosteten Zammad-Systems an Claude über die Zammad REST API. Läuft ausschliesslich als Docker-Container mit Streamable-HTTP-Transport (z.B. via Portainer) hinter einem Reverse-Proxy. Ein lokaler Betrieb (stdio/uvx) ist nicht vorgesehen.

Alle Zugangsdaten und Einstellungen (inkl. der URL der eigenen Zammad-Instanz) werden ausschliesslich über Umgebungsvariablen konfiguriert - im Code oder Repo stehen keine Secrets oder Instanz-spezifischen Angaben. Das Repo ist damit öffentlich auf GitHub hostbar und für jede beliebige selbst gehostete Zammad-Instanz nutzbar.

## Umgebungsvariablen

| Variable | Pflicht | Standard | Beschreibung |
|---|---|---|---|
| `ZAMMAD_URL` | ja | – (muss gesetzt werden) | Basis-URL der Zammad-Instanz |
| `ZAMMAD_TOKEN` | ja | – | Zammad API-Token (Bearer für die Zammad-REST-API) |
| `MCP_AUTH_TOKEN` | ja | – | Statisches Bearer-Token zum Schutz des öffentlichen Endpoints, mindestens 32 Zeichen (z.B. `openssl rand -base64 48`). Ohne oder mit zu kurzem Token startet der Server nicht (fail-safe) |
| `ZAMMAD_DEFAULT_OWNER` | nein | leer | Agent (E-Mail, Login oder 'Vorname Nachname'), dem ein Ticket zugewiesen wird, falls Zammad es beim Schreiben über das MCP dem MCP-User zuweist. Leer = Zuweisung wird entfernt. Der MCP-User selbst wird nie als Owner gesetzt |
| `MCP_HOST` | nein | `0.0.0.0` | Bind-Adresse des HTTP-Servers *innerhalb* des Containers |
| `MCP_PORT` | nein | `8000` | Port des HTTP-Servers *innerhalb* des Containers |
| `MCP_BIND_ADDR` | nein | `127.0.0.1` | Bind-Adresse *auf dem Docker-Host* (docker-compose Port-Mapping). Für den Zugriff des Reverse-Proxys über die Host-IP auf diese IP setzen (nicht `0.0.0.0`) |
| `MCP_HOST_PORT` | nein | `8420` | Port *auf dem Docker-Host* (docker-compose Port-Mapping) |
| `ATTACHMENT_TEXT_MAX_BYTES` | nein | `26214400` (25 MB) | Maximale Anhangsgrösse für `download_attachment`, `get_attachment_text`, `find_in_attachments` und die Anhänge von `forward_ticket` (Content-Length vorab, Abbruch beim Streamen) |
| `ATTACHMENT_TEXT_MAX_PDF_PAGES` | nein | `200` | Maximale Seitenzahl pro PDF |
| `ATTACHMENT_TEXT_TIMEOUT` | nein | `30` | Timeout pro Datei in Sekunden (Extraktion wird danach hart beendet) |
| `ATTACHMENT_TEXT_MAX_MEMORY_MB` | nein | `1024` | Speicherlimit des Extraktionsprozesses (nur Linux/macOS) |
| `URL_ATTACHMENT_MAX_BYTES` | nein | `20971520` (20 MB) | Maximale Dateigrösse pro URL-Download bei `forward_ticket` (`attachment_urls`) |
| `FIND_IN_ATTACHMENTS_TIMEOUT` | nein | `240` | Gesamtzeit für `find_in_attachments`, danach werden restliche Anhänge als `not_checked` gemeldet |
| `OCR_TIMEOUT_SECONDS` | nein | `50` | Zeitbudget pro Aufruf für OCR (inkl. Download und normaler Extraktion); danach Rückgabe mit `next_page` |
| `OCR_MAX_PAGES` | nein | `60` | OCR nur für die Seiten 1 bis N eines Dokuments, spätere Seiten werden als `ocr_skipped_pages` gemeldet |
| `OCR_DPI` | nein | `300` | Auflösung beim Rastern der PDF-Seiten (max. 600) |
| `OCR_LANGS` | nein | `deu+fra+eng+ita` | Tesseract-Sprachen (die Sprachpakete müssen im Image installiert sein) |
| `OCR_CACHE_DIR` | nein | leer (aus) | Verzeichnis für den Disk-Cache der OCR-Ergebnisse, z.B. ein Volume unter `/cache/ocr` |
| `OCR_CACHE_TTL_HOURS` | nein | `24` | Gültigkeit der Cache-Einträge (Memory und Disk) in Stunden |
| `OCR_MAX_IMAGE_PIXELS` | nein | `100000000` | Pixel-Limit pro Bild/Seite gegen Decompression-Bombs; PDF-Seiten werden darunter gerastert |
| `OCR_MAX_PARALLEL` | nein | `2` | Gleichzeitige OCR-Prozesse |
| `OCR_MAX_MEMORY_MB` | nein | `2048` | Speicherlimit des OCR-Prozesses (nur Linux/macOS) |
| `SMTP2GO_API_KEY` | für SMS | leer | API-Key von SMTP2GO für `send_sms` / `list_received_sms` (ohne Key liefern beide einen Fehler, der Rest läuft normal) |
| `SMTP2GO_SMS_SENDER` | nein | leer | Standard-Absendernummer im E.164-Format (z.B. `+447700900123`); leer = Standardabsender des SMTP2GO-Kontos |
| `SMS_MAX_UNITS` | nein | `3` | Maximale SMS-Teile pro Nachricht (1-10). Muss zum "Message size limit" in den SMTP2GO-SMS-Einstellungen passen, sonst schneidet SMTP2GO bei aktiver "Message truncation" längere Texte still ab |
| `SMTP2GO_API_URL` | nein | `https://api.smtp2go.com/v3` | Basis-URL der SMTP2GO-API (z.B. regionale Variante) |

`MCP_HOST`/`MCP_PORT` steuern den Server im Container, `MCP_BIND_ADDR`/`MCP_HOST_PORT` das Port-Mapping nach aussen (docker-compose `ports:`) - getrennt, damit man den Container z.B. nur auf localhost binden und den öffentlichen Zugriff über einen Reverse-Proxy führen kann.

## Image-Build (GitHub Actions → GitHub Container Registry)

Das Docker-Image wird **nicht** lokal aus dem Dockerfile gebaut, sondern bei jedem Push auf `main` automatisch per GitHub Actions gebaut und nach `ghcr.io/<repo>:latest` veröffentlicht (`.github/workflows/docker-publish.yml`). Grund: Portainer kann Images zuverlässig pullen, aber ein Dockerfile im Repo nicht zuverlässig automatisch neu bauen - "Pull and redeploy" funktioniert dadurch immer nur mit einer schon vorhandenen lokalen Kopie, nicht mit einem frischen Build. Mit einem fertigen Image aus einer Registry fällt dieses Problem weg.

**Einmalig nach dem ersten Push:** Das neu erstellte Package ist auf GitHub standardmässig privat, auch wenn das Repo öffentlich ist. Unter `github.com/KDT-Solutions/zammad-mcp` → Reiter **Packages** → `zammad-mcp` → **Package settings** → **Change visibility** → **Public** stellen, sonst kann Portainer das Image nicht ohne Zugangsdaten pullen.

## Cloud-Betrieb (Docker Compose, manuell)

```bash
git clone <repo-url> zammad-mcp
cd zammad-mcp
cp .env.example .env
# .env ausfüllen: ZAMMAD_URL, ZAMMAD_TOKEN, MCP_AUTH_TOKEN (zufälliges Token, mind. 32 Zeichen, z.B. openssl rand -base64 48)
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
   - für SMS: `SMTP2GO_API_KEY`, optional `SMTP2GO_SMS_SENDER`
   - `MCP_BIND_ADDR` auf die Host-IP setzen, über die der Reverse-Proxy den Container erreicht (Standard `127.0.0.1`), optional `MCP_HOST_PORT`
4. **Deploy the stack**
5. Für ein Update später: im Stack auf **Pull and redeploy** klicken - das zieht jetzt zuverlässig das aktuelle Image aus der Registry (kein lokaler Build mehr nötig, siehe oben)

Da `docker-compose.yml` alle Werte ausschliesslich über `${VARIABLE}`-Platzhalter referenziert, funktioniert das identisch für lokales `docker compose` (mit `.env`-Datei) und für Portainer (mit den dort hinterlegten Stack-Variablen) - ohne Anpassungen am Repo.

## Netzwerk / Reverse-Proxy

Der Container bindet standardmässig nur auf `127.0.0.1:8420` auf dem Docker-Host. Soll der Reverse-Proxy den Dienst über die Host-IP erreichen, `MCP_BIND_ADDR` auf diese IP setzen (nicht `0.0.0.0`); der Zugriff ist zusätzlich durch das Bearer-Token geschützt. Für den Zugriff aus einer Cloud-Claude-Session braucht es:

1. Eine eigene Subdomain (z.B. `zammad-mcp.deine-domain.ch`)
2. Einen Reverse-Proxy (Plesk/nginx) mit TLS-Zertifikat, der auf `<Host-IP>:8420` weiterleitet (Endpoint-Pfad: `/mcp`)
3. In der Cloud-Claude-Session wird der Server dann als Remote-MCP mit `https://zammad-mcp.deine-domain.ch/mcp` und dem `MCP_AUTH_TOKEN` als Bearer-Token eingebunden

## Sicherheit

- Der HTTP-Modus startet nur, wenn `MCP_AUTH_TOKEN` gesetzt und mindestens 32 Zeichen lang ist - es gibt also nie einen ungeschützten öffentlichen Endpoint.
- Jeder HTTP-Request muss den Header `Authorization: Bearer <MCP_AUTH_TOKEN>` mitschicken, sonst Antwort `401 unauthorized`. Der Vergleich ist timing-sicher (`hmac.compare_digest`).
- Brute-Force-Bremse: nach 10 falschen Tokens innerhalb von 10 Minuten antworten unauthentifizierte Requests mit `429` (Header `Retry-After`); Requests mit gültigem Token werden nie gebremst. Einfacher In-Memory-Zähler über alle Quellen, Fehlversuche stehen mit Quelladresse im Container-Log (`zammad_mcp.auth`). Zusätzlich empfohlen: `limit_req` im Reverse-Proxy.
- Tools mit Aussenwirkung haben ein confirm-Gate: `forward_ticket`, `send_sms`, `delete_ticket_article`, `merge_ticket`, `set_article_internal` (bei `internal=false`) und `set_ticket_customer` liefern ohne `confirm=true` nur eine Vorschau und ändern bzw. senden nichts.
- Ticket- und Anhangsinhalte stammen von Dritten (E-Mails) und können Anweisungen enthalten (Prompt Injection). Die Rückgaben von `get_ticket`, `download_attachment`, `get_attachment_text`, `find_in_attachments` und der Vorschauen tragen deshalb einen `content_notice`; die confirm-Gates sind die eigentliche Absicherung.
- Alle IDs und Ticketnummern, die in Zammad-API-Pfade eingesetzt werden, werden im Code validiert (ganze Zahlen bzw. nur Ziffern), unabhängig von der Schema-Prüfung des MCP-SDK (dafür `mcp >= 1.10`).
- Fehlermeldungen an den Client enthalten nur den Statuscode und eine generische Meldung - keine Zammad-URL, keinen Response-Text, keine gesendete Payload. Details stehen im Container-Log (`zammad_mcp.api`).
- URL-Downloads (`forward_ticket` mit `attachment_urls`): nur http/https ohne Zugangsdaten, Hostname wird einmal aufgelöst und muss ausschliesslich auf öffentliche Adressen zeigen; die Verbindung wird an die geprüfte IP gepinnt (Host-Header und SNI = Originalname, keine Proxy-Umgebung), jeder Redirect-Hop wird erneut geprüft (kein DNS-Rebinding-Fenster). Grössenlimit `URL_ATTACHMENT_MAX_BYTES`, nur PDFs.
- Container läuft als User `app` (UID 10001), Base-Image per Digest und `requirements.txt` exakt gepinnt, Compose mit `no-new-privileges` und `cap_drop: ALL`.
- Einzige Ausnahme ist `GET /healthz` für den Docker-Healthcheck: ohne Token, liefert nur `{"status": "ok"}` (keine Version, keine Konfiguration, keine Abfrage der Zammad-API). Das Image bringt den `HEALTHCHECK` mit (interval 30s, timeout 5s, start_period 15s, retries 3).
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
| `download_attachment` | Anhang herunterladen (als base64, max. `ATTACHMENT_TEXT_MAX_BYTES`) |
| `get_attachment_text` | Text eines Anhangs (PDF, XLSX, DOCX, CSV, TXT, Scans und Bilder per OCR) serverseitig extrahieren, mit Paging über `offset`/`max_chars` und `page_from`/`page_to` |
| `find_in_attachments` | Tickets per Suche finden und alle Anhänge nach einem Begriff (oder Regex) durchsuchen, optional inkl. OCR |
| `find_invoice_aggregation_tickets` | Tickets mit Invoice-Aggregation-Excel-Anhängen |
| `create_ticket` | Neues Ticket erstellen (Gruppe ist Pflicht, unbekannte Gruppen werden abgelehnt) |
| `list_groups` | Alle aktiven Gruppen auflisten |
| `set_ticket_group` | Ticket in eine andere Gruppe verschieben |
| `set_ticket_owner` | Ticket einem Agent zuweisen (E-Mail, Login oder Name; `none` entfernt die Zuweisung) |
| `set_ticket_customer` | Kunde eines Tickets wechseln (E-Mail, Login oder Name eines bestehenden Users; Organisation wird mitgesetzt, kein automatisches Anlegen). Ohne `confirm=true` nur Vorschau |
| `update_customer_name` | Vor- und/oder Nachname eines Kunden korrigieren (per E-Mail oder Ticket) |
| `add_ticket_note` | Interne Notiz hinzufügen |
| `update_ticket_state` | Ticket-Status ändern |
| `set_ticket_pending` | Ticket auf "pending reminder" setzen |
| `merge_ticket` | Zwei Tickets zusammenführen (`master_ticket_number` nur Ziffern). Ohne `confirm=true` nur Vorschau von Quell- und Zielticket |
| `delete_ticket_article` | Ticket-Artikel löschen. Ohne `confirm=true` nur Vorschau des Artikels |
| `set_article_internal` | Bestehenden Ticket-Artikel auf intern (direkt) oder öffentlich stellen (`internal=false` macht ihn kundensichtbar: ohne `confirm=true` nur Vorschau) |
| `send_sms` | SMS über das SMS-Gateway von SMTP2GO senden. Ohne `confirm=true` nur Vorschau. Ohne `to` wird die Mobilnummer des Ticket-Kunden verwendet; mit `ticket_id` wird der Versand als interne Notiz im Ticket dokumentiert |
| `list_received_sms` | Bei SMTP2GO eingegangene SMS (Antworten) abfragen, read-only |
| `forward_ticket` | Ticket-Artikel per E-Mail weiterleiten. Ohne `confirm=true` wird nichts gesendet und nichts heruntergeladen, nur Vorschau (Empfänger, Betreff, Anhangsliste, Textauszug). `to` = genau eine Adresse, `body` = Klartext (wird HTML-escaped). Mit `attachment_urls` werden PDFs, die nur als Link in der Mail stehen (z.B. Online-Rechnungen), serverseitig geladen und im Original angehaengt (nur oeffentliche http/https-Adressen, Verbindung an die geprüfte IP gepinnt, max. 20 MB, via `URL_ATTACHMENT_MAX_BYTES` anpassbar). Schlägt ein Download fehl, wird nicht gesendet |

## SMS (SMTP2GO)

`send_sms` schickt die SMS direkt über die SMTP2GO-API (`POST /sms/send`), nicht über einen Zammad-SMS-Kanal.

- **Bestätigung:** Ohne `confirm=true` wird nichts gesendet. Die Vorschau zeigt Empfänger, Absender, Kodierung (GSM-7 oder UCS-2) und die Anzahl SMS-Teile. Längere Texte als `SMS_MAX_UNITS` Teile (Standard 3, höchstens 10) werden abgelehnt. Der Wert muss zum "Message size limit" bei SMTP2GO passen.
- **Nummern:** Schweizer Mobilnummern (`07x 123 45 67`) werden automatisch zu `+417x...` ergänzt, sonst internationales Format (`+49...` oder `0049...`). Andere nationale Nummern (z.B. Festnetz `061...`) werden abgelehnt.
- **Empfänger aus dem Ticket:** Ohne `to` wird das Feld *Mobil* des Ticket-Kunden verwendet. Ist es leer, kommt ein Fehler.
- **Dokumentation:** Mit `ticket_id` landet nach dem Versand eine interne Notiz (Typ `note`) mit Empfänger, Absender und Text im Ticket. Bewusst nicht als Artikeltyp `sms`, damit ein eventuell eingerichteter Zammad-SMS-Kanal die Nachricht nicht ein zweites Mal verschickt. Das Ticket muss vor dem Versand existieren, sonst wird nichts gesendet.
- **Antworten:** kommen nicht über diesen Server zurück. In SMTP2GO unter *SMS Settings → Forward replies* eine E-Mail-Adresse eintragen, die als Kanal in Zammad eingebunden ist; die Adresse muss bei SMTP2GO ein verifizierter Absender sein (Domain oder Single Sender). Zusätzlich lassen sich eingegangene SMS mit `list_received_sms` abfragen.
- **Absender:** Antworten sind nur möglich, wenn die SMS von einer Nummer kommt, die Antworten empfangen kann. Mit einer alphanumerischen Sender-ID gehen laut SMTP2GO keine Antworten ein.

## Textextraktion aus Anhängen

`download_attachment` liefert Base64, das bei grösseren PDFs im Client abgeschnitten wird. `get_attachment_text` und `find_in_attachments` extrahieren den Inhalt deshalb auf dem Server und geben nur Klartext zurück.

**get_attachment_text(ticket_id, article_id, attachment_id, offset=0, max_chars=20000, ocr="auto", page_from=None, page_to=None)**

- Rückgabe: `filename`, `content_type`, `pages` (PDF/Bild), `page_from`, `page_to`, `extraction_method`, `ocr_pages`, `total_chars`, `offset`, `returned_chars`, `truncated`, `next_offset` bzw. `next_page` (falls `truncated`), `text`
- `page_from`/`page_to` wählen gezielt einzelne Seiten (PDF, mehrseitiges TIFF), z.B. `page_from=3, page_to=3`.
- `max_chars` ist auf 50000 begrenzt. Lange Dokumente mit `offset=next_offset` weiter abrufen, bis `truncated=false`. Das extrahierte Ergebnis wird im Prozess zwischengespeichert, Folgeaufrufe laden die Datei nicht erneut.
- PDF: pro Seite mit Trenner `--- Seite N ---`, Zeilen nach y gruppiert und nach x sortiert, Tabellenspalten durch drei Leerzeichen getrennt. XLSX: pro Blatt `--- Blatt: Name ---`, Zellen tab-getrennt. DOCX: Absätze und Tabellen in Dokumentreihenfolge, Kopf-/Fusszeilen am Ende.
- Andere Dateitypen (Bilder, .doc, .xls, ...) werden mit einer klaren Fehlermeldung abgelehnt.

**PDF-Extraktion (`extraction_method`)**

1. `pdfminer` – Standardweg über pdfminer.six.
2. `font_cmap` – Fallback, wenn der Text Zeichensalat ist (Anteil an `(cid:..)`, Steuerzeichen, Private-Use-Zeichen oder U+FFFD über 20 %). Typisch bei PDFs aus WPS Office: eingebettete TrueType-Subset-Fonts ohne brauchbare ToUnicode-CMap, der Text steht als Glyph-IDs im Content-Stream (`<0027>Tj`). Die eingebetteten Fonts (FontFile2) werden mit fontTools geladen, aus der cmap-Tabelle (bzw. den Glyph-Namen) wird eine Reverse-Map GID → Unicode gebaut und die Content-Streams werden damit dekodiert.
3. `gid_offset_heuristic` – nur wenn Fallback 1 nichts Brauchbares liefert: GID + 29 = ASCII (Standard-Glyph-Reihenfolge, z.B. `0x0027` → `D`, `0x0003` → Leerzeichen, `0x0013` → `0`). Das ist geraten, das Ergebnis also prüfen.

4. `ocr` – Seite per Tesseract gelesen (siehe unten).

Die Entscheidung fällt pro Seite. `extraction_method` nennt die unsicherste Methode, die im Dokument nötig war, bzw. `ocr` (alle zurückgegebenen Seiten per OCR) oder `mixed` (teils OCR, teils Textebene). `pages_by_method` zeigt bei gemischten Dokumenten, welche Seite wie extrahiert wurde, `ocr_pages` listet die OCR-Seiten.

**OCR für Scans und Bilder**

Gescannte PDFs (z.B. vom Netzwerkscanner) und Bilder haben keine Textebene. Dafür gibt es einen OCR-Fallback mit Tesseract (Sprachen deu, fra, eng, ita). PDF-Seiten werden mit pypdfium2 in Graustufen gerastert (Standard 300 dpi).

- `ocr="auto"` (Standard): nur Seiten, deren normale Extraktion weniger als 20 Buchstaben/Ziffern liefert, werden per OCR gelesen.
- `ocr="force"`: alle Seiten im gewählten Bereich per OCR, z.B. wenn die Textebene eines Scans unbrauchbar ist.
- `ocr="off"`: kein OCR.
- Bilder (PNG, JPEG, TIFF inkl. mehrseitiger TIFFs) werden direkt per OCR gelesen, jede TIFF-Seite ist eine Seite.

**Laufzeit:** OCR braucht je nach Seite etwa 2 bis 6 Sekunden. Pro Aufruf gilt ein Zeitbudget (`OCR_TIMEOUT_SECONDS`, Standard 50 s, inkl. Download und normaler Extraktion). Ist es aufgebraucht, kommt die Antwort sofort mit den fertigen Seiten zurück, dazu `truncated=true`, `ocr_incomplete=true` und `next_page`. Der nächste Aufruf mit `page_from=next_page` setzt dort fort. Fertige Seiten sind pro (Ticket, Artikel, Anhang, Seite) gecacht, Folgeaufrufe mit `offset` oder `page_from` lesen sie nicht erneut. Es werden nur so viele Seiten per OCR gelesen, wie für `offset + max_chars` nötig sind. Braucht eine einzelne Seite das ganze Budget, wird sie als `ocr_failed_pages` gemeldet und übersprungen.

**Cache:** In-Memory-LRU, optional zusätzlich auf Disk (`OCR_CACHE_DIR`, z.B. als Volume). Die Dateinamen sind ein SHA-256-Hash aus den IDs, der Seite und den OCR-Einstellungen, nie der Anhangname. Dateien werden mit Rechten 0600 angelegt, das Verzeichnis mit 0700. Einträge laufen nach `OCR_CACHE_TTL_HOURS` (Standard 24 h) ab.

**find_in_attachments(ticket_query, pattern, regex=false, limit_tickets=50, context_chars=150)**

- Sucht Tickets über die normale Zammad-Ticketsuche und extrahiert alle unterstützten Anhänge aller Artikel. Gleiche Anhänge (identische `attachment_id`) werden nur einmal verarbeitet.
- Standard: einfacher Substring, Gross-/Kleinschreibung egal (`52.5` sucht wörtlich nach `52.5`). Mit `regex=true` als regulärer Ausdruck (ebenfalls case-insensitive, max. 200 Zeichen, Timeout gegen ReDoS über das Paket `regex`).
- Rückgabe: `matches` (Ticketnummer, Titel, Artikel, Anhang, Dateiname, Seite bzw. Blatt, Treffer mit Kontext, `extraction_method`), `attachments_without_match`, `errors` (z.B. Limits überschritten), `skipped_unsupported` und gegebenenfalls `not_checked`, damit klar ist, was tatsächlich geprüft wurde.
- `limit_tickets` ist auf 200 begrenzt, Treffer auf 50 pro Anhang und 500 insgesamt (`match_count` zählt trotzdem alle).
- `ocr` (Standard `"off"`, weil teuer): mit `"auto"` werden auch Scans und Bilder per OCR durchsucht, Treffer aus OCR-Seiten tragen `"ocr": true`. Mit `"off"` werden Bilder als `skipped_unsupported` gemeldet und PDF-Seiten ohne Textebene bei den Anhängen ohne Treffer als `pages_without_text` aufgeführt. Reicht das Zeitbudget pro Anhang nicht, steht der Anhang in `ocr_incomplete`; ein erneuter Aufruf macht dank Cache dort weiter.

**Sicherheit**

- Beide Tools sind strikt read-only (nur GET-Requests an Zammad).
- Limits pro Datei: 25 MB, 200 Seiten, 30 s für die normale Extraktion. Bei Überschreitung wird abgebrochen und das in der Rückgabe gemeldet (`limit_exceeded`: `size`, `pages`, `timeout`, `memory`, `pixels`).
- OCR läuft ebenfalls in einem eigenen Prozess (Zeitbudget, Speicherlimit, Pixel-Limit über `PIL.Image.MAX_IMAGE_PIXELS`). Tesseract wird nur mit festen Argumenten aus der Konfiguration aufgerufen, nie über eine Shell. Temporäre Dateien liegen in einem eigenen Verzeichnis, das danach immer gelöscht wird.
- Im Log stehen bei OCR nur Seitenzahl, Dauer und Zeichenanzahl, nie der erkannte Text.
- Die Extraktion läuft in einem eigenen Python-Prozess mit Timeout und Speicherlimit, damit defekte oder präparierte Dateien den Server nicht blockieren. XLSX/DOCX werden vorher auf die entpackte Grösse geprüft (Zip-Bomben).

## Typischer Workflow (Invoice Aggregation)

1. Claude ruft `find_invoice_aggregation_tickets` auf
2. Findet alle Excel-Files nach Datum sortiert
3. Lädt die relevanten Files via `download_attachment` herunter
4. Vergleicht mit dem Rebill-Dump → kein manuelles Hochladen nötig
