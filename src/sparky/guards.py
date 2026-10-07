"""Tool-call guards shared by the web agent (hooks in agent.py) and the MCP server (mcp_server.py)."""
from typing import Any

from . import pii

# Semantic-layer tools whose arguments can name an identifier (group_by, where, dimension).
SEMANTIC_GUARDED = frozenset({"query_metrics", "get_metrics_compiled_sql", "get_dimension_values"})


def check_call(tool: str, args: dict[str, Any] | None) -> str | None:
    """Reason to refuse a dbt tool call before it runs, or None to allow it."""
    if tool in SEMANTIC_GUARDED:
        return pii.check_semantic_args(args or {})
    return None


def scrub_result(result: Any) -> Any:
    """Copy of a dbt tool result with identifier columns dropped and PII values redacted."""
    return pii.scrub_payload(result)
