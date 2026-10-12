import json
import os

import pytest
from conftest import fake

from callva.harness_runner import FailureKind, Profile, Session, run


def codex(**knobs) -> Profile:
    return Profile(harness="codex", **{"cli_path": fake("codex"), "timeout_seconds": 20, **knobs})


def requests(record, method):
    return [r["params"] for r in record()["requests"] if r["method"] == method]


def test_every_codex_knob_reaches_the_app_server(fake_env, record, tmp_path):
    config = {"default_permissions": "rf", "permissions": {"rf": {"extends": ":read-only",
                                                                  "network": {"enabled": True}}}}
    profile = codex(model="gpt-5.6-luna", effort="low", codex_config=config,
                    approval_policy="never", service_tier="flex", append_system_prompt="APPENDED",
                    output_schema={"type": "object"}, name="nightly",
                    codex_extra_args=["-c", "features.x=true", "--enable", "y"])
    result = run("hi", profile, tmp_path, environ={**fake_env, "FAKE_CODEX": "structured"})
    assert result.ok, result.failure
    rec = record()
    assert rec["argv"] == ["-c", "features.x=true", "--enable", "y",
                           "app-server", "--listen", "stdio://"]
    [start] = requests(record, "thread/start")
    assert start["model"] == "gpt-5.6-luna" and start["config"] == config
    assert start["approvalPolicy"] == "never" and start["serviceTier"] == "flex"
    assert start["developerInstructions"] == "APPENDED" and start["cwd"] == str(tmp_path)
    assert "baseInstructions" not in start
    [name] = requests(record, "thread/name/set")
    assert name["name"] == "nightly"
    [turn] = requests(record, "turn/start")
    assert turn["effort"] == "low" and turn["outputSchema"] == {"type": "object"}
    assert result.structured == {"verdict": "ok"}


def test_auto_review_and_default_approval(fake_env, record, tmp_path):
    assert run("x", codex(approval_policy="auto_review"), tmp_path, environ=fake_env).ok
    [start] = requests(record, "thread/start")
    assert start["approvalPolicy"] == "on-request" and start["approvalsReviewer"] == "auto_review"


def test_defaults_send_nothing_the_profile_did_not_name(fake_env, record, tmp_path):
    assert run("x", codex(), tmp_path, environ=fake_env).ok
    [start] = requests(record, "thread/start")
    assert set(start) <= {"cwd", "approvalPolicy", "approvalsReviewer"}
    assert "ephemeral" not in start and "baseInstructions" not in start
    [turn] = requests(record, "turn/start")
    assert "effort" not in turn and "outputSchema" not in turn


def test_success_reads_everything(fake_env, tmp_path):
    events = []
    result = run("What is up?", codex(model="gpt-x"), tmp_path, environ=fake_env,
                 on_event=events.append)
    assert result.ok and result.answer == "codex answer to: What is up?"
    assert result.session_id and result.model == "gpt-x" and result.cost_usd is None
    assert (result.tokens.input, result.tokens.output, result.tokens.cache_read) == (100, 20, 50)
    assert result.duration_ms == 3683 and result.harness_version == "0.159.0"
    assert result.raw["turn"]["status"] == "completed"
    assert events[-1]["method"] == "turn/completed"


def test_resume_continues_the_thread_and_counts_only_this_turn(fake_env, record, tmp_path):
    first = run("x", codex(), tmp_path, environ=fake_env)
    again = run("y", codex(append_system_prompt="A", codex_config={"sandbox_mode": "read-only"}),
                tmp_path, environ=fake_env, session=Session.resume(first.session_id))
    assert again.ok and again.session_id == first.session_id
    [resume] = requests(record, "thread/resume")
    assert resume["threadId"] == first.session_id
    assert resume["developerInstructions"] == "A"
    assert resume["config"] == {"sandbox_mode": "read-only"}
    assert requests(record, "thread/start") == []
    assert again.tokens.input == 100


def test_a_light_thread_is_ephemeral_with_its_own_base_instructions(fake_env, record, tmp_path):
    profile = codex(system_prompt="You decide.", append_system_prompt="A", persist_session=False)
    assert run("x", profile, tmp_path, environ=fake_env).ok
    [start] = requests(record, "thread/start")
    assert start["ephemeral"] is True and start["baseInstructions"] == "You decide."
    assert start["developerInstructions"] == "A"


def test_a_resumed_thread_keeps_the_replaced_base_instructions(fake_env, record, tmp_path):
    first = run("x", codex(system_prompt="S"), tmp_path, environ=fake_env)
    assert run("y", codex(system_prompt="S"), tmp_path, environ=fake_env,
               session=Session.resume(first.session_id)).ok
    [resume] = requests(record, "thread/resume")
    assert resume["baseInstructions"] == "S" and "ephemeral" not in resume


def test_a_resume_without_persistence_is_refused(fake_env, tmp_path):
    with pytest.raises(ValueError, match="codex cannot resume a thread without persisting"):
        run("x", codex(persist_session=False), tmp_path, environ=fake_env,
            session=Session.resume("abc"))


def test_a_pinned_session_is_refused(fake_env, tmp_path):
    with pytest.raises(ValueError, match="codex cannot pin"):
        run("x", codex(), tmp_path, environ=fake_env, session=Session.pinned("abc"))


@pytest.mark.parametrize("mode,kind", [
    ("quota", FailureKind.QUOTA),
    ("unauthorized", FailureKind.NOT_AUTHENTICATED),
    ("badmodel", FailureKind.MODEL_REFUSED),
    ("http429", FailureKind.QUOTA),
    ("budget", FailureKind.BUDGET),
    ("crash", FailureKind.CRASH),
])
def test_failures_map_to_their_kind(fake_env, tmp_path, mode, kind):
    result = run("x", codex(), tmp_path, environ={**fake_env, "FAKE_CODEX": mode})
    assert not result.ok and result.failure.kind == kind, result.failure


def test_a_wrapped_provider_error_is_unwrapped(fake_env, tmp_path):
    result = run("x", codex(), tmp_path, environ={**fake_env, "FAKE_CODEX": "badmodel"})
    assert result.failure.message == (
        "The 'gpt-nonexistent-9' model is not supported when using Codex with a ChatGPT account.")


def test_structured_answer_that_does_not_parse(fake_env, tmp_path):
    result = run("x", codex(output_schema={"type": "object"}), tmp_path,
                 environ={**fake_env, "FAKE_CODEX": "prose_under_schema"})
    assert result.failure.kind == FailureKind.INVALID_OUTPUT and result.answer == "just prose"


def test_a_completed_turn_without_a_final_message_is_ok_and_silent(fake_env, tmp_path):
    result = run("x", codex(), tmp_path, environ={**fake_env, "FAKE_CODEX": "empty"})
    assert result.ok and result.failure is None and result.answer == ""
    assert result.raw["turn"]["status"] == "completed"


def test_a_silent_turn_under_a_schema_is_invalid_output(fake_env, tmp_path):
    result = run("x", codex(output_schema={"type": "object"}), tmp_path,
                 environ={**fake_env, "FAKE_CODEX": "empty"})
    assert not result.ok and result.failure.kind == FailureKind.INVALID_OUTPUT


def test_bypass_hook_trust_trusts_the_project_and_its_hooks_for_the_thread(
        fake_env, record, tmp_path):
    project = tmp_path / "project"
    (project / ".git").mkdir(parents=True)
    work = project / "sub"
    work.mkdir()
    config = {"sandbox_mode": "read-only", "projects": {"/elsewhere": {"trust_level": "trusted"}}}
    first = run("x", codex(bypass_hook_trust=True, codex_config=config), work, environ=fake_env)
    assert first.ok, first.failure
    trusted = {"trust_level": "trusted"}
    expected = {"sandbox_mode": "read-only", "bypass_hook_trust": True,
                "projects": {"/elsewhere": trusted, str(project): trusted,
                             os.path.realpath(project): trusted}}
    [start] = requests(record, "thread/start")
    assert start["config"] == expected
    run("y", codex(bypass_hook_trust=True, codex_config=config), work, environ=fake_env,
        session=Session.resume(first.session_id))
    [resume] = requests(record, "thread/resume")
    assert resume["config"] == expected


def test_the_trusted_project_is_the_nearest_git_root_else_the_working_directory(tmp_path):
    from callva.harness_runner.codex import project_root
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    deep = tmp_path / "repo" / "a" / "b"
    deep.mkdir(parents=True)
    assert project_root(str(deep)) == str(tmp_path / "repo")
    loose = tmp_path / "loose"
    loose.mkdir()
    if not any((parent / ".git").exists() for parent in loose.parents):
        assert project_root(str(loose)) == str(loose)


TODAY = ["gpt-6.1-sol", "gpt-6-astra", "gpt-6-sol", "gpt-6-luna", "gpt-reserve", "gpt-5.6-sol",
         "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.5", "codex-auto-review"]
HIDDEN = {"gpt-reserve", "codex-auto-review"}


def catalog(*added):
    """Today's catalog in codex's order, with `added` (slug, hidden) entries at its end."""
    entries = [{"id": slug, "hidden": slug in HIDDEN} for slug in TODAY]
    return json.dumps(entries + [{"id": slug, "hidden": hidden} for slug, hidden in added])


@pytest.mark.parametrize("family,added,resolved", [
    ("sol", (), "gpt-6.1-sol"),
    # Last in the catalog's order, and still the newest: the version decides, not the priority.
    ("sol", (("gpt-6.2-sol", False),), "gpt-6.2-sol"),
    ("sol", (("gpt-6.3-sol", True),), "gpt-6.1-sol"),
    ("astra", (), "gpt-6-astra"),
    ("luna", (), "gpt-6-luna"),
])
def test_a_family_runs_the_newest_listed_model_of_that_family(
        fake_env, record, tmp_path, family, added, resolved):
    env = {**fake_env, "FAKE_CODEX_MODELS": catalog(*added)}
    result = run("x", codex(model=family), tmp_path, environ=env)
    assert result.ok, result.failure
    [start] = requests(record, "thread/start")
    assert start["model"] == resolved and result.model == resolved
    assert requests(record, "model/list") == [{"includeHidden": True}]


def test_a_resumed_thread_runs_the_family_resolved_again(fake_env, record, tmp_path):
    first = run("x", codex(), tmp_path, environ=fake_env)
    env = {**fake_env, "FAKE_CODEX_MODELS": catalog(("gpt-6.2-sol", False))}
    again = run("y", codex(model="sol"), tmp_path, environ=env,
                session=Session.resume(first.session_id))
    assert again.ok and again.model == "gpt-6.2-sol"
    [resume] = requests(record, "thread/resume")
    assert resume["model"] == "gpt-6.2-sol"


@pytest.mark.parametrize("family,models", [
    ("nova", catalog()),
    ("nova", catalog(("gpt-7-nova", True))),
    ("sol", json.dumps([{"id": "gpt-6-astra", "hidden": False}])),
], ids=["unknown", "only-hidden", "other-family-only"])
def test_a_family_with_no_listed_model_fails_and_runs_nothing(
        fake_env, record, tmp_path, family, models):
    env = {**fake_env, "FAKE_CODEX_MODELS": models}
    result = run("x", codex(model=family), tmp_path, environ=env)
    assert not result.ok and result.failure.kind == FailureKind.MODEL_REFUSED
    assert f"'{family}'" in result.failure.message
    assert "no model was run" in result.failure.message
    assert requests(record, "thread/start") == [] and requests(record, "turn/start") == []


def test_an_unreadable_catalog_fails_and_runs_nothing(fake_env, record, tmp_path):
    result = run("x", codex(model="sol"), tmp_path, environ={**fake_env, "FAKE_CODEX": "nocatalog"})
    assert not result.ok and result.failure.kind == FailureKind.ERROR
    assert "'sol'" in result.failure.message and "no model was run" in result.failure.message
    assert "model catalog unavailable" in result.failure.message
    assert requests(record, "thread/start") == [] and requests(record, "turn/start") == []


@pytest.mark.parametrize("slug", TODAY)
def test_every_slug_in_the_catalog_is_literal(slug):
    from callva.harness_runner.codex import is_family
    assert not is_family(slug)


@pytest.mark.parametrize("name", ["sol", "luna", "astra"])
def test_a_family_name_is_a_family(name):
    from callva.harness_runner.codex import is_family
    assert is_family(name)


@pytest.mark.parametrize("slug", ["gpt-6.1-sol", "gpt-6-sol", "gpt-nonexistent-9"])
def test_a_literal_slug_is_passed_as_given_without_a_catalog_lookup(
        fake_env, record, tmp_path, slug):
    result = run("x", codex(model=slug), tmp_path, environ={**fake_env, "FAKE_CODEX": "success"})
    assert result.ok and result.model == slug
    [start] = requests(record, "thread/start")
    assert start["model"] == slug
    assert requests(record, "model/list") == []
