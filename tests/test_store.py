"""Profiles kept in a store, as versions, resolved by scope.

The spec checks, the file form and the resolution run with no store. The
versioning runs the shipped steps and the queries against a real PostgreSQL
named by HARNESS_RUNNER_TEST_DSN, through psycopg, which is not a dependency of
the library; those cases skip when either is missing:

    HARNESS_RUNNER_TEST_DSN=postgresql://... \\
        uv run --extra dev --with 'psycopg[binary]>=3.2' pytest -q
"""

from __future__ import annotations

import os
import re
import secrets
import tomllib

import pytest

from callva.harness_runner import (
    PROFILE_STORE_OWNER,
    PROFILE_STORE_STEPS,
    ProfileConflict,
    ProfileError,
    ProfileNotFound,
    StoredProfile,
    active_profile,
    check_spec,
    profile_history,
    profile_text,
    profile_version,
    remove_profile,
    resolve_among,
    resolve_profile,
    resolve_profiles,
    save_profile,
    shipped_profile_names,
    stored_profiles,
)

SHIPPED = ["claude-act", "claude-read", "claude-read-sandboxed",
           "codex-act", "codex-read", "codex-read-sandboxed"]
SPEC = {"harness": "claude", "model": "opus", "timeout_seconds": 600,
        "env_set": {"CAPABILITIES_READ_ONLY": "1"},
        "settings": {"sandbox": {"enabled": True}, "permissions": {"deny": ["Edit(./**)"]}},
        "append_system_prompt": 'Read.\nChange "nothing".'}


# --- With no store -----------------------------------------------------------

def test_a_spec_is_judged_whole_by_the_profile_rules():
    assert check_spec(SPEC) == SPEC
    for spec, words in (({"harness": "claude", "modle": "opus"}, "unknown key 'modle'"),
                        ({"model": "opus"}, "no top-level `harness`"),
                        ({"harness": "claude", "codex": {"model": "x"}}, "harness table"),
                        ({"harness": "claude", "approval_policy": "never"}, "codex knob"),
                        ({"harness": "claude", "fence": "read"}, "'fence' was removed"),
                        ({"harness": "claude", "model": None}, "leave the knob out"),
                        ({"harness": "claude", "timeout_seconds": float("nan")}, "not text"),
                        ("harness = 'claude'", "mapping of knobs")):
        with pytest.raises(ProfileError, match=re.escape(words)):
            check_spec(spec)


def test_the_file_form_of_a_spec_reads_back_as_the_same_spec():
    text = profile_text(SPEC)
    assert text.splitlines()[0] == 'harness = "claude"'
    assert tomllib.loads(text) == SPEC
    odd = {"harness": "codex", "codex_config": {"a.b": {"c d": [1, 2.5, "\x7f"]}}}
    assert tomllib.loads(profile_text(odd)) == odd


def _version(name, scope, spec, project=None, id_="v"):
    return StoredProfile(id=f"{id_}-{scope}", scope=scope,
                         scope_id=project if scope == "project" else None, name=name,
                         spec=spec, active=True, created_at=None, created_by=None)


def test_a_name_resolves_to_the_project_then_the_global_then_the_shipped_profile():
    mine = _version("claude-read", "project", {"harness": "claude", "model": "mine"}, "prj_a")
    theirs = _version("claude-read", "project", {"harness": "claude", "model": "theirs"}, "prj_b")
    everyone = _version("claude-read", "global", {"harness": "claude", "model": "global"})
    found = resolve_among("claude-read", [theirs, everyone, mine], project="prj_a")
    assert (found.layer, found.id, found.profile().model) == ("project", "v-project", "mine")
    assert found.shadows == ("global", "shipped")
    found = resolve_among("claude-read", [theirs, everyone], project="prj_a")
    assert (found.layer, found.id, found.profile().model) == ("global", "v-global", "global")
    found = resolve_among("claude-read", [theirs], project="prj_a")
    assert (found.layer, found.id, found.shadows) == ("shipped", None, ())
    assert found.profile().model == "opus" and found.text().startswith("# Profile")
    with pytest.raises(ProfileNotFound):
        resolve_among("nowhere", [theirs], project="prj_a")
    with pytest.raises(ProfileNotFound, match="'claude-act' and 'codex-act'"):
        resolve_among("act", [], project="prj_a")
    names = [r.name for r in resolve_profiles([mine, theirs], project="prj_a")]
    assert names == SHIPPED and shipped_profile_names() == SHIPPED


# --- Against a real store ----------------------------------------------------

DSN = os.environ.get("HARNESS_RUNNER_TEST_DSN")


@pytest.fixture
def conn():
    if not DSN:
        pytest.skip("HARNESS_RUNNER_TEST_DSN is unset")
    psycopg = pytest.importorskip("psycopg")
    schema = "harness_test_" + secrets.token_hex(4)
    connection = psycopg.connect(DSN)
    connection.execute(f'create schema "{schema}"')
    connection.execute(f'set search_path to "{schema}"')
    for _step, sql in PROFILE_STORE_STEPS:
        connection.execute(sql)
    connection.commit()
    try:
        yield connection
    finally:
        connection.rollback()
        connection.execute(f'drop schema "{schema}" cascade')
        connection.commit()
        connection.close()


def test_the_steps_create_only_objects_named_for_the_owner(conn):
    rows = conn.execute("select relname from pg_class c join pg_namespace n "
                        "on n.oid = c.relnamespace where n.nspname = current_schema()").fetchall()
    assert rows and all(r[0].startswith(PROFILE_STORE_OWNER + "_") for r in rows)


def test_a_save_is_a_version_and_an_identical_save_creates_none(conn):
    first, created = save_profile(conn, "builder", SPEC, scope="project", scope_id="prj_a",
                                  created_by="kz")
    conn.commit()
    assert created and first.active and first.created_by == "kz" and first.spec == SPEC
    again, created = save_profile(conn, "builder", dict(reversed(list(SPEC.items()))),
                                  scope="project", scope_id="prj_a")
    assert not created and again.id == first.id
    second, created = save_profile(conn, "builder", {**SPEC, "effort": "high"},
                                   scope="project", scope_id="prj_a", base=first.id)
    conn.commit()
    assert created and second.id != first.id
    history = profile_history(conn, "builder", scope="project", scope_id="prj_a")
    assert [(v.id, v.active) for v in history] == [(first.id, False), (second.id, True)]
    assert profile_version(conn, first.id).spec == SPEC
    assert active_profile(conn, "builder", scope="project", scope_id="prj_a").id == second.id


def test_a_save_over_a_version_already_replaced_is_refused(conn):
    first, _ = save_profile(conn, "builder", SPEC, scope="global")
    second, _ = save_profile(conn, "builder", {**SPEC, "effort": "high"}, scope="global",
                             base=first.id)
    conn.commit()
    with pytest.raises(ProfileConflict, match=second.id):
        save_profile(conn, "builder", {**SPEC, "effort": "low"}, scope="global", base=first.id)
    conn.rollback()
    with pytest.raises(ProfileConflict):
        remove_profile(conn, "builder", scope="global", base=first.id)
    conn.rollback()
    assert [v.active for v in profile_history(conn, "builder", scope="global")] == [False, True]


def test_a_writer_that_read_a_version_another_writer_cleared_is_refused(conn):
    import psycopg
    first, _ = save_profile(conn, "builder", SPEC, scope="global")
    conn.commit()
    other = psycopg.connect(DSN)
    try:
        other.execute(f"set search_path to {conn.execute('select current_schema()').fetchone()[0]}")
        save_profile(other, "builder", {**SPEC, "effort": "high"}, scope="global", base=first.id)
        other.commit()
    finally:
        other.close()
    with pytest.raises(ProfileConflict):
        save_profile(conn, "builder", {**SPEC, "effort": "low"}, scope="global", base=first.id)
    conn.rollback()


def test_the_store_holds_one_active_version_per_scope_and_name(conn):
    psycopg = pytest.importorskip("psycopg")
    save_profile(conn, "builder", SPEC, scope="global")
    conn.commit()
    with pytest.raises(psycopg.errors.UniqueViolation):
        conn.execute("insert into harness_profiles (scope, scope_id, name, spec) "
                     "values ('global', null, 'builder', '{}'::jsonb)")
    conn.rollback()
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("insert into harness_profiles (scope, scope_id, name, spec) "
                     "values ('global', 'prj_a', 'other', '{}'::jsonb)")
    conn.rollback()


def test_a_spec_the_profile_rules_refuse_is_never_saved(conn):
    with pytest.raises(ProfileError, match="unknown key 'modle'"):
        save_profile(conn, "builder", {"harness": "claude", "modle": "x"}, scope="global")
    with pytest.raises(ValueError):
        save_profile(conn, "builder", SPEC, scope="project")
    with pytest.raises(ProfileError):
        save_profile(conn, "../builder", SPEC, scope="global")
    conn.rollback()
    assert profile_history(conn, "builder", scope="global") == []


def test_removing_keeps_the_history_and_the_name_falls_through_to_the_next_layer(conn):
    everyone, _ = save_profile(conn, "claude-read", {**SPEC, "model": "global"}, scope="global")
    mine, _ = save_profile(conn, "claude-read", {**SPEC, "model": "mine"}, scope="project",
                           scope_id="prj_a")
    save_profile(conn, "claude-read", {**SPEC, "model": "theirs"}, scope="project",
                 scope_id="prj_b")
    conn.commit()
    found = resolve_profile(conn, "claude-read", project="prj_a")
    assert (found.layer, found.id, found.shadows) == ("project", mine.id, ("global", "shipped"))
    assert resolve_profile(conn, "claude-read").id == everyone.id
    assert {v.scope_id for v in stored_profiles(conn, project="prj_a")} == {None, "prj_a"}
    assert remove_profile(conn, "claude-read", scope="project", scope_id="prj_a").id == mine.id
    conn.commit()
    assert resolve_profile(conn, "claude-read", project="prj_a").id == everyone.id
    remove_profile(conn, "claude-read", scope="global")
    conn.commit()
    found = resolve_profile(conn, "claude-read", project="prj_a")
    assert (found.layer, found.id) == ("shipped", None)
    with pytest.raises(ProfileNotFound):
        remove_profile(conn, "claude-read", scope="global")
    conn.rollback()
    assert len(profile_history(conn, "claude-read", scope="global")) == 1
