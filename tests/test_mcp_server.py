"""MCP server: tool allow-list, PII guard on calls and results, rules and context delivery."""
import json
from pathlib import Path

import mcp.types as t
import pytest
from mcp.client import Client

from sparky.config import DBT_TOOLS, Settings
from sparky.mcp_server import GET_CONTEXT, PROMPT_NAME, build_instructions, build_server


class FakeUpstream:
    def __init__(self, reply: str = "[]", is_error: bool = False):
        self.tools = [t.Tool(name=n, description=n, input_schema={"type": "object"}) for n in DBT_TOOLS]
        self.calls: list[tuple[str, dict]] = []
        self.reply, self.is_error = reply, is_error

    async def call_tool(self, name, args):
        self.calls.append((name, args))
        if self.reply == "boom":
            raise RuntimeError("warehouse said emplid 123456789")
        return t.CallToolResult(content=[t.TextContent(text=self.reply)], is_error=self.is_error)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    cards = {"metrics": {"enrollment": {"description": "Headcount", "context_card": {"caveats": ["Census lag"]}}}}
    path = tmp_path / "cards.json"
    path.write_text(json.dumps(cards))
    return Settings(context_path=path)


async def connect(upstream, settings):
    return Client(build_server(upstream, settings))


async def test_lists_only_allow_listed_tools_plus_context(settings):
    async with await connect(FakeUpstream(), settings) as c:
        names = {tool.name for tool in (await c.list_tools()).tools}
    assert names == set(DBT_TOOLS) | {GET_CONTEXT}
    assert "execute_sql" not in names


async def test_unknown_tool_is_refused_and_not_forwarded(settings):
    up = FakeUpstream()
    async with await connect(up, settings) as c:
        res = await c.call_tool("execute_sql", {"sql": "select 1"})
    assert res.is_error and up.calls == []


async def test_identifier_group_by_is_denied_before_the_call(settings):
    up = FakeUpstream()
    async with await connect(up, settings) as c:
        res = await c.call_tool("query_metrics", {"metrics": ["m"], "group_by": [{"name": "student"}]})
    assert res.is_error and res.content[0].text.startswith("Denied")
    assert up.calls == []


async def test_clean_call_is_forwarded(settings):
    up = FakeUpstream('[{"term": "Fall A", "n": 5}]')
    async with await connect(up, settings) as c:
        res = await c.call_tool("query_metrics", {"metrics": ["m"], "group_by": ["metric_time"]})
    assert not res.is_error
    assert json.loads(res.content[0].text) == [{"term": "Fall A", "n": 5}]
    assert up.calls == [("query_metrics", {"metrics": ["m"], "group_by": ["metric_time"]})]


async def test_identifier_columns_are_scrubbed_from_results(settings):
    up = FakeUpstream('[{"emplid": "123456789", "n": 5}]')
    async with await connect(up, settings) as c:
        res = await c.call_tool("query_metrics", {"metrics": ["m"]})
    assert "emplid" not in res.content[0].text.lower() and "123456789" not in res.content[0].text


async def test_upstream_failure_does_not_leak_exception_text(settings):
    async with await connect(FakeUpstream("boom"), settings) as c:
        res = await c.call_tool("list_metrics", {})
    assert res.is_error and "123456789" not in res.content[0].text


async def test_get_context_returns_cards(settings):
    async with await connect(FakeUpstream(), settings) as c:
        res = await c.call_tool(GET_CONTEXT, {"metric": "enrollment"})
    assert "Census lag" in res.content[0].text


async def test_prompt_and_instructions_carry_rules_and_cards(settings):
    async with await connect(FakeUpstream(), settings) as c:
        prompt = await c.get_prompt(PROMPT_NAME)
    text = prompt.messages[0].content.text
    assert "Socratic" in text and "Census lag" in text and "MCP client" in text


def test_instructions_work_without_context_cards():
    assert "Socratic" in build_instructions({"metrics": {}})


async def test_upstream_error_result_is_logged_as_error(settings, caplog):
    caplog.set_level("INFO", logger="sparky.mcp")
    async with await connect(FakeUpstream("User is not authorized", is_error=True), settings) as c:
        res = await c.call_tool("list_metrics", {})
    assert res.is_error
    assert "tool=list_metrics outcome=error" in caplog.text
