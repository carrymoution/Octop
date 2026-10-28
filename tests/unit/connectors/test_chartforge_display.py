from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest
from langchain_core.messages import ToolMessage

from octop.infra.connectors.chartforge_display import (
    TOOLS,
    ChartForgeDisplay,
    ThinkStripper,
    display_for_thread,
)

SECRET = "ADS.PRIVATE_TABLE.SECRET_FIELD"
SQL = f'SELECT "SECRET_FIELD" FROM "ADS"."PRIVATE_TABLE" /* {SECRET} */'
AUDIT = "3d57519b-e810-4df6-a7e8-facb61cec4dd"


def test_public_privacy_rules_cover_every_registered_report_tool():
    from octop.infra.connectors.chartforge import build_tools

    assert {tool.name for tool in build_tools(SimpleNamespace())} == TOOLS


@pytest.mark.parametrize("name", sorted(TOOLS))
def test_tool_details_are_allowlisted_and_originals_unchanged(name):
    view = ChartForgeDisplay(protected=True)
    call = {"type": "tool_call_chunk", "id": "c1", "name": name, "args": json.dumps({"sql": SQL})}
    result = {
        "type": "tool_result",
        "messages": [
            ToolMessage(
                content=json.dumps(
                    {
                        "queryId": AUDIT,
                        "columns": [SECRET],
                        "rows": [[SECRET]],
                        "elapsedMs": 37,
                        "truncated": True,
                    }
                ),
                tool_call_id="c1",
                name=name,
                artifact={"download": SECRET},
                additional_kwargs={"sql": SQL},
            )
        ],
    }
    before = copy.deepcopy((call, result))
    public = [*view.stream(call), *view.stream(result)]
    text = json.dumps(public)
    assert SECRET not in text and "SELECT" not in text and "columns" not in text
    assert AUDIT in text and '"row_count"' in public[1]["messages"][0]["content"]
    assert public[0]["args"] == "{}" and public[0]["details_redacted"] is True
    assert (call, result) == before


def test_unknown_name_and_fragmented_name_do_not_leak_initial_arguments():
    view = ChartForgeDisplay()
    assert view.stream({"type": "tool_call_chunk", "id": "c", "index": 0, "args": SQL}) == []
    assert (
        view.stream({"type": "tool_call_chunk", "index": 0, "name": "chartforge_", "args": SECRET})
        == []
    )
    public = view.stream(
        {"type": "tool_call_chunk", "index": 0, "name": "query_sql", "args": SECRET}
    )
    assert public[0]["name"] == "chartforge_query_sql"
    result = view.stream(
        {
            "type": "tool_result",
            "messages": [{"type": "tool", "tool_call_id": "c", "content": SECRET}],
        }
    )
    assert SECRET not in json.dumps(public + result)


def test_parallel_speakers_and_tools_do_not_borrow_classification():
    view = ChartForgeDisplay()
    for speaker, name in (("reports", "chartforge_query_sql"), ("files", "read_file")):
        view.stream(
            {
                "type": "tool_call_chunk",
                "agent_id": speaker,
                "id": "same",
                "index": 0,
                "name": name,
                "args": "{}",
            }
        )
    private = view.stream(
        {
            "type": "tool_result",
            "agent_id": "reports",
            "messages": [{"type": "tool", "tool_call_id": "same", "content": SECRET}],
        }
    )
    ordinary = view.stream(
        {
            "type": "tool_result",
            "agent_id": "files",
            "messages": [{"type": "tool", "tool_call_id": "same", "content": "file contents"}],
        }
    )
    assert SECRET not in json.dumps(private)
    assert ordinary[0]["messages"][0]["content"] == "file contents"
    call = {"type": "tool_call_chunk", "id": "other", "name": "read_file", "args": '{"path":"ok"}'}
    assert view.stream(call) == [call]


def test_legacy_wire_and_paginated_nameless_results_are_redacted():
    view = ChartForgeDisplay()
    old_page = {
        "type": "ai",
        "data": {
            "content": "",
            "tool_calls": [{"id": "old", "name": "chartforge_query_sql", "args": {"sql": SQL}}],
            "additional_kwargs": {"history_tool_args": {"0": SQL}, "reasoning_content": SECRET},
        },
    }
    view.seed(old_page)
    newest_page = [
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": SECRET},
                {"type": "text", "text": "业务结果"},
            ],
        },
        {
            "role": "tool",
            "content": [{"type": "tool_result", "id": "old", "output": SECRET, "artifact": SECRET}],
        },
    ]
    for raw in (old_page, newest_page):
        public = view.project(raw)
        assert SECRET not in json.dumps(public, ensure_ascii=False)
    assert view.project(newest_page)[0]["content"] == [{"type": "text", "text": "业务结果"}]


@pytest.mark.parametrize("cut", range(1, 15))
def test_thinking_tags_split_at_any_character_are_removed(cut):
    text = "前文<think>" + SECRET + "</think>结果"
    parser = ThinkStripper()
    assert "".join(parser.feed(text[i : i + cut]) for i in range(0, len(text), cut)) == "前文结果"


def test_reasoning_and_state_snapshots_are_filtered_but_plain_answers_are_not_rewritten():
    view = ChartForgeDisplay(protected=True)
    assert view.stream({"type": "reasoning", "content": SECRET}) == []
    frame = {
        "type": "state_snapshot",
        "data": {
            "messages": [
                {
                    "type": "ai",
                    "content": "<think>" + SECRET + "</think>结果",
                    "tool_calls": [
                        {
                            "type": "tool_call",
                            "name": "chartforge_query_sql",
                            "args": {"sql": SQL},
                            "id": "c",
                        }
                    ],
                }
            ]
        },
    }
    assert SECRET not in json.dumps(view.stream(frame))
    answer = {"type": "token", "content": "用户明确要求的 SQL 示例"}
    assert view.stream(answer) == [answer]


def test_trace_errors_context_and_raw_content_references_are_hidden():
    view = ChartForgeDisplay(protected=True)
    event = {
        "kind": "tool",
        "is_error": True,
        "agent_id": "a",
        "summary": SQL,
        "payload": {
            "name": "chartforge_query_sql",
            "args": SQL,
            "result": SECRET,
            "content_sha256": SECRET,
            "file_path": SECRET,
        },
    }
    public = view.trajectory(event)
    assert SECRET not in json.dumps(public) and "SELECT" not in json.dumps(public)
    assert public["payload"]["status"] == "failed"
    for kind in ("context", "system", "compacted"):
        assert SECRET not in json.dumps(view.trajectory({**event, "kind": kind}))


class Settings:
    def __init__(self):
        self.values = {}

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value):
        self.values[key] = value


@pytest.mark.asyncio
async def test_team_capability_is_protected_before_first_token_and_stays_after_disconnect():
    settings = Settings()
    agents = {
        "host": SimpleNamespace(config=SimpleNamespace(mcp_server_configs={})),
        "member": SimpleNamespace(config=SimpleNamespace(mcp_server_configs={"chartforge": {}})),
    }
    registry = SimpleNamespace(
        get_agent=agents.__getitem__,
        teams=SimpleNamespace(member_ids=lambda aid: ["member"] if aid == "host" else []),
    )
    services = SimpleNamespace(settings_repo=settings)
    args = {
        "services": services,
        "registry": registry,
        "archive": None,
        "trajectory": None,
        "agent_id": "host",
        "thread_id": "t",
    }
    first = await display_for_thread(**args)
    assert first.stream({"type": "reasoning", "content": SECRET}) == []
    agents["member"].config.mcp_server_configs = {}
    second = await display_for_thread(**args)
    assert second.protected is True
    assert second.stream({"type": "reasoning", "content": SECRET}) == []


def test_normal_conversation_and_tool_output_stay_identical():
    view = ChartForgeDisplay()
    for frame in (
        {"type": "reasoning", "content": "planning"},
        {"type": "token", "content": "hello"},
        {
            "type": "tool_result",
            "node": "tools",
            "messages": [
                {"type": "tool", "name": "read_file", "tool_call_id": "x", "content": "hello"}
            ],
        },
    ):
        assert view.stream(frame) == [frame]


def test_orphan_results_in_a_protected_history_fail_closed():
    view = ChartForgeDisplay(protected=True)
    for value in (
        {"type": "tool_result", "id": "missing", "output": SECRET, "artifact": SECRET},
        {"type": "tool", "name": "tool", "tool_call_id": "missing", "content": SECRET},
    ):
        assert SECRET not in json.dumps(view.project(value))


def test_openai_function_arguments_and_raw_fragments_are_not_an_export_bypass():
    view = ChartForgeDisplay(protected=True)
    value = {
        "type": "ai",
        "data": {
            "content": "",
            "additional_kwargs": {
                "history_tool_args": {"0": SQL},
                "tool_calls": [
                    {
                        "id": "c",
                        "type": "function",
                        "function": {
                            "name": "chartforge_query_sql",
                            "arguments": json.dumps({"sql": SQL}),
                        },
                    }
                ],
            },
        },
    }
    assert SECRET not in json.dumps(view.project(value))


def test_full_name_after_partial_name_and_parallel_ids_without_indices():
    view = ChartForgeDisplay()
    assert not view.stream(
        {"type": "tool_call_chunk", "id": "sql", "name": "chartforge_", "args": SQL}
    )
    public = view.stream(
        {"type": "tool_call_chunk", "id": "sql", "name": "chartforge_query_sql", "args": SQL}
    )
    assert SECRET not in json.dumps(public) and public[0]["details_redacted"]
    ordinary = {
        "type": "tool_call_chunk",
        "id": "file",
        "name": "read_file",
        "args": '{"path":"ok"}',
    }
    assert view.stream(ordinary) == [ordinary]
    results = view.stream(
        {
            "type": "tool_result",
            "messages": [
                {"type": "tool", "tool_call_id": "sql", "content": SECRET},
                {"type": "tool", "tool_call_id": "file", "content": "file content"},
            ],
        }
    )
    assert len(results) == 2
    assert SECRET not in json.dumps(results)
    assert results[1]["messages"][0]["content"] == "file content"


def test_other_team_member_done_cannot_reset_an_open_thinking_block():
    view = ChartForgeDisplay(protected=True)
    assert view.stream({"type": "token", "agent": "reports", "content": "<think>"}) == []
    view.stream({"type": "done", "agent_id": "files"})
    public = view.stream(
        {"type": "token", "agent_id": "reports", "content": SECRET + "</think>业务结果"}
    )
    assert public[0]["content"] == "业务结果"
    assert SECRET not in json.dumps(public)


def test_thinking_tags_split_across_archived_text_blocks_are_hidden():
    view = ChartForgeDisplay(protected=True)
    value = {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "<thi"},
            {"type": "text", "text": "nk>" + SECRET},
            {"type": "text", "text": "</think>业务结果"},
        ],
    }
    public = view.project(value)
    assert SECRET not in json.dumps(public)
    assert public["content"][-1]["text"] == "业务结果"


@pytest.mark.parametrize("protected", [False, True])
def test_reconnected_turn_error_is_redacted_only_in_report_conversations(protected):
    view = ChartForgeDisplay(protected=protected)
    error = {"type": "error", "message": "Request failed: " + SQL}
    public = view.stream(error)
    if protected:
        assert SECRET not in json.dumps(public)
    else:
        assert public == [error]
