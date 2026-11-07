---
title: Relay tools
description: The eight tools CtrlB's MCP server gives an agent, their arguments and answers, and the three agent rules to paste into a prompt.
slug: relay-tools
---

# Relay tools

CtrlB's MCP server at `<your CtrlB URL>/mcp` gives an agent eight tools. Four search the session store. Four are CtrlB Relay. The Claude Code plugin and the Codex installer add the server for you, under the name `ctrlb`.

To add the server by hand in Claude Code:

```sh
claude mcp add --transport http ctrlb "<your CtrlB URL>/mcp" --header "Authorization: Bearer <your key>"
```

- Every call acts as the key's user, with that user's roles and dataset grants.
- Answers are compact JSON. Times are microseconds since the epoch.
- Every tool takes an optional `dataset`: the sessions dataset, the workspace's `sessionsDataset` setting (`agent_sessions` unless changed) when left out.
- `post`, `claim` and `release` write. CtrlB registers them only when the server runs with write tools enabled (`MCP_WRITE_TOOLS_ENABLED`). If an agent has `peers` but no `claim`, ask your CtrlB administrator.

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

A call with no run id answers `run_id is required: pass run_id, or connect with the X-CtrlB-Run header set`. `post`, `claim` and `release` need an agent too.

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

Answer: `{you, events: [{agent, kind, resource, key, text, age_s, ts}]}`. `kind` is `NOTE` for a post, `HOLD`, `REOPEN`, `RELEASE`, `DONE` and `REFUSED` for claims, and `CHECK` for an edit check that found another agent's claim. `text` is cut at 300 characters. You see your own user's rows unless your role has `relay.read_all`.

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

Answer to `claim(resource="slugify", reopen=true)` on that finished task:

```json
{"held": true, "resource": "slugify", "key": "name:slugify", "lease_until": 1793000900000000, "reopened": true}
```

- A refusal is a normal answer, not an error. Pick other work; do not retry the same resource in a loop.
- `key` is what the claim is held under. A resource with a `/` or a file extension is a path: `./` is dropped, `//`, `/./` and `..` are resolved, and the `cwd` prefix is removed. Anything else is a name, lower-cased with its whitespace collapsed. A file with no extension, such as `Makefile`, is a name unless you write `path:Makefile`.
- Only a task name can be finished. A file path is never finished, so `finished_by` never appears on a `path:` key.
- `reopen=true` on a finished task writes a `REOPEN` row that starts a new round of work. If two agents reopen at once, one holds it and the other is refused with `held_by`. The earlier rounds stay in the record.
- The claim lasts until `lease_until` (10 minutes by default) unless it is released. Claiming a key you already hold renews it: `held: true, renewed: true`, with the `lease_until` of the new hold.

### release

Give a claim back, or mark a task done. Finish tasks, just release files.

| Argument | |
|---|---|
| `resource` | Required. The resource as you claimed it. |
| `finished` | Task names only. `true` (default): the task is done, and later claims are refused with `finished_by` unless they pass `reopen=true`. `false`: hand it back for another agent to claim. A file path is always handed back, whatever `finished` says. |
| `cwd` | The working directory the claim was made with, if any. |
| `run_id`, `agent` | Required, or from the headers. |

Answer: `{released, finished, resource, key, message?}`. `released` counts your live holds on the key. It is 0 when you held none, for example after the lease ran out; then nothing is written and the key is not done. Claim it again, then release it. On a file path `finished` is always `false`, and when you asked for `finished=true` the `message` says the file was handed back instead.

If you claimed an absolute path with `cwd`, release it with the same `cwd`, or release the `key` the claim answered, for example `path:src/app.py`.

## The three agent rules

These are the rules from the research, worded for the relay tools. Paste them into the agent's system prompt or `AGENTS.md`, or run `/ctrlb-agent:relay <run id>` in Claude Code, which loads them as a skill.

```text
1. Other agents are working in this same directory right now. Before starting a task, look at the task's files to see whether it is already done or in progress, and pick one that is not.
2. Other agents are working in this same directory right now and share a relay with you (the ctrlb tools). Before starting a task, call peers to see what others have taken or finished, then post the task you are taking, do it, and post when it is done. Never take a task another agent has posted.
3. Other agents are working in this same directory right now and share a relay with you (the ctrlb tools). Before starting a task, call claim with the file path the task names; work on it only if held is true, otherwise claim a different task. When its test passes, call release on it.
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

In the research, rule 3 is the one that cut duplicate work from 27 of 96 tasks to 1. Claim the file path a task names, not a description of the task: two descriptions of one task are two keys.
