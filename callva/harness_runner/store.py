"""Profiles kept in a store, as versions, resolved by scope.

The table is `harness_profiles`: one row is one version of a profile at one
scope, `global` or `project` (with the project's id), and exactly one version of
a name is active per scope. `spec` is the whole profile, `harness` included, in
the shape `Profile.from_dict` takes and validated whole on every save.

The library opens no connection and runs no migration of its own. The caller
applies `PROFILE_STORE_STEPS` under the ledger `PROFILE_STORE_OWNER` at
`PROFILE_STORE_MAJOR`.`PROFILE_STORE_MINOR` with the database library it already
uses, and passes its open DB-API connection (psycopg's, `%s` parameters) to
every function here. A function that writes runs inside the caller's
transaction and leaves the commit, or the rollback, to it.

A change never edits a row: a save inserts a new version and clears `active` on
the version the writer read (`... where id = <read> and active`), so two writers
can never both replace the same version; the second is refused with
`ProfileConflict` and reads again. A save identical to the active version
creates none. A name with no active version at a scope is not held there.

A name resolves to the most specific active scope: the project's version, then
the global version, then the profile this package ships.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .discovery import ProfileFile, ProfileNotFound, check_name, shipped_folder
from .profile import Profile, ProfileError
from .renamed import SPLIT_PROFILES, split_profile

PROFILE_STORE_OWNER = "harness"
"""The ledger the steps are applied under: every object they create is named
`harness_*`."""

PROFILE_STORE_MAJOR = 1
PROFILE_STORE_MINOR = 0

SCOPES = ("project", "global")
"""The scopes a stored profile is kept at, the most specific first."""

LAYERS = ("project", "global", "shipped")
"""Where a name can resolve, the most specific first."""

_PROFILES_TABLE = r"""
create table if not exists harness_profiles (
  id          uuid        not null default gen_random_uuid(),
  scope       text        not null,
  scope_id    text,
  name        text        not null,
  spec        jsonb       not null,
  active      boolean     not null default true,
  created_at  timestamptz not null default clock_timestamp(),
  created_by  text,
  constraint harness_profiles_pkey primary key (id),
  constraint harness_profiles_scope_check check (scope in ('global', 'project')),
  constraint harness_profiles_scope_id_check check ((scope = 'global') = (scope_id is null))
);

comment on table harness_profiles is
  'One row is one version of a callva-harness-runner profile at one scope. A change '
  'never edits a row: it inserts a new version and clears active on the version the '
  'writer read, in one transaction. History is every version of a name by created_at; '
  'a name with no active version is not held at that scope.';
comment on column harness_profiles.scope is
  'global, or project with the project''s id in scope_id. A name resolves to the '
  'project''s version, then the global one, then the profile the library ships.';
comment on column harness_profiles.spec is
  'The whole profile, harness included, as Profile.from_dict takes it; validated '
  'whole on write, an unknown key refused.';
comment on column harness_profiles.created_at is
  'When this version was saved: the clock at the insert, not the transaction''s '
  'start, so versions saved in one transaction still order by when each was saved.';
comment on column harness_profiles.created_by is
  'Who saved this version, as the writing tool resolved it.';

-- One active version of a name per scope, enforced by the store rather than by
-- every writer agreeing. A global row has no scope_id, and nulls are distinct in
-- a unique index, so the index keys on it with null read as ''.
create unique index if not exists harness_profiles_one_active_idx
  on harness_profiles (scope, coalesce(scope_id, ''), name) where active;
create index if not exists harness_profiles_name_idx
  on harness_profiles (scope, scope_id, name, created_at);
"""

PROFILE_STORE_STEPS: tuple[tuple[str, str], ...] = (
    ("0001-profiles", _PROFILES_TABLE),
)
"""The store steps, `(step id, SQL)`, in order. An applied step is never edited;
a change to the table is a new step and a new version."""


class ProfileConflict(ProfileError):
    """Another save or removal came first: the version the writer read is no
    longer the active one. Read again and save over what is active now."""


_SCHEMA_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,62}")


def _table(schema: str | None) -> str:
    if schema is None:
        return "harness_profiles"
    if not isinstance(schema, str) or not _SCHEMA_NAME.fullmatch(schema):
        raise ValueError(f"schema {schema!r} is not a plain identifier")
    return f'"{schema}".harness_profiles'


def _scope(scope: str, scope_id: str | None) -> tuple[str, str | None]:
    if scope == "global":
        if scope_id is not None:
            raise ValueError("a global profile has no scope_id")
        return scope, None
    if scope == "project":
        if not isinstance(scope_id, str) or not scope_id.strip():
            raise ValueError("a project profile needs the project's id as scope_id")
        return scope, scope_id
    raise ValueError(f"scope must be one of {list(SCOPES)}, got {scope!r}")


# --- The spec ----------------------------------------------------------------

def check_spec(spec: Mapping[str, Any], where: str = "profile") -> dict:
    """The whole profile `spec` as it is stored, or ProfileError saying why it is
    not one: what a flat profile file must hold - a top-level `harness`, no
    harness table - and what `Profile.from_dict` accepts, an unknown key refused.
    The answer is a plain copy of plain values, so it compares equal to what the
    store hands back."""
    if not isinstance(spec, Mapping):
        raise ProfileError(f"{where}: a profile is a mapping of knobs")
    try:
        plain = json.loads(json.dumps(dict(spec), allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise ProfileError(f"{where}: a knob holds a value that is not text, a number, "
                           f"a boolean, a list or a mapping: {exc}") from None
    try:
        text = profile_text(plain)
    except ProfileError as refused:
        raise ProfileError(f"{where}: {refused}; a profile file cannot say it, so leave "
                           "the knob out") from None
    ProfileFile(where, "store", where, text).knobs()
    try:
        Profile.from_dict(plain)
    except ProfileError as refused:
        raise ProfileError(f"{where}: {refused}") from None
    return plain


_BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")


def _toml_string(value: str) -> str:
    text = json.dumps(value, ensure_ascii=False)
    return text.replace("\x7f", "\\u007f")


def _toml_key(key: str) -> str:
    return key if _BARE_KEY.fullmatch(key) else _toml_string(key)


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ProfileError(f"a profile knob cannot hold {value!r}")
        return repr(value)
    if isinstance(value, str):
        return _toml_string(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    if isinstance(value, Mapping):
        if not value:
            return "{}"
        return "{ " + ", ".join(f"{_toml_key(str(k))} = {_toml_value(v)}"
                                for k, v in value.items()) + " }"
    raise ProfileError(f"a profile knob cannot hold {type(value).__name__}")


def profile_text(spec: Mapping[str, Any]) -> str:
    """A profile as the flat TOML file it would be: `harness` first, then each
    knob on a line of its own, a mapping as an inline table. A profile file read
    from this text gives `spec` back."""
    keys = ["harness"] if "harness" in spec else []
    keys += [k for k in spec if k != "harness"]
    return "".join(f"{_toml_key(k)} = {_toml_value(spec[k])}\n" for k in keys)


# --- Versions ----------------------------------------------------------------

def _plain_spec(value: Any) -> dict:
    if isinstance(value, (str, bytes, bytearray)):
        value = json.loads(value)
    return dict(value)


@dataclass(frozen=True)
class StoredProfile:
    """One stored version of a profile."""

    id: str
    scope: str
    """`project` or `global`."""
    scope_id: str | None
    """The project's id for a project profile; `None` for a global one."""
    name: str
    spec: dict
    """The whole profile, `harness` included."""
    active: bool
    created_at: datetime | None
    created_by: str | None

    @property
    def harness(self) -> str | None:
        return self.spec.get("harness")

    def profile(self) -> Profile:
        """The Profile this version defines."""
        try:
            return Profile.from_dict(self.spec)
        except ProfileError as refused:
            raise ProfileError(f"{self.where}: {refused}") from None

    def text(self) -> str:
        """The version as a flat profile file; a save of this text is identical."""
        return profile_text(self.spec)

    @property
    def where(self) -> str:
        scope = "global" if self.scope == "global" else f"project {self.scope_id}"
        return f"profile {self.name!r} version {self.id} ({scope})"

    def to_dict(self) -> dict:
        return {"id": self.id, "scope": self.scope, "scope_id": self.scope_id,
                "name": self.name, "harness": self.harness, "spec": self.spec,
                "active": self.active,
                "created_at": self.created_at.isoformat() if self.created_at else None,
                "created_by": self.created_by}


def _records(cur) -> list[dict]:
    rows = cur.fetchall()
    names = [column[0] for column in cur.description or ()]
    return [dict(row) if isinstance(row, Mapping) else dict(zip(names, row, strict=False))
            for row in rows]


def _version(record: Mapping[str, Any]) -> StoredProfile:
    return StoredProfile(id=str(record["id"]), scope=record["scope"],
                         scope_id=record["scope_id"], name=record["name"],
                         spec=_plain_spec(record["spec"]), active=bool(record["active"]),
                         created_at=record.get("created_at"),
                         created_by=record.get("created_by"))


_COLUMNS = "id, scope, scope_id, name, spec, active, created_at, created_by"


def _same_scope() -> str:
    return "scope = %s and coalesce(scope_id, '') = coalesce(%s, '')"


def active_profile(conn, name: str, *, scope: str, scope_id: str | None = None,
                   schema: str | None = None, lock: bool = False) -> StoredProfile | None:
    """The active version of `name` at one scope, or None. `lock` holds the row
    for the rest of the caller's transaction."""
    check_name(name)
    scope, scope_id = _scope(scope, scope_id)
    with conn.cursor() as cur:
        cur.execute(f"select {_COLUMNS} from {_table(schema)} "
                    f"where {_same_scope()} and name = %s and active"
                    f"{' for update' if lock else ''}", (scope, scope_id, name))
        found = _records(cur)
    return _version(found[0]) if found else None


def profile_version(conn, version: str, *, schema: str | None = None) -> StoredProfile | None:
    """One version by its id, active or not, or None."""
    with conn.cursor() as cur:
        cur.execute(f"select {_COLUMNS} from {_table(schema)} where id::text = %s",
                    (str(version),))
        found = _records(cur)
    return _version(found[0]) if found else None


def profile_history(conn, name: str, *, scope: str, scope_id: str | None = None,
                    schema: str | None = None) -> list[StoredProfile]:
    """Every version of `name` at one scope, oldest first."""
    check_name(name)
    scope, scope_id = _scope(scope, scope_id)
    with conn.cursor() as cur:
        cur.execute(f"select {_COLUMNS} from {_table(schema)} "
                    f"where {_same_scope()} and name = %s order by created_at, id",
                    (scope, scope_id, name))
        return [_version(r) for r in _records(cur)]


def stored_profiles(conn, *, project: str | None = None,
                    schema: str | None = None) -> list[StoredProfile]:
    """The active versions a lookup from `project` can reach: every global one,
    and every one of that project. Without a project, the global ones."""
    with conn.cursor() as cur:
        cur.execute(f"select {_COLUMNS} from {_table(schema)} "
                    "where active and (scope = 'global' or (scope = 'project' and scope_id = %s)) "
                    "order by name, scope", (project,))
        return [_version(r) for r in _records(cur)]


def _unique_violation(exc: BaseException) -> bool:
    return (getattr(exc, "sqlstate", None) or getattr(exc, "pgcode", None)) == "23505"


def save_profile(conn, name: str, spec: Mapping[str, Any], *, scope: str,
                 scope_id: str | None = None, base: str | None = None,
                 created_by: str | None = None,
                 schema: str | None = None) -> tuple[StoredProfile, bool]:
    """Save `spec` as the new active version of `name` at one scope, inside the
    caller's transaction, and say whether a version was created.

    `spec` is judged whole first (`check_spec`); a ProfileError leaves the store
    untouched. A save identical to the active version creates none. `base` is
    the version the writer read; without it the version active now is the one
    replaced. When `base` is not the active version, or another save clears it
    first, ProfileConflict is raised: the caller rolls back and reads again."""
    check_name(name)
    scope, scope_id = _scope(scope, scope_id)
    plain = check_spec(spec, f"profile {name!r}")
    active = active_profile(conn, name, scope=scope, scope_id=scope_id, schema=schema,
                            lock=True)
    if base is not None and (active is None or active.id != str(base)):
        raise ProfileConflict(
            f"profile {name!r} is no longer at version {base}: "
            + (f"{active.id} is active" if active else "it has no active version"))
    if active is not None and active.spec == plain:
        return active, False
    with conn.cursor() as cur:
        if active is not None:
            cur.execute(f"update {_table(schema)} set active = false "
                        "where id = %s and active", (active.id,))
            if cur.rowcount != 1:
                raise ProfileConflict(f"profile {name!r} was saved by someone else first")
        try:
            cur.execute(f"insert into {_table(schema)} "
                        "(scope, scope_id, name, spec, active, created_by) "
                        f"values (%s, %s, %s, %s::jsonb, true, %s) returning {_COLUMNS}",
                        (scope, scope_id, name, json.dumps(plain), created_by))
        except Exception as exc:
            if _unique_violation(exc):
                raise ProfileConflict(
                    f"profile {name!r} was saved by someone else first") from None
            raise
        return _version(_records(cur)[0]), True


def remove_profile(conn, name: str, *, scope: str, scope_id: str | None = None,
                   base: str | None = None, schema: str | None = None) -> StoredProfile:
    """Clear the active version of `name` at one scope, inside the caller's
    transaction, and answer the version cleared. Every version stays in the
    history. ProfileNotFound when none is active; ProfileConflict when `base`
    is not the active version or another writer clears it first."""
    check_name(name)
    scope, scope_id = _scope(scope, scope_id)
    active = active_profile(conn, name, scope=scope, scope_id=scope_id, schema=schema,
                            lock=True)
    if active is None:
        raise ProfileNotFound(f"profile {name!r} has no active version at scope {scope}")
    if base is not None and active.id != str(base):
        raise ProfileConflict(f"profile {name!r} is no longer at version {base}: "
                              f"{active.id} is active")
    with conn.cursor() as cur:
        cur.execute(f"update {_table(schema)} set active = false where id = %s and active",
                    (active.id,))
        if cur.rowcount != 1:
            raise ProfileConflict(f"profile {name!r} was changed by someone else first")
    return active


# --- Resolution --------------------------------------------------------------

@dataclass(frozen=True)
class ResolvedProfile:
    """What a name resolved to, and from where."""

    name: str
    layer: str
    """`project`, `global` or `shipped`: the most specific layer holding the name."""
    id: str | None
    """The stored version that won; `None` for a shipped profile."""
    spec: dict
    """The whole profile, `harness` included."""
    shadows: tuple[str, ...] = ()
    """The less specific layers that also hold the name, which this one hides."""
    version: StoredProfile | None = None
    """The stored version that won; `None` for a shipped profile."""

    @property
    def harness(self) -> str | None:
        return self.spec.get("harness")

    @property
    def where(self) -> str:
        if self.version is not None:
            return self.version.where
        return f"profile {self.name!r} (shipped)"

    def profile(self) -> Profile:
        """The Profile the winning layer defines."""
        try:
            return Profile.from_dict(self.spec)
        except ProfileError as refused:
            raise ProfileError(f"{self.where}: {refused}") from None

    def text(self) -> str:
        """The winning version as a flat profile file; for a shipped profile, its
        file exactly as shipped."""
        if self.version is None:
            return _shipped_text(self.name) or profile_text(self.spec)
        return self.version.text()

    def to_dict(self) -> dict:
        return {"name": self.name, "layer": self.layer, "id": self.id,
                "harness": self.harness, "shadows": list(self.shadows)}


def _shipped_text(name: str) -> str | None:
    entry = shipped_folder().joinpath(f"{name}.toml")
    try:
        return entry.read_text(encoding="utf-8") if entry.is_file() else None
    except OSError:
        return None


def shipped_profile_names() -> list[str]:
    """The names of the profiles this package ships, the lowest layer."""
    folder = shipped_folder()
    return sorted(entry.name[:-5] for entry in folder.iterdir()
                  if entry.name.endswith(".toml") and entry.is_file())


def resolve_among(name: str, active: Iterable[StoredProfile], *,
                  project: str | None = None) -> ResolvedProfile:
    """Resolve `name` against active versions the caller already read (as
    `stored_profiles` answers them), then the shipped profiles. ProfileNotFound
    when no layer holds it; ProfileError when the shipped file cannot be read
    as a profile."""
    check_name(name)
    held: dict[str, StoredProfile] = {}
    for one in active:
        if one.name != name or not one.active:
            continue
        if one.scope == "project" and project is not None and one.scope_id == project:
            held["project"] = one
        elif one.scope == "global":
            held["global"] = one
    text = _shipped_text(name)
    layers = [layer for layer in LAYERS
              if (layer in held) or (layer == "shipped" and text is not None)]
    if not layers:
        where = "this project, " if project is not None else ""
        said = f"no profile named {name!r} in {where}the global profiles, or the shipped profiles"
        if name in SPLIT_PROFILES:
            said = f"{said}; {split_profile(name)}"
        raise ProfileNotFound(said)
    winner, shadows = layers[0], tuple(layers[1:])
    if winner == "shipped":
        spec = ProfileFile(name, "shipped", f"shipped profile {name!r}", text).knobs()
        return ResolvedProfile(name, "shipped", None, spec, shadows, None)
    version = held[winner]
    return ResolvedProfile(name, winner, version.id, dict(version.spec), shadows, version)


def resolve_profile(conn, name: str, *, project: str | None = None,
                    schema: str | None = None) -> ResolvedProfile:
    """Resolve `name` to the most specific active scope: the project's version,
    else the global one, else the shipped profile. The answer names the layer
    and the version that won."""
    check_name(name)
    return resolve_among(name, stored_profiles(conn, project=project, schema=schema),
                         project=project)


def resolve_profiles(active: Iterable[StoredProfile], *,
                     project: str | None = None) -> list[ResolvedProfile]:
    """Every name a lookup from `project` can reach, each as it resolves, from
    active versions the caller already read and the shipped profiles."""
    active = list(active)
    names = sorted({one.name for one in active} | set(shipped_profile_names()))
    found = []
    for name in names:
        try:
            found.append(resolve_among(name, active, project=project))
        except ProfileNotFound:
            continue
    return found


__all__ = [
    "LAYERS",
    "PROFILE_STORE_MAJOR",
    "PROFILE_STORE_MINOR",
    "PROFILE_STORE_OWNER",
    "PROFILE_STORE_STEPS",
    "SCOPES",
    "ProfileConflict",
    "ResolvedProfile",
    "StoredProfile",
    "active_profile",
    "check_spec",
    "profile_history",
    "profile_text",
    "profile_version",
    "remove_profile",
    "resolve_among",
    "resolve_profile",
    "resolve_profiles",
    "save_profile",
    "shipped_profile_names",
    "stored_profiles",
]
