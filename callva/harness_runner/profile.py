"""How a turn is run, and which conversation it belongs to.

A profile is a flat record of explicit knobs. There is no inheritance, no
preset and no fence: every behaviour a turn has is named by one field here, and
a field that one harness cannot honour is refused for that harness when the
profile is built, never ignored while it runs.
"""

from __future__ import annotations

import fnmatch
import json
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from importlib import resources
from typing import Any

from .renamed import refuse_attribute, refuse_module_attribute

HARNESSES = ("claude", "codex")

# Variables a running Claude Code session sets in the environment of everything
# it starts. A child harness that inherits them believes it runs inside that
# session: it may reuse its effort, its session id or its messaging socket.
SESSION_MARKERS = (
    "CLAUDECODE",
    "CLAUDECODE_*",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_SESSION_ATTENDED",
    "CLAUDE_CODE_EXECPATH",
    "CLAUDE_CODE_MESSAGING_*",
    "CLAUDE_CODE_SSE_PORT",
    "CLAUDE_CODE_SDK_READS_SESSION_STATE",
    "CLAUDE_CODE_ENABLE_SDK_FILE_CHECKPOINTING",
    "CLAUDE_AGENT_SDK_VERSION",
    "CLAUDE_PID",
    "CLAUDE_EFFORT",
    "AI_AGENT",
)

# Keys that no longer exist, and what replaces each.
REMOVED_KEYS = {
    "engine": "'engine' was renamed 'harness' in 0.3.0",
    "fence": (
        "'fence' was removed in 0.2.0: a profile names every behaviour explicitly. "
        "On claude use tools, allowed_tools, disallowed_tools, permission_mode, "
        "setting_sources, settings (sandbox and permissions), mcp_config and strict_mcp; "
        "on codex use codex_config (permissions and sandbox) and approval_policy"
    ),
    "allow_tools": "'allow_tools' was removed in 0.2.0: use tools and allowed_tools (claude)",
    "env": "'env' was removed in 0.2.0: use env_set, env_remove and strip_session_markers",
    "extra_args": "'extra_args' was removed in 0.2.0: use claude_extra_args or codex_extra_args",
}


class ProfileError(ValueError):
    """A profile that cannot be run; the message names the knob and why."""


def load_profile_schema() -> dict:
    with resources.files(__package__).joinpath("profile.schema.json").open("rb") as fh:
        return json.load(fh)


PROFILE_SCHEMA: dict = load_profile_schema()


@dataclass(frozen=True)
class Profile:
    """How one harness runs one turn. Every field is a knob; `None`, an empty
    collection or `False` leaves the harness's own default in place."""

    harness: str
    """Which harness runs the turn: `claude` (Claude Code through claude-agent-sdk) or `codex`
    (Codex through openai-codex)."""

    cli_path: str | None = None
    """The harness CLI to run. `None` finds the installed CLI on `PATH` and in the usual
    install places; the CLI bundled inside either SDK is never used. Claude: passed to the SDK
    as the CLI it launches. Codex: the binary whose `app-server` the SDK talks to."""

    model: str | None = None
    """Claude: `--model`, passed as given (Claude Code resolves its own aliases, such as
    `opus`). Codex: the thread's model. A value made only of lowercase letters (`sol`, `luna`,
    `astra`) is a model family: before the turn it resolves to the newest model codex's own
    catalog lists (not hidden) whose slug is `gpt-<version>-<family>`, newest by the version in
    the slug, and `Result.model` is that slug. A family with no listed model, or a catalog that
    cannot be read, fails the run with no model run; it never falls back to another model. Any
    other value is a literal slug, passed as given."""

    effort: str | None = None
    """Claude: `--effort` (`low`, `medium`, `high`, `xhigh`, `max`). Codex: the turn's reasoning
    effort (`minimal` to `xhigh` and what that codex build adds)."""

    thinking: bool | None = None
    """Claude: `--thinking`; false turns extended thinking off for the turn, true makes it
    adaptive, `None` leaves Claude Code's default. Codex: refused; codex's reasoning is set by
    `effort`."""

    max_turns: int | None = None
    """Claude: `--max-turns`; the harness ends the turn after that many agentic turns, a
    `max_turns` failure. Codex: refused; codex has no turn cap."""

    timeout_seconds: float = 600.0
    """Both: the deadline for the whole turn. When it passes the harness and every process it
    started are killed, grandchildren included, and the turn is a `timeout` failure."""

    budget_usd: float | None = None
    """Claude: `--max-budget-usd`; the harness ends the turn when its own cost estimate crosses
    it. Codex: refused, codex reports no cost."""

    tools: tuple[str, ...] | None = None
    """Claude: `--tools`, the built-in tools that exist in the turn; an empty tuple removes all
    of them. Codex: refused."""

    allowed_tools: tuple[str, ...] = ()
    """Claude: `--allowedTools`, tool patterns that run without a permission prompt, such as
    `Bash(git status:*)`. Codex: refused."""

    disallowed_tools: tuple[str, ...] = ()
    """Claude: `--disallowedTools`, tool patterns removed from the turn. Codex: refused."""

    permission_mode: str | None = None
    """Claude: `--permission-mode` (`default`, `acceptEdits`, `plan`, `dontAsk`, `auto`,
    `bypassPermissions`). Codex: refused; use `approval_policy` and `codex_config`."""

    setting_sources: tuple[str, ...] | None = None
    """Claude: `--setting-sources`, which settings files load (`user`, `project`, `local`);
    `("project", "local")` loads the target's CLAUDE.md, rules and hooks without the user's.
    `None` loads what the CLI loads by default. Codex: refused."""

    settings: Mapping[str, Any] | str | None = None
    """Claude: `--settings`, a settings object (or a path or JSON text) layered above the
    settings files; this is where a sandbox and permission rules go. Codex: refused; use
    `codex_config`."""

    mcp_config: Mapping[str, Any] | str | None = None
    """Claude: `--mcp-config`, an object shaped `{"mcpServers": {...}}` or a path to one.
    Codex: refused; MCP servers go in `codex_config` under `mcp_servers`."""

    strict_mcp: bool = False
    """Claude: `--strict-mcp-config`, so only `mcp_config` servers exist in the turn. Codex:
    refused."""

    add_dirs: tuple[str, ...] = ()
    """Claude: `--add-dir` per entry, directories the file tools may reach beyond the working
    directory. Codex: refused; give a directory access in the permissions of `codex_config`."""

    system_prompt: str | None = None
    """Both: the whole system prompt of the turn, in place of the harness's own, for a caller
    that is not a coding agent. `None` keeps the harness's own. Claude: `--system-prompt`, in
    place of the `claude_code` preset; refused together with `append_system_prompt`, since a
    replaced prompt has nothing to append to. Codex: the thread's base instructions, in place
    of codex's own, on start and on resume."""

    append_system_prompt: str | None = None
    """Claude: appended to the Claude Code system prompt (the `claude_code` preset). Codex: the
    thread's developer instructions, added beside codex's instructions (its own, or
    `system_prompt`)."""

    output_schema: Mapping[str, Any] | None = None
    """Both: a JSON schema the final answer must satisfy (claude `--json-schema`, codex turn
    output schema). The parsed answer lands in `Result.structured`; an answer that does not
    parse is an `invalid_output` failure."""

    name: str | None = None
    """Claude: `--name`, the session's display name. Codex: the thread's name."""

    persist_session: bool = True
    """Both: false runs the turn without writing its session anywhere, so it is in neither
    harness's session list and cannot be resumed. Claude: `--no-session-persistence`; a
    resumed session is read and nothing is written back to it. Codex: the thread is started
    ephemeral; a resumed thread is already persisted, so `Session.resume` with this off is
    refused."""

    env_set: Mapping[str, str] = field(default_factory=dict)
    """Both: variables set in the harness's environment, over what it inherits."""

    env_remove: tuple[str, ...] = ()
    """Both: names or globs removed from what the harness inherits, removed for real by the
    launcher the library puts in front of the CLI. A name in `env_set` is never removed."""

    strip_session_markers: bool = True
    """Both: also remove `SESSION_MARKERS`, the variables a calling Claude Code session leaves
    in its children's environment. On claude the values the SDK itself sets for the harness
    (its entrypoint and version) stay."""

    codex_config: Mapping[str, Any] | None = None
    """Codex: the thread's config, keyed as in `config.toml`; this is where permission profiles
    (`default_permissions`, `permissions`), `sandbox_mode`, network and `mcp_servers` go. Claude:
    refused; use `settings`."""

    approval_policy: str | None = None
    """Codex: what happens when the turn asks to escalate. `never` refuses every escalation
    without asking; `auto_review` lets codex's reviewer decide. `None` is the SDK's default,
    `auto_review`. Claude: refused; use `permission_mode`."""

    service_tier: str | None = None
    """Codex: the thread's service tier. Claude: refused."""

    bypass_hook_trust: bool = False
    """Codex: run the project's hooks (`.codex/hooks.json`) on any machine, with no trust
    recorded there beforehand and nothing written to it. For this thread only, the project the
    working directory belongs to (the nearest directory holding `.git`, else the working
    directory) is trusted, so its `.codex` config layer and hooks load, and the app-server's
    thread override `bypass_hook_trust` runs them without hook trust. The user's
    `config.toml` is never written. False leaves the machine's own trust in place: a project's
    hooks run only when codex trusts the project and each hook. Claude: refused; claude has no
    hook trust."""

    claude_extra_args: Mapping[str, str | None] = field(default_factory=dict)
    """Claude: extra CLI flags, `{"flag": "value"}` or `{"flag": None}` for a bare flag, for a
    flag no knob names. Codex: refused."""

    codex_extra_args: tuple[str, ...] = ()
    """Codex: arguments placed before `app-server` on the codex command line, such as
    `("-c", "key=value")` or `("--enable", "feature")`. Claude: refused."""

    def __post_init__(self) -> None:
        for name in ("tools", "setting_sources"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, tuple(value))
        for name in ("allowed_tools", "disallowed_tools", "env_remove", "codex_extra_args"):
            object.__setattr__(self, name, tuple(getattr(self, name)))
        object.__setattr__(self, "add_dirs", tuple(str(d) for d in self.add_dirs))
        object.__setattr__(self, "env_set", dict(self.env_set))
        object.__setattr__(self, "claude_extra_args", dict(self.claude_extra_args))
        for name in ("settings", "mcp_config", "codex_config", "output_schema"):
            value = getattr(self, name)
            if isinstance(value, Mapping):
                object.__setattr__(self, name, dict(value))
        validate_profile(self.to_dict())
        _refuse_for_harness(self)

    def to_dict(self) -> dict:
        data: dict[str, Any] = {}
        for f in fields(self):
            value = getattr(self, f.name)
            if isinstance(value, tuple):
                value = list(value)
            elif isinstance(value, Mapping):
                value = dict(value)
            data[f.name] = value
        return data

    def __getattr__(self, name: str):
        raise refuse_attribute("Profile", name)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Profile:
        """Build a profile from configuration, refusing unknown and 0.1.x keys by name."""
        if not isinstance(data, Mapping):
            raise ProfileError("profile: expected an object")
        for key, message in REMOVED_KEYS.items():
            if key in data:
                raise ProfileError(f"profile.{key}: {message}")
        validate_profile(data)
        return cls(**dict(data))


_dataclass_init = Profile.__init__


def _init(self, *args: Any, **knobs: Any) -> None:
    for key, message in REMOVED_KEYS.items():
        if key in knobs:
            raise ProfileError(f"profile.{key}: {message}")
    _dataclass_init(self, *args, **knobs)


_init.__doc__ = _dataclass_init.__doc__
_init.__signature__ = __import__("inspect").signature(_dataclass_init)  # type: ignore[attr-defined]
Profile.__init__ = _init  # type: ignore[method-assign]


# Knobs that only one harness can honour, and what setting them on the other means.
_CLAUDE_ONLY = (
    "budget_usd", "tools", "allowed_tools", "disallowed_tools", "permission_mode",
    "setting_sources", "settings", "mcp_config", "strict_mcp", "add_dirs", "claude_extra_args",
    "thinking", "max_turns",
)
_CODEX_ONLY = ("codex_config", "approval_policy", "service_tier", "codex_extra_args")


def _refuse_for_harness(profile: Profile) -> None:
    harness = profile.harness
    other_only = _CODEX_ONLY if harness == "claude" else _CLAUDE_ONLY
    for name in other_only:
        value = getattr(profile, name)
        # An empty `tools` tuple is a real claude setting (no built-in tools), so a
        # tuple counts as set whenever it is not None for that one knob.
        # `thinking` is a choice either way, so false is as set as true.
        is_set = value is not None if name in ("tools", "thinking") else bool(value)
        if is_set:
            raise ProfileError(
                f"profile.{name}: {harness} cannot honour it; it is a "
                f"{'codex' if harness == 'claude' else 'claude'} knob"
            )
    if profile.bypass_hook_trust and harness == "claude":
        raise ProfileError("profile.bypass_hook_trust: claude has no hook trust to bypass")
    if harness == "claude" and profile.system_prompt is not None and profile.append_system_prompt:
        raise ProfileError(
            "profile.append_system_prompt: claude replaces its system prompt with system_prompt, "
            "so there is nothing to append to; put the text in system_prompt"
        )
    overlap = [name for name in profile.env_set if _matches(name, profile.env_remove)]
    if overlap:
        raise ProfileError(
            f"profile.env_set: {overlap} also match env_remove; a variable is either set or removed"
        )


def _matches(name: str, patterns) -> bool:
    return any(fnmatch.fnmatchcase(name, p) for p in patterns)


@dataclass(frozen=True)
class Session:
    """Which conversation a turn belongs to.

    `fresh` starts one; on claude the id is generated before launch and pinned,
    on codex it is learned from the thread. `pinned` lets the caller choose the
    id and is claude-only. `resume` continues an existing one on either harness.
    """

    kind: str = "fresh"
    id: str | None = None

    @classmethod
    def fresh(cls) -> Session:
        return cls("fresh", None)

    @classmethod
    def pinned(cls, session_id: str) -> Session:
        if not session_id:
            raise ValueError("a pinned session needs an id")
        return cls("pinned", session_id)

    @classmethod
    def resume(cls, session_id: str) -> Session:
        if not session_id:
            raise ValueError("a resumed session needs an id")
        return cls("resume", session_id)


_TYPES = {
    "string": (str,),
    "number": (int, float),
    "integer": (int,),
    "boolean": (bool,),
    "array": (list, tuple),
    "object": (Mapping,),
    "null": (type(None),),
}


def _type_ok(value: Any, kind: str) -> bool:
    if kind in ("number", "integer") and isinstance(value, bool):
        return False
    return isinstance(value, _TYPES[kind])


def _validate(value: Any, schema: Mapping[str, Any], path: str) -> None:
    kinds = schema.get("type")
    if kinds is not None:
        allowed = kinds if isinstance(kinds, list) else [kinds]
        if not any(_type_ok(value, k) for k in allowed):
            expected = " or ".join(allowed)
            raise ProfileError(f"{path}: expected {expected}, got {type(value).__name__}")
    if "enum" in schema and value not in schema["enum"]:
        raise ProfileError(f"{path}: must be one of {schema['enum']}, got {value!r}")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "exclusiveMinimum" in schema and not value > schema["exclusiveMinimum"]:
            raise ProfileError(f"{path}: must be greater than {schema['exclusiveMinimum']}")
        if "minimum" in schema and value < schema["minimum"]:
            raise ProfileError(f"{path}: must be at least {schema['minimum']}")
    if isinstance(value, Mapping):
        props = schema.get("properties", {})
        for name in schema.get("required", ()):
            if name not in value:
                raise ProfileError(f"{path}: missing required key {name!r}")
        extra = schema.get("additionalProperties", True)
        for key, item in value.items():
            if key in props:
                _validate(item, props[key], f"{path}.{key}")
            elif extra is False:
                raise ProfileError(f"{path}: unknown key {key!r}")
            elif isinstance(extra, Mapping):
                _validate(item, extra, f"{path}.{key}")
    if isinstance(value, (list, tuple)) and "items" in schema:
        for index, item in enumerate(value):
            _validate(item, schema["items"], f"{path}[{index}]")


def validate_profile(data: Mapping[str, Any]) -> None:
    """Raise ProfileError when `data` does not fit profile.schema.json."""
    if not isinstance(data, Mapping):
        raise ProfileError("profile: expected an object")
    _validate(data, PROFILE_SCHEMA, "profile")


def __getattr__(name: str):
    raise refuse_module_attribute(__name__, name)
