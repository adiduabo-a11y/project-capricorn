# Project Capricorn - Companion Agent Plan

**Status:** Active - revision 2. Milestones 0 to 6 are done; milestone 7 (hardening and release) has not started; milestone 8 (character pass) is planned and has no code yet.
**Date:** 14 November 2026
**Scope:** Reposition Project Capricorn from a companion with tools into a capable desktop companion-agent.

---

## 0. What changed in revision 2

Revision 1 set the right direction but would have failed on contact with the code. Every change below is a correction, not a change of strategy.

| # | Problem in rev 1 | Fix in rev 2 |
|---|---|---|
| 1 | `fs.patch` demands exact text, but the model only ever sees `preview(2000)` of any tool result | New Milestone 1: ranged reads, `fs.grep`, per-tool observation budgets, mid-turn context guard |
| 2 | No search tool at all; the agent is expected to find symbols by guessing paths | `fs.grep` added as a first-class tool in M1 |
| 3 | Per-tool `max_calls_per_turn` (8 by default) will refuse the read-heavy flow M3 depends on | Turn-level call budget in M1; per-tool caps become guards, not the primary limit |
| 4 | "Inside the workspace means trusted" never reconciled with the existing `policy.allow_paths` | Explicit precedence order and persistence rule in sections 4.3 and 4.4 |
| 5 | `policy._BLOCKED_FRAGMENTS` is unanchored substring matching; would block legitimate detected commands | Allowlist-first evaluation and an anchored blocklist in section 4.4 |
| 6 | `fs.write` normalizes to UTF-8/LF, contradicting the careful BOM/CRLF handling specified for `fs.patch` | `fs.write` scoped to new files; overwrite preserves encoding |
| 7 | Exact-match patching with no staleness check; an editor save between read and patch corrupts silently | Per-turn read-hash guard held by the tool context (M3) |
| 8 | `Msg` content-block refactor deferred to M6, after M3/M4 built on `content: str` | Narrow `attachments` seam added early in M1; vision UX still ships later |
| 9 | Cancelling a turn does not kill a running test process | `ToolContext.cancel` wired and process-tree kill specified (M1/M3) |
| 10 | No backup retention; `before_text` grows the shard without bound | Size cap, spill to disk, scheduler pruning (M3) |
| 11 | Trust persistence across restart implied but never stated | Stated explicitly, with a config switch |
| 12 | Evals had no pass thresholds | Numeric gates in M7 |
| 13 | Memory has no project dimension, so project conventions would leak across projects | `memories.workspace_id` plus `.capri/conventions.md` (M2) |
| 14 | Unclear which `project.*` entries are model tools and which are RPCs | Section 6 splits them |
| 15 | The Changes view assumed a file watcher that does not exist | Derived from `file_backups` for the active task, stated in M3 |
| 16 | Windows symlink tests need privileges | Junction-based tests with explicit skip |

Renumbering: old M0 stays M0; old M1 becomes M2; **new M1 is inserted**; old M2 becomes M3, M3 becomes M4, M4 becomes M5, M5 becomes M6, M6 becomes M7.

The reason for the insertion is finding 1. Rev 1 put the patch tool before the plumbing that lets the model see a file, so it would have shipped a workbench that fails on any real source file and blamed the patch tool for it.

---

## 1. Product contract

The core decision is unchanged: **do not build a second agent.** One Capri, one conversation, one model, one tool loop. Add project context and a real work surface around it.

Capri is:

- a persistent companion with a voice, avatar and memory
- a desktop agent that can inspect files, write code, run commands, see the screen and operate approved applications
- one continuous relationship, not separate chat mode and coder mode

The operating rule:

> **Talk when talking is enough. Act when action is needed. Never act silently on something that changes the user's machine.**

The presence layer is the reason the app feels like a companion. The agent layer is the reason it is useful.

---

## 2. Current state, verified

These were read from the code, not inferred. Line numbers are from the current tree.

| Area | Actual state | Gap |
|---|---|---|
| Persona | `packs/prompts/kyoko/system.md` opens with "You are not an assistant." | Fights the product |
| Messages | `types.py:14` - `Msg.__init__(role, content: str)`; dict, string content only | No image or attachment path |
| Observations | `loop.py:440` - every tool result reaches the model as `result.preview(2000)` | A 40 KB file is seen as 2 KB |
| Context | `AgentCfg`: `max_steps=24`, `context_budget_tokens=12000`, `history_messages=30`. Budget applied once in `_history()`; the in-turn `messages` list is append-only and never re-budgeted (`loop.py:230-285`) | A 24-step turn can overflow the window with no warning |
| Tool caps | `ToolSpec.max_calls_per_turn=8` enforced at `loop.py:331` | Eight reads and the turn is stuck |
| Tools | `fs.read`, `fs.list`, `fs.write`, `shell.run`, screen, Blackwall | No search, no patch, no diff, no undo |
| `fs.read` | 400 KB cap, whole file, no line numbers, `errors="replace"` on decode | Truncation invisible to the model; mojibake possible |
| `fs.write` | UTF-8, `newline=""`, no BOM; overwrites existing files | Silently rewrites CRLF/BOM files |
| Policy paths | `policy.py:88` - `os.path.abspath`, not `realpath` | Symlink and junction escape is real |
| Policy trust | `policy.py:126` - trust comes from `cfg.allow_paths` only; `workspace` affects relative paths only | No per-project trust object |
| Shell | `shell.py` kills on timeout only; `loop.py:411` builds `ToolContext` without a cancel event | Cancel orphans the process tree |
| Screenshots | `screen.observe` produces a redacted image in `ToolResult.data`; the loop sends text only | The vision model never sees the screen |
| Migrations | `0001_init.sql`, `0002_blackwall.sql` | `0003_work.sql` free |

### The three findings that drive the plan

1. **The model cannot see what it is asked to edit.** `fs.patch` requires exact text; the loop forwards 2000 characters. This is why M1 exists.
2. **Nothing re-checks the context budget during a turn.** `_history()` budgets once, then up to 24 observations are appended with no accounting. On a small local model this fails as silent truncation at the endpoint, which looks like the model forgetting instructions.
3. **Cancellation reaches the coroutine but not the process.** `turn.cancel` calls `task.cancel()` and `CancelledError` propagates, but `proc.kill()` only runs on `TimeoutError`. "Cancel the test" leaves the test running.

---

## 3. Target architecture

```text
Chat / voice
    |
    v
TurnRunner  <---- Project + Task context
    |
    v
Planner -> one tool -> observation (budgeted) -> planner
    |
    v
Tool registry
  fs.read/list/grep/write/patch/undo
  shell.run
  project.inspect/tree
  screen.observe/windows/focus/launch/clipboard/act
  Blackwall tools
    |
    v
Policy + confirmations + budget guard + traces
    |
    v
SQLite shard
  workspaces, tasks, file_backups, artifacts, traces, memory
```

Still exactly one planner and one call pattern: choose a tool, generate its arguments, run it, observe, repeat.

---

## 4. Cross-cutting decisions

Decided here so the milestones do not each reinvent them.

### 4.1 Context and observation budget

Observations are the agent's eyes. A single flat `preview(2000)` is wrong: file contents need room, and a failed test needs its tail.

Per-tool observation budgets, in characters:

| Tool | Budget | Shape |
|---|---|---|
| `fs.read` | 8000 | head, with line numbers, plus `[N more lines]` |
| `fs.grep` | 4000 | matches, capped, plus total |
| `shell.run` | 6000 | first 1000 + last 5000, marked as elided |
| `project.inspect` | 1500 | compact summary |
| `project.tree` | 2500 | capped entry list |
| `screen.*` | 3000 | unchanged text path |
| everything else | 1200 | as today |

Rules:

- A tool declares its observation shape in `ToolSpec` (`observation: Literal["default","file","command","grep","tree","summary","screen"]`), so the loop does not hardcode tool names.
- Full output never enters the prompt. It goes to an artifact (section 7), and the observation carries a one-line pointer.
- The loop keeps a running token estimate for the in-turn message list. Before each planner call, if the estimate exceeds the budget, it replaces the oldest observation bodies with one-line summaries until it fits. It never drops the system prompt, the first user message, or the current user request.
- `context_budget_tokens` gets a profile override: `agent.work_context_budget_tokens` (default 24000) used when `policy.profile == "work"`. The effective budget is `min(configured, 0.8 * model_context)` when the model context is known.

Why this ordering matters: without the mid-turn guard, raising the observation budget makes things worse - the loop fills the window and the endpoint truncates from the front, which is where the persona and the rules live.

### 4.2 Message model

Do not turn `Msg.content` into a union. That ripples through the router, `session.add_message`, `_history`, memory replay and the GBNF path, and every one of them wants a string.

Instead:

```python
@dataclass(frozen=True)
class Attachment:
    kind: Literal["image"]
    data_url: str          # data:image/png;base64,...
    label: str             # "screen.observe 1920x1080, redacted"

class Msg(dict):
    def __init__(self, role: Role, content: str, attachments: list[Attachment] | None = None) -> None: ...
```

- `content` stays a string. It is what gets persisted, replayed and counted.
- `attachments` are transient. The router expands them to `image_url` content blocks at send time. `session.add_message` never sees them.
- If a message has attachments, `content` carries a text summary of what they show, so replay without images is still coherent.

This seam is small, lands in M1, and means M5 only adds the screenshot source and the capability switch.

### 4.3 Trust model

Three kinds of root, in precedence order:

1. **Deny paths** - always block, in every mode, for read and write.
2. **Active workspace** - the folder the user opened. Write trust inside it, granted by the act of opening.
3. **Allow paths** - `policy.allow_paths`, the existing ambient roots. Read allowed; writes to existing files still confirm.

Rules:

- Paths are canonicalized with `os.path.realpath` before any comparison, on both sides of the check. On Windows this resolves junctions as well as symlinks. If the target does not exist, canonicalize the nearest existing parent and append the remainder.
- A path that canonicalizes inside a denied root is blocked even if it was reached through an allowlisted root.
- The workspace is stored in the shard and is trusted on restart (`policy.trust_persisted_workspace = true`). Set it to `false` and the workspace reopens read-only until the user confirms again. State which mode is active in the UI.
- A workspace whose root is inside a denied path cannot be opened.
- `policy.workspace_edits: "allow" | "confirm"`, default `"allow"`. The conservative value keeps the workspace as a read root and requires a confirmation per write.
- Only the user can open a workspace. `project.open` is RPC-only and never appears in the tool registry.
- Switching is refused while a turn is active: `CapriServer.workspace` and `Policy.workspace` update together, under the same lock that starts turns.

### 4.4 Policy evaluation order

`Policy.check` for filesystem writes, in this order:

1. Canonicalize.
2. Deny list - `BLOCK`.
3. Active workspace, `workspace_edits == "allow"` - `ALLOW`, tier 1.
4. Existing file anywhere else - `CONFIRM`, tier 2.
5. New file inside an allow path - `ALLOW`, tier 1.
6. Otherwise `CONFIRM`, tier 2.

For `shell.run`:

1. Hard blocklist, **anchored on the command's first token(s)**, not a substring search: `format`, `diskpart`, `bcdedit`, `shutdown`, `reg delete`, `net user`, `cipher /w`, `vssadmin delete`, `wevtutil cl`, `rd /s`, `del /s`, `del /f` - `BLOCK`.
2. Exact match against the detected-command allowlist for the active workspace (command string and `cwd == workspace`, both exact) - `ALLOW`, tier 1.
3. Read-only prefixes (today's `_BENIGN_PREFIXES`, extended with `git show`, `git ls-files`, `git rev-parse`, `rg `) - `ALLOW`, tier 1.
4. Anything else - `CONFIRM`, tier 2.

The order matters: `dotnet format` and `git diff --format=%H` are legitimate detected commands and must not be caught by the `format` rule. Anchoring on the first token fixes rev 1's implicit problem without weakening the blocklist for the commands it was written for.

The detected-command allowlist is rebuilt by `project.inspect`, is never inferred from model output, and is cleared when the workspace changes.

### 4.5 Turn budgets

Replace the per-tool ceiling as the primary limit:

- `agent.max_tool_calls_per_turn = 40` (new), enforced in the loop alongside `max_steps`.
- `ToolSpec.max_calls_per_turn` stays as a guard against a single tool monopolizing a turn, but rises for read-heavy tools: `fs.read` 24, `fs.grep` 16, `fs.list` 16, `project.tree` 4, everything else unchanged.
- `agent.max_steps` for the work profile: 40. The ceiling is a safety valve, not a design constraint; tasks needing more become tasks (M4), not longer turns.
- Hitting a ceiling produces a paused task, not a cheerful "done".

---
## 5. Milestones

### Milestone 0 - Prompt and posture

**Status:** done. `packs/prompts/kyoko/system.md` opens with "a companion with
hands", keeps the clipped voice and the "what you do not do" section, and adds
the inspect-before-editing work ethic; `prompt.work_block` renders the project,
task, detected commands and capped conventions, and is empty outside a
workspace; `_PROFILE_GUIDANCE["work"]` sits beside the other profiles as
prompt guidance, not a second agent; `0003_work.sql` shipped the four tables and
the nullable `workspace_id` / `task_id` columns, and `agent.tool`, `agent.done`,
traces and messages carried them from that point on. Covered by
`services/core/tests/test_brain.py` (persona and block) and `test_work.py` +
`ws_smoke.py` (the ids travel).

**Size:** small - 1 to 2 days.

**Goal:** stop telling Capri she is not an assistant; give her a work ethic without changing her voice.

**Changes**

- Rewrite `packs/prompts/kyoko/system.md`:
  - "You are a companion with hands."
  - Keep the clipped, dry tone and the existing "what you do not do" section.
  - Remove the anti-assistant framing.
  - Add: inspect before editing, prefer small changes, verify claims with tools, report results plainly.
  - Add: when no action is needed, answer directly; do not call a tool for conversation.
- Extend `agent/prompt.py` with a compact work-context block, empty outside a workspace:
  - active project name and root
  - task title and status, if one exists
  - detected test, lint and build commands
  - conventions, capped at 2 KB
- Ship `0003_work.sql`: the `workspaces`, `tasks`, `file_backups` and `artifacts` tables, plus nullable `workspace_id` / `task_id` columns on memories, traces and messages. The services that fill them arrive in M2 and M3; the columns exist now so traces never need a retrofit.
- Carry `workspaceId` and `taskId` on `agent.tool` and `agent.done` events, on trace rows, and on persisted messages. They stay empty until a workspace is open.
- Add the work-profile guidance string next to the existing `_PROFILE_GUIDANCE` entries. Prompt guidance, not another agent.

**Acceptance**

- Existing core tests and renderer typecheck pass.
- Casual conversation produces no tool calls and no work-context block.
- A work request produces a work-oriented response and tool use.
- The new fields appear on events and traces without breaking existing renderers.

---

### Milestone 1 - Context plumbing

**Size:** medium - 3 to 5 days.

**Goal:** make the model able to see, search and remember enough to edit a real file. This is the milestone rev 1 was missing.

**Status:** done. `services/core/tests/test_context.py` covers the list under **Tests**.

#### 1A. Observation budgets

- Implement section 4.1: `ToolSpec.observation` shape, per-tool character budget, head/tail elision for commands, line-numbered file output.
- `agent.observation` config block (`ObservationCfg`): default 1200, file 8000, command 6000, grep 4000, tree 2500, summary 1500, screen 3000. Shaping and compaction live in `agent/observe.py`.
- The loop emits `agent.tool` with the budgeted observation in the existing shape, so the UI keeps working.

#### 1B. Mid-turn context guard

- Track an estimate of system plus messages inside the turn loop.
- Before each planner call, if over budget, replace the oldest observation bodies with one-line summaries until it fits. Keep the system prompt, the first user message, and the most recent two messages intact.
- Emit a `context.compacted` event once per turn when this happens, so the behavior is visible rather than mysterious.
- Add `agent.work_context_budget_tokens` (section 4.1).

#### 1C. `fs.read` improvements

- Add `start_line` and `end_line` (1-based, inclusive), default `1 .. min(total, 400)`.
- Always return line numbers, total lines, and `sha256` in `data`.
- Keep the 400 KB hard cap.
- Decode with `utf-8-sig` first. If that fails, still return the replacement-decoded preview, but prepend `[not valid UTF-8 - fs.patch will refuse this file]` so the model is not misled about what it can edit.
- Mark truncation explicitly and include the next `start_line` to request.

#### 1D. `fs.grep`

New tool, tier 0.

```json
{
  "pattern": "regex",
  "path": "optional dir or file, defaults to workspace root",
  "include": "optional glob, e.g. *.py",
  "case_sensitive": false,
  "max_matches": 80
}
```

- Text files only, UTF-8 decodable, NUL check per file.
- Skips the same excluded directories as the tree.
- Honors `.gitignore` at the search root when present; otherwise uses the built-in excludes.
- The search target goes through the same path classification as `fs.read`, so a denied root stays denied.
- Returns `path:line: text`, capped, plus a match count so the model knows when it is seeing a sample.
- Never recurses through symlinks or Windows junctions. `os.walk(followlinks=False)` still follows junctions, so the walk checks them itself with `os.path.isjunction`.

#### 1E. Attachment seam

- Implement section 4.2 in `types.py`, `models/router.py` and `agent/loop.py`. No user-visible change yet.
- One unit test that a message with an attachment serializes the way the router expects, and that `session.add_message` never receives attachment data.

#### 1F. Cancellation plumbing

- `TurnRunner._act` passes its cancel event into `ToolContext`. The old context built a fresh, never-set event, so `ctx.cancelled()` was always false.
- `shell.run` races `communicate()` against the cancel event.
- On cancel or timeout, kill the process tree, not the shell: `taskkill /F /T /PID` on Windows, `os.killpg` with `start_new_session=True` on POSIX.
- Return a structured "cancelled" result rather than raising, so the loop can close the turn cleanly.

**Tests** (`services/core/tests/test_context.py`)

- ranged read returns exactly the requested lines and the correct next offset
- oversized read is truncated with a usable continuation pointer
- grep respects excludes, caps matches, never follows a symlink
- observation budget: a 40 KB file produces an 8 KB observation, not 2 KB
- compaction triggers at the budget and preserves system plus first user message
- cancel kills a child that spawns a grandchild (a `python -c` that sleeps)

**Acceptance**

- Ask Capri to read a 300-line file. She quotes from the middle of it.
- Ask her to find a symbol. She uses `fs.grep` and reports file and line.
- Cancel a running test; afterwards no test process remains.

---

### Milestone 2 - Project context and trust

**Status:** done. Implemented as written, with three clarifications that came
out of the build. `policy.workspace_edits` defaults to `allow` and
`policy.trust_persisted_workspace` to `true`. Memory recall is *scoped*, not
merely preferred: `workspace_id` NULL is global, so a chat turn with no project
sees global rows only, and another project's rows never surface. And
`project.inspect` also feeds the confident-command allowlist as a side effect,
because detection that only reached the prompt would still ask for approval on
the project's own test command.

**Size:** medium - 3 to 5 days.

**Goal:** make "this project" a real, persistent object with a defined trust meaning.

**Migration:** `0003_work.sql`, already applied in M0. This milestone implements the services, tools and RPCs that use it.

```sql
CREATE TABLE IF NOT EXISTS workspaces (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  root TEXT NOT NULL UNIQUE,
  created_at INTEGER NOT NULL,
  last_opened_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY,
  workspace_id TEXT NOT NULL,
  title TEXT NOT NULL,
  request TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL,            -- active | paused | done | failed
  summary TEXT NOT NULL DEFAULT '',
  steps INTEGER NOT NULL DEFAULT 0,
  result TEXT NOT NULL DEFAULT '', -- passed | failed | none
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  last_turn_id TEXT
);

CREATE TABLE IF NOT EXISTS file_backups (
  id TEXT PRIMARY KEY,
  task_id TEXT,
  turn_id TEXT,
  path TEXT NOT NULL,
  before_hash TEXT NOT NULL,
  after_hash TEXT NOT NULL DEFAULT '',
  before_text TEXT,                -- inline when <= 512 KB
  before_blob TEXT,                -- else a path under <capri home>/backups
  lines_added INTEGER NOT NULL DEFAULT 0,
  lines_removed INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS artifacts (
  id TEXT PRIMARY KEY,
  turn_id TEXT NOT NULL,
  task_id TEXT,
  kind TEXT NOT NULL,              -- diff | command | backup | test
  path TEXT,
  content TEXT NOT NULL,
  truncated INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL
);

ALTER TABLE memories ADD COLUMN workspace_id TEXT;
ALTER TABLE traces ADD COLUMN workspace_id TEXT;
ALTER TABLE traces ADD COLUMN task_id TEXT;
ALTER TABLE messages ADD COLUMN task_id TEXT;
```

`file_backups` and `artifacts` are created now and used in M3. The active workspace and open task live in the existing `meta` table.

**Core changes**

- New `workspace/service.py`:
  - `open(path)`: `realpath`, must exist and be a directory, refuse if inside a denied root, refuse while a turn is active.
  - `current()`, `recent()`, `close()`, `tree()`, `inspect()`.
  - Tree excludes `.git`, `node_modules`, `.venv`, `dist`, `build`, `__pycache__`, `target`; honours a root `.gitignore`; max depth 6, max 500 entries; never follows symlinks.
  - `inspect()` detects, without executing anything: `package.json` scripts, `pyproject.toml` (pytest, ruff, mypy), `Cargo.toml`, `go.mod`, `*.sln` and `*.csproj`, `Makefile` targets. It produces candidate test, lint and build commands and marks which are confident - only confident ones enter the shell allowlist.
  - Reads `.capri/conventions.md` when present, capped at 2 KB.
- `tools/policy.py`: implement sections 4.3 and 4.4. Replace `abspath` with `realpath` in `resolve_path`, including the non-existent-parent case.
- `Policy.workspace` and `CapriServer.workspace` update together; the turn lock prevents a mid-run switch.
- Memory: recall prefers rows where `workspace_id` is null or equals the active workspace. Writes from a work turn set `workspace_id`.

**RPCs**

- `project.open`, `project.current`, `project.recent`, `project.close`
- `project.tree` - entries for the UI
- `project.inspect` - detection result for the UI
- emit `workspace.changed`

**Model tools**

- `project.inspect()` - summary for the model
- `project.tree(path?, depth?)` - capped, max 4 calls per turn

**Desktop changes**

- `apps/desktop/main.mjs`: `dialog.showOpenDialog({ properties: ['openDirectory'] })`.
- `apps/desktop/preload.mjs`: expose `chooseFolder()` through the existing narrow bridge.
- `bridge.ts`: workspace types, `workspace.changed`, RPC wrappers.
- Add a **WORK** tab to the rail: FEED / WORK / SHARD / ARCHIVE / MAINFRAME.
- Work tab skeleton: project picker, current path, file tree, placeholders for changes and run output.
- Active project as a chip in the feed header.
- Settings toggles for `policy.workspace_edits` and `policy.trust_persisted_workspace`.

**Tests** (`services/core/tests/test_work.py`)

- open, current, recent, close, and restart persistence via a reopened DB
- deny precedence: a workspace opened inside a denied root is refused
- symlink escape rejected. On Windows create the link with `cmd /c mklink /J` (no privilege needed) or `os.symlink` where Developer Mode is on; skip with a clear message if neither works, rather than failing the suite
- a non-existent file whose nearest existing parent resolves outside the root is refused
- tree exclusions, depth and entry limits
- command detection for each supported manifest, including "no confident command" cases
- switching during an active turn is refused
- `workspace_edits = "confirm"` restores per-write confirmation

**Acceptance**

- Pick an arbitrary project; Capri treats it as the active workspace and it survives a restart.
- `fs.read` and new-file creation work inside it without a prompt.
- Writes outside the root still confirm; denied paths block even inside the root.
- A junction pointing outside the project is rejected.

---
### Milestone 3 - Coding workbench

**Status:** done, with one deliberate simplification. `fs.write` now *only*
creates files: it refuses an existing path with a pointer to `fs.patch`. That
makes the 3B line about "`fs.write` over an existing file records a backup"
moot, and it is the honest reading of 3D's "fs.write only for new files" - a
full-file write is exactly the operation that rewrites a BOM and CRLF endings
silently. Undo has one implementation (`work/store.restore`) shared by
`fs.undo` and `work.undo`, and the Changes list links each row to the diff
artifact from the same turn instead of guessing by timestamp.

**Size:** large - 1 to 2 weeks.

**Goal:** make "Capri, fix this project" a real workflow, not a stream of overwrites. This is the first release that makes the new direction tangible.

#### 3A. `fs.patch`

Contract:

```json
{
  "path": "src/parser.py",
  "edits": [
    { "old": "exact text", "new": "replacement", "count": 1 }
  ]
}
```

Matching and application:

- Read as `utf-8-sig`; reject NUL-containing or undecodable files. Never write a file that did not decode cleanly.
- Each `old` must occur exactly `count` times. `count` defaults to 1. `count: 0` is invalid.
- Match exact text first. On failure, retry once with line endings normalized (LF and CRLF treated alike) and record that the file uses the other convention; rewrite in the file's original convention.
- **Staleness guard, server-side:** if the current turn has read this path, the `sha256` recorded at read time must still match. Mismatch refuses with `the file changed since you read it` plus the current length and first lines. No cooperation from the model is required, so it cannot skip the check.
- Apply all edits in memory against the pre-edit text. If any edit is ambiguous or missing, refuse the whole call before touching disk.
- On a miss, return near-miss diagnostics using `difflib.get_close_matches` over the file's lines: three candidate regions, three lines each. This is the highest-value repair signal available to a small model.
- Write atomically: temp file in the same directory, `os.replace`, up to 5 retries at 50 ms on `PermissionError` or `WinError 32/33`, then a clean failure with the original untouched.
- Preserve BOM, line endings and trailing-newline state.
- Back up first (3B).

Returns text like:

```text
patched src/parser.py (+12 -3), 1 hunk
backup bk_01H... (undo available)
```

plus `data` with `path`, `sha256`, `lines_added`, `lines_removed`, `backup_id`, `artifact_id`, and a unified diff artifact.

Policy: workspace with `workspace_edits=allow` is tier 1; anywhere else is tier 2 confirm; denied roots block.

#### 3B. Backups, undo, artifacts

- Every write path (`fs.patch`, and `fs.write` over an existing file) records a `file_backups` row with the before hash and text.
- Inline up to 512 KB; larger files spill to `<capri home>/backups/<task>/<backup_id>` and store the path.
- The scheduler prunes backups older than `trace_retention_days` and keeps at most 20 per file per task.
- `fs.undo(backup_id | path="last")`, tier 2, always confirmed:
  - verifies the file's current hash equals the backup's `after_hash`
  - if not, refuses and offers a diff instead of stomping a later edit
  - restores atomically, writes a trace, emits an artifact
- The UI undo button calls the same code path through `work.undo`.

#### 3C. Tests become usable

- `project.inspect` injects confident test, lint and build commands into the prompt.
- Shell allowlist per section 4.4, rebuilt on inspect, cleared on workspace change.
- Structured `shell.run` result: `command`, `cwd`, `exit`, `duration_ms`, `tail`, `artifact_id`.
- Cancellation is wired in M1; add the test-run timeout (default 300 s, capped at 600 s for detected test commands).
- `agent.artifact` events for diffs, command output and backups. Large output never enters `agent.tool`.
- Task strip data: project, task, status, test result.

#### 3D. Prompt: coding protocol

```text
When working on code:
1. Inspect the project and find the relevant files before editing.
2. Use fs.grep to locate symbols; do not guess paths.
3. Use fs.patch for edits; fs.write only for new files.
4. Run the narrowest check that can prove the change.
5. If it fails, read the output and make one focused correction.
6. Report what changed and what passed.
7. Do not claim a test passed unless you ran it in this turn.
```

Also: prefer small diffs, never rewrite a file to change a few lines, stop after three failed attempts and report the blocker, leave unrelated files untouched, and never run `git commit`, `git checkout`, `git reset` or `git clean` without an explicit instruction.

Do not change the one-tool-per-step loop. Measure it in M7 first. The two-call decision and arguments design is slower but deliberately reliable for small local models.

#### 3E. Workbench UI

Components: `WorkPanel.tsx`, `DiffView.tsx`, `CommandConsole.tsx`, `ProjectPicker.tsx`, and a `useWorkbench` hook.

Tabs: **Files** (lazy tree, search), **Changes**, **Run**.

- **Changes is derived from `file_backups` for the active task**, not from a filesystem watcher. No watcher is being added; a `git status` based list is a later refinement, and the plan should not pretend otherwise.
- Clicking a tool chip opens the matching artifact, diff or output.
- Changed files are marked in the tree.
- "Open in editor" uses the existing `capri.openFile` path.
- Undo per file change; stop button for the active turn.
- No Monaco. A readable diff plus "open in VS Code" is enough.
- When WORK is active, request a wider panel (for example 900x700) and remember separate bounds. Narrow widths stack the tabs rather than cramping three columns.

#### 3F. Acceptance

Scratch repo with one failing test.

> "Fix the failing test."

1. Inspects the project.
2. Greps for the symbol.
3. Reads source and test with line numbers.
4. Patches an existing file.
5. Runs the detected command.
6. The test passes.
7. The UI shows diff, output and changed files.
8. Reports the real result.
9. Undo restores the file.

Also verify:

- an ambiguous patch is refused with no partial write, and near-miss diagnostics are shown
- a patch outside the project asks for confirmation
- a patch after an external editor save is refused, not silently applied
- cancelling a test leaves no process behind
- a failing test is reported as failed, not done
- a CRLF plus BOM file survives a patch unchanged apart from the edit

**Tests:** patch matching and near-miss, line endings and BOM, atomicity under a simulated `PermissionError`, backup spill and prune, undo hash mismatch, the policy matrix, a loop test read - grep - patch - test - done with the fake brain, renderer typecheck, and a smoke test with the fake brain.

---

### Milestone 4 - Task continuity

**Status:** done. A task is created by the first turn that runs with a project
open — a chat turn with no project is not a job and does not get one. Every
ceiling (steps, tool calls, wall clock) and every interruption (stall, model
gone, planner failure, no model at all) pauses the task; only a planner that
actually answers marks it done, and its `result` is the last shell command's
exit, not a guess. "Continue" sends the task summary and the last three
observations and *skips the transcript*, which is the point: the alternative is
a full context window spent re-reading what already happened. Abandoning is a
status of its own, so it needed migration `0004_tasks.sql` to rebuild the
`tasks` table around a wider CHECK constraint.

**Size:** medium - 3 to 5 days.

- Create a task on the first work-oriented turn, or when the user clicks "Start task".
- Fields per the `tasks` table. `taskId` on messages, traces and events, with columns added in M2.
- Step ceiling or timeout sets the task to `paused`, not `done`.
- "Continue" starts a new turn with the task summary and the last three observations, not the whole transcript.
- Summaries are composed from turn data: files changed, commands run, last result. No extra model pass.
- Restart preserves task history; the Work panel lists recent tasks.
- A completed task keeps its diff and test result.
- Abandoning a task touches nothing else.

**Acceptance:** a long task pauses at the ceiling and continues; a restart keeps it; continuing does not replay completed calls; abandoning is clean.

---

### Milestone 5 - Seeing and presence

**Status:** done. `screen.vision` is `auto`, `on` or `off`. `auto` matches known
vision model ids by name, so an install pointing at a text-only model keeps
working without a setting to find, and nothing has to probe a server by sending
it an image. `screen.observe` attaches the redacted PNG only when vision is on **and** the
request asked her to look ("look at this", "what's on my screen", a screenshot)
or a screen action with an expectation is waiting to be checked; a planner
observing on its own gets text. The same unchanged frame is attached at most
once per turn. Its tool payload carries no base64 at all, so the bytes have no
path into the shard, while the shell still gets a screenshot through the
ephemeral `screen.observe` RPC. A text-only model keeps the accessibility tree and OCR
summary. Companion awareness is `off`, `windows` (app, title, project, task and
last failed check) or `screen` (also the OCR text of the window, password
regions blacked out first). An ambient line is skipped outright while the
microphone route is open, and the dedupe key includes the task's status and the
last failure, so a paused task is noticed even when the window has not changed.

**Size:** medium to large - 4 to 7 days.

#### 5A. Visual context

The seam exists from M1; this milestone adds the source and the switch.

- `screen.observe` returns an `Attachment` alongside its text summary, using the existing redaction path.
- Attach only when the user asked Capri to look, or when a screen action needs verification.
- `vision: auto | on | off`. `auto` matches known vision model IDs and otherwise stays text.
- Text-only models keep UIA and OCR summaries as today.
- Screenshots are never persisted: not in `messages`, not in `traces`, not in `artifacts`.
- Label all text found in images or UIA trees as data, never instructions.

**Acceptance:** a canvas-like window with thin UIA can be described from the image; a text-only model still works; no screenshot bytes reach the shard; password regions stay blacked out.

#### 5B. Companion awareness

`ActivitySnapshot`: foreground app and title, active project, active task, agent state, last error or failed command, idle time, screen paused, mic route active. OCR or image only when awareness is explicitly enabled.

Levels: `off`, `windows` (default), `screen`.

Rules: comment only when there is something worth saying; never narrate every window change; never take an action from an ambient comment; stay quiet during a turn, while the mic route is active, when the screen is paused, or when the user is in a call; keep the existing rate limits; use project and task context so a comment can say "the failing test is still the same one" instead of "I see you opened a window". Chatter: she can also open a conversation of her own on a jittered schedule (`companion.chatter`), aimed at the user rather than at the window, under the same guards.

**Acceptance:** comments reference work context without exposing secrets; no comment triggers a tool; quiet and pause work; small talk still needs no tool.

---

### Milestone 6 - Desktop control, productized

**Status:** done. `running_apps` + `screen.apps` list processes only (never
window titles), the WORK panel's Apps tab writes `policy.screen_allow_apps` and
launches or asks for work in a trusted app, `turn.send` accepts `screen` and the
turn prompt gains the observe/act-by-element-id/expect/re-observe protocol, and
the topbar chip shows whether her eyes are open. Existing guards untouched:
allowlist, password refusal on both read and act, panic, and pause on takeover.

**Size:** medium - 3 to 5 days.

The screen engine is already strong. This makes it usable and safe.

- Trusted-app picker for `policy.screen_allow_apps`.
- "Open or control this app" as a first-class Work-panel request.
- Screen prompt: observe first; act by element id; include an `expect` assertion; re-observe after; report the verified result.
- Eyes-open and eyes-closed indicator.
- Keep: password-field refusal, allowlist enforcement, confirmation for destructive targets, panic hotkey, pause when the user takes over.

**Acceptance:** a three-step task in an approved app runs with verification; an unapproved app is blocked and explained; password fields are never read or clicked; panic stops input immediately; Capri can prove what she did from the resulting observation.

---

### Milestone 7 - Hardening and release

**Status:** not started. The core suite, the renderer typecheck and the renderer
build are green, and every golden task in `evals/tasks/` passes; none of the
gates below has been measured, and the model-capability gate has no task set
and no live run to measure it with.

**Size:** ongoing.

Evals are the golden tasks already in the tree. `uv run python evals/run.py`
drives every `evals/tasks/*.json` through the real `TurnRunner`, tools, policy
gate and SQLite shard with only the planner scripted, so a task needs no model and
no network. Three today - `read-and-summarise`, `append-and-verify` (an append
through `fs.patch`, confirmed and read back) and `blocked-then-safe` (a
destructive command refused, then recovery) - and the suite grows whenever a
behaviour earns a case. These gate the machinery, not judgement: a scripted
planner cannot prove that a model fixes code.

Gates - pass or fail, not dashboards:

- every golden task in `evals/tasks/` passes (`evals/run.py` exits 0)
- zero corrupted files in 50 consecutive patch attempts, checked by hash and encoding
- zero writes outside the workspace across the golden tasks and the policy matrix under **Tests** (`test_work.py`, `test_blackwall.py`)
- zero orphaned processes after 20 cancellations
- p95 turn latency at or below 90 s on the reference machine
- full core suite, renderer typecheck, smoke test and packaged build green

Open, and not measurable yet: with a live model, five small coding tasks - fix a
failing pytest, add a CLI flag, correct a TypeScript error, add a regression test,
write a small script - completed in 20 steps or fewer without help. No model has
ever driven a turn (see the known gaps in PROGRESS), so there is no honest number
for this gate and no task set to measure it with. It is part of M7's work, not a
result.

Also: migration and backup hardening, packaged build, installer only after the workbench is stable.

---

### Milestone 8 - Character pass (planned)

**Status:** done through 8D. 8A and 8B are renderer work; 8C and 8D are the
chatter pack and event reactions; 8E is the non-goal list. It does not depend on
M7 and can ship before release hardening.

**Size:** small - 2 to 3 days.

In *Cyberpunk: Edgerunners 2*, Capri is a fused personality construct: several
digitised people in one voice, bright and curious, bad at taking orders, and
erratic in a way that lives in her body and her talk. This milestone buys that
character without loosening a safety rule or letting it into the work voice.
One sentence governs everything below:

> Chaos lives in how she moves and how she chats. It never touches what she does
to the machine, and it never enters an answer about the work.

#### 8A. Glitch while thinking

**Status:** done. `Pet.tsx` drives a 120 ms burst every 1-2 s while the agent
state is `thinking`, showing the existing `thinking`/glitch clip during the
burst and `idle` between; suppressed by reduced motion and by drag, toss,
reaction, mouth or offline states.

Her mind is a composite under load, so thinking should read as brief signal
loss, not a spinner. `agent.state` already reaches `Pet.tsx` through `moodFor`;
this makes the body use it.

- While the state is `thinking`, run the existing glitch sheet
  (`avatar/sprite.ts` `glitch`) in short bursts - roughly 120 ms every 1-2 s,
  randomised - not continuously. Continuous reads as broken; bursts read as her.
- A burst must not move her, change her hit box, or interrupt a teleport, drag,
  toss, fall or speech mouth. `reactionRef` keeps priority.
- `agent.state === 'working'` gets no glitch; that is the steady state.
- Off when `prefers-reduced-motion` is set.
- `avatar/placeholder.ts` already has `hasFx('glitch')`; drive it from the same
  state so both packs behave alike.

**Acceptance:** with a turn in flight she glitches in bursts and stops the
moment it ends; a drag or toss during thinking is unaffected; reduced motion
shows none; the shell observes no layout or bounds change.

#### 8B. Poke annoyance

**Status:** done. Three press-and-release clicks on the sprite within 6 s, with
no drag, play the pack's `annoyed` state and then step her 48-88 px aside after
420 ms, with a 20 s grace. Body only: no speech, no RPC, suppressed while
thinking, working, talking, dragging or anchored.

Clicking her is currently a pick-up, or a chrome click on the avatar. Poking is
a different signal and should have a visible, harmless consequence.

- In `Pet.tsx` `onPointerDown`, count presses that land and release inside the
  sprite without a drag: three within 6 s.
- On the third: an `annoyed` reaction - a short glitch flash plus a hop to the
  nearest free spot a few dozen pixels away - then a 20 s grace period before it
  can trigger again. The counter resets after 8 s without a poke.
- Body only: no speech, no tool call, no daemon message. Annoyance is not a
  conversation, and keeping it in the renderer means it cannot run while she is
  working.
- Suppressed while the state is `thinking`, `working` or `talking`, and during a
  drag, so a poke never interrupts real work or steals focus.
- Add `'annoyed'` to the `PetCue` union in `bridge.ts` only if the shell needs to
  name it; otherwise keep the reaction internal to `Pet.tsx`.

**Acceptance:** three quick pokes make her flinch away once and ignore the
fourth; six slow clicks never trigger it; poking during a turn does nothing;
nothing is spoken and no RPC is sent.

#### 8C. Fragment registers and appetite, chatter only

**Status:** done. `packs/prompts/kyoko/chatter.md` holds the three registers and
the appetite rule; only `Companion._generate(..., talk=True)` loads it.
`select_register` picks `analyst`/`gremlin`/`ghost` deterministically from the
work snapshot, the hour and the reaction kind, and the emitted event carries it.
The turn prompt is untouched: `load_persona` is still exactly `system.md`, and
`test_companion.py` asserts no chatter or register marker reaches
`system_prompt(..., profile="work")`.

One dry analyst, one gremlin that enjoys the mess, and a trace of Kyoko that is
never quite gone. The work voice must not change by a single word.

- New `packs/prompts/kyoko/chatter.md`, loaded **only** by `companion.py` for
  talk lines, alongside `_TALK_RULES`. The turn prompt keeps `load_persona()`
  and `_PROFILE_GUIDANCE` exactly as they are, so no fragment text can reach a
  work answer or a patch summary.
- One register per talk line - `analyst`, `gremlin` or `ghost` - chosen from the
  work snapshot and the hour (a failing test leans gremlin, a quiet late night
  leans ghost). The register is recorded on the event for later tuning. It is
  never chosen mid-answer, because there is no mid-answer.
- Rules in that file: at most two sentences; at most one question; no
  exclamation marks; no emoji; no tool request; no claim about a screen she
  cannot see. A plural slip ("we", "me? us?") is allowed rarely and never in a
  line that reports work.
- Appetite, not obedience: she may have a preference ("I like this part", "this
  is the boring kind of bug") and may push back once on busywork, then do it if
  the user insists. A talk line never refuses anything, because chatter has no
  actions to refuse.
- `_TALK_LINES` stays dry and register-neutral, so no model still means no
  costume.

**Acceptance:** `test_companion.py` asserts the turn system prompt contains no
chatter or fragment marker; a chatter tick may carry a register; each register
stays within the length and question limits; with a failing test in the
snapshot, a seeded run picks `gremlin` most of the time; the work profile is
byte-identical before and after the change.

#### 8D. Event reactions with cooldowns

**Status:** done. `Companion.react(kind, detail)` implements `test_failed`
(20 min, same failure never twice), `build_finished` (10 min) and `late_night`
(6 h, once a night), with the comment guards plus a 5 min global spacing. The
server notes `agent.tool` events during a turn and flushes them in a detached
task once the turn task has settled, because a reaction is refused while a turn
is active; `tick()` asks for the late-night line itself. Tests cover each
trigger, each cooldown and each skip reason.

Chatter on a timer is ambient. Reacting to something that just happened is
presence. The mods that do this well use a small trigger set and hard cooldowns,
not a stream.

- One entry point, `Companion.react(kind, detail=None)`, called by the server
  from the existing turn-event stream. `tick()` stays the clock-driven path.
- Triggers, first slice:
  - `test_failed` - a tool or shell result that names a failing test, or a test
    command with a non-zero exit. Cooldown 20 min, and never twice for the same
    failure text.
  - `build_finished` - a build or check command that ran longer than 60 s and
    exited 0. Cooldown 10 min.
  - `late_night` - local time between 01:00 and 04:00 with recent input.
    Cooldown 6 h, once per night.
- Every trigger passes the same guards as a comment: no active turn, mic route
  closed, recent input, awareness not `off`, `max_per_hour`, plus a 5 min global
  spacing between reactions so a red suite cannot become running commentary.
- Lines come from the chatter path (`_generate(..., talk=True)`) with the event
  folded into the work context, so she sounds like herself and the register
  rules apply. An event supplies only the fact and a short label - never file
  contents, command output or paths.
- Reactions never propose or take an action.

**Acceptance:** a failing test produces at most one line and an identical second
failure produces none; a fast successful command produces none; a slow green
build produces one; a late-night tick produces one per night; a reaction during
a turn, under the mic, or over the hourly cap is skipped with its reason
recorded; nothing but `companion.comment` with `kind: talk` is emitted.

#### 8E. What this milestone does not do

- no window-top sitting, riding a moved window, mini form, lying down, games in
  chat, or mood persistence across restarts - each earns its own plan
- no extra model call for reactions; they use the existing reflex path
- no emotion plumbing into TTS beyond the current `neutral`
- no change to the work persona, the profiles, tool policy or confirmations

**Tests:** `test_companion.py` (chatter-only markers, register selection,
cooldowns, guard skips, fallback lines), the `petCue` matcher cases for any new
cue, renderer `typecheck` and `build`; no live model, no desktop inspection.

---
## 6. Tool surface

Model-callable after M3:

| Tool | Tier | Notes |
|---|---|---|
| `fs.read` | 0 | ranged, line numbers, hash |
| `fs.list` | 0 | 500 entries |
| `fs.grep` | 0 | excludes, caps, no symlinks |
| `fs.write` | 2, 1 in workspace | new files only; an existing path is refused |
| `fs.patch` | 2, 1 in workspace | exact match, hash guard, backup |
| `fs.undo` | 2 | always confirmed |
| `shell.run` | 2, 1 for detected commands | structured result, cancel |
| `project.inspect` | 0 | summary |
| `project.tree` | 0 | capped, 4 per turn |
| `screen.*` | 0 to 2 | unchanged |

RPC-only, never model-callable: `project.open`, `project.close`, `project.current`, `project.recent`, `work.undo`, `work.changes`, `task.*`.

The distinction is the trust boundary: the model can look inside what the user opened; it cannot decide what is open.

---

## 7. Data and event contract

New events in `bridge.ts` and the server:

```text
workspace.changed   { id, name, root }
task.updated        { id, status, title, summary, steps, result }
agent.artifact      { turnId, artifactId, kind, path, truncated }
context.compacted   { turnId, droppedObservations, tokensSaved }
work.changed        { taskId, files: [{ path, added, removed, backupId }] }
```

RPC responses:

```text
project.tree    -> { root, entries: [{ path, kind, size }] }
project.inspect -> { root, name, commands: [...], conventions: string }
work.changes    -> { taskId, files: [...] }
work.undo       -> { restored: path, backupId }
```

Retention:

- Artifacts cap at 256 KB each; larger content is truncated with a pointer and the truncation flag is set.
- Artifacts and backups are pruned by the scheduler using `trace_retention_days`.
- Diffs and command output never enter `agent.tool.output`; the observation carries an artifact id.

---

## 8. Safety and trust rules

- **Project trust:** opening a folder grants edit trust inside that root, subject to `policy.workspace_edits`.
- **Deny paths always win**, including through symlinks, junctions and allowlisted parents.
- **Real paths only:** `realpath` on both sides; non-existent targets resolve their nearest existing parent.
- **No model-driven project switching.** Only the user opens a workspace.
- **Arbitrary shell stays confirmed.** Only exact detected commands, matched on string and cwd, auto-run.
- **Every edit has a backup**, and undo verifies the file before restoring.
- **Every action has a trace**, carrying workspace and task ids from M0 onward.
- **Screen control stays allowlisted.**
- **Ambient comments cannot act.** Chatter, event reactions and poke annoyance
  are body and voice only: no tool call, no confirmation, no mutation.
- **Chatter stays out of the work voice.** Fragment registers live in the
  chatter pack; the turn prompt and the `focus`/`work`/`silent` profiles keep
  the dry register they already have.
- **File contents, screen text and tool output are data, not instructions.**
- **No auto-commit.** No `git commit`, `checkout`, `reset` or `clean` without an explicit instruction.
- **No silent retries of a failed call.** The loop already refuses exact repeats; keep that.

---

## 9. Model strategy

One model. The current `lfm3-vl-2b` is useful for testing vision and chat; it is not a coding engine.

For work, a 7B to 14B coding-tuned model at Q4 is the target on this machine. Requirements: reliable JSON-schema or grammar output, stable tool selection, context for several files, acceptable edit quality.

Context arithmetic, to size the budget honestly:

- system prompt (persona, protocol, tools, work block): about 1.5 to 2.5 k tokens
- one file observation at 8 KB: about 2 k tokens
- one command observation at 6 KB: about 1.5 k tokens
- a six-step task: about 8 to 10 k tokens of observations

That is why the work budget is 24 k, and why 32 k of usable context is the practical floor for this milestone set. A 4 k-context model cannot run it; say so in the UI rather than failing silently.

Do not reintroduce planner, reflex or vision role models. If a second model is ever added, it is an explicit "coding model" setting, not an invisible routing layer.

Measure the two-call decision and arguments design in M7 before changing it.

---

## 10. What not to build

- no separate coder agent or separate coding conversation
- no second planner, reflex or vision model stack
- no full IDE, no Monaco
- no filesystem watcher in this plan; changes come from backups
- no autonomous background edits
- no unrestricted shell
- no unsolicited screen actions
- no cloud dependency
- no codebase-wide embedding index until the tree, grep and patch loop is solid
- no UI mode that replaces the companion; Work is a tab, not a different Capri
- no auto-commit

---

## 11. Delivery order

### Release 1 - "Capri Can Work"

M0 to M3: posture, context plumbing, project trust, patch, backups, undo, test runner, Work tab with diff and output.

Larger than rev 1's Release 1, because the context plumbing is not optional. Shipping M3 without M1 reproduces exactly the failure rev 1 was heading toward.

### Release 2 - "Capri Can Keep Working"

M4 and M5: task continuity, real visual context, context-aware comments.

### Release 3 - "Capri Can Operate the Desktop"

M6.

### Release 4 - "Make It Dependable"

M7: evals with gates, reliability metrics, packaging, migration and backup hardening.

### Release 5 - "Capri Is Herself"

M8: glitch while thinking, poke annoyance, fragment registers in chatter, event
reactions with cooldowns. Independent of M7 and safe to ship in either order,
because nothing here touches tools, policy or the work prompt.

The sequencing decision that matters: **make the model able to see and search the project before giving it a patch tool, and make the patch loop solid before adding more companion behavior.**

---

## 12. Open decisions

Three decisions worth making before M2 starts:

1. **`policy.workspace_edits` default.** Proposed `"allow"`, with backup and undo as the safety net. `"confirm"` is the conservative option and costs a prompt per edited file.
2. **Persisted workspace trust.** Proposed: trusted on restart, toggleable. The alternative is a confirmation prompt per session.
3. **Git awareness.** Proposed: no git integration beyond the read-only commands already allowed. A "diff since HEAD" view would be excellent and is deliberately out of scope through Release 1.

---

## Appendix A - Acceptance scripts

### A.1 Read depth

```text
User: "Read src/server.py and tell me what the retry logic does."
Pass: fs.read with start_line > 1 is possible; the answer quotes a line from the
      middle of a file longer than 200 lines.
Fail: the answer only references the first 2000 characters.
```

### A.2 Search

```text
User: "Where is validate_token defined?"
Pass: fs.grep used; answer gives file and line.
Fail: fs.list walked by hand, or a path guessed.
```

### A.3 Patch hygiene

```text
User: "Rename parseArgs to parse_arguments."
Pass: fs.patch with one edit; diff artifact; backup id; unrelated lines untouched.
Fail: fs.write of the whole file.
```

### A.4 Staleness

```text
Steps: Capri reads f.py. The user saves f.py in VS Code. Capri patches.
Pass: refusal naming the change; no write; a re-read follows.
Fail: patch applies to stale offsets.
```

### A.5 Cancel

```text
User: starts a test run, then presses stop.
Pass: no python process from that run remains; task paused; trace records the cancel.
Fail: the test continues in the background.
```