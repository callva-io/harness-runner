"""Codex through openai-codex: the thread a profile maps to, and the reading of the turn's stream.

The turn is read from `turn().stream()` rather than `thread.run()`, because
`run()` raises on a failed turn and drops `codexErrorInfo`, which is where
codex says whether a failure was a spent quota, a refused login or a budget.
"""

from __future__ import annotations

import contextlib
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from openai_codex import ApprovalMode, Codex, CodexConfig, Thread
from openai_codex.types import ModelListResponse

from .profile import Profile
from .result import FailureKind, Tokens

APPROVAL_MODES = {"never": ApprovalMode.deny_all, "auto_review": ApprovalMode.auto_review}

# codexErrorInfo values that name a failure kind on their own.
ERROR_INFO_KINDS = {
    "usageLimitExceeded": FailureKind.QUOTA,
    "rateLimitExceeded": FailureKind.QUOTA,
    "unauthorized": FailureKind.NOT_AUTHENTICATED,
    "sessionBudgetExceeded": FailureKind.BUDGET,
}


def config(profile: Profile, *, launcher: str, cwd: str, env: dict[str, str]) -> CodexConfig:
    """How the SDK starts codex: the launcher, the profile's extra arguments, then `app-server`."""
    args = (launcher, *profile.codex_extra_args, "app-server", "--listen", "stdio://")
    return CodexConfig(launch_args_override=args, cwd=cwd, env=env or None)


def project_root(cwd: str) -> str:
    """The project codex keys trust by for `cwd`: the nearest directory at or above it that
    holds `.git`, else `cwd` itself."""
    start = os.path.abspath(cwd)
    here = start
    while True:
        if os.path.exists(os.path.join(here, ".git")):
            return here
        parent = os.path.dirname(here)
        if parent == here:
            return start
        here = parent


def trusted_projects(cwd: str) -> dict[str, Any]:
    """`projects` entries that trust the project `cwd` belongs to, under its path as given and
    as resolved."""
    root = project_root(cwd)
    return {path: {"trust_level": "trusted"} for path in (root, os.path.realpath(root))}


# A model family is a name made only of lowercase letters (`sol`, `luna`, `astra`); anything
# else is a literal slug. A slug belongs to family F when it is `gpt-<version>-F`, the version
# being dot-separated numbers.
FAMILY = re.compile(r"[a-z]+")
FAMILY_SLUG = re.compile(r"gpt-(\d+(?:\.\d+)*)-([a-z]+)")


class NoModel(Exception):
    """A model family that resolved to no model, so no model was run."""

    def __init__(self, kind: FailureKind, message: str) -> None:
        super().__init__(message)
        self.kind = kind


def is_family(model: str | None) -> bool:
    """Whether a profile's `model` names a family rather than a literal slug."""
    return bool(model) and FAMILY.fullmatch(model) is not None


def newest_in_family(family: str, models: list[dict]) -> str | None:
    """The newest listed slug of `family` in a catalog of `{"id", "hidden"}` entries, by the
    version in the slug; `None` when none is listed."""
    found: list[tuple[tuple[int, ...], str]] = []
    for entry in models:
        slug = entry.get("id")
        if entry.get("hidden") or not isinstance(slug, str):
            continue
        match = FAMILY_SLUG.fullmatch(slug)
        if match and match.group(2) == family:
            found.append((tuple(int(part) for part in match.group(1).split(".")), slug))
    return max(found)[1] if found else None


def catalog(client: Codex) -> list[dict]:
    """Every model the app-server lists, hidden ones included, across all its pages."""
    page = client.models(include_hidden=True)
    models = [{"id": entry.id, "hidden": entry.hidden} for entry in page.data]
    while page.next_cursor:
        page = client._client.request(
            "model/list", {"includeHidden": True, "cursor": page.next_cursor},
            response_model=ModelListResponse)
        models += [{"id": entry.id, "hidden": entry.hidden} for entry in page.data]
    return models


def resolve_model(client: Codex, family: str) -> str:
    """The newest listed model of `family` in codex's own catalog; raises `NoModel` when there
    is none or the catalog cannot be read, and never falls back to another model."""
    try:
        models = catalog(client)
    except Exception as exc:
        raise NoModel(FailureKind.ERROR, (
            f"the codex model catalog could not be read to resolve the model family "
            f"'{family}', so no model was run: {exc}")) from exc
    slug = newest_in_family(family, models)
    if slug is None:
        raise NoModel(FailureKind.MODEL_REFUSED, (
            f"the codex model catalog lists no model of the family '{family}', "
            f"so no model was run"))
    return slug


def thread_options(profile: Profile, *, cwd: str, model: str | None = None,
                   resume: bool = False) -> dict[str, Any]:
    """The keyword arguments of `thread_start`, or of `thread_resume` when `resume`, a profile
    sets; `model`, when given, is the slug a family resolved to."""
    opts: dict[str, Any] = {"cwd": cwd}
    if model or profile.model:
        opts["model"] = model or profile.model
    config = dict(profile.codex_config or {})
    if profile.bypass_hook_trust:
        # The project's hooks run for this thread only, with nothing persisted: the project is
        # trusted, so its `.codex` layer and `hooks.json` load, and `bypass_hook_trust`, which
        # the app-server takes as a thread override though `config.toml` does not, runs its
        # hooks without the machine's hook trust.
        config["bypass_hook_trust"] = True
        config["projects"] = {**dict(config.get("projects") or {}), **trusted_projects(cwd)}
    if config:
        opts["config"] = config
    if profile.system_prompt is not None:
        opts["base_instructions"] = profile.system_prompt
    if profile.append_system_prompt:
        opts["developer_instructions"] = profile.append_system_prompt
    if not profile.persist_session and not resume:
        # Only a new thread can be ephemeral; `run` refuses a resume with persistence off.
        opts["ephemeral"] = True
    if profile.service_tier:
        opts["service_tier"] = profile.service_tier
    if profile.approval_policy:
        opts["approval_mode"] = APPROVAL_MODES[profile.approval_policy]
    return opts


def turn_options(profile: Profile) -> dict[str, Any]:
    opts: dict[str, Any] = {}
    if profile.effort:
        opts["effort"] = profile.effort
    if profile.output_schema:
        opts["output_schema"] = dict(profile.output_schema)
    return opts


def _dump(obj: Any) -> Any:
    dump = getattr(obj, "model_dump", None)
    if dump is None:
        return obj
    return dump(mode="json", by_alias=True, exclude_none=True)


@dataclass
class Outcome:
    """Everything one codex turn produced, as the app-server reported it."""

    thread_id: str | None = None
    model: str | None = None
    turn: dict | None = None
    items: list[dict] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)
    usage_first: dict | None = None
    usage_last: dict | None = None
    error: BaseException | None = None
    stderr: tuple[str, ...] = ()


def run_turn(
    prompt: str,
    profile: Profile,
    *,
    cfg: CodexConfig,
    cwd: str,
    resume_id: str | None,
    outcome: Outcome,
    on_client: Callable[[Codex], None],
    on_event: Callable[[dict], None] | None,
) -> None:
    """Start codex, resolve a model family, run one turn on a fresh or resumed thread, and read
    its stream."""
    client = None
    try:
        client = Codex(cfg)
        on_client(client)
        if is_family(profile.model):
            outcome.model = resolve_model(client, profile.model)
        opts = thread_options(profile, cwd=cwd, model=outcome.model, resume=bool(resume_id))
        if resume_id:
            thread: Thread = client.thread_resume(resume_id, **opts)
        else:
            thread = client.thread_start(**opts)
        outcome.thread_id = thread.id
        if profile.name:
            thread.set_name(profile.name)
        handle = thread.turn(prompt, **turn_options(profile))
        for event in handle.stream():
            params = _dump(event.payload)
            method = event.method
            if method == "item/completed" and isinstance(params, dict):
                outcome.items.append(params.get("item") or {})
            elif method == "thread/tokenUsage/updated" and isinstance(params, dict):
                usage = params.get("tokenUsage") or {}
                if outcome.usage_first is None:
                    outcome.usage_first = usage
                outcome.usage_last = usage
            elif method == "error" and isinstance(params, dict):
                outcome.errors.append(params)
            elif method == "turn/completed" and isinstance(params, dict):
                outcome.turn = params.get("turn") or {}
            if on_event is not None:
                # A caller's renderer never ends the turn.
                with contextlib.suppress(Exception):
                    on_event({"method": method, "params": params})
    except BaseException as exc:  # every ending is read, none is raised
        outcome.error = exc
    finally:
        if client is not None:
            inner = getattr(client, "_client", None)
            outcome.stderr = tuple(getattr(inner, "_stderr_lines", ()) or ())
            with contextlib.suppress(Exception):
                client.close()


def answer(outcome: Outcome) -> str:
    """The last agent message of the turn."""
    items = list(outcome.items)
    if outcome.turn and isinstance(outcome.turn.get("items"), list):
        items += outcome.turn["items"]
    for item in reversed(items):
        if isinstance(item, dict) and item.get("type") == "agentMessage" and item.get("text"):
            return str(item["text"])
    return ""


def tokens(outcome: Outcome) -> Tokens:
    """This turn's tokens: the thread's running total, less what it held before the turn."""
    last = outcome.usage_last or {}
    total = last.get("total") or {}
    if not total:
        return Tokens()
    first = outcome.usage_first or {}
    before = {}
    if first.get("total") and first.get("last"):
        before = {k: first["total"].get(k, 0) - first["last"].get(k, 0) for k in first["total"]}

    def count(key: str) -> int | None:
        value = total.get(key)
        return None if value is None else value - before.get(key, 0)

    return Tokens(input=count("inputTokens"), output=count("outputTokens"),
                  cache_read=count("cachedInputTokens"), cache_creation=None)


def turn_error(outcome: Outcome) -> dict | None:
    """The turn's own error, else the last fatal error event."""
    if outcome.turn and isinstance(outcome.turn.get("error"), dict):
        return outcome.turn["error"]
    for event in reversed(outcome.errors):
        if isinstance(event.get("error"), dict) and not event.get("willRetry"):
            return event["error"]
    return None


def error_info_kind(error: dict | None) -> tuple[FailureKind | None, int | None]:
    """The kind codexErrorInfo names, and the HTTP status it carries."""
    info = (error or {}).get("codexErrorInfo")
    if isinstance(info, str):
        return ERROR_INFO_KINDS.get(info), None
    if isinstance(info, dict):
        for value in info.values():
            if isinstance(value, dict) and isinstance(value.get("httpStatusCode"), int):
                return None, value["httpStatusCode"]
    return None, None
