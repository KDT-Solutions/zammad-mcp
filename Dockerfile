FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY pyproject.toml ./
COPY zammad_mcp ./zammad_mcp

ENV MCP_TRANSPORT=http \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8000

# Git-Commit des Builds (von GitHub Actions gesetzt) - get_version gibt ihn
# zurueck, damit nach einem Redeploy pruefbar ist, welcher Stand wirklich laeuft.
ARG GIT_SHA=unbekannt
ENV GIT_SHA=$GIT_SHA

EXPOSE 8000

CMD ["python", "-m", "zammad_mcp"]
