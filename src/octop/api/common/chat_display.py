"""Bind the public chat projection to the current server repositories."""

from __future__ import annotations

from typing import Any

from octop.infra.connectors.chartforge_display import ChartForgeDisplay, display_for_thread


async def chat_display(
    server: Any, agent_id: str, thread_id: str, locale: str = "en"
) -> ChartForgeDisplay:
    runtime = server.app_runtime
    return await display_for_thread(
        services=server.services,
        registry=runtime.agent_registry,
        archive=getattr(runtime, "history_archive", None),
        trajectory=getattr(runtime, "trajectory_service", None),
        agent_id=agent_id,
        thread_id=thread_id,
        locale=locale,
    )
