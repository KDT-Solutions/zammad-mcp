FROM python:3.12-slim

WORKDIR /app

# Tesseract fuer den OCR-Fallback (Scans, Bilder) mit den Sprachpaketen deu/fra/eng/ita.
# --no-install-recommends und geleerte apt-Listen halten das Image klein; die Seiten
# werden mit pypdfium2 gerastert (pip-Wheel), Ghostscript/Poppler sind nicht noetig.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        tesseract-ocr tesseract-ocr-deu tesseract-ocr-fra tesseract-ocr-eng tesseract-ocr-ita \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY pyproject.toml ./
COPY zammad_mcp ./zammad_mcp

ENV MCP_HOST=0.0.0.0 \
    MCP_PORT=8000

# Git-Commit und Version des Builds (von GitHub Actions gesetzt) - get_version
# gibt beides zurueck, damit nach einem Redeploy pruefbar ist, welcher Stand wirklich laeuft.
ARG GIT_SHA=unbekannt
ARG APP_VERSION=
ENV GIT_SHA=$GIT_SHA \
    APP_VERSION=$APP_VERSION

EXPOSE 8000

# /healthz ist ohne Auth erreichbar und prueft nur, ob der Prozess antwortet
# (nicht die Zammad-API). Kein curl/wget im Image, darum per Python/urllib.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('MCP_PORT', '8000') + '/healthz', timeout=4)"]

CMD ["python", "-m", "zammad_mcp"]
