---
title: Relay hooks
description: What each CtrlB Relay hook of the Claude Code plugin and the Codex hooks reads, writes and how long it may take, including the round 8 hooks that register spawned sub-agents, keep scratch artefacts, start a session with the record and manage shared-file slots.
slug: relay-hooks
---

# Relay hooks

The plugin's hooks run `python3 -I plugin/hooks/hook.py <mode>` (Codex: `codex/hook.py`) with the hook's JSON on stdin. A hook never fails a session: every error goes to `~/.ctrlb/hooks.log` and the hook exits 0. Every round 8 hook is built to fail open: when Seeker is slow, down or answers an error, the session goes on as if the hook were not there. This page lists, per hook, the event, the input fields it relies on, what it writes and its time budget. The field names and budgets are from the Claude Code hooks reference (code.claude.com/docs/en/hooks.md; sections cited by name below).

## Fields the hooks rely on

From "Common input fields": `session_id`, `transcript_path`, `cwd`, `hook_event_name`, `agent_id` (set when the hook fires inside a sub-agent) and `scratchpad_dir` (the session's scratchpad; absent when the session has none; Claude Code v2.1.257 or later). From the event sections:

| Event | Fields read | Reference section |
|---|---|---|
| SessionStart | `source` (`startup`, `resume`, `clear`, `compact`, `fork`) | SessionStart input |
| PreToolUse | `tool_name`, `tool_input`; for the `Agent` tool `tool_input.prompt`, `tool_input.description` (`subagent_type`, `model` are not used); for `Write` and `Edit` `tool_input.file_path` (always absolute) | PreToolUse input, `##### Agent`, `##### Write`, `##### Edit` |
| PostToolUse | `tool_name`, `tool_input`, `tool_response` (`duration_ms` is not used) | PostToolUse input |
| PostToolUseFailure | `tool_name`, `tool_input`, `error` | PostToolUseFailure input |
| SessionEnd | `reason` (not used: every reason hands off) | SessionEnd input |

Outputs: SessionStart prints `hookSpecificOutput.additionalContext` ("SessionStart decision control"). PreToolUse prints `hookSpecificOutput.permissionDecision` `deny` with `permissionDecisionReason`, or `allow` with `updatedInput`, or `additionalContext` ("PreToolUse decision control"). A hook that prints nothing lets the call go ahead.

Budgets ("Timeouts", SessionStart, SessionEnd): a hook's `timeout` in `hooks.json` cancels it and discards its output; on PreToolUse a timed-out hook does not block the tool call. There is no SessionStart-specific budget beyond the hook's own `timeout`, and Claude's first response waits for SessionStart hooks. SessionEnd hooks share a 1.5 s budget that a plugin's `timeout` does not raise. A hook that leaves work to a background process prints its decision first and redirects that process's stdout and stderr ("Hooks that start a background process"); the artefact poster does both.

## Settings

| Setting | Environment | `~/.ctrlb/config.json` key | Default |
|---|---|---|---|
| `scratchDir` | `CTRLB_SCRATCH_DIR` (`{run}` is replaced by the run id) | `scratchDir` | `~/.ctrlb/relay/<run id>/scratch` (under `CTRLB_HOME`) |
| `autoSlots` | `CTRLB_AUTO_SLOTS` (comma-separated relative paths; empty turns it off) | `autoSlots` (a list) | `["REGISTRY.md"]` |

A hook "is in a relay run" as the session-end handoff decides it: `CTRLB_RUN` is set, or a ctrlb relay tool was called in this session.

## The hooks

| Hook (mode) | Event, matcher | `timeout` | Runs | Seeker calls | Fails |
|---|---|---|---|---|---|
| Spawn registers a task (`pre-agent`, decision 43) | PreToolUse, `Agent\|Task` | 5 s | synchronous, all writes inside 2.5 s (alarm at 3 s) | `POST /ai/relay/{dataset}/runs/{run}/briefs` (task), again for the contract | open: prints nothing, ever; the spawn goes ahead unchanged |
| Artefacts (`post-tool`, decision 44) | PostToolUse, `Edit\|Write\|MultiEdit\|NotebookEdit\|...` | 5 s | the checks inline, the post detached (10 s) | `POST .../posts` with `key` and `hash` | open: no output; a failed post is retried on the next write |
| Record at session start (`session-start`, decision 45) | SessionStart | 10 s | synchronous, all reads in parallel inside 3 s | `GET .../tasks`, `GET .../contracts?agent=`, `GET .../peers?kind=note`, `GET .../peers?kind=handoff`, `GET .../artifacts?agent=` | the rule always; a part that did not answer comes from the local cache, or is left out |
| Scratch directory (`session-start`, D) | SessionStart | 10 s | local | none | — |
| Session index (`session-start`, store item 2) | SessionStart | 10 s | synchronous, at most 2 s | `GET /ai/sessions/{dataset}/index?cwd=&limit=10`, with `branch` when it is not a default branch, and `session_id` on `resume` and `compact` | the index under the recall rule; on `resume` and `compact` the session's running note above it; nothing on a timeout, an error or no earlier session |
| Parent line (`subagent-start`, store item 2) | SubagentStart | 10 s | synchronous, at most 2 s | `GET /ai/sessions/{dataset}/index?cwd=&limit=10` | the parent session's line only; nothing when it is not listed |
| Running note (`flush` on PreCompact, store item 3) | PreCompact | 45 s | the transcript upload first, then the note flush, waiting until 2 s before the hook's end; then, only on a flush timeout or error, the index's note in at most 2 s | `POST /ai/sessions/{dataset}/note` `{session_id, wait_s}` (`wait_s` the budget left minus 3 s); fallback `GET /ai/sessions/{dataset}/index?cwd=&limit=1&session_id=` | the note the flush answers on stdout as plain text, under 10,000 characters (Claude Code uses it as the compaction summary's instructions); on a flush timeout or error the index's latest note; nothing when there is no note |
| autoSlots claim (`pre-tool`, decision 47) | PreToolUse, `Edit\|Write\|MultiEdit\|NotebookEdit` | 5 s | synchronous, the claim inside 1.5 s (alarm at 2 s) | `POST .../claims` `{resource: "slot:<path>", paths: [<path>]}` | open: deny only on a definite answer that another agent holds it |
| autoSlots release (`post-tool`, `post-tool-failure`, decision 47) | PostToolUse and PostToolUseFailure, edit tools | 5 s | synchronous, inside 1 s | `DELETE .../claims` `{resource: "slot:<path>"}` | the slot lapses (a slot's lease is two minutes) |
| Session-end handoff (`session-end`, decision 36) | SessionEnd | 10 s (the 1.5 s budget applies) | synchronous, 0.8 s | `POST .../handoffs` `{all: true, to: ""}` | nothing written |
| Note flush at session end (`session-end`, store item 3) | SessionEnd (Claude Code; a Codex main thread, not a subagent thread) | 10 s (the 1.5 s budget applies) | after the handoff and the transcript upload (its budget 1 s shorter); waits at most 1 s for Seeker to take the request, never for the update | `POST /ai/sessions/{dataset}/note` `{session_id, wait_s: 0}` | none: Seeker updates the note in the background, so the next session's SessionStart finds it current |

Every call goes to Seeker under the agent's key (`Authorization: Bearer`), as every other hook call does.

### Spawning a sub-agent registers a task (decision 43)

PreToolUse on the `Agent` tool (`Task` is its earlier name), in a relay run. The hook registers `task <n> <description>`: `n` is the next number of a per-run counter on this machine (two digits), the description is `tool_input.description` with `/` and `\` made spaces (so the name is never keyed as a path), at most 80 characters. The task's text is `tool_input.prompt` with the plugin's secret patterns redacted, cut at 2,000 bytes. Its paths are every path the prompt names (a token with a `/` or a file extension; `src/app.py:12` names `src/app.py`) that resolves to a file or directory under the workspace (`cwd`), relative to it, at most 32; URLs and paths outside the workspace are left out. When the prompt quotes a test command in backticks or a fenced block (`pytest`, `python -m unittest`, `go test`, `npm test`, `cargo test`, `make test`), it is the task's `test`. A prompt that names no workspace path registers nothing (Seeker needs at least one path), with a line in the hook log.

If the prompt carries an interface, it is posted as a contract named `<task name> <heading>` on the same paths: a section under a heading that names one (API, contract, interface, shape, schema, fields, signature; a markdown heading or a short line ending in `:`), up to the next heading, or a fenced block whose language is `json`, `typescript`, `proto`, `graphql` or `openapi`, or whose language or preceding line names an interface.

The hook never prints anything and never changes the prompt. Ordering: Claude Code runs the tool only after its PreToolUse hooks return, so a registration written synchronously by this hook is in the record before the sub-agent exists, and so before its first `claim(next=true)`. A detached write would race the sub-agent's first claim, so the hook writes synchronously and gives up after 2.5 s; a registration that did not finish in time may or may not have landed, and the spawn goes ahead either way.

### Small text artefacts are kept in the record (decision 44)

PostToolUse on the edit tools, in a relay run, for a file under the session's `scratchpad_dir` or under `scratchDir`. A text file (UTF-8, no NUL byte) under 32 KB is posted as a NOTE keyed `artifact:<path relative to that directory>` with its text and `hash` (`sha256:<hex>`). The hook keeps the hash of the last posted content per path, so a write with the same content posts nothing; the hash is kept before the post and dropped again when the post fails, so the next write retries. Skipped, with a line in the hook log naming only the relative path and why: binaries, files of 32 KB or more, files with a secret-looking name (`.env*`, `*.pem`, `*.key`, `id_rsa`, `auth.json`, `credentials`, `.netrc`, ...) and files any of the plugin's secret patterns (`lib/redact.py`) matches. Nothing leaves the machine before those checks. The post runs in a detached process (its own session, stdout and stderr to `/dev/null`, the content on its stdin), so the hook returns at once. The plugin also keeps a local copy of each posted artefact under `~/.ctrlb/state/`, for the session-start fallback.

### A session starts with the record (decision 45)

SessionStart, when the environment carries a run id (`CTRLB_RUN`) and `source` is not `compact` (after a compaction the same session goes on, with its holds and scratch files in place). Within 3 s, in parallel, the hook reads the run's tasks (holder, `stalled`, `test`, `handed_off`), contracts, notes, handoffs and this agent's artefacts. It writes the artefacts this machine does not have into `scratchDir` (never outside it; an existing file is left alone) and adds a block under 2 KB after the rule text:

```text
CtrlB Relay record of run nightly-7 at session start:
Open tasks: task 01 parse (held by agent-b, stalled; test: python3 -m pytest tests/test_parse.py); task 02 emit (free, handed off).
Contracts: parse api. Read them with `contracts` before building on them.
Handoffs: task 02 emit handed off openly by agent-c: Session ended. Edited: src/emit.py. ... | task 04 render handed to you by agent-c: ...
Your last notes: parse: tokenizer done.
Your artefacts from the previous session are in ~/.ctrlb/relay/nightly-7/scratch.
```

Lines with nothing to say are left out; when the block would pass 2 KB, the notes go first, then the handoffs, then the contracts.

Decision: the reads are synchronous inside the hook. SessionStart has no budget of its own beyond the hook's `timeout` (10 s here), and Claude's first response waits for SessionStart hooks, so a synchronous read puts the block in context before the agent's first claim; a background refresh could not reach this session's context. A part that does not answer inside 3 s (or answers an error) comes from the local cache, which every successful read refreshes, and the block says which parts came from the cache and when. Artefacts fall back to the local copies of what this machine posted. With neither, the session gets the rule alone. The hook ends inside its budget either way.

### Durable scratch directory (D)

SessionStart creates `scratchDir` (mode 0700) and the rule text names it: "Put helper scripts and binaries in the scratch directory <dir>, not in /tmp." A sub-agent gets its parent's run, so the same directory.

### Hook-managed slots (decision 47)

For an edit of a file `autoSlots` lists (matched by its path relative to `cwd`), in a relay run, the PreToolUse hook claims `slot:<path>` with the file (`paths: [<path>]`), so the agent spends no turn on it; Seeker's `slot:` prefix is a valid key kind with a two-minute lease. The claim's token is kept like any claim's. When the answer is `held: false` with another agent as `held_by`, the edit is denied with `CtrlB Relay: REGISTRY.md is held by agent-b, try again in a moment.` The PostToolUse hook (and PostToolUseFailure, for an edit that failed) releases the slot when the hook holds it.

Fail-open: a claim that errors or does not answer inside 1.5 s lets the edit go ahead, with no edit check, and a line in the hook log. A slow Seeker must not stall every edit of a shared file; the cost is that two agents may write it at once while Seeker is slow, which `Edit`'s own exact-match replacement mostly catches. Only a definite answer that another agent holds the slot denies.

## Codex

| Hook | Codex |
|---|---|
| Spawn registers a task (43) | No: Codex's hooks give no sub-agent spawn tool input to read (its PreToolUse hook is installed for `apply_patch` only). |
| Artefacts (44) | No: Codex has no hook after a tool call. |
| Record at session start (45) and scratch directory (D) | Yes: `codex/hook.py session-start` adds the same block (artefacts only from the record; Codex posts none). |
| autoSlots (47) | No: with no hook after a patch, a slot claimed before it could not be released after it. Codex agents claim `slot:REGISTRY.md` themselves (see [Relay tools](relay-tools.md#slots)). |
| Session-end handoff (36) | Yes, with a fixed text. |
| Running note around a compaction (store item 3) | Yes, differently: Codex ignores a PreCompact hook's output. `flush` on PreCompact uploads (its budget 1 s shorter), then sends `POST /ai/sessions/{dataset}/note` `{session_id, wait_s: 0}`, waits at most 1 s for Seeker to take it and prints nothing. The `session-start` that follows (`source` `compact`) sends the flush again with `wait_s` 7 inside an 8 s budget, beside the 2 s index read, and puts the note it answers above the index. When the flush answers `running`, times out or errors, the index's latest note is used, as on `resume`. Codex's SessionStart timeout is 10 s. A subagent thread does neither. |

## Seeker reads and writes this needs

- `POST .../posts` takes `key` (`artifact:<clean relative path>`) and `hash` (at most 80 characters) for an artefact and keeps its text whole up to 32,768 bytes (longer is refused with 400, never cut); it answers `201 {"ok": true, "key": "artifact:..."}`. Peers leaves artefact notes out of its feed.
- `GET .../artifacts?agent=<agent>` answers `{agent, artifacts: [{key, path, agent, hash, ts, text}]}`: the newest artefact of each path of that agent, newest first, with its full text.
- A Seeker without the artifacts read (404) leaves the artefacts to the local copies of what this machine posted.
