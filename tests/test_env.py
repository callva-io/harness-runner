import os

import pytest
from conftest import fake

from callva.harness_runner import SESSION_MARKERS, Profile, run
from callva.harness_runner.launch import plan_env

PARENT = {
    "CLAUDECODE": "1",
    "CLAUDE_CODE_ENTRYPOINT": "cli",
    "CLAUDE_CODE_SESSION_ID": "parent-session",
    "CLAUDE_EFFORT": "high",
    "AI_AGENT": "claude-code_parent",
    "SECRET_TOKEN": "s3cret",
    "SECRET_OTHER": "s3cret2",
    "KEEP_ME": "yes",
}


@pytest.fixture
def parent(monkeypatch):
    """This process's environment carries a calling Claude session's markers and a secret,
    as it does when a Claude Code session runs the caller."""
    for key, value in PARENT.items():
        monkeypatch.setenv(key, value)


@pytest.mark.parametrize("harness", ["claude", "codex"])
def test_removed_variables_never_reach_the_harness(fake_env, record, tmp_path, parent, harness):
    profile = Profile(harness=harness, cli_path=fake(harness), env_remove=["SECRET_*"],
                      env_set={"SET_ME": "v"})
    assert run("x", profile, tmp_path, environ={**os.environ, **fake_env}).ok
    env = record()["env"]
    assert "SECRET_TOKEN" not in env and "SECRET_OTHER" not in env
    assert env["KEEP_ME"] == "yes" and env["SET_ME"] == "v"
    for marker in ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CLAUDE_EFFORT", "AI_AGENT"):
        assert marker not in env, marker
    if harness == "claude":
        # The SDK's own entrypoint, not the calling session's.
        assert env["CLAUDE_CODE_ENTRYPOINT"] == "sdk-py"
    else:
        assert "CLAUDE_CODE_ENTRYPOINT" not in env


@pytest.mark.parametrize("harness", ["claude", "codex"])
def test_markers_stay_when_asked(fake_env, record, tmp_path, parent, harness):
    profile = Profile(harness=harness, cli_path=fake(harness), strip_session_markers=False)
    assert run("x", profile, tmp_path, environ={**os.environ, **fake_env}).ok
    env = record()["env"]
    assert env["CLAUDE_EFFORT"] == "high" and env["SECRET_TOKEN"] == "s3cret"


@pytest.mark.parametrize("harness", ["claude", "codex"])
def test_environ_is_the_base_and_extra_env_goes_on_top(fake_env, record, tmp_path, parent,
                                                       harness):
    # The caller's environ leaves out KEEP_ME and changes SECRET_OTHER; the harness sees
    # exactly that, although the SDK itself only ever inherits this process's environment.
    base = {**fake_env, "SECRET_OTHER": "changed", "ONLY_IN_BASE": "b"}
    profile = Profile(harness=harness, cli_path=fake(harness), env_set={"FROM_PROFILE": "p"})
    assert run("x", profile, tmp_path, environ=base, extra_env={"FROM_CALL": "c"}).ok
    env = record()["env"]
    assert "KEEP_ME" not in env and "SECRET_TOKEN" not in env
    assert env["SECRET_OTHER"] == "changed" and env["ONLY_IN_BASE"] == "b"
    assert env["FROM_PROFILE"] == "p" and env["FROM_CALL"] == "c"


def test_a_codex_home_of_its_own_reaches_the_app_server(fake_env, record, tmp_path, parent):
    # A run that must not read the user's codex configuration sets its own CODEX_HOME,
    # which wins over the one in the caller's environment.
    home = str(tmp_path / "codex-home")
    profile = Profile(harness="codex", cli_path=fake("codex"), env_set={"CODEX_HOME": home})
    base = {**os.environ, **fake_env, "CODEX_HOME": str(tmp_path / "user-home")}
    assert run("x", profile, tmp_path, environ=base).ok
    assert record()["env"]["CODEX_HOME"] == home


def test_the_launcher_never_learns_a_value():
    plan = plan_env(inherited={"A": "1", "SECRET": "x"}, wanted_base={"A": "2"},
                    set_env={"B": "3"}, remove_patterns=SESSION_MARKERS)
    assert plan.set == {"A": "2", "B": "3"}
    assert plan.remove_names == ("SECRET",) and "B" in plan.keep
