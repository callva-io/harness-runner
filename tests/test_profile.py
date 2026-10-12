import ast
import inspect
from dataclasses import fields
from pathlib import Path

import pytest

from callva.harness_runner import PROFILE_SCHEMA, Profile, ProfileError, Session
from callva.harness_runner import profile as profile_module

README = (Path(__file__).parent.parent / "README.md").read_text()
KNOBS = [f.name for f in fields(Profile)]

CLAUDE_ONLY = {
    "budget_usd": 1.0, "tools": ["Read"], "allowed_tools": ["Read"], "disallowed_tools": ["Edit"],
    "permission_mode": "plan", "setting_sources": ["project"], "settings": {"a": 1},
    "mcp_config": {"mcpServers": {}}, "strict_mcp": True, "add_dirs": ["/tmp"],
    "claude_extra_args": {"x": None}, "thinking": True, "max_turns": 1,
}
CODEX_ONLY = {
    "codex_config": {"sandbox_mode": "read-only"}, "approval_policy": "never",
    "service_tier": "flex", "codex_extra_args": ["--enable", "x"],
}


def knob_docstrings() -> dict[str, str]:
    tree = ast.parse(inspect.getsource(profile_module))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Profile")
    docs, last = {}, None
    for node in cls.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            last = node.target.id
        elif (last and isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
              and isinstance(node.value.value, str)):
            docs[last] = node.value.value
            last = None
    return docs


def test_every_knob_has_a_docstring_naming_both_harnesses():
    docs = knob_docstrings()
    assert set(docs) == set(KNOBS)
    for name, doc in docs.items():
        if name == "harness":
            continue
        said = doc.lower()
        assert ("claude" in said or "both" in said) and ("codex" in said or "both" in said), name


def test_every_knob_is_in_the_schema_and_the_readme():
    assert set(PROFILE_SCHEMA["properties"]) == set(KNOBS)
    for name in KNOBS:
        assert f"`{name}`" in README, name


def test_defaults_leave_the_harness_alone():
    p = Profile(harness="claude")
    assert p.tools is None and p.permission_mode is None and p.setting_sources is None
    assert p.strip_session_markers is True and p.timeout_seconds == 600.0


@pytest.mark.parametrize("name,value", CLAUDE_ONLY.items())
def test_claude_knobs_are_refused_on_codex(name, value):
    Profile(harness="claude", **{name: value})
    with pytest.raises(ProfileError, match=f"profile.{name}: codex cannot honour it"):
        Profile(harness="codex", **{name: value})


@pytest.mark.parametrize("name,value", CODEX_ONLY.items())
def test_codex_knobs_are_refused_on_claude(name, value):
    Profile(harness="codex", **{name: value})
    with pytest.raises(ProfileError, match=f"profile.{name}: claude cannot honour it"):
        Profile(harness="claude", **{name: value})


def test_an_empty_tools_list_is_a_claude_setting():
    assert Profile(harness="claude", tools=[]).tools == ()
    with pytest.raises(ProfileError, match=r"profile\.tools"):
        Profile(harness="codex", tools=[])


def test_hook_trust_bypass_is_a_codex_knob_refused_on_claude():
    assert Profile(harness="codex", bypass_hook_trust=True).bypass_hook_trust is True
    with pytest.raises(ProfileError, match="claude has no hook trust"):
        Profile(harness="claude", bypass_hook_trust=True)


def test_thinking_off_is_a_claude_setting():
    assert Profile(harness="claude", thinking=False).thinking is False
    with pytest.raises(ProfileError, match=r"profile\.thinking: codex cannot honour it"):
        Profile(harness="codex", thinking=False)


def test_a_replaced_system_prompt_takes_no_append_on_claude():
    with pytest.raises(ProfileError, match=r"profile\.append_system_prompt: claude replaces"):
        Profile(harness="claude", system_prompt="S", append_system_prompt="A")
    both = Profile(harness="codex", system_prompt="S", append_system_prompt="A")
    assert (both.system_prompt, both.append_system_prompt) == ("S", "A")


def test_a_turn_cap_is_at_least_one():
    with pytest.raises(ProfileError, match=r"profile\.max_turns: must be at least 1"):
        Profile(harness="claude", max_turns=0)


def test_fence_key_is_refused_with_the_knobs_that_replace_it():
    with pytest.raises(ProfileError) as refused:
        Profile.from_dict({"harness": "claude", "fence": "read"})
    message = str(refused.value)
    for knob in ("tools", "allowed_tools", "permission_mode", "setting_sources", "settings",
                 "mcp_config", "strict_mcp", "codex_config", "approval_policy"):
        assert knob in message


@pytest.mark.parametrize("key,replacement", [
    ("allow_tools", "allowed_tools"), ("env", "env_remove"), ("extra_args", "claude_extra_args")])
def test_other_removed_keys_name_their_replacements(key, replacement):
    with pytest.raises(ProfileError, match=replacement):
        Profile.from_dict({"harness": "codex", key: []})


def test_from_dict_builds_and_refuses_unknown_keys():
    p = Profile.from_dict({"harness": "codex", "model": "gpt-x", "effort": "high",
                           "codex_config": {"sandbox_mode": "workspace-write"},
                           "approval_policy": "never", "env_set": {"A": "1"},
                           "env_remove": ["SECRET_*"], "timeout_seconds": 30})
    assert p.codex_config == {"sandbox_mode": "workspace-write"} and p.env_remove == ("SECRET_*",)
    assert Profile.from_dict(p.to_dict()) == p
    with pytest.raises(ProfileError, match="unknown key 'bogus'"):
        Profile.from_dict({"harness": "claude", "bogus": 1})
    with pytest.raises(ProfileError, match="must be one of"):
        Profile.from_dict({"harness": "codex", "approval_policy": "sometimes"})
    with pytest.raises(ProfileError, match="missing required key 'harness'"):
        Profile.from_dict({"model": "x"})


def test_a_variable_is_either_set_or_removed():
    with pytest.raises(ProfileError, match="also match env_remove"):
        Profile(harness="claude", env_set={"SECRET_A": "x"}, env_remove=["SECRET_*"])


def test_sessions():
    assert Session.fresh().kind == "fresh"
    assert Session.pinned("abc").id == "abc"
    assert Session.resume("t1").kind == "resume"
    with pytest.raises(ValueError):
        Session.pinned("")
