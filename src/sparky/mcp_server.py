"""AL as an MCP server: the dbt Semantic Layer tools behind the PII guard, plus the arm 3 rules.

Claude Desktop (or any MCP client) is the LLM, so there is no agent loop here. This process proxies
the allow-listed dbt-mcp tools, checks every call with guards.py before it runs, scrubs every result
before the client sees it, and serves the Socratic rules and context cards as server instructions,
a prompt and a `get_context` tool. It holds no per-user state.

    sparky-mcp                              # stdio (Claude Desktop config, `docker run -i`)
    SPARKY_TRANSPORT=http sparky-mcp        # streamable HTTP on SPARKY_HOST:SPARKY_PORT at /mcp
"""
import contextlib
import logging
import os
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, Protocol

import anyio
import mcp.types as t
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from . import guards
from .agent import dbt_mcp_config, load_system_prompt
from .config import Settings
from .context import load_context_cards, render_for_prompt
from .modes import get_mode

log = logging.getLogger("sparky.mcp")
RULES_DIR = Path(__file__).parent / "rules"
PROMPT_NAME = "al"
GET_CONTEXT = "get_context"
GET_CONTEXT_TOOL = t.Tool(
    name=GET_CONTEXT,
    description=(
        "Business context cards for AL's metrics: known structural causes, caveats and investigation "
        "notes. Read them before explaining an unexpected or unusual result."
    ),
    input_schema={
        "type": "object",
        "properties": {"metric": {"type": "string", "description": "Metric name; omit for all cards"}},
    },
    annotations=t.ToolAnnotations(read_only_hint=True),
)
DENIED = "Denied: {}"


class Upstream(Protocol):
    tools: list[t.Tool]

    async def call_tool(self, name: str, args: dict[str, Any]) -> t.CallToolResult: ...


class DbtUpstream:
    """One long-lived dbt-mcp subprocess shared by every client session."""

    def __init__(self, settings: Settings):
        self._settings = settings
        self._session: ClientSession | None = None
        self.tools: list[t.Tool] = []

    @contextlib.asynccontextmanager
    async def running(self) -> AsyncIterator["DbtUpstream"]:
        cfg = dbt_mcp_config(self._settings, get_mode(self._settings.mode))
        params = StdioServerParameters(command=cfg["command"], args=cfg["args"], env=cfg["env"])
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            allowed = set(get_mode(self._settings.mode).dbt_tools)
            listed = (await session.list_tools()).tools
            self.tools = [
                tool.model_copy(update={"annotations": t.ToolAnnotations(read_only_hint=True)})
                for tool in listed if tool.name in allowed
            ]
            missing = allowed - {tool.name for tool in self.tools}
            if missing:
                log.warning("dbt-mcp did not offer tools: %s", sorted(missing))
            self._session = session
            try:
                yield self
            finally:
                self._session = None

    async def call_tool(self, name: str, args: dict[str, Any]) -> t.CallToolResult:
        if self._session is None:
            raise RuntimeError("dbt-mcp is not running")
        return await self._session.call_tool(name, args)


def _text_result(text: str, is_error: bool = False) -> t.CallToolResult:
    return t.CallToolResult(content=[t.TextContent(text=text)], is_error=is_error)


def scrub_tool_result(res: t.CallToolResult) -> t.CallToolResult:
    """Drop identifier columns and redact PII from every text block (errors can quote data too).

    Anything that is not text is dropped: the dbt tools only return text.
    """
    content = [t.TextContent(text=guards.scrub_result(c.text)) for c in res.content if isinstance(c, t.TextContent)]
    structured = guards.scrub_result(res.structured_content) if res.structured_content is not None else None
    return t.CallToolResult(content=content, structured_content=structured, is_error=res.is_error)


def build_instructions(pack: dict[str, Any] | None) -> str:
    mode = get_mode("arm3")
    return "\n\n".join([load_system_prompt(mode, pack), (RULES_DIR / "mcp.md").read_text()]).strip()


def build_server(upstream: Upstream, settings: Settings | None = None) -> Server:
    settings = settings or Settings()
    pack = load_context_cards(settings.context_path)
    instructions = build_instructions(pack)

    async def list_tools(ctx, params) -> t.ListToolsResult:
        return t.ListToolsResult(tools=[*upstream.tools, GET_CONTEXT_TOOL])

    async def call_tool(ctx, params: t.CallToolRequestParams) -> t.CallToolResult:
        name, args = params.name, params.arguments or {}
        started = time.monotonic()
        outcome = "ok"
        try:
            if name == GET_CONTEXT:
                metric = args.get("metric")
                scope = {metric: pack["metrics"][metric]} if metric in pack["metrics"] else pack["metrics"]
                return _text_result(render_for_prompt({"metrics": scope}) or "No context cards available.")
            if name not in {tool.name for tool in upstream.tools}:
                outcome = "unknown"
                return _text_result(f"Unknown tool {name!r}.", is_error=True)
            reason = guards.check_call(name, args)
            if reason:
                outcome = "denied"
                return _text_result(DENIED.format(reason), is_error=True)
            return scrub_tool_result(await upstream.call_tool(name, args))
        except Exception:
            outcome = "error"
            # The exception text can quote warehouse data, so only the server log gets it.
            log.exception("tool %s failed", name)
            return _text_result("The tool call failed. The details are in the server log.", is_error=True)
        finally:
            # Audit trail: tool and outcome only, never arguments or results.
            log.info("tool=%s outcome=%s ms=%d", name, outcome, (time.monotonic() - started) * 1000)

    async def list_prompts(ctx, params) -> t.ListPromptsResult:
        return t.ListPromptsResult(prompts=[t.Prompt(
            name=PROMPT_NAME,
            description="Start an AL analytics conversation with the Socratic rules and context cards.",
        )])

    async def get_prompt(ctx, params: t.GetPromptRequestParams) -> t.GetPromptResult:
        if params.name != PROMPT_NAME:
            raise ValueError(f"Unknown prompt {params.name!r}")
        return t.GetPromptResult(messages=[
            t.PromptMessage(role="user", content=t.TextContent(text=instructions)),
        ])

    return Server(
        "al", title="AL, powered by Action Lab", instructions=instructions,
        on_list_tools=list_tools, on_call_tool=call_tool,
        on_list_prompts=list_prompts, on_get_prompt=get_prompt,
    )


async def _health(request: Request) -> JSONResponse:
    return JSONResponse({"ok": True})


async def serve_stdio(settings: Settings) -> None:
    upstream = DbtUpstream(settings)
    async with upstream.running():
        server = build_server(upstream, settings)
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())


def build_http_app(upstream: DbtUpstream, settings: Settings, host: str):
    server = build_server(upstream, settings)
    app = server.streamable_http_app(
        host=host, stateless_http=True, custom_starlette_routes=[Route("/health", _health)],
    )
    inner = app.router.lifespan_context

    @contextlib.asynccontextmanager
    async def lifespan(a):
        async with upstream.running(), inner(a):
            yield

    app.router.lifespan_context = lifespan
    return app


def main() -> None:
    logging.basicConfig(level=os.getenv("SPARKY_LOG_LEVEL", "INFO"))
    settings = Settings()
    transport = os.getenv("SPARKY_TRANSPORT", "stdio").lower()
    if transport == "stdio":
        anyio.run(serve_stdio, settings)
    elif transport == "http":
        import uvicorn
        host = os.getenv("SPARKY_HOST", "127.0.0.1")
        if settings.use_oauth:
            log.warning("DBT_TOKEN is empty: dbt-mcp will try a browser OAuth login, which fails in a container")
        uvicorn.run(build_http_app(DbtUpstream(settings), settings, host), host=host,
                    port=int(os.getenv("SPARKY_PORT", "8080")))
    else:
        raise SystemExit(f"SPARKY_TRANSPORT must be 'stdio' or 'http', got {transport!r}")


if __name__ == "__main__":
    main()
