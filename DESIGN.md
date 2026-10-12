# Design

`callva-harness-runner` runs one turn of an agent harness from a profile and hands back one result. It exists because the same fifty lines of launch-and-parse code had been written nine times across one owner's tools, each with a different bug, and a tenth was about to be written.

## 1. Scope

One turn: a prompt goes in, a harness runs to its end or its deadline, one `Result` comes out. The library runs the harness through its vendor's SDK against the CLI installed on the machine, configures it only from the profile's explicit knobs, keeps every process it starts under the deadline, reads the harness's report, and classifies a failure by what the harness said. That is the whole job.

### Non-goals

It does not schedule, queue, retry, or loop. It holds no ledger of runs, no session store, no conversation memory, and it opens no database: the one table it defines, the stored profiles of §4, lives in the caller's store, migrated by the caller and read and written on the caller's connection. It renders no progress; it hands events to a callback and the caller renders. It does not know what a task, a message or a job is. It never decides what a turn may do: the profile does. It ships six reusable profiles as package data, three per harness, which a caller chooses by name and can shadow with its own.

## 2. Names

The vocabulary: a harness is the vendor's agent runtime (Claude Code, the Codex CLI); a profile is one harness with its settings; a run is one turn. The library holds no queue and no long-lived consumer of work; those belong to its callers.

The distribution is `callva-harness-runner` and the import path is `callva.harness_runner`: the distribution name is the import path with dashes, the same rule `callva-livekit` follows, and `callva` is a namespace package with no `__init__.py` so the two coexist in one environment. It was `callva-agentworker` (`callva.agentworker`) up to 0.2.0; the README maps each old name to its new one, and every old name is refused with a message naming the new one. The three nouns a caller uses are `Profile` (how to run), `Session` (which conversation), and `Result` (what came back); `Started` is what a caller learns while the turn runs. A profile kept in a store is a `StoredProfile` (one version), and what a name resolved to there is a `ResolvedProfile`. `run()` is the one verb. `probe()` answers whether a harness is present.

## 3. Harnesses

`claude` is Claude Code through `claude-agent-sdk`; `codex` is Codex through `openai-codex`, which drives `codex app-server`. Both are dependencies pinned exactly, because an SDK release changes the protocol it speaks and the flags it passes. Importing the package imports neither SDK: a run imports the one its harness needs, so a caller that only reads, checks or stores profiles, as a migration on every connection does, never pays for loading them.

Both SDKs bundle a CLI, and the library never runs it: it always passes the installed CLI (`cli_path` on claude, the `app-server` launch on codex). The CLI is the profile's `cli_path`, or else the first `claude` or `codex` on the caller's `PATH`, then in the places a login shell would have added and a launchd job does not: `~/.local/bin`, fnm and nvm node directories, Homebrew. Codex is started at the file a symlinked install points to, because it finds its companion executables beside the path it was started from.

Claude runs on the `claude_code` system-prompt preset unless the profile sets `system_prompt`. The SDK's own default is a minimal prompt that is not Claude Code's; the preset keeps the prompt a `claude -p` turn has. `system_prompt` replaces the whole prompt on either harness (claude's preset, codex's base instructions), for a caller that is not a coding agent and should not pay for one's prompt; `append_system_prompt` adds to claude's preset and is codex's developer instructions. On claude the two are refused together: a replaced prompt has nothing to append to, and one knob that holds the whole text is the plainer reading. Measured on claude 2.1.296 and codex 0.162.1 with a one-word answer and a one-sentence `system_prompt`: a claude turn (haiku, no tools, no setting sources, strict MCP) took 574 prompt tokens against 3,551 on the preset, and a codex turn 10,869 input tokens against 14,383 on codex's own instructions; the rest of codex's prompt is its tool definitions and environment context, which `system_prompt` does not reach.

Codex's turn is read from the turn's notification stream, not from `thread.run()`, which raises on a failed turn and drops `codexErrorInfo`.

## 4. Profile

A `Profile` is a frozen, flat record of explicit knobs. There is no inheritance and no named fence. A knob at its default leaves the harness's own default in place. A knob one harness cannot honour is refused for that harness when the profile is built, with a `ProfileError` naming the knob; it is never ignored while the turn runs. Each knob's docstring on `Profile` is the home of what it does on each harness, and the README carries the same table for readers of the package page.

The knobs: `harness`, `cli_path`, `model`, `effort`, `thinking`, `max_turns`, `timeout_seconds`, `budget_usd`; `tools`, `allowed_tools`, `disallowed_tools`, `permission_mode`, `setting_sources`; `settings`, `mcp_config`, `strict_mcp`; `add_dirs`, `system_prompt`, `append_system_prompt`, `output_schema`, `name`, `persist_session`; `env_set`, `env_remove`, `strip_session_markers`; `codex_config`, `approval_policy`, `service_tier`, `bypass_hook_trust`; `claude_extra_args`, `codex_extra_args`.

`Profile.from_dict()` validates against `profile.schema.json`, which ships in the package so that every consumer's configuration file is checked by the same rule. Unknown keys are refused, and the 0.1.x keys (`fence`, `allow_tools`, `env`, `extra_args`) are refused with a message naming the knobs that replace them.

`bypass_hook_trust` runs a codex project's hooks from the profile alone, on a machine that has never trusted the project or its hooks, and persists nothing. Codex loads a project's `.codex` layer, `hooks.json` included, only for a trusted project, and runs a loaded hook only when `config.toml` holds its trusted hash (`hooks.state."<hooks.json path>:<event>:<group>:<handler>".trusted_hash`, a hash of the handler that `hooks/list` reports as `currentHash`); an interactive session writes both. The knob sets neither in `config.toml`. It adds two keys to the thread's config, on start and on resume: `projects.<root>.trust_level = "trusted"` for the project root codex keys trust by (the nearest directory at or above the working directory holding `.git`, else the working directory, under its path as given and as resolved), and `bypass_hook_trust = true`, which the app-server accepts as a thread override although `config.toml` does not. Measured on 0.159.0: with both, a never-trusted project's `SessionStart` hook runs and `config.toml` is byte-identical afterwards; with the bypass alone the hook is not even loaded; the CLI's `--dangerously-bypass-hook-trust` before `app-server` does neither. Trusting the project also loads its `.codex/config.toml`, as an interactively trusted project would. On claude the knob is refused: claude has no hook trust.

`python -m callva.harness_runner knobs [--harness claude|codex]` prints every knob with its type, allowed values, default and what it does on each harness, read at runtime from the knob's homes: type and allowed values from `profile.schema.json`, the default from the `Profile` field, and the meaning from the field's docstring, split at its `Claude:`, `Codex:` and `Both:` labels. A docstring therefore opens with what holds on both harnesses and labels what differs. `profiles check FILE` reads one profile file as `find_profile` would and prints `ok` with its harness, or the refusal naming the knob with exit status 2.

### Model families

On codex a `model` made only of lowercase letters (`sol`, `luna`, `astra`) is a model family; any other value is a literal slug and is passed to codex as given, with no lookup, as every slug in the Codex catalog is (`gpt-6.1-sol`, `gpt-5.5`, `codex-auto-review`). Codex has no alias of its own for its newest model, and its default (the catalog's first entry) may be another family, so a family is resolved by the runner: before the thread starts or resumes, on the app-server connection the turn uses, `model/list` with hidden models included gives codex's own catalog; the entries not hidden whose slug is `gpt-<version>-<family>` are the family, and the newest by the version in the slug (compared as numbers: 6.1 > 6 > 5.6), not by catalog order, is the thread's model and `Result.model`. The per-client `models_cache.json` is never read. A family with no listed model is a `model_refused` failure and a catalog that cannot be read is an `error` failure, both naming the family and saying no model was run; neither falls back to the codex default or to any other model. On claude every `model` is passed to `--model` as given, since Claude Code resolves its own aliases.

### Environment

Both SDKs merge the profile's variables over the environment the Python process has, and neither can remove one. The library therefore puts a launcher of its own where the SDK expects the CLI: a two-line shell script that runs `_launcher.py` with `python -I -S`, which removes the variables the turn removes and `exec`s the real CLI. What reaches the launcher is a spec of names and paths; a variable's value only ever travels through the SDK's own env option.

The harness's environment is the caller's `environ` (this process's by default), with the profile's `env_remove` globs and, unless `strip_session_markers` is false, `SESSION_MARKERS` removed, and with `env_set` and the call's `extra_env` set on top. A variable in `env_set` is never removed, and a profile that both sets and removes one name is refused. On claude the variables the SDK itself sets for the CLI (its entrypoint, its version, its session-state flag, `PWD`) are the SDK's values and are not removed.

### Profiles by name

A profile is one harness with its settings, and a profile file holds exactly one: flat TOML with a required top-level `harness` and that harness's knobs beside it, the same shape `Profile.from_dict` takes. A caller names a profile and never a harness, so `find_profile(name, folders)` takes no harness and refuses one passed the way 0.4.0 took it; a positional harness string would otherwise land in `folders` and be searched one character at a time. A file with a `[claude]` or `[codex]` table, or without a top-level `harness`, is refused with a message naming the flat form; there is no reader for any other form. A name resolves to the first `NAME.toml` in the caller's folders (in the order given), then the machine folder (`$XDG_CONFIG_HOME/callva-harness-runner/profiles`, else `~/.config/...`), then the shipped profiles, read with `importlib.resources` so they need no install step. The file found is used whole, with the harness it names; a found file that cannot be read as a profile is an error and does not fall through, because a name that silently resolved to another source's profile would run with a fence nobody chose. Names are one plain file-name segment, so a name can never climb out of a folder, and a stored profile's name follows the same rule. A caller that keeps its profiles in a store resolves a name there instead, by scope, as the next section says; the two lookups are separate and neither reads the other's source.

The shipped profiles are `claude-read`, `claude-act`, `claude-read-sandboxed`, `codex-read`, `codex-act` and `codex-read-sandboxed`; the harness is part of each name. A lookup of `read`, `act` or `read-sandboxed` that finds no file names the two harness-prefixed profiles instead. Each shipped profile is a change to what a run does, so each is proven by a live run on its harness before a release that changes it. The claude profiles follow the newest Opus through `opus`, Claude Code's alias for it, so a newer Opus needs no release; codex has no alias, so the codex profiles name the model family `sol`, which the runner resolves at run time (see Model families), so a newer sol model needs no release either. `claude-read-sandboxed` fences the working directory with the permission rule `Edit(./**)`: Claude Code resolves a `./` rule against the working directory and merges Edit deny rules into the sandbox's write denials, while a sandbox `denyWrite` path resolves against the settings root (for inline `--settings`, the session's temporary directory), so `denyWrite: ["."]` fences the wrong place. Measured on Claude Code 2.1.280: with `Edit(./**)` a write in the target and in its subdirectories is blocked, and a write beside the target, in the temporary directory and in `~/.cache` is allowed, exactly as with the target's absolute path. `claude-read-sandboxed` also sets `disableAllHooks`: the target's hooks run outside the sandbox, so a hook is a write path the sandbox cannot fence; context a hook would build is read as it is on disk, while CLAUDE.md and rules still load. `claude-read` runs the target's hooks.

### Profiles in a store

A caller that keeps its profiles in a database rather than in files keeps them in the table `harness_profiles`, which this library defines and owns. One row is one version of one profile at one scope: `global`, or `project` with the project's id in `scope_id`. `spec` is the whole profile, `harness` included, in the shape `Profile.from_dict` takes. Exactly one version of a name is active per scope, which the store enforces with a unique index (a global row's null `scope_id` is keyed as the empty string, since nulls are distinct in a unique index). Who saved a version is `created_by`, as the writing tool resolved it.

The library opens no connection and needs no database driver. It ships the table as store steps, `PROFILE_STORE_STEPS`, plain SQL that the caller applies with the database library it already uses under the ledger `PROFILE_STORE_OWNER` (`harness`; every object the steps create is named `harness_*`) at `PROFILE_STORE_MAJOR`.`PROFILE_STORE_MINOR`, and a caller whose own tables reference this one applies these steps before its own. Every function here takes the caller's open DB-API connection (psycopg's `%s` parameter style) and, optionally, the schema to qualify the table with; a function that writes runs inside the caller's transaction and leaves the commit or the rollback to it, so a caller can save several profiles all or none. An applied step is never edited: a change to the table is a new step and a new store version.

A change never edits a row. `save_profile` judges `spec` whole first, with `check_spec`: what a flat profile file must hold and what `Profile.from_dict` accepts, an unknown key refused, and a knob a file cannot say (a null) refused. A save identical to the active version, compared in code, creates none and says so. Otherwise it inserts the new version and clears `active` on the version the writer read, `... where id = <read> and active`, in the caller's transaction; when that clears no row, or `base` (the version the writer read) is not the one active, or the unique index refuses a second active row, another writer came first and the save is refused with `ProfileConflict`, so the writer reads again. `remove_profile` clears the active version; a name with no active version at a scope is not held there. `profile_history` is every version of a name at a scope by `created_at`, the clock at each insert rather than the transaction's start, so versions saved in one transaction still order as they were saved; no version is ever deleted.

A name resolves to the most specific layer holding an active version: the project's, then the global one, then the profile this package ships, which stays in its code as the lowest layer. `resolve_profile` answers a `ResolvedProfile` naming the layer that won, the id of the version that won (`None` for a shipped profile), its spec, and the less specific layers it shadows; `resolve_among` resolves against active versions the caller already read with `stored_profiles`, so a caller that reads them once resolves every name without another query. This resolution does not read files: the file lookup above is unchanged and is a separate way to name a profile, for callers that keep their profiles as files. `profile_text` writes a spec as the flat file it would be, so a stored version can be shown, edited and saved back, and reads back as the same spec.

## 5. Session

`Session.fresh()` starts a new conversation. On claude the library generates the id before launch and pins it, so a turn that is killed is still addressable. On codex the id is the thread id the app-server gives, reported even when the turn is later killed. `Session.pinned(id)` lets the caller choose the id; it is claude-only and refused on codex. `Session.resume(id)` continues an existing conversation on either harness; on codex the thread is resumed with the profile's model, config, base and developer instructions, service tier and approval policy.

`persist_session = false` writes the turn's session nowhere, so it appears in neither harness's session list and cannot be resumed: claude runs with `--no-session-persistence`, and codex starts the thread ephemeral. Claude can still resume a persisted session under it and writes nothing back to it; codex can make only a new thread ephemeral, so `Session.resume` with persistence off is refused on codex with a `ValueError`, as a pinned session is.

## 6. Harness guard

`TESTED_VERSIONS` declares, per harness, the inclusive range of CLI versions the release was tested with: the low end is the CLI installed where the release was tested, the high end is the newer of the CLI the pinned SDK bundles and an installed CLI a live run of the release passed on. Before every turn the installed CLI's `--version` is read (cached per binary file):

- below the range, or unreadable: the turn is refused with `incompatible_harness`, naming both versions;
- above the range: the turn runs and `Result.warnings` names both versions;
- no CLI: `binary_missing`, as it was.

## 7. Running

The turn runs on a thread of its own; the calling thread holds the deadline and the cancel. The launcher records the harness's pid and command line, makes the harness a session leader, and, once the stop file no longer stops it, records that the harness is starting.

`run(on_start=...)` receives a `Started` (`harness`, `pid`, `session_id`) exactly once for a run whose harness process started, and never for one where none did (no CLI, a refused version, a stop before the start). `pid` is the harness the launcher started, the leader of its own session and process group, so a service that restarts after its own crash can end an orphaned tree. `session_id` is claude's pinned or resumed id, or codex's thread id once `thread/start` or `thread/resume` has answered. It is called as soon as both are known, from the watching thread or from the turn's thread, and returns before the first `on_event`; a harness that ended before codex gave a thread id is reported once when the turn ends, with `session_id` `None`. An exception it raises is swallowed: the callback never ends or alters the turn. When the deadline passes or the caller's `threading.Event` is set, the library first writes a stop file that the launcher checks before any later start, then finds the harness's tree three ways: by parentage from the harness, by the process groups of what it found, and by the sessions of what it found, the harness's own included. The harnesses give each tool command a process group of its own, and a command that backgrounds a child and exits leaves that child with its group and session but a new parent, so parentage alone would miss it. The tree gets SIGTERM, three seconds, then SIGKILL; the caller's own group and session are never touched. Then the SDK is closed.

A caller killed outright cannot do any of this, and the harness's tree outlives it.

## 8. Reading

Claude: the SDK's `ResultMessage` gives `result` as the answer (its `errors` when there is none), `structured_output`, `session_id`, `total_cost_usd`, `duration_ms`, `num_turns`, the four token counts from `usage`, the model that ran from `modelUsage`, `permission_denials`, and `subtype` with `is_error` as the verdict. The SDK raises after it hands over an error result; the result is the report and the raise is not. The last `api_retry` event is kept, because a turn that dies by the deadline while the harness retries a 401 or a 429 has already said why.

Codex: the turn's `item/completed` notifications give the answer (the last agent message), `thread/tokenUsage/updated` gives this turn's tokens (the thread's running total less what it held before the turn), and `turn/completed` gives the status, the duration and the error with its `codexErrorInfo`. Cost is `None`: codex reports none under ChatGPT authentication.

When `output_schema` is given and the harness returned no structured field, the answer text is parsed as JSON; if that fails, the turn is an `invalid_output` failure with the text kept.

## 9. Result

| Field | Meaning |
|---|---|
| `ok` | the harness confirmed a completed turn: claude `subtype == "success"` and `is_error` false; codex status `completed` and no error. On either harness a completed turn may end without a final message, as a worker that answered by a side channel does: it is `ok` with an empty `answer`, unless `output_schema` required one. |
| `harness` | which harness ran. |
| `answer` | the final text, possibly empty on failure. |
| `structured` | the parsed structured answer, or `None`. |
| `session_id` | claude session id or codex thread id; set whenever it is known, including after a kill. |
| `model` | the model that actually ran when claude reports it; on codex the slug a model family resolved to; else the requested one, else `None`. |
| `cost_usd`, `duration_ms`, `num_turns`, `tokens` | as reported; `None` where the harness does not say. |
| `permission_denials` | claude's list, empty on codex. |
| `failure` | `None` when `ok`; otherwise a kind and the harness's own sentence. |
| `exit_code` | the exit code the claude SDK reported for a failed process, else `None`. |
| `stderr_tail` | the last lines of the harness's stderr, for a person. |
| `command` | the CLI command line the launcher started. |
| `raw` | claude's result message as a dict, or codex's `{"turn", "items", "errors"}`, for anything this table left out. |
| `harness_version` | the CLI version the guard read. |
| `warnings` | the guard's warning when the CLI is newer than tested. |

## 10. Failure kinds

`quota`: the subscription or rate limit is spent; the turn can be retried later without changing anything. `model_refused`: the harness will not run this model, or a codex model family has no listed model; retrying changes nothing. `not_authenticated`: no login or an invalid key. `timeout`: the deadline passed and the tree was killed. `cancelled`: the caller's event was set. `binary_missing`: no harness found. `max_turns`, `budget`: the harness ended the turn on its own limit. `invalid_output`: a schema was required and nothing parsed. `error`: the harness reported a failure this table does not name; the sentence is in `message`. `crash`: the harness ended without a report. `incompatible_harness`: the installed CLI is older than the tested range, or its version cannot be read.

Classification reads the harness's own verdict first (claude's `error_max_turns` and budget subtypes; codex's `codexErrorInfo`: `usageLimitExceeded` and `rateLimitExceeded` are `quota`, `unauthorized` is `not_authenticated`, `sessionBudgetExceeded` is `budget`), then an HTTP status the harness reports (claude's `api_error_status` or its last `api_retry` status, codex's `httpStatusCode`: 429 is `quota`, 401 is `not_authenticated`), then the sentence, in a fixed order: quota before model refusal, because a spent subscription on one model says both, and only one of them is a pause. Codex wraps a provider's HTTP error as JSON; the sentence inside it is what is read and kept.

## 11. Constraints worth knowing

The cost claude reports is its own estimate and includes subagents; a turn that dies reports no cost at all, so an accumulated total is a floor. Codex under ChatGPT authentication reports tokens but no cost. A profile with no knobs runs the harness exactly as the machine configures it: user settings, hooks and MCP servers included. Hooks on codex run only when the machine trusts them, unless the profile sets `bypass_hook_trust`.

## 12. Versioning

The major stays 0. A breaking change or a new feature bumps the minor; a fix bumps the patch. A changed shipped profile is a change to what a run does, so it is at least a minor. `callva/harness_runner/version.py` is the one home of the number.

## 13. Later

Progress rendering helpers, if two consumers end up writing the same one. Cost lookup for codex by token price table, if anyone needs a number rather than `None`. A parent watch in the launcher, if a crashed caller's orphaned tree becomes a real problem.
