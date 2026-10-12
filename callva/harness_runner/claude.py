"""Claude Code through claude-agent-sdk: the options a profile maps to, and what came back."""

from __future__ import annotations

import contextlib
import dataclasses
import json
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, SystemMessage, query

from .profile import Profile
from .result import Tokens

# A stream line longer than this ends the turn in the SDK; a tool result that
# carries a large file is one line, so the SDK's 1 MB default is too tight.
MAX_LINE_BYTES = 64 * 1024 * 1024


def options(
    profile: Profile,
    *,
    cli_path: str,
    cwd: str,
    env: dict[str, str],
    session_id: str | None,
    resume_id: str | None,
    stderr: Callable[[str], None],
) -> ClaudeAgentOptions:
    """The SDK options for one turn. The system prompt is the claude_code preset unless the
    profile replaces it."""
    system_prompt: dict[str, Any] | str = {"type": "preset", "preset": "claude_code"}
    if profile.system_prompt is not None:
        system_prompt = profile.system_prompt
    elif profile.append_system_prompt:
        system_prompt["append"] = profile.append_system_prompt
    extra: dict[str, str | None] = dict(profile.claude_extra_args)
    if profile.name:
        extra["name"] = profile.name
    if not profile.persist_session:
        extra["no-session-persistence"] = None
    settings = profile.settings
    if isinstance(settings, dict):
        settings = json.dumps(settings)
    mcp = profile.mcp_config
    if isinstance(mcp, dict):
        mcp = json.dumps(mcp)
    return ClaudeAgentOptions(
        cli_path=cli_path,
        cwd=cwd,
        system_prompt=system_prompt,
        model=profile.model,
        effort=profile.effort,
        thinking=(None if profile.thinking is None
                  else {"type": "adaptive"} if profile.thinking else {"type": "disabled"}),
        max_turns=profile.max_turns,
        max_budget_usd=profile.budget_usd,
        tools=list(profile.tools) if profile.tools is not None else None,
        allowed_tools=list(profile.allowed_tools),
        disallowed_tools=list(profile.disallowed_tools),
        permission_mode=profile.permission_mode,
        setting_sources=(list(profile.setting_sources)
                         if profile.setting_sources is not None else None),
        settings=settings,
        mcp_servers=mcp if mcp is not None else {},
        strict_mcp_config=profile.strict_mcp,
        add_dirs=list(profile.add_dirs),
        env=env,
        extra_args=extra,
        session_id=session_id,
        resume=resume_id,
        output_format=({"type": "json_schema", "schema": dict(profile.output_schema)}
                       if profile.output_schema else None),
        stderr=stderr,
        max_buffer_size=MAX_LINE_BYTES,
    )


def event_dict(message: Any) -> dict:
    """One SDK message as a plain dict for `on_event`: a system message is the
    harness's own event; any other message is its fields plus `type`, the SDK's class name."""
    if isinstance(message, SystemMessage) and isinstance(message.data, dict):
        return dict(message.data)
    if dataclasses.is_dataclass(message):
        data = dataclasses.asdict(message)
        data["type"] = type(message).__name__
        return data
    return {"type": type(message).__name__, "value": repr(message)}


@dataclass
class Outcome:
    """Everything one claude turn produced, as the SDK handed it over."""

    result: ResultMessage | None = None
    init: dict | None = None
    last_retry: dict | None = None
    error: BaseException | None = None
    stderr: deque = field(default_factory=lambda: deque(maxlen=200))


async def turn(prompt: str, opts: ClaudeAgentOptions, outcome: Outcome,
               on_event: Callable[[dict], None] | None) -> None:
    try:
        async for message in query(prompt=prompt, options=opts):
            if isinstance(message, SystemMessage):
                if message.subtype == "init":
                    outcome.init = dict(message.data)
                elif message.subtype == "api_retry":
                    outcome.last_retry = dict(message.data)
            elif isinstance(message, ResultMessage):
                outcome.result = message
            if on_event is not None:
                # A caller's renderer never ends the turn.
                with contextlib.suppress(Exception):
                    on_event(event_dict(message))
    except BaseException as exc:  # every ending is read, none is raised
        # The SDK raises after yielding an error result; the result is the report.
        outcome.error = exc


def _model_from_usage(usage: Any) -> str | None:
    if not isinstance(usage, dict) or not usage:
        return None
    best, best_tokens = None, -1
    for model, counts in usage.items():
        tokens = counts.get("outputTokens", 0) if isinstance(counts, dict) else 0
        if tokens > best_tokens:
            best, best_tokens = model, tokens
    return best


def read(message: ResultMessage) -> dict[str, Any]:
    """The fields of a result message the Result carries."""
    usage = message.usage if isinstance(message.usage, dict) else {}
    answer = message.result
    if answer is None and message.errors:
        answer = "; ".join(str(e) for e in message.errors)
    return {
        "answer": (answer if isinstance(answer, str)
                   else "" if answer is None else json.dumps(answer)),
        "structured": message.structured_output,
        "session_id": message.session_id,
        "cost_usd": message.total_cost_usd,
        "duration_ms": message.duration_ms,
        "num_turns": message.num_turns,
        "tokens": Tokens(
            input=usage.get("input_tokens"),
            output=usage.get("output_tokens"),
            cache_read=usage.get("cache_read_input_tokens"),
            cache_creation=usage.get("cache_creation_input_tokens"),
        ),
        "model": _model_from_usage(message.model_usage),
        "permission_denials": tuple(message.permission_denials or ()),
        "subtype": message.subtype,
        "is_error": bool(message.is_error),
        "api_error_status": message.api_error_status,
    }


def raw(message: ResultMessage | None) -> dict:
    return dataclasses.asdict(message) if message is not None else {}
