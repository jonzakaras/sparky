import asyncio

import pytest

from sparky.agent import build_options, dbt_mcp_config, is_read_only_sql, load_system_prompt
from sparky.config import Settings
from sparky.context import load_context_cards
from sparky.modes import MODES, get_mode
from sparky.tools.clarify import ClarifyBroker

PACK = {"metrics": {"m": {"context_card": {"investigations": {"known_structural_causes": ["Fall B census trails Fall A"]}}}}}


def _opts(mode, pack=None):
    return build_options(Settings(), ClarifyBroker(lambda e: asyncio.sleep(0)), get_mode(mode), pack)


def test_unknown_mode_rejected():
    with pytest.raises(ValueError):
        get_mode("arm9")


def test_arm1_has_raw_sql_only():
    m = get_mode("arm1")
    assert m.allowed_tools == ["mcp__dbt__execute_sql"]
    assert "mcp__dbt__execute_sql" not in m.disallowed_tools
    assert dbt_mcp_config(Settings(dbt_host="h"), m)["env"]["DBT_MCP_ENABLE_TOOLS"] == "execute_sql"
    assert set(_opts("arm1").mcp_servers) == {"dbt"}


def test_arm2_is_semantic_layer_without_socratic_or_context():
    m = get_mode("arm2")
    assert "mcp__dbt__query_metrics" in m.allowed_tools
    assert not any(t.startswith("mcp__sparky__") for t in m.allowed_tools)
    assert "mcp__dbt__execute_sql" in m.disallowed_tools
    assert set(_opts("arm2", PACK).mcp_servers) == {"dbt"}
    prompt = load_system_prompt(m, PACK)
    assert "Socratic" not in prompt and "Fall B" not in prompt  # never sees the pack


def test_arm3_has_clarify_cite_and_context():
    m = get_mode("arm3")
    assert "mcp__sparky__ask_clarifying_question" in m.allowed_tools
    assert "mcp__sparky__cite_context" in m.allowed_tools
    prompt = load_system_prompt(m, PACK)
    assert "Cite context used" in prompt and "Fall B census trails Fall A" in prompt
    assert "investigations.known_structural_causes" in prompt


def test_arms_1_and_2_never_load_the_context_pack_text():
    for arm in ("arm1", "arm2"):
        assert "Business context cards" not in load_system_prompt(get_mode(arm), PACK)


def test_sample_pack_loads():
    pack = load_context_cards("data/context_cards.json")
    assert "enrollment_headcount" in pack["metrics"]


def test_all_modes_have_prompt_files():
    for m in MODES.values():
        assert load_system_prompt(m).strip()


@pytest.mark.parametrize("sql,ok", [
    ("select 1", True), ("WITH a AS (select 1) select * from a;", True), ("show tables", True),
    ("select created_at, updated_at from t", True), ("-- c\nselect 2", True),
    ("drop table x", False), ("select 1; drop table x", False), ("insert into t values (1)", False),
    ("update t set a=1", False), ("", False),
])
def test_read_only_sql_guard(sql, ok):
    assert is_read_only_sql(sql) is ok


def test_comparison_rule_is_arm3_only():
    arm3 = load_system_prompt(get_mode("arm3"), PACK)
    assert "Comparison questions" in arm3 and "Suggested follow-ups" in arm3
    assert "do NOT present intermediate results" in arm3
    for arm in ("arm1", "arm2"):
        assert "Comparison questions" not in load_system_prompt(get_mode(arm), PACK)


def test_every_arm3_answer_ends_with_suggested_followups():
    arm3 = load_system_prompt(get_mode("arm3"), PACK)
    assert "Close EVERY answer with" in arm3 and arm3.count("Suggested follow-ups") >= 2
    assert "one useful follow-up question" not in arm3
    assert "Never end an answer with a question in prose" in arm3
    assert "invite correction" not in arm3
    for arm in ("arm1", "arm2"):
        assert "Suggested follow-ups" not in load_system_prompt(get_mode(arm), PACK)


def test_arm3_asks_when_metric_or_span_is_open():
    arm3 = load_system_prompt(get_mode("arm3"), PACK)
    assert "Never assume which metric when several fit" in arm3
    assert "never\n   assume what \"over time\" means" in arm3
    assert "as few questions as possible" not in arm3
    for arm in ("arm1", "arm2"):
        assert "ask_clarifying_question" not in load_system_prompt(get_mode(arm), PACK)


def test_arm3_keeps_to_its_own_numbers():
    arm3 = load_system_prompt(get_mode("arm3"), PACK)
    assert "Never mention other offices" in arm3 and "campus immersion" in arm3
