"""Real browser transports and history APIs must never return raw report details."""

from __future__ import annotations

import asyncio
import copy
import json

import pytest
from langchain_core.messages import ToolMessage

from octop.infra.history.reader import export_messages
from tests.integration.test_trajectory_api import _open_asgi_sse
from tests.support.auth import auth_header
from tests.support.http import chat_ws_path, ws_connect

SECRET = "ADS.PRIVATE_TABLE.SECRET_FIELD"
SQL = f"SELECT SECRET_FIELD FROM ADS.PRIVATE_TABLE /* {SECRET} */"


@pytest.fixture(autouse=True)
def enable_versioned_history(monkeypatch):
    monkeypatch.setenv("OCTOP_HISTORY_V2_ENABLED", "true")
    monkeypatch.setenv("OCTOP_ENABLE_API_DOCS", "true")


def _chunks():
    return [
        {"type": "reasoning", "content": SECRET},
        {"type": "token", "content": "<thi"},
        {"type": "token", "content": "nk>" + SECRET + "</think>"},
        {
            "type": "tool_call_chunk",
            "id": "report-call",
            "index": 0,
            "name": "chartforge_query_sql",
            "args": json.dumps({"sql": SQL}),
        },
        {
            "type": "tool_result",
            "messages": [
                ToolMessage(
                    content=json.dumps({"columns": [SECRET], "rows": [[1]], "elapsedMs": 15}),
                    tool_call_id="report-call",
                    name="chartforge_query_sql",
                )
            ],
        },
        {"type": "token", "content": "业务结果正常"},
    ]


async def test_websocket_history_exports_and_admin_impersonation_are_redacted(env_alice_bob_agent):
    client, server, alice, _bob, aid = env_alice_bob_agent
    runtime = server.app_runtime
    runtime.history_archive.enabled = True
    fake = runtime.agent_registry.get_agent(aid)
    fake._mcp_tool_name_set = frozenset({"chartforge_query_sql"})
    fake.chunks = _chunks()
    originals = copy.deepcopy(fake.chunks)
    response = await client.post(f"/api/agents/{aid}/threads", headers=alice)
    tid = response.json()["thread_id"]
    async with ws_connect(client._octop_app, chat_ws_path(aid, alice)) as ws:
        await ws.send_json({"type": "user_turn", "thread_id": tid, "text": "查询业务报表"})
        frames = await ws.drain_turn()
    blob = json.dumps(frames, ensure_ascii=False)
    assert SECRET not in blob and "SELECT" not in blob, blob
    assert "业务结果正常" in blob and "details_redacted" in blob
    assert fake.chunks == originals
    fake._mcp_tool_name_set = frozenset()
    fake.config.mcp_server_configs = {}
    root = f"/api/agents/{aid}/threads/{tid}"
    owner_id = runtime.gateway.thread_registry.get_thread(tid).user_id
    admin = await auth_header(client)
    for headers, params in ((alice, {}), (admin, {"as_user": owner_id})):
        for path, extra in (
            ("/history", {"limit": 1}),
            ("/history", {"limit": 1, "offset": 1}),
            ("/history/export", {}),
            ("/trajectory", {}),
            ("/trajectory/export", {"format": "json"}),
            ("/trajectory/export", {"format": "jsonl"}),
        ):
            result = await client.get(root + path, headers=headers, params={**params, **extra})
            assert result.status_code == 200, result.text
            assert SECRET not in result.text and "SELECT" not in result.text, result.text
        ledger = (await client.get(root + "/trajectory", headers=headers, params=params)).json()
        tools = [event for event in ledger["events"] if event["kind"] == "tool"]
        assert tools and all(event["payload"].get("details_redacted") for event in tools)
        detail = await client.get(
            root + "/trajectory/events/" + tools[0]["event_id"], headers=headers, params=params
        )
        assert detail.status_code == 200 and SECRET not in detail.text
    # Raw archive/checkpoint data is still usable by the model and internal audit.
    raw = await export_messages(runtime.history_archive, runtime.agent_registry, aid, tid)
    assert SECRET in json.dumps(raw) and "SELECT" in json.dumps(raw)
    async with _open_asgi_sse(
        client._octop_app, root + "/trajectory/stream", alice, {"after_seq": 0}
    ) as stream:
        replay = ""
        live_sent = False
        async with asyncio.timeout(20):
            async for frame in stream.aiter_text():
                replay += frame
                if "event: metrics" in replay and not live_sent:
                    live_sent = True
                    service = runtime.trajectory_service
                    service.observe_chunk(
                        aid,
                        tid,
                        {
                            "type": "tool_call_chunk",
                            "id": "live",
                            "name": "chartforge_query_sql",
                            "args": {"sql": SQL},
                        },
                    )
                    service.observe_chunk(
                        aid,
                        tid,
                        {
                            "type": "tool_result",
                            "id": "live",
                            "name": "chartforge_query_sql",
                            "content": json.dumps(
                                {"isError": True, "content": [{"type": "text", "text": SECRET}]}
                            ),
                        },
                    )
                    service.finish_turn(tid)
                if '"call_id": "live"' in replay and '"status": "failed"' in replay:
                    break
    assert SECRET not in replay and "SELECT" not in replay
    assert "details_redacted" in replay
    schema = await client.get("/api/openapi.json", headers=admin)
    assert schema.status_code == 200
    docs = await client.get("/api/docs", headers=admin)
    assert docs.status_code == 200 and "/api/openapi.json" in docs.text
    assert (
        "details_redacted"
        in schema.json()["components"]["schemas"]["TrajectoryEventOut"]["properties"]["payload"][
            "description"
        ]
    )


async def test_reconnect_during_tool_execution_hides_nameless_result(env_with_agent):
    client, server, auth, aid = env_with_agent
    server.app_runtime.history_archive.enabled = True
    fake = server.app_runtime.agent_registry.get_agent(aid)
    fake._mcp_tool_name_set = frozenset({"chartforge_query_sql"})
    gate = asyncio.Event()

    async def slow(request):
        yield {"type": "reasoning", "content": SECRET}
        yield {
            "type": "tool_call_chunk",
            "id": "slow",
            "name": "chartforge_query_sql",
            "args": json.dumps({"sql": SQL}),
        }
        await gate.wait()
        yield {
            "type": "tool_result",
            "messages": [ToolMessage(content=SECRET, tool_call_id="slow")],
        }
        yield {"type": "token", "content": "完成"}

    fake.stream = slow
    tid = (await client.post(f"/api/agents/{aid}/threads", headers=auth)).json()["thread_id"]
    async with ws_connect(client._octop_app, chat_ws_path(aid, auth)) as first:
        await first.send_json({"type": "user_turn", "thread_id": tid, "text": "查询"})
        while True:
            frame = await first.receive_json()
            assert SECRET not in json.dumps(frame)
            if frame.get("type") == "tool_call_chunk":
                break
    async with ws_connect(client._octop_app, chat_ws_path(aid, auth)) as second:
        await second.send_json({"type": "subscribe", "thread_id": tid})
        await second.receive_json()
        gate.set()
        frames = await second.drain_turn()
    assert SECRET not in json.dumps(frames)
    assert any(frame.get("type") == "tool_result" for frame in frames)
