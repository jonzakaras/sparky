# AL MCP server. No Node or Claude CLI: Claude Desktop is the LLM, this image only proxies dbt-mcp.
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
COPY data ./data
RUN uv sync --frozen --no-dev

RUN useradd --create-home sparky && chown -R sparky /app
USER sparky
# dbt-mcp is started with `uvx`; warm its cache at build time so the first request is not a download.
RUN uvx dbt-mcp --help >/dev/null 2>&1 || true

ENV PATH="/app/.venv/bin:$PATH" \
    SPARKY_TRANSPORT=http \
    SPARKY_HOST=0.0.0.0 \
    SPARKY_PORT=8080 \
    SPARKY_CONTEXT_PACK=/app/data/context_cards.json
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s \
  CMD python -c "import urllib.request,sys; sys.exit(urllib.request.urlopen('http://127.0.0.1:8080/health').status != 200)"

# `docker run -i -e SPARKY_TRANSPORT=stdio ...` serves a local Claude Desktop over stdio instead.
CMD ["sparky-mcp"]
