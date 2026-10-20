# Changelog

## 1.28 (Nachtrag)

- `send_sms`: Schweizer Mobilnummern im Format `07x 123 45 67` werden automatisch zu `+417x...` ergänzt.

## 1.28

- Neues Tool `send_sms`: SMS über das SMS-Gateway von SMTP2GO senden. Ohne `confirm=true` nur Vorschau (Empfänger, Absender, Kodierung, SMS-Teile). Empfänger im internationalen Format oder Mobilnummer des Ticket-Kunden; mit `ticket_id` wird der Versand als interne Notiz dokumentiert. Max. 10 SMS-Teile.
- Neues Tool `list_received_sms`: eingegangene SMS bei SMTP2GO abfragen (read-only).
- Neue Umgebungsvariablen `SMTP2GO_API_KEY`, `SMTP2GO_SMS_SENDER`, `SMTP2GO_API_URL`.

## 1.27

- Neuer Endpunkt `GET /healthz` ohne Auth, liefert nur `{"status": "ok"}`. Prüft nur, ob der Prozess antwortet, nicht die Zammad-API. Alle anderen Pfade brauchen weiterhin `MCP_AUTH_TOKEN`.
- `HEALTHCHECK` im Dockerfile (Python/urllib auf `/healthz`, interval 30s, timeout 5s, start_period 15s, retries 3).
- HTTP-App in `build_http_app()` ausgelagert (testbar), Verhalten unverändert.
