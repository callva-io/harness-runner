import json

import pytest
from conftest import fake

from callva.harness_runner import FailureKind, Profile, Session, run


def claude(**knobs) -> Profile:
    return Profile(harness="claude", **{"cli_path": fake("claude"), "timeout_seconds": 20, **knobs})


def flag(argv, name):
    """The value of `name` on the command line, in either the two-token or the = form."""
    for index, item in enumerate(argv):
        if item == name:
            return argv[index + 1]
        if item.startswith(name + "="):
            return item.split("=", 1)[1]
    return None


def test_every_claude_knob_reaches_the_command_line(fake_env, record, tmp_path):
    extra = tmp_path / "extra"
    extra.mkdir()
    profile = claude(
        model="haiku", effort="low", budget_usd=0.5, tools=["Read", "Bash"],
        allowed_tools=["Read", "Bash(git status:*)"], disallowed_tools=["Edit", "Write"],
        permission_mode="dontAsk", setting_sources=["project", "local"],
        settings={"sandbox": {"enabled": True}, "permissions": {"ask": ["Bash"]}},
        mcp_config={"mcpServers": {}}, strict_mcp=True, add_dirs=[str(extra)],
        append_system_prompt="APPENDED", output_schema={"type": "object"}, name="nightly",
        claude_extra_args={"max-turns": "7", "verbose": None},
    )
    result = run("hi", profile, tmp_path, environ={**fake_env, "FAKE_CLAUDE": "structured"})
    assert result.ok, result.failure
    argv = record()["argv"]
    assert flag(argv, "--model") == "haiku" and flag(argv, "--effort") == "low"
    assert flag(argv, "--max-budget-usd") == "0.5"
    assert flag(argv, "--tools") == "Read,Bash"
    assert flag(argv, "--allowedTools") == "Read,Bash(git status:*)"
    assert flag(argv, "--disallowedTools") == "Edit,Write"
    assert flag(argv, "--permission-mode") == "dontAsk"
    assert flag(argv, "--setting-sources") == "project,local"
    assert json.loads(flag(argv, "--settings")) == {"sandbox": {"enabled": True},
                                                   "permissions": {"ask": ["Bash"]}}
    assert json.loads(flag(argv, "--mcp-config")) == {"mcpServers": {}}
    assert "--strict-mcp-config" in argv
    assert flag(argv, "--add-dir") == str(extra)
    assert flag(argv, "--append-system-prompt") == "APPENDED"
    assert json.loads(flag(argv, "--json-schema")) == {"type": "object"}
    assert flag(argv, "--name") == "nightly"
    assert flag(argv, "--max-turns") == "7"
    # Without system_prompt the system prompt is the claude_code preset.
    assert "--system-prompt" not in argv and "--system-prompt-file" not in argv
    assert result.command[1:] == tuple(argv)


def test_an_empty_tools_list_removes_every_built_in_tool(fake_env, record, tmp_path):
    assert run("x", claude(tools=[]), tmp_path, environ=fake_env).ok
    assert flag(record()["argv"], "--tools") == ""


def test_defaults_add_no_flags_and_keep_the_preset(fake_env, record, tmp_path):
    assert run("x", claude(), tmp_path, environ=fake_env).ok
    argv = record()["argv"]
    for name in ("--model", "--tools", "--permission-mode", "--setting-sources", "--settings",
                 "--mcp-config", "--system-prompt", "--append-system-prompt", "--thinking",
                 "--max-turns", "--no-session-persistence"):
        assert flag(argv, name) is None and name not in argv, name


def test_success_reads_everything(fake_env, tmp_path):
    events = []
    result = run("What is up?", claude(), tmp_path, environ=fake_env, on_event=events.append)
    assert result.ok and result.failure is None
    assert result.answer == "answer to: What is up?"
    assert result.model == "claude-fake-1" and result.cost_usd == 0.0123
    assert result.num_turns == 3 and result.duration_ms == 1234
    assert (result.tokens.input, result.tokens.output) == (100, 20)
    assert (result.tokens.cache_read, result.tokens.cache_creation) == (50, 5)
    assert result.permission_denials == () and result.harness_version == "2.1.280"
    assert result.raw["subtype"] == "success"
    assert result.command[0].endswith("fakes/claude")
    assert events[0]["subtype"] == "init" and events[0]["apiKeySource"] == "none"
    assert events[-1]["type"] == "ResultMessage"


def test_fresh_session_is_pinned_before_launch(fake_env, record, tmp_path):
    result = run("x", claude(), tmp_path, environ=fake_env)
    assert flag(record()["argv"], "--session-id") == result.session_id


def test_pinned_and_resumed_sessions(fake_env, record, tmp_path):
    pinned = "3f1f3a52-4f5e-4b8e-9d3a-7c1c2b3a4d5e"
    result = run("x", claude(), tmp_path, environ=fake_env, session=Session.pinned(pinned))
    assert result.session_id == pinned and flag(record()["argv"], "--session-id") == pinned
    again = run("y", claude(), tmp_path, environ=fake_env, session=Session.resume(pinned))
    assert again.ok and again.session_id == pinned
    argv = record()["argv"]
    assert flag(argv, "--resume") == pinned and flag(argv, "--session-id") is None


def test_structured_answers(fake_env, tmp_path):
    schema = {"type": "object"}
    result = run("x", claude(output_schema=schema), tmp_path,
                 environ={**fake_env, "FAKE_CLAUDE": "structured"})
    assert result.ok and result.structured == {"verdict": "ok", "schema_seen": True}
    prose = run("x", claude(output_schema=schema), tmp_path,
                environ={**fake_env, "FAKE_CLAUDE": "prose_under_schema"})
    assert not prose.ok and prose.failure.kind == FailureKind.INVALID_OUTPUT
    assert prose.answer == "just prose"


@pytest.mark.parametrize("mode,kind", [
    ("quota", FailureKind.QUOTA),
    ("api429", FailureKind.QUOTA),
    ("auth", FailureKind.NOT_AUTHENTICATED),
    ("badmodel", FailureKind.MODEL_REFUSED),
    ("budget", FailureKind.BUDGET),
    ("max_turns", FailureKind.MAX_TURNS),
    ("crash", FailureKind.CRASH),
])
def test_failures_map_to_their_kind(fake_env, tmp_path, mode, kind):
    result = run("x", claude(), tmp_path, environ={**fake_env, "FAKE_CLAUDE": mode})
    assert not result.ok and result.failure.kind == kind, result.failure
    assert result.failure.message


def test_failure_keeps_the_harness_sentence(fake_env, tmp_path):
    result = run("x", claude(), tmp_path, environ={**fake_env, "FAKE_CLAUDE": "budget"})
    assert result.failure.message == "Reached maximum budget ($0.0001)"
    refused = run("x", claude(), tmp_path, environ={**fake_env, "FAKE_CLAUDE": "badmodel"})
    assert refused.failure.message.startswith("API error 404: There's an issue with the selected")


def test_a_login_the_harness_keeps_retrying_is_not_authenticated(fake_env, tmp_path):
    result = run("x", claude(timeout_seconds=3), tmp_path,
                 environ={**fake_env, "FAKE_CLAUDE": "retry401"})
    assert result.failure.kind == FailureKind.NOT_AUTHENTICATED
    assert "HTTP 401" in result.failure.message and "timed out" in result.failure.message


@pytest.mark.parametrize("model", ["opus", "sol"])
def test_a_model_name_reaches_claude_as_given(fake_env, record, tmp_path, model):
    # Codex's model families do not apply on claude: every name is passed to `--model`.
    result = run("hi", claude(model=model), tmp_path, environ=fake_env)
    assert result.ok, result.failure
    assert flag(record()["argv"], "--model") == model


def test_a_light_turn_replaces_the_prompt_persists_nothing_and_does_not_think(
        fake_env, record, tmp_path):
    profile = claude(system_prompt="You decide.", thinking=False, max_turns=1,
                     persist_session=False)
    assert run("x", profile, tmp_path, environ=fake_env).ok
    argv = record()["argv"]
    assert flag(argv, "--system-prompt") == "You decide."
    assert "--append-system-prompt" not in argv
    assert flag(argv, "--thinking") == "disabled" and flag(argv, "--max-turns") == "1"
    assert "--no-session-persistence" in argv


def test_thinking_on_is_adaptive(fake_env, record, tmp_path):
    assert run("x", claude(thinking=True), tmp_path, environ=fake_env).ok
    assert flag(record()["argv"], "--thinking") == "adaptive"
