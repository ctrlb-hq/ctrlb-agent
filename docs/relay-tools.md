---
title: Relay tools
description: The seventeen tools CtrlB's MCP server gives an agent, and an eighteenth, done, their arguments and answers, and the three agent rules to paste into a prompt.
slug: relay-tools
---

# Relay tools

CtrlB's MCP server at `<your CtrlB URL>/mcp` gives an agent seventeen tools, and an eighteenth, `done`, that finishes a task with the evidence of its check. Four search the session store. Thirteen, and `done`, are CtrlB Relay. The Claude Code plugin and the Codex installer add the server for you, under the name `ctrlb`.

To add the server by hand in Claude Code:

```sh
claude mcp add --transport http ctrlb "<your CtrlB URL>/mcp" --header "Authorization: Bearer <your key>"
```

- Every call acts as the key's user, with that user's roles and dataset grants.
- Answers are compact JSON. Times are microseconds since the epoch.
- Every tool takes an optional `dataset`: the sessions dataset, the workspace's `sessionsDataset` setting (`agent_sessions` unless changed) when left out.
- The coordination tools come before a prose note (`post`): `handoff` to leave a task you hold to a named agent in one step, `ask` when you are blocked on something only another agent or the orchestrator can decide, `answer` to settle an open question by its id, `contract` when you finish an interface others will build on, and `contracts` before you build on another agent's interface. A note is for what none of them covers.
- `post`, `claim`, `release`, `handoff`, `ask`, `answer`, `brief`, `task` and `contract` write, and so does `done`. CtrlB registers them only when the server runs with write tools enabled (`MCP_WRITE_TOOLS_ENABLED`). If an agent has `peers` but no `claim`, ask your CtrlB administrator.

## Run id and agent id

The relay tools take `run_id` and `agent`. Each falls back to a header of the MCP connection when the argument is left out:

| Argument | Header | Set from |
|---|---|---|
| `run_id` | `X-CtrlB-Run` | `CTRLB_RUN` |
| `agent` | `X-CtrlB-Agent` | `CTRLB_AGENT` |

The plugin's `.mcp.json` and the Codex installer send both headers from the environment. When a variable is unset the header is empty, and the agent passes the ids from its session-start note:

```text
CtrlB Relay: your run id is <run> and your agent id is <agent>. ...
```

A call with no run id answers `run_id is required: pass run_id, or connect with the X-CtrlB-Run header set`. `post`, `claim`, `release`, `done`, `handoff`, `ask`, `answer`, `brief`, `task` and `contract` need an agent too.

## Session store tools

These read your own rows, unless your role has `relay.read_all`.

### search_sessions

Whole-word, case-insensitive search over messages, thinking, tool calls (with their shell commands) and tool results, across current and past sessions and their subagents.

| Argument | |
|---|---|
| `term` | Required. A word or phrase; it needs a word of 3 to 50 letters, digits, `.` or `+`. |
| `session_id` | Search one session. |
| `tool` | Only rows of this tool, for example `Bash`. |
| `order` | `newest` (default) or `oldest`. |
| `one_per_session` | Keep the first hit of each session. |
| `limit`, `offset` | 10 hits by default, at most 25. `offset` pages. |

Answer: `{total, offset, capped_at, hits: [{session_id, parent_session, seq, role, block_type, tool_name, is_error, snippet}]}`. The snippet is 300 characters around the hit. Read around a hit with `fetch_records`.

### find_command

Every past tool call whose command contains the text, ignoring runs of whitespace, newest first.

| Argument | |
|---|---|
| `command` | Required. The command text. |
| `limit`, `offset` | 10 by default, at most 25. |

Answer: `{total, offset, occurrences: [{session_id, seq, tool_name, command}]}`. Pass a `session_id` and `seq` to `next_actions` to see what happened.

### next_actions

What happened after one tool call: its result, then the next `k` tool calls and their results, skipping text and thinking.

| Argument | |
|---|---|
| `session_id`, `seq` | Required. The tool call. |
| `k` | Tool calls to follow: 3 by default, at most 8. |
| `tool` | Only this tool's calls, for example `Bash`. |

Answer: `{session_id, seq, events: [{seq, kind, tool_name, is_error, text}], skipped_text_records}`, where `kind` is `call` or `result`.

### fetch_records

The rows of one session with `seq_from <= seq <= seq_to`, in order.

| Argument | |
|---|---|
| `session_id`, `seq_from`, `seq_to` | Required. |

Answer: `{session_id, rows: [{ts, seq, block_idx, role, block_type, tool_name, tool_use_id, command, paths, is_error, body_len, body}]}`. At most 50 rows; each `body` is cut at 4,000 characters and `body_len` gives the full length.

## Relay tools

### peers

What the other agents in the run have posted, claimed and released, newest first. Call it before choosing a task.

| Argument | |
|---|---|
| `run_id`, `agent` | Optional, from the headers. `agent` only fills `you` in the answer. |
| `limit` | 40 by default, at most 500. |
| `kind` | Only events of this kind, for example `question`, `handoff` or `brief`. |
| `open` | `true`: only questions with no answer. `peers(kind="question", open=true)` is an orchestrator's inbox. |
| `since` | Only events after this `ts` (µs): the `ts` of the newest event you have seen. |
| `wait_s` | When no event matches yet, wait up to this many seconds (at most 55) for one instead of answering empty. `peers(kind="answer", since=<ts>, wait_s=30)` waits for your answer without a sleep loop. |

Answer: `{you, events: [{agent, kind, resource, key, text, age_s, ts, to?, id?, answered?}]}`. A `QUESTION` event carries its `id`, the agent it asks (`to`) and, once answered, `answered: {agent, ts, text}`. `kind` is `NOTE` for a post, `BRIEF` for a brief, `HOLD`, `REOPEN`, `RELEASE`, `DONE` and `REFUSED` for claims, and `CHECK` for an edit check that found another agent's claim. `text` is cut at 300 characters. You see your own user's rows unless your role has `relay.read_all`.

### post

A short note to the other agents in the run: what you are taking, or what you finished.

| Argument | |
|---|---|
| `text` | Required. Cut at 2,000 characters. |
| `run_id`, `agent` | Required, or from the headers. |

Answer: `{ok: true, run_id, agent}`.

### claim

An exclusive claim on a resource, a file path or a task name.

| Argument | |
|---|---|
| `resource` | Required. A file path, for example `src/app.py`, or a task name. |
| `run_id`, `agent` | Required, or from the headers. |
| `cwd` | Your working directory. An absolute path under it is keyed relative to it. |
| `reopen` | `true` to claim a finished task name anyway and start a new round of work on it. It has no effect on a key that is not finished. |
| `paths` | Files (or `slot:<name>` slots) that come with the resource: all are taken in one step with it, or none is (at most 32). A task your orchestrator registered brings its files when you leave this out. |
| `wait_s` | Seconds to wait in line (at most 55) when another agent holds it, instead of a refusal at once: the claim is held as soon as it is your turn, or answers `reason: timed_out`. Waiters are served in the order they claimed. Available, not recommended: in the benchmark waiting did not beat claiming something else, and never wait on a stalled hold (take it over). |
| `takeover` | `true` to take over a hold whose holder has stopped: the first live hold is another agent's (of your user) and is stalled, that is its holder wrote no relay row on the key or its files and edited none of them for the workspace's `stallAfterSeconds` (300 by default). The holder's hold is handed to you in one write (a `HANDOFF` with the holder as giver and the text `stalled: taken over by <you>`), with the task's files; its next edit of them is refused. On a free key, or one whose holder's lease ran out, it is an ordinary claim. |

Answer when the claim is yours:

```json
{"held": true, "resource": "src/app.py", "key": "path:src/app.py", "lease_until": 1793000600000000}
```

Answer when another agent holds it, or it is done:

```json
{"held": false, "resource": "src/app.py", "key": "path:src/app.py", "held_by": "agent-b", "message": "src/app.py is already held by agent-b; pick something else"}
```

```json
{"held": false, "resource": "slugify", "key": "name:slugify", "finished_by": "agent-b", "finished_at": 1793000300000000, "message": "slugify was already finished by agent-b at 2026-11-07T10:05:00Z. If you were asked to work on it again, claim it with reopen=true to start a new round of work; otherwise pick something else"}
```

Answer to `claim(resource="task 03 median")` when the orchestrator registered that task with its file:

```json
{"held": true, "resource": "task 03 median", "key": "name:task 03 median", "lease_until": 1793000600000000, "paths": ["path:src/median.py"]}
```

Answer when a file of the task is held by another agent: nothing is taken.

```json
{"held": false, "resource": "task 03 median", "key": "name:task 03 median", "paths": ["path:src/median.py"], "held_by": "agent-b", "conflict_key": "path:src/median.py", "message": "task 03 median was not claimed: path:src/median.py, which comes with it, is already held by agent-b; nothing was taken. Pick something else"}
```

Answer when the run has registered tasks and none has that name:

```json
{"held": false, "resource": "median", "key": "name:median", "reason": "unknown_task", "suggestion": "task 03 median", "message": "median is not a task of this run. Did you mean \"task 03 median\"? Claim the task by the name your orchestrator registered"}
```

Answer to `claim(resource="task 03 median", wait_s=30)` when another agent still held it after 30 s (a claim that got its turn answers `held: true` with `waited_ms`):

```json
{"held": false, "resource": "task 03 median", "key": "name:task 03 median", "reason": "timed_out", "held_by": "agent-b", "position": 2, "waited_ms": 30000, "message": "task 03 median is still held by agent-b after waiting 30s; nothing was taken. Pick something else"}
```

Answer to `claim(resource="task 03 median", takeover=true)` when agent-b stopped while holding it (`tasks` showed it `stalled`): you hold it and its files. Read its evidence and notes (`tasks`, `briefs`, `peers`) and continue it.

```json
{"held": true, "resource": "task 03 median", "key": "name:task 03 median", "lease_until": 1793000900000000, "paths": ["path:src/median.py"], "token": "9f2c...", "taken_over_from": "agent-b", "stall": {"holder": "agent-b", "last_activity": "edit", "last_activity_ts": 1793000200000000, "idle_s": 310, "stall_after_s": 300}}
```

Answer when the holder is still working: nothing is written. Pick other work.

```json
{"held": false, "resource": "task 03 median", "key": "name:task 03 median", "held_by": "agent-b", "reason": "not_stalled", "stall": {"holder": "agent-b", "last_activity": "edit", "last_activity_ts": 1793000500000000, "idle_s": 40, "stall_after_s": 300, "stalled": false}, "message": "task 03 median is held by agent-b, whose last progress on it (edit) was 40 s ago; a hold is stalled after 300 s with no relay row and no edit, so it was not taken over and nothing was written. Pick something else, or look at tasks later"}
```

`reason: other_user` means another user's agent holds it: only that user's agents can take it over. A claim without `takeover` that is refused because the holder's hold is stalled says so: the answer carries `stall` and its message ends "claim it with takeover=true instead of waiting". This is how a stalled bare file, which `tasks` never lists, shows. A bare file works the same way: `claim(resource="REGISTRY.md", takeover=true)` when the agent holding `REGISTRY.md` has stopped.

Answer to a claim of a file that belongs to a registered task you do not hold:

```json
{"held": false, "resource": "src/median.py", "key": "path:src/median.py", "reason": "belongs_to_task", "task": "task 03 median", "held_by": "agent-b", "message": "src/median.py belongs to the task \"task 03 median\" (agent-b holds it): its files are held through the task. Claim \"task 03 median\" instead, and its files come with it"}
```

A held claim of a task whose registration lists `ask_about` answers them: `"ask_about": ["which date format the API returns"]`. Ask the task's owner about those before deciding them yourself.

A held claim of a task whose registration names a test command answers it: `"test": "python3 -m pytest tests/test_median.py"`. Run it before you call `done`, and give it as the evidence's `command`.

Answer to `claim(resource="slugify", reopen=true)` on that finished task:

```json
{"held": true, "resource": "slugify", "key": "name:slugify", "lease_until": 1793000900000000, "reopened": true}
```

- A refusal is a normal answer, not an error. Pick other work; do not retry the same resource in a loop.
- `key` is what the claim is held under. A resource with a `/` or a file extension is a path: `./` is dropped, `//`, `/./` and `..` are resolved, and the `cwd` prefix is removed. Anything else is a name, lower-cased with its whitespace collapsed. A file with no extension, such as `Makefile`, is a name unless you write `path:Makefile`.
- Only a task name can be finished. A file path is never finished, so `finished_by` never appears on a `path:` key.
- `reopen=true` on a finished task writes a `REOPEN` row that starts a new round of work. If two agents reopen at once, one holds it and the other is refused with `held_by`. The earlier rounds stay in the record.
- A held claim answers `token`, the nonce of your hold. The Claude Code plugin keeps it and sends it with the edit check, so CtrlB can tell you when a task you held was reopened by another agent (`stale_round`).
- The claim lasts until `lease_until` (10 minutes by default) unless it is released. Claiming a key you already hold renews it: `held: true, renewed: true`, with the `lease_until` of the new hold.

### release

Give a claim back, or mark a task done. Finish tasks, just release files.

| Argument | |
|---|---|
| `resource` | Required. The resource as you claimed it. |
| `finished` | Task names only. `true` (default): the task is done, and later claims are refused with `finished_by` unless they pass `reopen=true`. `false`: hand it back for another agent to claim. A file path is always handed back, whatever `finished` says. |
| `cwd` | The working directory the claim was made with, if any. |
| `run_id`, `agent` | Required, or from the headers. |

Answer: `{released, finished, resource, key, message?, paths?}`. `paths` lists the files of a task (or of a claim made with `paths`) handed back with it. `released` counts your live holds on the key. It is 0 when you held none, for example after the lease ran out; then nothing is written and the key is not done. Claim it again, then release it. On a file path `finished` is always `false`, and when you asked for `finished=true` the `message` says the file was handed back instead.

If you claimed an absolute path with `cwd`, release it with the same `cwd`, or release the `key` the claim answered, for example `path:src/app.py`.

`finished` is replaced by `done`: a release that finishes a task name answers `"deprecated": "finished is replaced by done(resource)"`, and a workspace that requires evidence refuses it with `"reason": "evidence_required"` and writes nothing. Finish tasks with `done`.

#### done (finish a task)

Finish a task you hold, when its test passes, and say what you checked. Run the task's test command (`tasks` and `claim` show it as `test`) and give that command, its exit code and its last output line as the evidence; when the task has no test command, give the check you ran. A task is finished only when the record shows what was checked.

| Argument | |
|---|---|
| `resource` | Required. The task name as you claimed it. A file path is never done: release it (`a file path is never done: release it`). |
| `evidence` | `{command, exit_code, summary}`: the test or check command you ran, its exit code, and one line of its output (at most 300 characters). Free text is kept as the summary. |
| `cwd` | The working directory the claim was made with, if any. |
| `run_id`, `agent` | Required, or from the headers. |

```text
done(resource="task 03 median", evidence={"command": "python3 -m pytest tests/test_median.py", "exit_code": 0, "summary": "4 passed in 0.03s"})
```

Answer: `{done, resource, key, evidence?, finished_after_lease?, paths?, message?, test?}`. `done: true` finishes the task: later claims are refused with `finished_by` unless they pass `reopen=true`, and its files are handed back with it (`paths`). The evidence is stored with the finish, and `tasks` and the run view show it. A done whose command is not the task's test is still accepted; the run view records `evidence_matches_test: false` for it and counts the tasks finished with their own test as `done_with_task_test`.

When the workspace requires evidence (the `evidenceRequired` setting), a done that does not name the command is refused, and nothing is written:

```json
{"done": false, "resource": "task 03 median", "key": "name:task 03 median", "reason": "evidence_required", "message": "say which command you ran and its exit code; the task's test command is: python3 -m pytest tests/test_median.py", "test": "python3 -m pytest tests/test_median.py"}
```

`test` (and the end of the message) is the task's test command, when its registration names one: run it and call `done` again with it. Otherwise call `done` again with the command you ran. The Claude Code plugin remembers your last test command (`pytest`, `python -m unittest`, `go test`, `npm test`, `cargo test`, `make test`), its exit code and last output line, and quotes it after such a refusal so you can repeat the call with it; when the refusal carries the task's `test` and your last test command is not it, it shows the task's test instead and tells you to run it. It never fills the evidence in for you. Codex gets the refusal only.

### handoff

Hand a claim you hold to another agent of the run in one step. From then on it holds it, and you do not; no third agent can take it in between, as it could if you released it and the other agent claimed it.

| Argument | |
|---|---|
| `resource` | Required. The resource as you claimed it. A task you hold with its files is handed with its files. |
| `to` | Required. The agent id to hand it to. |
| `cwd` | The working directory the claim was made with, if any. |
| `run_id`, `agent` | Required, or from the headers. |

Answer when handed: `{handed: true, resource, key, to, lease_until, token, paths?}`. `lease_until` is the receiver's; the receiver can renew by claiming the resource again.

Answer when you are not the holder: `{handed: false, resource, key, to, held_by?, message}`. Nothing is written.

- Handing to yourself renews your claim.
- Hand off to an agent of your own user (an agent id under the same key).

### ask

Ask a question you are blocked on and only another agent or the orchestrator can decide. Do not post a QUESTION note: a note cannot be answered, so nobody can tell whether it is still open.

| Argument | |
|---|---|
| `text` | Required. Cut at 2,000 characters. |
| `to` | The agent id to ask. Leave it out to ask anyone (the orchestrator). |
| `run_id`, `agent` | Required, or from the headers. |

Answer: `{id, run_id, agent}`. The question stays open until someone calls `answer` with that id.

### answer

Answer an open question by its id, from `peers(kind="question", open=true)` or the run view.

| Argument | |
|---|---|
| `id` | Required. The question's id. |
| `text` | Required. Cut at 2,000 characters. |
| `run_id`, `agent` | Required, or from the headers. |

Answer: `{ok: true, id, run_id, agent}`. The first answer closes the question; an id that is no question of the run is an error.

### brief

A short note about a file or document you read, keyed by that resource, so the other agents of the run can read the brief instead of reading the whole file again.

| Argument | |
|---|---|
| `resource` | Required. The file path or document name the brief is about. It is keyed exactly as `claim` keys it. |
| `text` | Required. Up to a few hundred words: what the resource is for, what it exports or decides, and what not to touch. Cut at 2,000 characters. |
| `cwd` | Your working directory. An absolute path under it is keyed relative to it. |
| `run_id`, `agent` | Required, or from the headers. |

Answer: `{ok: true, run_id, agent, key, resource}`.

- A brief is not a claim and reserves nothing: anyone can write one at any time, and the newest one for a key is what `briefs` returns.
- Write a brief after reading a file you will edit, or one that other agents will need.

### briefs

The newest brief of each resource in the run, newest first. Call it before reading a file or document that other agents may already have read.

| Argument | |
|---|---|
| `resources` | The resources to look up: a list, or one comma-separated string, at most 200, keyed as `claim` keys them. Left out: the newest brief of every resource in the run. |
| `limit` | 40 by default, at most 200. |
| `cwd` | Your working directory, for absolute paths. |
| `run_id` | Optional, from the header. |

Answer: `{briefs: [{key, resource, agent, text, ts, age_s, paths?, changed_since?}]}`. A brief that names files (`paths`) also carries `changed_since`, those of its files claimed or edited after it. A resource with no brief is left out of the answer. You see your own user's briefs unless your role has `relay.read_all`.

### task

For an orchestrator: register a task of the run, with the files that come with it. A claim of the task then takes its files in the same step, and a claim of a task name the run did not register is refused with `reason: unknown_task` and the likeliest registered name as `suggestion`.

| Argument | |
|---|---|
| `name` | Required. The task name agents will claim, for example `task 03 median`. Not a file path. |
| `text` | Required. What the task is. Cut at 2,000 characters. |
| `paths` | Required. The task's files, at least one, at most 32. |
| `ask_about` | Optional. The facts or questions whoever takes the task should ask you about (at most 16). `tasks` and a held claim of the task show them. |
| `test` | Optional. The command that tests the task, for example `python3 -m pytest tests/test_median.py` (one line, at most 500 bytes). `tasks` and a held claim of the task show it, and its holder gives it as `done`'s evidence. |
| `cwd` | Your working directory, for absolute paths. |
| `run_id`, `agent` | Required, or from the headers. |

Answer: `{ok: true, run_id, agent, key, name, paths, ask_about?, test?}`. Registering a name again replaces its text, files, `ask_about` and `test`.

### tasks

The run's registered tasks and their state now.

| Argument | |
|---|---|
| `run_id` | Optional, from the header. |

Answer: `{tasks: [{name, key, text, paths, agent, ts, state, held_by?, finished_by?, round, ask_about?}]}`, and `test` when the task's registration names one. `state` is `open`, `held` or `finished` in the task's current round; `ask_about` is what the task's owner wants its holder to ask about; `test` is the command that tests the task. A finished task with a `test` also carries `evidence_matches_test`: whether its done's command named that test. Each task also carries `evidence` (`{command, exit_code, summary}`, what the `done` that finished it says was checked) when it was finished with evidence, and `stalled`: `true` when it is held (or its holder's lease ran out) and not finished, and its holder wrote nothing on it and edited none of its files for the workspace's `stallAfterSeconds` (300 by default). Claim a stalled task you need with `takeover=true`; never wait in line on it. The run view counts them as `stalled_tasks`, and lists every stalled hold, bare files and slots too, in `detectors.stalled` (with `kind` and `resource`; `stalled_holds` is the total).

### contract

When you finish an interface others will build on (a type, a route, a config shape), write its contract: the shape in a few lines, and the files that define it. Use it instead of a `CONTRACT:` note: a note names no files, so nobody sees when the interface changes after it.

| Argument | |
|---|---|
| `name` | Required. The interface's name, for example `seeker-4 routes`. It is keyed `contract:<name>`. |
| `text` | Required. The shape in a few lines. Cut at 2,000 characters. |
| `paths` | Required. The files that define it, at least one, at most 32. |
| `cwd` | Your working directory, for absolute paths. |
| `run_id`, `agent` | Required, or from the headers. |

Answer: `{ok: true, run_id, agent, key, paths}`. Writing a name again replaces its contract. A contract is never claimed.

### contracts

Read the contracts of the run before building on an interface another agent finished.

| Argument | |
|---|---|
| `names` | Only these contract names: a list, or one comma-separated string. Left out: every contract of the run. |
| `run_id`, `agent` | Optional, from the headers. With `agent`, each contract says whether it changed since you last read the contracts. |
| `since` | When you last read the contracts: the `read_at` of that answer. Left out, your last `contracts` call is used. |

Answer: `{contracts: [{name, key, agent, ts, age_s, text, paths, changed_since, text_since, text_changed_at?, changed_since_read?}], last_read?, read_at?}`, the newest contract of each name, newest first. `changed_since` lists its files claimed or edited after its current text was first written (`text_since`; writing the same text again does not reset it): read those files before trusting the contract. `text_changed_at` is set when the text changed from an earlier version. `changed_since_read` is true when the contract's text or files changed since you last read the contracts: read it again before building on it.

## Edits need a claim (hold required)

When the workspace turns on `holdRequired` with `enforce`, the plugin's edit check refuses an edit of a file you do not hold, and names the reason and the claim call to make:

| Reason | When | Do |
|---|---|---|
| `held` | Another agent holds the file. | Leave it to its holder and pick other work. |
| `lapsed` | You held the file, your lease ran out, and nobody holds it now. | Claim it again, then edit. |
| `unheld` | Nobody holds the file and you never claimed it (or you released it). | Claim it before editing. |
| `stale_round` | You held the file's task in an earlier round, and another agent reopened it. | Leave it to the round's holder. |

An edit of a file you hold past half its lease renews the claim, so a long edit session does not run out. In `advisory` mode the same reasons come back as a note and the edit goes ahead.

## Slots

A slot is a shared thing that is not a file: the dev server, a port, a database migration, the browser that takes screenshots, the commit. Two agents that start the dev server at once collide on the port even when they edit different files. Claim the slot before using the thing, and release it when you are done:

```text
claim(resource="slot:devserver")    # before npm run dev
release(resource="slot:devserver")  # after
```

- Names to use: `slot:devserver`, `slot:port-3000`, `slot:db-migrate`, `slot:screenshots`, `slot:commit`.
- A slot's claim lasts two minutes, whatever the workspace's lease is. Claim it again before `lease_until` to keep it longer.
- A slot is never finished: `release` always hands it back, like a file.
- `slot:` is case-sensitive: `Slot:devserver` is a task name, not a slot.

## The three agent rules

These are the rules from the research, worded for the relay tools. Paste them into the agent's system prompt or `AGENTS.md`, or run `/ctrlb-agent:relay <run id>` in Claude Code, which loads them as a skill.

```text
1. Other agents are working in this same directory right now. Before starting a task, look at the task's files to see whether it is already done or in progress, and pick one that is not.
2. Other agents are working in this same directory right now and share a relay with you (the ctrlb tools). Before starting a task, call peers to see what others have taken or finished, then post the task you are taking, do it, and post when it is done. Never take a task another agent has posted.
3. Other agents are working in this same directory right now and share a relay with you (the ctrlb tools). Claim the task by the name your orchestrator registered (for example claim(resource="task 03 median"); tasks lists them). Its files come with it. If held is false, pick another task. Work only while you hold it. When the task is done, release it with finished=true. A file that belongs to no task is claimed by its path.
```

For a script that starts several agents in one run, save the rules to `relay-rules.txt` and give every agent the same run id:

```sh
export CTRLB_RUN=nightly-refactor-1
for n in 1 2 3; do
  CTRLB_AGENT="builder-$n" claude -p "Work through TASKS.md, one task at a time." \
    --append-system-prompt "$(cat relay-rules.txt)" &
done
wait
```

In the research, rule 3 is the one that cut duplicate work from 27 of 96 tasks to 1. Register the run's tasks first, from the orchestrator or the script that starts the agents (`task(name, text, paths)` for each, or `POST .../briefs` with `paths`): a claim of a registered task then takes the task and its files in one step, so no two agents can each hold half of it, and a misspelled task name is refused with the registered one as `suggestion`. The files are handed back when the task is released, so another agent can edit them later; the task, released with `finished=true`, is what stops another agent from doing it again.

To have agents share what they read, add this paragraph to the rules:

```text
Before reading a file or document that other agents of the run may also need, call briefs for it and read the brief first. After reading a file you will edit, or one that others need, post a brief for it with up to a few hundred words: what it is for, what it exports or decides, and what not to touch.
```
