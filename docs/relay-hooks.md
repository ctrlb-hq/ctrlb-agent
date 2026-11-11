---
title: Relay hooks
description: What each CtrlB Relay hook of the Claude Code plugin and the Codex hooks reads, writes and how long it may take, including the round 8 hooks that register spawned sub-agents, keep scratch artefacts, start a session with the record and manage shared-file slots.
slug: relay-hooks
---

# Relay hooks

## The agent-side state machine

In a run, the agent makes no Relay bookkeeping call. The plugin's hooks run the protocol on the harness's events, Seeker decides, and the model does the work. One agent session is one machine. Every action is one Seeker call made by a hook, never by the model. `lib/hooks_shared.py` (`Machine`) implements this table for Claude Code (`plugin/hooks/hook.py`) and Codex (`codex/hook.py`). The two `hook.py` files only parse their harness's input.

| State | Event | Action | Next state |
|---|---|---|---|
| idle | SessionStart, or SubagentStart, with a run id | `claim(next=true)`. Held: inject the task's name, files, test and ask_about. | holding |
| idle | SessionStart with no run id, or no free task (also: the claim failed, or the sub-agent is a read-only type: `Explore`, `Plan`, `claude-code-guide`, `statusline-setup`) | inject only the run and agent ids and the peers line (with a run id, also the hooks line) | free |
| holding | SessionStart with `source` `compact`, `resume` or `clear` | no call; inject the held tasks' briefs again from the state file | same |
| free | SessionStart with `source` `compact` | no call (the same session goes on) | same |
| holding or free | PreToolUse on an edit of a file nobody holds (not in `check`'s `conflicts` or `own`; or a `lapsed` or `unheld` conflict) | `claim(resource=path)` for this agent. On `belongs_to_task`: `claim(resource=task)` and inject its brief. | holding |
| holding or free | PreToolUse on an edit of a file the agent holds (in `check`'s `own`) | nothing | same |
| holding or free | PreToolUse on an edit of a file another agent holds (a `held` conflict, or a claim refused with `held_by`) | advisory: inject who holds it. enforce: deny with that reason. | same |
| holding or free | PreToolUse on an edit of a task's file whose task claim is refused with `reason: waiting_on` (the task's `after` list is not finished) | as another agent's hold: advisory: inject the task and the unfinished task names. enforce: deny with them. | same |
| holding | PreToolUse on an edit, own hold (or the hold's task) within 20% of its lease end | claim again, which renews: the task name for a file of a held task, else the path. No injection. | holding |
| holding | PostToolUse (or PostToolUseFailure) on Bash whose command is a held task's registered `test` run alone (below), exit code 0 (Codex: the rollout's run, read at the next PostToolUse, Stop or SessionEnd) | `done(resource=task, evidence={command, exit_code, summary: last output line}, witnessed=true, next=true)`. Inject the next task's brief, or that no task is free (with `reason: waiting_on`: which tasks the open ones wait on). | holding, or free |
| holding | PostToolUse or PostToolUseFailure on Bash whose command names a held task's `test` but is not the test alone (a pipe, `;`, `&&` around it), any exit code | no `done`. Inject once per task per session (the state file's `nudged`): `CtrlB Relay: that run did not count as the test of task "<name>" because the test was not run alone. Run it exactly as written, on its own line: <test>` | same |
| holding or free | PreToolUse on an edit of a file of a task this agent finished (the path claim is held with `after_done`, or the task claim is refused with `finished_by` this agent) | the finisher keeps its files: a plain path hold (`claim(resource=path)`), and the edit goes ahead in advisory and enforce. A file of a task another agent finished is named (advisory) or denied (enforce) as before, with the finisher's name. | same |
| holding | PostToolUse or PostToolUseFailure on Bash, a `done` pending from an earlier failed call | send that `done` again first | as above |
| holding | Stop | the existing upload (Stop fires after every turn; it does not end the session) | same |
| holding or free | SessionEnd | the existing open handoff and upload | ended |
| ended | SessionStart | as idle | as idle |
| any | SubagentStart | the existing join behaviour, then the idle rule, for the sub-agent's own machine (Codex: join only; the sub-agent thread's own SessionStart runs the idle rule) | as above |

Rules for the machine:

- **Run id.** `CTRLB_RUN`, or `run` in `~/.ctrlb/config.json`. With no run id the hooks do the session store work and the edit check of decision 7, and never claim.
- **State file.** `~/.ctrlb/state/<session_id>.json` (a Claude Code sub-agent: `<session_id>.<agent_id>.json`; a Codex sub-agent: its own thread id, which is the `agent_id` its tool calls carry), written by the hooks (held exclusively while it is read and written), never by the model. It is the only memory between events: `{session_id, agent, run, state, tasks: {name: {key, paths, test, ask_about, lease_until, lease_s}}, holds: {key: {resource, task, lease_until, lease_s, after_done?}}, pending_done, nudged: [task names], calls: {claim, check, done, read}}`. `nudged` lists the tasks whose not-alone test run was already said. `lease_until` is in µs as Seeker sends it. `lease_s` is the length of the lease: `check`'s `own[].lease_s`, or `lease_until` minus the time of the claim's answer. `calls` counts this session's hook calls to Seeker.
- **Time budgets.** The edit check gives up after two seconds and lets the edit through. A claim on write has its own two seconds (alarm at 4.5 s, inside the hook's 5 s `timeout`). If it fails or does not answer, advisory mode (the mode `check` answered) lets the edit through and enforce mode denies it. A `done` that fails (no answer, or an HTTP error) is logged and sent again on the next Bash event. A `done` that Seeker refuses (`done: false`) is not sent again: its message is injected. SessionStart's `claim(next=true)` has 5 s inside the hook's 10 s. Errors go to `~/.ctrlb/hooks.log`, one line per hook call to Seeker (`relay-hook <agent> <call> <resource> -> <answer>`).
- **Facts only.** The hook decides from what it can see: the file path, `check`'s `conflicts` and `own`, the registered task list in the claim answers, the Bash command and its exit code, and the lease times in Seeker's answers. A test "passes" when the command is the task's `test` run alone and the exit code is 0. Run alone (Seeker's rule, `IsTestAlone`, and the plugin's `test_alone`, one function for Claude Code's PostToolUse, the Codex rollout scan and the done witness): with each run of white space as one space, the command is exactly the test, allowing only a leading `cd <one path> &&` (or `cd <one path>;`) and a trailing `2>&1`. Any other pipe, `;`, `&&`, `||`, backtick or `$(` makes the exit code another command's: in the suite of 2026-11-10 the agents ran `python3 -m unittest ... 2>&1 | tail -8`, `tail` exited 0, and 8 of 20 tasks were finished over output that said `FAILED`. A command that names the test but is not it alone sends no `done`, whatever its exit code, and gets the one-line nudge above once per task per session.
- **The finisher keeps its files.** After the hook's `done`, the agent that finished a task may still edit that task's files until its session ends (a note in the task's own `VERIFY.md`, say): the claim on write is held as a plain path hold (Seeker answers `after_done: true` and `finished_by`), recorded in the state file's `holds` with `after_done`, and released by the session-end handoff with every other hold. Nobody else gains anything: another agent's edit of the file is still named or denied with the finisher's name, and the task is reopened only by an explicit `reopen`.
- **Mode off.** `CTRLB_ENFORCE=off`, or Seeker's mode `off`: no check and no claim on write. The SessionStart claim and the `done` on a passing test still run.
- **autoSlots files** (decision 47, below) are not claimed on write. Their slot is claimed and released around the edit, as before.
- **The model can still call the tools.** The `claim`, `release` and `done` tools work as before, and the witness (decision 38) still checks a `done` the model sends. The hook's own `done` is witnessed: the hook saw the command and its exit code.

The injected text names the run and agent ids. It says that Relay holds files and tasks for the agent through hooks, so the agent never claims or releases by hand, and that a blocked edit names who holds the file. It gives the peers line (`peers`, `post`, `ask`, `answer`, with the run and agent ids) and the scratch directory. It says nothing about claim, release, renew or done. A held task's brief adds the task's name, files, the tasks it comes after (its registration's `after` list, from `next_task.after` or the claim's `after`), test command and ask_about. The test sentence is: `When your change is ready, run its test exactly as written, on its own line, with no pipe, tail or other command before or after it: <test>. When it passes, the hooks record it and hand you your next task.` The round 8 rule text (v10), for a harness without hooks, is in [Relay tools](relay-tools.md#rule-text-for-a-harness-without-hooks).

## Each hook

The plugin's hooks run `python3 -I plugin/hooks/hook.py <mode>` (Codex: `codex/hook.py`) with the hook's JSON on stdin. A hook never fails a session: every error goes to `~/.ctrlb/hooks.log` and the hook exits 0. Every round 8 hook is built to fail open: when Seeker is slow, down or answers an error, the session goes on as if the hook were not there. This page lists, per hook, the event, the input fields it relies on, what it writes and its time budget. The field names and budgets are from the Claude Code hooks reference (code.claude.com/docs/en/hooks.md; sections cited by name below).

## Fields the hooks rely on

From "Common input fields": `session_id`, `transcript_path`, `cwd`, `hook_event_name`, `agent_id` (set when the hook fires inside a sub-agent) and `scratchpad_dir` (the session's scratchpad; absent when the session has none; Claude Code v2.1.257 or later). From the event sections:

| Event | Fields read | Reference section |
|---|---|---|
| SessionStart | `source` (`startup`, `resume`, `clear`, `compact`, `fork`) | SessionStart input |
| SubagentStart | `agent_id`, `agent_type` (a read-only type claims nothing) | SubagentStart input |
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
| Claim the next task (`session-start`, `subagent-start`, the hooks protocol) | SessionStart, SubagentStart | 10 s | synchronous, 5 s | `POST .../claims` `{next: true}` | open: state free, the text without a brief |
| Claim on write, renew, name the holder (`pre-tool`, the hooks protocol) | PreToolUse, `Edit\|Write\|MultiEdit\|NotebookEdit` | 5 s | synchronous: the check inside 2 s, the claims inside 2 s more (alarm at 4.5 s) | `POST .../check` (answer has `own`), then `POST .../claims` `{resource: <path or task>}` | check fails or is slow: open, no output. A claim fails or is slow: advisory open, enforce deny |
| Done on a passing test (`post-tool`, `post-tool-failure`, the hooks protocol) | PostToolUse and PostToolUseFailure, `Bash` | 5 s | synchronous, 3 s | `POST .../done` `{resource, evidence, witnessed: true, next: true}` | kept and sent again on the next Bash event |
| Artefacts at session start (`session-start`, decision 45) | SessionStart | 10 s | synchronous, inside 3 s | `GET .../artifacts?agent=` | the text without the artefacts line |
| Scratch directory (`session-start`, D) | SessionStart | 10 s | local | none | — |
| autoSlots claim (`pre-tool`, decision 47) | PreToolUse, `Edit\|Write\|MultiEdit\|NotebookEdit` | 5 s | synchronous, the claim inside 1.5 s (alarm at 2 s) | `POST .../claims` `{resource: "slot:<path>", paths: [<path>]}` | open: deny only on a definite answer that another agent holds it |
| autoSlots release (`post-tool`, `post-tool-failure`, decision 47) | PostToolUse and PostToolUseFailure, edit tools | 5 s | synchronous, inside 1 s | `DELETE .../claims` `{resource: "slot:<path>"}` | the slot lapses (a slot's lease is two minutes) |
| Session-end handoff (`session-end`, decision 36) | SessionEnd | 10 s (the 1.5 s budget applies) | synchronous, 0.8 s | `POST .../handoffs` `{all: true, to: ""}` | nothing written |

Every call goes to Seeker under the agent's key (`Authorization: Bearer`), as every other hook call does.

### Spawning a sub-agent registers a task (decision 43)

PreToolUse on the `Agent` tool (`Task` is its earlier name), in a relay run. The hook registers `task <n> <description>`: `n` is the next number of a per-run counter on this machine (two digits), the description is `tool_input.description` with `/` and `\` made spaces (so the name is never keyed as a path), at most 80 characters. The task's text is `tool_input.prompt` with the plugin's secret patterns redacted, cut at 2,000 bytes. Its paths are every path the prompt names (a token with a `/` or a file extension; `src/app.py:12` names `src/app.py`) that resolves to a file or directory under the workspace (`cwd`), relative to it, at most 32; URLs and paths outside the workspace are left out. When the prompt quotes a test command in backticks or a fenced block (`pytest`, `python -m unittest`, `go test`, `npm test`, `cargo test`, `make test`), it is the task's `test`. A prompt that names no workspace path registers nothing (Seeker needs at least one path), with a line in the hook log.

If the prompt carries an interface, it is posted as a contract named `<task name> <heading>` on the same paths: a section under a heading that names one (API, contract, interface, shape, schema, fields, signature; a markdown heading or a short line ending in `:`), up to the next heading, or a fenced block whose language is `json`, `typescript`, `proto`, `graphql` or `openapi`, or whose language or preceding line names an interface.

The hook never prints anything and never changes the prompt. Ordering: Claude Code runs the tool only after its PreToolUse hooks return, so a registration written synchronously by this hook is in the record before the sub-agent exists, and so before its first `claim(next=true)`. A detached write would race the sub-agent's first claim, so the hook writes synchronously and gives up after 2.5 s; a registration that did not finish in time may or may not have landed, and the spawn goes ahead either way.

### Small text artefacts are kept in the record (decision 44)

PostToolUse on the edit tools, in a relay run, for a file under the session's `scratchpad_dir` or under `scratchDir`. A text file (UTF-8, no NUL byte) under 32 KB is posted as a NOTE keyed `artifact:<path relative to that directory>` with its text and `hash` (`sha256:<hex>`). The hook keeps the hash of the last posted content per path, so a write with the same content posts nothing; the hash is kept before the post and dropped again when the post fails, so the next write retries. Skipped, with a line in the hook log naming only the relative path and why: binaries, files of 32 KB or more, files with a secret-looking name (`.env*`, `*.pem`, `*.key`, `id_rsa`, `auth.json`, `credentials`, `.netrc`, ...) and files any of the plugin's secret patterns (`lib/redact.py`) matches. Nothing leaves the machine before those checks. The post runs in a detached process (its own session, stdout and stderr to `/dev/null`, the content on its stdin), so the hook returns at once. The plugin also keeps a local copy of each posted artefact under `~/.ctrlb/state/`, for the session-start fallback.

### Artefacts at session start (decision 45)

SessionStart, when the environment carries a run id (`CTRLB_RUN`) and `source` is not `compact`. Under the hooks protocol the session no longer starts with the record block (open tasks and holders, contracts, handoffs, notes): the hooks hold the agent's task, so the injected text is who the agent is, how to talk to peers and, when one is held, its task's brief. The hook still reads this agent's artefacts (`GET .../artifacts?agent=`, inside 3 s) and writes those this machine does not have into `scratchDir` (never outside it; an existing file is left alone), falling back to the local copies of what this machine posted. When it wrote any, one line says where: `Your artefacts from the previous session are in <scratchDir>.` A read that fails or is slow leaves the line out.

### Durable scratch directory (D)

SessionStart creates `scratchDir` (mode 0700) and the rule text names it: "Put helper scripts and binaries in the scratch directory <dir>, not in /tmp." A sub-agent gets its parent's run, so the same directory.

### Hook-managed slots (decision 47)

For an edit of a file `autoSlots` lists (matched by its path relative to `cwd`), in a relay run, the PreToolUse hook claims `slot:<path>` with the file (`paths: [<path>]`), so the agent spends no turn on it; Seeker's `slot:` prefix is a valid key kind with a two-minute lease. The claim's token is kept like any claim's. When the answer is `held: false` with another agent as `held_by`, the edit is denied with `CtrlB Relay: REGISTRY.md is held by agent-b, try again in a moment.` The PostToolUse hook (and PostToolUseFailure, for an edit that failed) releases the slot when the hook holds it.

Fail-open: a claim that errors or does not answer inside 1.5 s lets the edit go ahead, with no edit check, and a line in the hook log. A slow Seeker must not stall every edit of a shared file; the cost is that two agents may write it at once while Seeker is slow, which `Edit`'s own exact-match replacement mostly catches. Only a definite answer that another agent holds the slot denies.

## Codex

| Hook | Codex |
|---|---|
| Spawn registers a task (43) | No: Codex's hooks give no sub-agent spawn tool input to read (its PreToolUse hook is installed for `apply_patch` only). |
| Artefacts (44) | No: Codex's PostToolUse hook is installed for `Bash` only. |
| The hooks protocol | Yes. SessionStart claims the next task. PreToolUse on `apply_patch` checks the patch's files, claims on write and renews. Codex 0.166 fires PostToolUse on `Bash` for each shell command, with `tool_input.command` but only the command's output as `tool_response`: no exit code (seen in a probe on 10 November). So the hook cannot tell a passing test from the call itself. Each PostToolUse, the Stop hook and SessionEnd (before its handoff) read the rollout's shell calls since the last scan, whose results carry the exit code, and send `done` for a passing run of a held task's test: one tool call after the test ran, with the next task's brief injected at PostToolUse (Stop and SessionEnd take no context). Codex runs the model's tools in code mode, one JavaScript `exec` per model call with one JSON result chunk per `exec_command`; the scan pairs each command with its own chunk, one to one, and leaves the exit code unknown otherwise. A run older than the machine's last `done` is skipped, so one test run never finishes two tasks that share a test command. A sub-agent's SubagentStart only joins; the sub-agent thread's own SessionStart claims. |
| Hook trust | Codex keeps a `trusted_content_hash` per hook: a change to the plugin's files (the hook script or the libraries it imports) or to the `python3` the command finds makes the hooks untrusted again, and then `codex exec` hung in our runs instead of running without them (Codex 0.166 and its 0.167 app server). A person runs `/hooks` again after each update. `CTRLB_HOOK_DUMP=<file>` makes `codex/hook.py` append each hook's input, secrets redacted, to that file. |
| Artefacts at session start (45) and scratch directory (D) | Yes: the scratch directory; artefacts only from the record (Codex posts none). |
| autoSlots (47) | No: with no hook after a patch, a slot claimed before it could not be released after it. Codex agents claim `slot:REGISTRY.md` themselves (see [Relay tools](relay-tools.md#slots)). |
| Session-end handoff (36) | Yes, with a fixed text. |

## Seeker reads and writes this needs

- `POST .../posts` takes `key` (`artifact:<clean relative path>`) and `hash` (at most 80 characters) for an artefact and keeps its text whole up to 32,768 bytes (longer is refused with 400, never cut); it answers `201 {"ok": true, "key": "artifact:..."}`. Peers leaves artefact notes out of its feed.
- `GET .../artifacts?agent=<agent>` answers `{agent, artifacts: [{key, path, agent, hash, ts, text}]}`: the newest artefact of each path of that agent, newest first, with its full text.
- A Seeker without the artifacts read (404) leaves the artefacts to the local copies of what this machine posted.
