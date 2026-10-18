# Changelog

## 1.27

- Neuer Endpunkt `GET /healthz` ohne Auth, liefert nur `{"status": "ok"}`. Prüft nur, ob der Prozess antwortet, nicht die Zammad-API. Alle anderen Pfade brauchen weiterhin `MCP_AUTH_TOKEN`.
- `HEALTHCHECK` im Dockerfile (Python/urllib auf `/healthz`, interval 30s, timeout 5s, start_period 15s, retries 3).
- HTTP-App in `build_http_app()` ausgelagert (testbar), Verhalten unverändert.
