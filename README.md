# callva-harness-runner

One run of an agent harness, as a Python library. A harness is the vendor's agent runtime: Claude Code, driven through Anthropic's `claude-agent-sdk`, or the Codex CLI, driven through OpenAI's `openai-codex`, always the CLI installed on the machine. A profile is one harness with its settings, a flat set of explicit knobs. A run is one turn: a prompt goes in, the harness works to its end or its deadline, and one `Result` comes out, with the failure named. The deadline kills everything the harness started. What a run may do is whatever its profile says, and nothing else; the library never decides it. A caller names a profile and never a harness: the profile names its harness. It ships six profiles, `claude-read`, `claude-act`, `claude-read-sandboxed`, `codex-read`, `codex-act` and `codex-read-sandboxed`, that a caller loads by name, and a caller's own profile files of the same name come first. A caller that keeps its profiles in its own PostgreSQL store, versioned and scoped to a project or to everyone, uses the table this library defines and the functions under Profiles in a store.

`DESIGN.md` is the contract. The source is at https://github.com/callva-io/harness-runner.

Formerly `callva-agentworker`, whose last release is 0.2.0. The 0.3.0 renames, with no aliases (each old name is refused with a message naming the new one):

| 0.2.0 | 0.3.0 |
|---|---|
| distribution `callva-agentworker` | `callva-harness-runner` |
| import `callva.agentworker` | `callva.harness_runner` |
| profile knob `engine` | `harness` |
| `Result.engine` | `Result.harness` |
| `Result.engine_version` | `Result.harness_version` |
| failure kind `incompatible_engine` (`FailureKind.INCOMPATIBLE_ENGINE`) | `incompatible_harness` (`FailureKind.INCOMPATIBLE_HARNESS`) |
| `ENGINES` | `HARNESSES` |
| `Probe.engine` | `Probe.harness` |

## Install

```
pip install callva-harness-runner
```

It depends on `claude-agent-sdk==0.2.161` and `openai-codex==0.159.0`, pinned exactly; together they add about 0.5 GB, most of it the CLIs both SDKs bundle. The bundled CLIs are never run: the library always runs the CLI installed on the machine.

In a PEP 723 script, pin the exact version so nothing outside the script's own release changes what it runs:

```python
# /// script
# dependencies = ["callva-harness-runner==0.9.0"]
# ///
```

## Use

```python
from callva.harness_runner import Profile, Session, run

profile = Profile.from_dict(config["profile"])   # your configuration, your knobs
result = run("Summarise README.md in one line.", profile, cwd="/path/to/project")
if result.ok:
    print(result.answer, result.cost_usd, result.session_id)
else:
    print(result.failure.kind, result.failure.message)
```

`run()` never raises for anything the harness did. Every ending is a `Result`; `result.failure.kind` says what kind, and `result.failure.message` carries the harness's own sentence. It raises `ValueError` only for a request it cannot express, such as a pinned session on codex.

`run(prompt, profile, cwd, *, session=None, environ=None, extra_env=None, on_event=None, cancel=None, stderr_tail=40, on_start=None)`:

- `session`: `Session.fresh()` (the default), `Session.pinned(id)` or `Session.resume(id)`, below.
- `environ`: the environment the harness starts from; this process's by default. A variable absent from it is absent in the harness, even though both SDKs inherit this process's environment.
- `extra_env`: variables set on top of `environ` and the profile's `env_set`.
- `on_event`: called with one dict per harness event while the turn runs. On claude it is the SDK's message: a system message is the harness's own event (`subtype` `init` carries `apiKeySource`, `model` and `claude_code_version`), any other message is its fields plus `type`, the SDK class name. On codex it is `{"method", "params"}`, one app-server notification.
- `cancel`: a `threading.Event`; setting it ends the turn and kills its processes.
- `on_start`: called once with a `Started` as soon as the harness process runs and its session id is known, before the first `on_event`: `started.harness`, `started.pid` (the harness process, the leader of its own session and process group, so a service that restarts after a crash of its own can end an orphaned tree) and `started.session_id` (claude's pinned or resumed id, codex's thread id). It is not called when no harness process started, such as a missing CLI. A harness that ended before codex gave a thread id is reported when the turn ends, with `session_id` `None`. An exception it raises never ends or alters the turn.

## Profile

A profile is a flat set of explicit knobs. There is no inheritance and no fence. A knob left at its default leaves the harness's own default in place. A knob one harness cannot honour is refused for that harness with a `ProfileError` when the profile is built, never ignored while it runs. Each knob's docstring on `Profile` says what it does on each harness; `PROFILE_SCHEMA` (shipped as `profile.schema.json`) validates a profile kept in configuration, and `Profile.from_dict` refuses unknown keys.

| Knob | claude | codex |
|---|---|---|
| `harness` | `claude` | `codex` |
| `cli_path` | the CLI the SDK launches; `None` finds the installed `claude` | the binary whose `app-server` the SDK talks to; `None` finds the installed `codex` |
| `model` | `--model`, as given | the thread's model; a family name of lowercase letters only (`sol`) runs the newest listed `gpt-<version>-<family>` model in codex's catalog, and fails the run when there is none; any other value is a literal slug |
| `effort` | `--effort` | the turn's reasoning effort |
| `timeout_seconds` | the deadline; default 600 | the same |
| `budget_usd` | `--max-budget-usd` | refused: codex reports no cost |
| `tools` | `--tools`, the built-in tools that exist; `[]` removes them all | refused |
| `allowed_tools` | `--allowedTools`, patterns that run without a prompt | refused |
| `disallowed_tools` | `--disallowedTools` | refused |
| `permission_mode` | `--permission-mode` | refused: use `approval_policy` and `codex_config` |
| `setting_sources` | `--setting-sources`; `["project", "local"]` loads the target's CLAUDE.md, rules and hooks without the user's | refused |
| `settings` | `--settings`, an object or path layered above the settings files: sandbox, permissions | refused: use `codex_config` |
| `mcp_config` | `--mcp-config`, `{"mcpServers": {...}}` or a path | refused: `mcp_servers` in `codex_config` |
| `strict_mcp` | `--strict-mcp-config` | refused |
| `add_dirs` | `--add-dir` per entry | refused: give the directory access in `codex_config` permissions |
| `append_system_prompt` | appended to the Claude Code system prompt | the thread's developer instructions |
| `output_schema` | `--json-schema` | the turn's output schema |
| `name` | `--name` | the thread's name |
| `env_set` | set in the harness's environment | the same |
| `env_remove` | names or globs removed from what the harness inherits | the same |
| `strip_session_markers` | also remove `SESSION_MARKERS`; default true | the same |
| `codex_config` | refused: use `settings` | the thread's config, keyed as in `config.toml`: permission profiles, `sandbox_mode`, network, `mcp_servers` |
| `approval_policy` | refused: use `permission_mode` | `never` (escalations refused) or `auto_review` (codex's reviewer decides); `None` is the SDK default, `auto_review` |
| `service_tier` | refused | the thread's service tier |
| `bypass_hook_trust` | refused: claude has no hook trust | run the project's `.codex/hooks.json` hooks on any machine with no trust recorded there: the project is trusted and hook trust bypassed for this thread only, and `config.toml` is never written |
| `claude_extra_args` | extra CLI flags, `{"flag": "value"}` or `{"flag": None}` | refused |
| `codex_extra_args` | refused | arguments before `app-server`, such as `["-c", "key=value"]` |

Claude always runs on the `claude_code` system-prompt preset, so a turn has the same prompt `claude -p` has; the library never replaces either harness's system prompt, and `append_system_prompt` only adds to it.

### Profiles by name

A profile file holds one profile, so one harness: flat TOML with a required top-level `harness = "claude"` or `harness = "codex"` and that harness's knobs beside it. A knob that is an object, such as `settings` or `codex_config`, may be written as a TOML table. A file with a `[claude]` or `[codex]` table, or without a top-level `harness`, is refused with a `ProfileError` that names this form.

```toml
harness = "codex"
model = "gpt-6-sol"
effort = "medium"
approval_policy = "never"
codex_config = { sandbox_mode = "danger-full-access" }
```

```python
from callva.harness_runner import find_profile, run
import dataclasses

profile = find_profile("codex-read-sandboxed", ["capabilities/myapp/profiles"])
profile = dataclasses.replace(profile, model="gpt-5.6-luna")   # change a knob for one run
result = run("What does this project do?", profile, cwd="/path/to/project")
```

A name resolves to the first `NAME.toml` found in, in order:

1. the folders the caller passes, in the order given;
2. the machine folder, `$XDG_CONFIG_HOME/callva-harness-runner/profiles`, or `~/.config/callva-harness-runner/profiles` when `XDG_CONFIG_HOME` is unset;
3. the profiles shipped inside the package.

The file found is used whole, with the harness it names: no merging and no inheritance. A found file that cannot be read as a profile is an error naming the file, and the search does not go on to a later source. A name is one file-name segment of letters, digits, `.`, `-` and `_`, not starting with `.`; anything else is refused.

- `find_profile(name, folders=(), *, environ=None) -> Profile`. It takes no harness: a harness passed to it, as a positional string or as `harness=`, is refused with a `TypeError` naming the call that replaces it, and a single folder string instead of a list is refused rather than searched.
- `find_profile_file(name, folders=(), *, environ=None) -> ProfileFile`: the file itself (`name`, `source`, `path`, `text`, `shadows`), with `.harness`, `.knobs()` and `.profile()`.
- `list_profiles(folders=(), *, environ=None) -> list[ProfileListing]`: every visible profile with its `name`, `source` (`folder`, `machine` or `shipped`), `path`, `harness`, the same-named files it `shadows`, and an `error` when the file cannot be read as a profile file (its `harness` is then `None`).
- `machine_folder(environ=None) -> Path`

`ProfileNotFound`, a `ProfileError`, means no source has the name. For `read`, `act` and `read-sandboxed` its message names the harness-prefixed profiles that replace them.

From a shell, `python -m callva.harness_runner profiles` prints the listing as JSON, and `python -m callva.harness_runner profiles show NAME` prints the file a lookup resolves to, after a comment line naming its source and path and one naming its harness. Both take `--folder DIR`, once per folder, in search order. `python -m callva.harness_runner profiles check FILE` validates one profile file: it prints `ok` with the file's harness, or the refusal naming the knob and exits 2.

`python -m callva.harness_runner knobs` prints every knob with its type, allowed values, default and what it does on each harness; `--harness claude` or `--harness codex` shows one harness. It reads `profile.schema.json` and the docstrings on `Profile` at runtime, so it always matches the installed version.

### Profiles in a store

A caller can keep its profiles in its own PostgreSQL store instead of in files: the table `harness_profiles`, one row per version of a profile at one scope, `global` or `project` (with the project's id), and one active version per scope and name. The library opens no connection and adds no dependency. It ships the table as store steps; the caller applies them with its own database library, before any of its own steps that reference the table, and passes its open DB-API connection (psycopg's) to every function. A function that writes runs inside the caller's transaction, and the caller commits.

```python
from callva.harness_runner import (PROFILE_STORE_MAJOR, PROFILE_STORE_MINOR, PROFILE_STORE_OWNER,
                                   PROFILE_STORE_STEPS, ProfileConflict, find_profile_file,
                                   resolve_profile, run, save_profile)

db.migrate(conn, PROFILE_STORE_OWNER, PROFILE_STORE_STEPS,          # your database library
           major=PROFILE_STORE_MAJOR, minor=PROFILE_STORE_MINOR)

spec = find_profile_file("builder", ["profiles"]).knobs()           # or any mapping of knobs
try:
    version, created = save_profile(conn, "builder", spec, scope="project", scope_id="prj_1",
                                    created_by="kz")   # base=<the version id you read>
    conn.commit()
except ProfileConflict:
    conn.rollback()                                                 # someone saved first: read again

found = resolve_profile(conn, "builder", project="prj_1")
print(found.layer, found.id)            # "project" and the version id, "global", or "shipped" and None
result = run("...", found.profile(), cwd="/path/to/project")
```

- `PROFILE_STORE_STEPS`: the steps, `(step id, SQL)` in order, applied under the ledger `PROFILE_STORE_OWNER` (`harness`; every object they create is named `harness_*`) at version `PROFILE_STORE_MAJOR`.`PROFILE_STORE_MINOR`. The SQL names the table unqualified, so apply it with the search path on your schema.
- `save_profile(conn, name, spec, *, scope, scope_id=None, base=None, created_by=None, schema=None) -> (StoredProfile, created)`: judges `spec` whole with `check_spec` (a `ProfileError` names the knob, an unknown key included), creates no version when `spec` equals the active one, and otherwise inserts the new version and clears `active` on the version the writer read. `ProfileConflict`, a `ProfileError`, means another writer came first, or `base` is no longer active.
- `remove_profile(conn, name, *, scope, scope_id=None, base=None, schema=None) -> StoredProfile`: clears the active version and answers it; `ProfileNotFound` when none is active.
- `active_profile(conn, name, *, scope, scope_id=None, schema=None, lock=False)`, `profile_version(conn, id, *, schema=None)`, `profile_history(conn, name, *, scope, scope_id=None, schema=None)` (oldest first) and `stored_profiles(conn, *, project=None, schema=None)` (the active global versions and the project's) read versions as `StoredProfile` (`id`, `scope`, `scope_id`, `name`, `spec`, `active`, `created_at`, `created_by`, with `.harness`, `.profile()`, `.text()` and `.to_dict()`).
- `resolve_profile(conn, name, *, project=None, schema=None) -> ResolvedProfile`: the project's active version, else the global one, else the shipped profile; `ProfileNotFound` when none holds it. A `ResolvedProfile` has `name`, `layer` (`project`, `global` or `shipped`), `id` (the version that won, `None` for a shipped profile), `spec`, `shadows` (the less specific layers that also hold the name), `version`, `.profile()` and `.text()`. `resolve_among(name, active, *, project=None)` and `resolve_profiles(active, *, project=None)` do the same from versions already read with `stored_profiles`.
- `check_spec(spec)` validates a spec without saving it; `check_profile_name(name)` refuses a name that is not one plain file-name segment, the rule both lookups use; `profile_text(spec)` writes it as the flat profile file it would be, which reads back as the same spec; `shipped_profile_names()` lists the shipped profiles.

`schema`, when given, qualifies the table. The file lookup above is unchanged and reads no store; the store resolution reads no files.

### Shipped profiles

The claude profiles follow the newest Opus: they name `opus`, Claude Code's alias for its newest Opus, so a newer Opus is picked up without a release. Codex has no such alias, so the codex profiles name the model family `sol`, which the runner resolves at run time to the newest listed sol model in codex's own catalog (today `gpt-6.1-sol`): a newer sol model is picked up without a release, and a catalog with no sol model fails the run rather than running another family. All run at effort `medium` with a one-hour deadline. Print one with `profiles show NAME`.

- `claude-read`, `codex-read`: a research question held to reading by instruction alone. Every tool and a full shell (claude `bypassPermissions`, codex `danger-full-access`), the target's project and local settings on claude, a read-only instruction appended to the system prompt, and `CAPABILITIES_READ_ONLY=1`.
- `claude-act`, `codex-act`: a task with full access (claude `bypassPermissions`, codex `danger-full-access`, approvals never).
- `claude-read-sandboxed`, `codex-read-sandboxed`: the same read, enforced by the operating system. The run's working directory cannot be written; the network, `~/.cache` and the temporary directory stay writable. `claude-read-sandboxed` runs Claude Code's seatbelt sandbox with `ask: ["Bash"]`, the write tools disallowed, no MCP servers, `disableAllHooks`, and the permission rule `Edit(./**)`, which Claude Code resolves against the working directory and merges into the sandbox's write denials; a `denyWrite` path would resolve against the settings root instead, not the run's directory. `codex-read-sandboxed` runs a named permissions profile extending `:read-only` with `~/.cache` and `:tmpdir` writable and the network on. Both carry the same read-only instruction and `CAPABILITIES_READ_ONLY=1`. `claude-read-sandboxed` does not run the target's hooks, because a hook runs outside the sandbox and could write the target: context a hook would build, such as a ContextKit SessionStart build, is read as it already is on disk. CLAUDE.md and the project's rules still load. `claude-read` does run the target's hooks.

### Environment

Both SDKs only add to the environment they inherit. To remove a variable for real, the library puts a small launcher of its own in front of the CLI: the SDK starts the launcher, the launcher removes what the profile removes and becomes the CLI. The launcher learns names, never values. `SESSION_MARKERS` are the variables a running Claude Code session leaves in its children (`CLAUDECODE`, `CLAUDE_CODE_SESSION_ID`, `CLAUDE_EFFORT`, `AI_AGENT` and the rest); a child harness that inherits them believes it runs inside that session, so they are removed unless `strip_session_markers` is false. On claude the values the SDK itself sets for the CLI, such as its entrypoint, stay.

### Deadline and cancel

The launcher makes the harness a session leader. When the deadline passes, or `cancel` is set, no further harness start is allowed, and the harness, every process it started, and everything those left behind in their process group or session are sent SIGTERM and then SIGKILL, grandchildren included. A caller that is itself killed cannot do this, and the harness's tree then outlives it.

## Harness guard

`TESTED_VERSIONS` holds, per harness, the range of CLI versions this release was tested with: claude 2.1.280 to 2.1.284, codex 0.159.0. Before a turn the library reads the installed CLI's `--version`:

- older than the range: the turn is refused with failure kind `incompatible_harness`, and the message names the installed version and the tested one;
- newer than the range: the turn runs, and `result.warnings` names both versions;
- no CLI: `binary_missing`, as before.

## Session

`Session.fresh()` starts a conversation; on claude the id is chosen before launch so a killed turn is still addressable, on codex it is the thread id the app-server gives. `Session.resume(id)` continues one on either harness. `Session.pinned(id)` chooses the id and is claude-only.

## Result

| Field | Meaning |
|---|---|
| `ok` | the harness confirmed a completed turn; one that completed without a final message is `ok` with an empty `answer`, unless `output_schema` required one |
| `harness` | `claude` or `codex` |
| `answer` | the final text |
| `structured` | the parsed answer when `output_schema` was given |
| `session_id` | claude session id or codex thread id, set whenever known, including after a kill |
| `model` | the model that ran when claude reports it; on codex the slug a family resolved to; else the requested one |
| `cost_usd` | claude's own estimate; `None` on codex |
| `duration_ms`, `num_turns` | as reported; `num_turns` is `None` on codex |
| `tokens` | `input`, `output`, `cache_read`, `cache_creation` for this turn |
| `permission_denials` | claude's list; empty on codex |
| `failure` | `None` when `ok`; else `kind` and the harness's own `message` |
| `exit_code` | the exit code the claude SDK reported for a failed process, else `None` |
| `stderr_tail` | the harness's last stderr lines, for a person |
| `command` | the CLI command line the launcher started |
| `raw` | claude's result message, or codex's `{"turn", "items", "errors"}` |
| `harness_version` | the installed CLI version the guard read |
| `warnings` | the guard's warning when the CLI is newer than tested |

### Failure kinds

`quota`, `model_refused`, `not_authenticated`, `timeout`, `cancelled`, `binary_missing`, `max_turns`, `budget`, `invalid_output`, `error`, `crash`, `incompatible_harness`. The harness's own verdict is read first (claude's result subtype, codex's `codexErrorInfo`), then an HTTP status it reports, then its sentence: a spent quota before a refused model, because a subscription spent on one model says both and only one of them is a pause. A claude turn that times out while the harness is retrying a 401 or a 429 is `not_authenticated` or `quota`. `failure.retryable` is true for the kinds where running the same turn again later can succeed.

## Changelog

### 0.9.0

Profiles can be kept in a store. Additive: everything in 0.8.0 works unchanged, the file lookup included.

- The table `harness_profiles`, shipped as store steps a caller applies under the ledger `harness`: one row per version of a profile at the `global` or a `project` scope, one active version per scope and name.
- `save_profile`, `remove_profile`, `active_profile`, `profile_version`, `profile_history` and `stored_profiles` on the caller's connection, inside its transaction. A save is judged whole by the profile rules, creates nothing when it is identical to the active version, and is refused with `ProfileConflict` when another writer replaced the version it read.
- `resolve_profile`, `resolve_among` and `resolve_profiles`: a name resolves to the project's version, then the global one, then the shipped profile, and the answer names the layer and the version that won.
- `check_spec`, `check_profile_name`, `profile_text` and `shipped_profile_names`.
- Importing the package no longer imports either SDK; a run imports the one its harness needs.

### 0.8.0

A codex profile can name a model family. Additive: every literal model slug runs exactly as in 0.7.0.

- On codex, a `model` made only of lowercase letters (`sol`, `luna`, `astra`) is a family: before the turn the runner reads codex's own catalog through the app-server's `model/list` and runs the newest listed `gpt-<version>-<family>` model, newest by the version in the slug. `Result.model` is the slug that ran.
- A family with no listed model, or a catalog that cannot be read, fails the run (`model_refused` or `error`) naming the family; no model is run and there is no fallback.
- `codex-read`, `codex-act` and `codex-read-sandboxed` name the family `sol` in place of `gpt-6.1-sol`, so a newer sol model needs no release. Today it resolves to `gpt-6.1-sol`.

### 0.7.0

What a long-running service needs to launch turns with no launch code of its own. Additive: everything in 0.6.0 works unchanged, except that a silent codex turn is no longer a failure.

- `run(on_start=...)` receives a `Started` (`harness`, `pid`, `session_id`) once the harness process runs, before the first event, so a service can end an orphaned tree after its own crash or amend a turn still running.
- A codex turn that completes with no error and no final message is `ok` with an empty `answer`, as on claude; it was an `error` failure. With `output_schema` it is still `invalid_output`.
- `bypass_hook_trust` is honoured on codex: the project's hooks run on any machine with nothing trusted beforehand and nothing persisted. It was refused.
- `python -m callva.harness_runner knobs` describes every knob, and `profiles check FILE` validates a profile file.

### 0.6.0

The shipped profiles change model, so a run on them runs a different model; nothing else in them, and no API, changes.

- `claude-read`, `claude-act` and `claude-read-sandboxed` name `opus`, Claude Code's alias for its newest Opus, in place of `claude-opus-5-5`. They follow the newest Opus without a release.
- `codex-read`, `codex-act` and `codex-read-sandboxed` name `gpt-6.1-sol`, the Codex catalog's current default, in place of `gpt-6-sol`. Codex offers no alias, so this is a pin and moving it takes a release.
- A caller that needs a fixed model keeps its own profile file of the same name, which comes before the shipped one, or replaces `model` for one run.

### 0.5.0

A profile is one harness, so a caller names a profile and never a harness. What a 0.4.0 consumer must change:

- A profile file is flat: `harness = "claude"` or `"codex"` at the top and that harness's knobs beside it. A file with `[claude]` or `[codex]` tables is refused; split it into one file per harness, moving each table's knobs to the top of its file.
- `find_profile(name, harness, folders)` becomes `find_profile(name, folders)`; the name picks the harness. A harness passed to it is refused with a message naming the replacement call.
- `ProfileFile.profile(harness)` becomes `ProfileFile.profile()`, `ProfileFile.harnesses()` becomes `ProfileFile.harness`, `ProfileFile.tables()` becomes `ProfileFile.knobs()`, and `ProfileListing.harnesses` becomes `ProfileListing.harness`, which the `profiles` listing prints as `harness`.
- The shipped `read`, `act` and `read-sandboxed` are split into `claude-read`, `codex-read`, `claude-act`, `codex-act`, `claude-read-sandboxed` and `codex-read-sandboxed`, each with exactly the knobs of the matching table; asking for an old name finds nothing and the message names the new ones.

### 0.4.0

Profiles by name: profile files, `find_profile`, `find_profile_file`, `list_profiles`, `machine_folder`, the `profiles` command, and three shipped profiles, `read`, `act` and `read-sandboxed`. Additive: everything in 0.3.0 works unchanged.

### 0.3.0

The library is renamed and speaks of harnesses, profiles and runs; its behaviour is exactly 0.2.0's. What a 0.2.0 consumer must change: depend on `callva-harness-runner` and import `callva.harness_runner`; write `harness` where a profile said `engine`; read `Result.harness` and `Result.harness_version`; branch on `incompatible_harness`; import `HARNESSES`. The table at the top maps every old name.

### 0.2.0

The engines now run through the vendors' SDKs, and the profile is explicit knobs. What a 0.1.x consumer must change:

- `fence` is gone, and so is its default. A 0.1.x profile that named no fence ran fenced to `read`; the same profile now runs with the engine's own defaults. State what the turn may do with `tools`, `allowed_tools`, `disallowed_tools`, `permission_mode`, `setting_sources`, `settings`, `mcp_config` and `strict_mcp` on claude, and `codex_config` and `approval_policy` on codex. `Profile.from_dict` refuses a `fence` key with a message naming these knobs, and `Profile.fence` no longer exists.
- `allow_tools` becomes `tools` plus `allowed_tools`. `env` (`allow`, `deny`, `set`) becomes `env_set`, `env_remove` and `strip_session_markers`; there is no allow-list. `extra_args` becomes `claude_extra_args` (a mapping) or `codex_extra_args`. `EnvPolicy`, `DEFAULT_DENY` and `FENCES` are no longer exported.
- `run()` loses `schema` (now the profile's `output_schema`), `name` (now the profile's `name`) and `prompt_via` (the prompt always travels over the SDK's own channel).
- A knob the engine cannot honour is refused instead of ignored: `budget_usd` on codex, `service_tier` on claude.
- `on_event` receives SDK messages (claude) and app-server notifications (codex) instead of CLI JSON lines. `raw` changes shape the same way, `command` is the command line the launcher started, and `exit_code` is `None` unless the claude SDK reported one.
- A CLI older than the tested range is refused with the new kind `incompatible_engine`; `Result` gains `engine_version` and `warnings`.
- Two dependencies arrive, about 0.5 GB.

Unchanged: `Profile.from_dict`, `Session.fresh`, `Session.pinned`, `Session.resume`, `run(prompt, profile, cwd, session=, environ=, extra_env=, on_event=, cancel=)`, `FailureKind`, and every 0.1.x `Result` field.

## Versioning

The major stays 0. A breaking change or a new feature bumps the minor; a fix bumps the patch. A changed shipped profile is a change to what a run does, so it is at least a minor.

## License

Apache-2.0.
