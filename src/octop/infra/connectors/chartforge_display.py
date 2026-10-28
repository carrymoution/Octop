"""Public projections of ChartForge activity. Never apply these to model state.

The model still sees SQL and metadata. This prevents accidental disclosure in
tool/trace UI, not deliberate disclosure in an assistant's final answer.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from octop.i18n import tr
from octop.infra.errors import OctopError

TOOLS = frozenset(
    f"chartforge_{name}"
    for name in (
        "list_connections",
        "list_schemas",
        "list_objects",
        "describe_object",
        "query_sql",
        "list_saved_queries",
        "run_saved_query",
    )
)
_SETTING = "chartforge.display.private."
_REASONING = frozenset({"thinking", "reasoning", "reasoning_content", "history_tool_args"})
_FRAME_KEYS = frozenset({"type", "id", "index", "node", "agent_id", "thread_id", "turn_id"})
_TAGS = ("<think>", "</think>")


def _object(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump()
    return value


class ThinkStripper:
    """Incremental tag removal, including tags split between network frames."""

    def __init__(self) -> None:
        self.pending = ""
        self.hidden = False

    def feed(self, text: str) -> str:
        remaining = self.pending + text
        self.pending = ""
        output = ""
        while remaining:
            match = re.search(r"</?think>", remaining, re.IGNORECASE)
            if match:
                if not self.hidden:
                    output += remaining[: match.start()]
                self.hidden = match.group().lower() == "<think>"
                remaining = remaining[match.end() :]
                continue
            count = 0
            for size in range(1, min(len(remaining), len("</think>")) + 1):
                if any(tag.startswith(remaining[-size:].lower()) for tag in _TAGS):
                    count = size
            if count:
                self.pending = remaining[-count:]
                remaining = remaining[:-count]
            if not self.hidden:
                output += remaining
            break
        return output


def strip_thinking(text: str) -> str:
    return ThinkStripper().feed(text)


@dataclass
class _Call:
    name: str = ""
    call_id: str = ""
    buffered: list[dict[str, Any]] = field(default_factory=list)
    announced: bool = False


class ChartForgeDisplay:
    def __init__(
        self,
        *,
        protected: bool = False,
        locale: str = "en",
        mark: Any = None,
        root_agent_id: str = "",
    ):
        self.protected = protected
        self.locale = locale
        self._mark = mark
        self._needs_mark = False
        self._names: dict[tuple[str, str], str] = {}
        self._calls: dict[tuple[str, str], _Call] = {}
        self._thinking: dict[tuple[str, str], ThinkStripper] = {}
        self._capabilities: Any = None
        self.root_agent_id = root_agent_id

    def _speaker(self, value: str) -> str:
        return "" if value == self.root_agent_id else value

    def _owner(self, value: dict[str, Any], fallback: str = "") -> str:
        extra = value.get("additional_kwargs") or {}
        if not isinstance(extra, dict):
            extra = {}
        alias = value.get("agent")
        alias = alias if isinstance(alias, str) and alias != "agent" else ""
        return self._speaker(
            str(value.get("agent_id") or extra.get("speaker_agent_id") or alias or fallback)
        )

    async def refresh(self) -> None:
        if (
            not self.protected
            and self._capabilities is not None
            and await asyncio.to_thread(self._capabilities)
        ):
            self.protect()

    async def persist(self) -> None:
        if self._needs_mark and self._mark is not None:
            await asyncio.to_thread(self._mark)
            self._needs_mark = False

    def protect(self) -> None:
        if not self.protected:
            self.protected = True
            self._needs_mark = True

    def _remember(self, name: str, call_id: str, speaker: str) -> None:
        speaker = self._speaker(speaker)
        if (
            name
            and name != "tool"
            and call_id
            and (self._names.get((speaker, call_id)) not in TOOLS or name in TOOLS)
        ):
            self._names[speaker, call_id] = name
        if name in TOOLS:
            self.protect()

    def _name(self, value: dict[str, Any], speaker: str) -> str:
        name = value.get("name")
        call_id = str(value.get("tool_call_id") or value.get("call_id") or value.get("id") or "")
        known = self._names.get((self._speaker(speaker), call_id), "")
        if known in TOOLS:
            return known
        if isinstance(name, str) and name and name != "tool":
            self._remember(name, call_id, speaker)
            return name
        return self._names.get((self._speaker(speaker), call_id), "")

    def seed(self, value: Any, speaker: str = "") -> None:
        """Index the whole history before projecting any page or nameless result."""
        value = _object(value)
        if isinstance(value, list):
            for item in value:
                self.seed(item, speaker)
        elif isinstance(value, dict):
            speaker = self._owner(value, speaker)
            name = value.get("name")
            call_id = str(
                value.get("tool_call_id") or value.get("call_id") or value.get("id") or ""
            )
            if isinstance(name, str):
                self._remember(name, call_id, speaker)
            for item in value.values():
                if isinstance(item, (list, dict)):
                    self.seed(item, speaker)

    def summary(
        self, value: Any = None, *, error: bool = False, running: bool = False
    ) -> dict[str, Any]:
        """Allowlist metrics; never forward arbitrary strings, columns or errors."""
        data = _object(value)
        for _ in range(4):
            if isinstance(data, str):
                try:
                    data = json.loads(data)
                except (ValueError, TypeError):
                    data = None
            if not isinstance(data, dict):
                break
            error = error or data.get("isError") is True
            content = data.get("content")
            if isinstance(content, list) and content and isinstance(content[0], dict):
                data = content[0].get("text")
            else:
                break
        state = "running" if running else "failed" if error else "completed"
        result: dict[str, Any] = {
            "details_redacted": True,
            "status": state,
            "summary": tr(f"chartforge.display.{state}", self.locale),
        }
        if isinstance(data, dict):
            for source, target in (("elapsedMs", "elapsed_ms"), ("elapsed_ms", "elapsed_ms")):
                number = data.get(source)
                if (
                    isinstance(number, (int, float))
                    and not isinstance(number, bool)
                    and number >= 0
                    and math.isfinite(number)
                ):
                    result[target] = number
            rows = data.get("rows")
            if isinstance(rows, list):
                result["row_count"] = len(rows)
            if isinstance(data.get("truncated"), bool):
                result["truncated"] = data["truncated"]
            query_id = data.get("queryId") or data.get("query_id")
            if isinstance(query_id, str):
                with suppress(ValueError):
                    result["query_id"] = str(UUID(query_id))
        return result

    def _private_message(
        self, value: dict[str, Any], name: str, *, block: bool = False
    ) -> dict[str, Any]:
        error = value.get("status") == "error" or bool(value.get("error_code"))
        content = value.get("output", value.get("content", value.get("result")))
        summary = self.summary(content, error=error)
        error = summary["status"] == "failed"
        result = {k: value[k] for k in ("id", "tool_call_id") if k in value}
        result.update(name=name, details_redacted=True)
        if block:
            result.update(type="tool_result", output=json.dumps(summary, ensure_ascii=False))
            if error:
                result["error_code"] = "tool_error"
        else:
            result.update(
                type="tool",
                content=json.dumps(summary, ensure_ascii=False),
                status="error" if error else "success",
            )
        return result

    def project(self, value: Any, speaker: str = "") -> Any:
        """Copy raw LangChain wires, UI blocks, state snapshots or export payloads."""
        value = _object(value)
        if isinstance(value, list):
            return [
                self.project(item, speaker)
                for item in value
                if not (
                    self.protected
                    and isinstance(item, dict)
                    and isinstance(item.get("type"), str)
                    and item.get("type") in _REASONING
                )
            ]
        if not isinstance(value, dict):
            return value
        speaker = self._owner(value, speaker)
        kind = value.get("type") or value.get("role")
        # LangChain archive wire: {type: ai/tool, data: {...}}.
        if kind in ("ai", "human", "tool", "system") and isinstance(value.get("data"), dict):
            return {**value, "data": self.project({**value["data"], "type": kind}, speaker)}
        name = self._name(value, speaker)
        is_result = value.get("type") in ("tool", "tool_result") or (
            kind == "tool" and not isinstance(value.get("content"), list)
        )
        if is_result and (name in TOOLS or (self.protected and not name)):
            return self._private_message(value, name or "tool", block=kind == "tool_result")
        if name in TOOLS and (
            kind in ("tool_call", "tool_use")
            or any(k in value for k in ("args", "input", "arguments"))
        ):
            result = {k: value[k] for k in ("type", "id", "name") if k in value}
            result[
                "input" if "input" in value else "arguments" if "arguments" in value else "args"
            ] = "{}" if "arguments" in value else {}
            result["details_redacted"] = True
            return result
        out: dict[str, Any] = {}
        for key, item in value.items():
            if self.protected and key in _REASONING:
                continue
            if (
                key == "content"
                and isinstance(item, list)
                and kind in ("ai", "assistant")
                and self.protected
            ):
                # Providers may split a text tag across adjacent content blocks,
                # even after the streamed message has been archived.
                parser = ThinkStripper()
                blocks = []
                for block in item:
                    if isinstance(block, dict) and block.get("type") in ("thinking", "reasoning"):
                        continue
                    if (
                        isinstance(block, dict)
                        and block.get("type") == "text"
                        and isinstance(block.get("text"), str)
                    ):
                        blocks.append(
                            self.project({**block, "text": parser.feed(block["text"])}, speaker)
                        )
                    elif isinstance(block, str):
                        blocks.append(parser.feed(block))
                    else:
                        blocks.append(self.project(block, speaker))
                out[key] = blocks
                continue
            if (
                key == "content"
                and isinstance(item, str)
                and kind in ("ai", "assistant")
                and self.protected
                or key in ("text", "content")
                and kind == "text"
                and self.protected
                and isinstance(item, str)
            ):
                out[key] = strip_thinking(item)
            else:
                out[key] = self.project(item, speaker) if isinstance(item, (list, dict)) else item
        return out

    def trajectory(self, value: dict[str, Any]) -> dict[str, Any]:
        speaker = str(value.get("agent_id") or "")
        payload = value.get("payload") or {}
        name = self._name(payload, speaker)
        if value.get("kind") == "tool" and name in TOOLS:
            summary = self.summary(
                payload.get("result"),
                error=bool(value.get("is_error")),
                running="result" not in payload,
            )
            safe = {"name": name, "call_id": payload.get("call_id"), **summary}
            duration = payload.get("tool_duration_ms")
            if (
                isinstance(duration, (int, float))
                and not isinstance(duration, bool)
                and math.isfinite(duration)
                and duration >= 0
            ):
                safe["tool_duration_ms"] = duration
            if isinstance(payload.get("parent_event_id"), str):
                safe["parent_event_id"] = payload["parent_event_id"]
            return {**value, "summary": summary["summary"], "payload": safe}
        out: dict[str, Any] = self.project(value, speaker)
        if self.protected:
            if value.get("kind") in ("context", "system", "compacted"):
                out["summary"] = tr("chartforge.display.context", self.locale)
                out["payload"] = {"details_redacted": True}
            elif value.get("kind") == "assistant":
                out["summary"] = strip_thinking(str(value.get("summary") or ""))
                for key in ("content", "text"):
                    if isinstance(out["payload"].get(key), str):
                        out["payload"][key] = strip_thinking(out["payload"][key])
        return out

    def stream(self, frame: dict[str, Any]) -> list[dict[str, Any]]:
        """Filter before network serialization; may buffer unresolved names."""
        kind = frame.get("type")
        speaker = self._owner(frame)
        if speaker and not frame.get("agent_id"):
            frame = {**frame, "agent_id": speaker}
        if kind == "tool_call_chunk":
            key = (speaker, str(frame.get("id") or f"idx:{frame.get('index', 0)}"))
            index_key = (speaker, f"idx:{frame.get('index', 0)}")
            explicit_id = str(frame.get("id") or "")
            state = self._calls.get(key)
            if state is None:
                candidate = self._calls.get(index_key)
                state = (
                    candidate
                    if candidate
                    and (
                        not explicit_id or not candidate.call_id or candidate.call_id == explicit_id
                    )
                    else _Call()
                )
            if explicit_id:
                state.call_id = explicit_id
            self._calls[key] = self._calls[index_key] = state
            fragment = str(frame.get("name") or "")
            if fragment and state.name not in TOOLS:
                if fragment in TOOLS or fragment.startswith(state.name):
                    state.name = fragment
                elif not state.name.startswith(fragment):
                    state.name += fragment
            state.buffered.append(dict(frame))
            self._remember(state.name, str(frame.get("id") or ""), speaker)
            if not state.name or (
                state.name not in TOOLS and any(n.startswith(state.name) for n in TOOLS)
            ):
                return []
            pending, state.buffered = state.buffered, []
            for (owner, call_id), call in self._calls.items():
                if call is state and not call_id.startswith("idx:"):
                    self._remember(state.name, call_id, owner)
            if state.name not in TOOLS:
                return pending
            safe = {key: frame[key] for key in _FRAME_KEYS if key in frame}
            safe.update(
                name=state.name, details_redacted=True, args="" if state.announced else "{}"
            )
            state.announced = True
            return [safe]
        if kind == "tool_result":
            self.seed(frame, speaker)
            if not frame.get("messages"):
                name = self._name(frame, speaker)
                if name not in TOOLS:
                    return [self.project(frame, speaker)]
                frame = {
                    **frame,
                    "messages": [
                        {
                            "type": "tool",
                            "name": name,
                            "tool_call_id": str(frame.get("id") or ""),
                            "content": frame.get("content"),
                        }
                    ],
                }
            messages = []
            for raw in frame.get("messages", []):
                message = _object(raw)
                name = self._name(message, speaker) if isinstance(message, dict) else ""
                if not name and self.protected:
                    # A reconnect can start at a nameless result. Do not leak it.
                    messages.append(
                        self._private_message(message if isinstance(message, dict) else {}, "tool")
                    )
                else:
                    messages.append(self.project(message, speaker))
                if isinstance(message, dict):
                    call_id = str(message.get("tool_call_id") or "")
                    removed_state = self._calls.pop((speaker, call_id), None)
                    if removed_state is not None:
                        self._calls = {
                            k: s for k, s in self._calls.items() if s is not removed_state
                        }
            private = any(
                isinstance(item, dict) and item.get("details_redacted") for item in messages
            )
            base = (
                {key: frame[key] for key in _FRAME_KEYS if key in frame} if private else dict(frame)
            )
            if private and len(messages) > 1:
                return [{**base, "messages": [item]} for item in messages]
            return [{**base, "messages": messages}]
        if self.protected and kind in ("reasoning", "thinking"):
            return []
        if self.protected and kind == "token" and isinstance(frame.get("content"), str):
            key = (speaker, str(frame.get("turn_id") or ""))
            content = self._thinking.setdefault(key, ThinkStripper()).feed(frame["content"])
            return [self.project({**frame, "content": content}, speaker)] if content else []
        # A resumed/reconnected turn can fail before a new tool frame arrives;
        # provider errors may embed the old SQL request from model context.
        if kind == "error" and self.protected:
            return [
                {
                    **{key: frame[key] for key in _FRAME_KEYS if key in frame},
                    "message": tr("chartforge.display.failed", self.locale),
                }
            ]
        if kind in ("done", "turn.started"):
            self._thinking = {
                key: parser for key, parser in self._thinking.items() if key[0] != speaker
            }
            self._calls = {key: call for key, call in self._calls.items() if key[0] != speaker}
        return [self.project(frame, speaker)]


async def display_for_thread(
    *,
    services: Any,
    registry: Any,
    archive: Any,
    trajectory: Any,
    agent_id: str,
    thread_id: str,
    locale: str = "en",
) -> ChartForgeDisplay:
    """Resolve capability and seed all persisted calls, independent of pagination."""
    from octop.infra.history.service import HistoryArchive

    settings = services.settings_repo
    setting = _SETTING + thread_id
    protected = bool(await asyncio.to_thread(settings.get, setting))
    display = ChartForgeDisplay(
        protected=protected,
        locale=locale,
        mark=lambda: settings.set(setting, "1"),
        root_agent_id=agent_id,
    )

    def capabilities() -> bool:
        pending, seen = [agent_id], set()
        while pending:
            aid = pending.pop()
            if aid in seen:
                continue
            seen.add(aid)
            try:
                agent = registry.get_agent(aid)
            except (KeyError, ValueError, RuntimeError, OctopError):
                agent = None
            if agent is not None:
                configs = getattr(getattr(agent, "config", None), "mcp_server_configs", {})
                names = getattr(agent, "_mcp_tool_name_set", ())
                if "chartforge" in (configs or {}) or any(name in TOOLS for name in names):
                    return True
            teams = getattr(registry, "teams", None)
            if teams is not None:
                pending.extend(teams.member_ids(aid))
        return False

    if registry is not None:
        display._capabilities = capabilities
        await display.refresh()
    # Seed in chronological order so identical ids in different speakers never
    # borrow the other expert's classification. Raw records remain untouched.
    if isinstance(archive, HistoryArchive):
        from octop.infra.history.reader import export_messages

        try:
            display.seed(await export_messages(archive, registry, agent_id, thread_id))
        except OctopError as error:
            # A stopped legacy expert can still have readable trajectory rows.
            # Its unavailable checkpoint must not break those public endpoints.
            from octop.infra.errors import ErrorCode

            if error.code not in (
                ErrorCode.AGENT_NOT_RUNNING,
                ErrorCode.AGENT_NOT_FOUND,
                ErrorCode.AGENT_FAILED,
            ):
                raise
    else:
        messages = getattr(services, "thread_message_repo", None)
        if messages is not None:
            offset = 0
            while True:
                rows, more = await asyncio.to_thread(
                    messages.page, thread_id, limit=1000, offset=offset
                )
                display.seed([json.loads(row.message_json) for row in rows])
                if not more:
                    break
                offset += len(rows)
    if trajectory is not None:
        lines = await asyncio.to_thread(lambda: list(trajectory.export_jsonl(thread_id)))
        for line in lines:
            display.seed(json.loads(line))
    await display.persist()
    return display
