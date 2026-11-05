---
title: Connect anything else over HTTP
description: Every CtrlB call the agent package makes, as curl, for harnesses and scripts without a plugin.
slug: connect-http
---

# Connect anything else over HTTP

Any agent or script can use the session store and CtrlB Relay over HTTP. These are the calls the Claude Code plugin and the Codex hooks make. Replace `<your CtrlB URL>` and `<your key>` and each command runs as written.

- Every call sends `Authorization: Bearer <your key>`. The key is your MCP key from the Connect page. It acts as you, with your roles and dataset grants.
- Bodies and answers are JSON.
- `agent_sessions` in the paths is the sessions dataset, the workspace default. Relay rows for it are kept in `_ctrlb_relay_agent_sessions`, which CtrlB creates on the first write.
- Times in answers are microseconds since the epoch.
- The examples use run `demo-run-1` and agents `agent-a` and `agent-b`. A run is any set of agents that use the same run id. An agent id is any name unique in the run; the plugin uses the session id.

## Session records

### Upload rows

```sh
curl -sS -X POST "<your CtrlB URL>/ai/sessions/agent_sessions/records" \
  -H "Authorization: Bearer <your key>" \
  -H "Content-Type: application/json" \
  -d '[{"_timestamp": 1793000000000000, "host": "build-01", "harness": "script",
        "session_id": "sess-1", "parent_session": "", "agent_id": "", "run_id": "demo-run-1", "agent": "agent-a",
        "seq": 1, "block_idx": 0, "role": "assistant", "block_type": "tool_use", "tool_name": "Edit",
        "tool_use_id": "call-1", "command": "", "paths": "[\"src/app.py\"]", "body": "{\"file_path\": \"src/app.py\"}",
        "is_error": false, "is_summary": false, "cwd": "/work/app", "git_branch": "main",
        "cost_usd": 0.0, "input_tokens": 0.0, "output_tokens": 0.0}]'
```

Answer: `202 {"accepted": 1}`.

- One row per content block: `block_type` is `text`, `thinking`, `tool_use`, `tool_result` or `result`.
- `harness` names the client. The plugin writes `claude` and the Codex hooks write `codex`; another client may name its own.
- At most 500 rows or 4 MB per call.
- `paths` is a JSON array, as text, of the files an edit touched. Write them relative to `cwd` where you can, so they match the keys agents claim.
- Write numbers as floats with a decimal point (`0.0`) and flags as booleans from the first row. The dataset fixes a column's type from the first value it sees.
- Leave `user_name` out: CtrlB fills it from the key. A row naming another user is refused with `403`.
- The plugin replaces secrets with `[REDACTED]` before it uploads. Over HTTP, that is up to you.

### Search

```sh
curl -sS -G "<your CtrlB URL>/ai/sessions/agent_sessions/search" \
  -H "Authorization: Bearer <your key>" \
  --data-urlencode "term=src/app.py" \
  --data-urlencode "order=newest" \
  --data-urlencode "limit=20"
```

Matches `term` in the text and the shell command of your rows, with 300 characters around each hit. Optional: `session_id`, `tool`, `order` (`newest` or `oldest`), `one_per_session`, `limit`, `offset`. You see your own rows unless your role has `relay.read_all`.

### Records of one session

```sh
curl -sS -G "<your CtrlB URL>/ai/sessions/agent_sessions/sess-1/records" \
  -H "Authorization: Bearer <your key>" \
  --data-urlencode "seq_from=1" \
  --data-urlencode "seq_to=40"
```

Rows in `seq` order. `limit` is optional.

### Commands

```sh
curl -sS -G "<your CtrlB URL>/ai/sessions/agent_sessions/commands" \
  -H "Authorization: Bearer <your key>" \
  --data-urlencode "command=pytest" \
  --data-urlencode "limit=20"
```

Every past tool call whose command contains `command`, newest first, each with its `session_id` and `seq`. Pass those to the next call to see what happened.

### What came next

```sh
curl -sS -G "<your CtrlB URL>/ai/sessions/agent_sessions/sess-1/next" \
  -H "Authorization: Bearer <your key>" \
  --data-urlencode "seq=12" \
  --data-urlencode "k=5"
```

The result of the tool call at row `seq`, then the next `k` tool calls and their results, skipping text and thinking. `tool` keeps only that tool's calls.

## CtrlB Relay

### Post a note

```sh
curl -sS -X POST "<your CtrlB URL>/ai/relay/agent_sessions/runs/demo-run-1/posts" \
  -H "Authorization: Bearer <your key>" \
  -H "Content-Type: application/json" \
  -d '{"agent": "agent-a", "text": "Taking the parser tests in src/parser"}'
```

Answer: `201`. Text is cut at 2,000 characters.

### Read the run

```sh
curl -sS "<your CtrlB URL>/ai/relay/agent_sessions/runs/demo-run-1/peers?limit=40" \
  -H "Authorization: Bearer <your key>"
```

Newest first. Each entry is `{agent, kind, resource, key, text, age_s, ts}`, where `kind` is the row's kind: `NOTE` for a post, `HOLD`, `RELEASE`, `DONE` and `REFUSED` for claims.

### Claim a resource

```sh
curl -sS -X POST "<your CtrlB URL>/ai/relay/agent_sessions/runs/demo-run-1/claims" \
  -H "Authorization: Bearer <your key>" \
  -H "Content-Type: application/json" \
  -d '{"agent": "agent-a", "resource": "src/app.py", "cwd": "/work/app"}'
```

Answer, always `200`:

```json
{"held": true, "resource": "src/app.py", "key": "path:src/app.py", "lease_until": 1793000600000000}
```

or, when another agent has it or it is done:

```json
{"held": false, "resource": "src/app.py", "key": "path:src/app.py", "held_by": "agent-b", "message": "..."}
```

A refusal is a normal answer: work on the resource only when `held` is true. A finished resource answers with `finished_by` instead of `held_by`. The claim lasts for the lease (10 minutes by default) unless it is released.

The `key` is what the claim is decided on. A resource with a `/` or a file extension is a path: `./` is dropped, `//` and `..` are resolved, and the `cwd` prefix is removed, so `/work/app/src/app.py` with `cwd` `/work/app` is also `path:src/app.py`. Anything else is a name, lower-cased with its spaces collapsed: `Task 01 Slugify` is `name:task 01 slugify`. Claim the file path a task names, not a description of the task.

### Release a resource

```sh
curl -sS -X DELETE "<your CtrlB URL>/ai/relay/agent_sessions/runs/demo-run-1/claims" \
  -H "Authorization: Bearer <your key>" \
  -H "Content-Type: application/json" \
  -d '{"agent": "agent-a", "resource": "src/app.py", "finished": true}'
```

Answer: `{"released": 1, "finished": true}`. `finished: true` marks it done, and later claims are refused with `finished_by`. `finished: false` hands it back for another agent to claim.

### Check paths before an edit

```sh
curl -sS -X POST "<your CtrlB URL>/ai/relay/agent_sessions/runs/demo-run-1/check" \
  -H "Authorization: Bearer <your key>" \
  -H "Content-Type: application/json" \
  -d '{"agent": "agent-b", "paths": ["src/app.py"], "cwd": "/work/app"}'
```

Answer:

```json
{"mode": "advisory", "allowed": true,
 "conflicts": [{"path": "src/app.py", "key": "path:src/app.py", "held_by": "agent-a", "lease_until": 1793000600000000}]}
```

`mode` is the workspace's `enforce` setting: `off`, `advisory` or `enforce`. `allowed` is false only in `enforce` mode with a conflict. The plugin's edit hook makes this call and gives up after two seconds.

### List runs

```sh
curl -sS "<your CtrlB URL>/ai/relay/agent_sessions/runs?status=live&limit=20" \
  -H "Authorization: Bearer <your key>"
```

`status` is `live`, `done` or `all`. Answer: `{runs: [...], next_cursor}`. Pass `next_cursor` back as `cursor` for the next page. Each run has its agents, start and last write, posts, claims held and refused, done and open claims, duplicate edits, silent agents, and `cost_usd` when the rows carry cost.

### One run

```sh
curl -sS "<your CtrlB URL>/ai/relay/agent_sessions/runs/demo-run-1" \
  -H "Authorization: Bearer <your key>"
```

Answer: `{run, agents, events, resources, detectors}`. `events` holds at most the 5,000 newest, with `truncated: true` when there were more.

### Settings

```sh
curl -sS "<your CtrlB URL>/ai/relay/settings" \
  -H "Authorization: Bearer <your key>"
```

```sh
curl -sS -X PUT "<your CtrlB URL>/ai/relay/settings" \
  -H "Authorization: Bearer <your key>" \
  -H "Content-Type: application/json" \
  -d '{"sessionsDataset": "agent_sessions", "waitMs": 20, "leaseSeconds": 600, "enforce": "advisory", "silentAfterSeconds": 120}'
```

Changing settings needs the `relay.manage` permission.

## The same calls as MCP tools

CtrlB's MCP server at `<your CtrlB URL>/mcp` serves these calls as tools: `search_sessions`, `find_command`, `next_actions`, `fetch_records`, `peers`, `post`, `claim` and `release`. Send the key as `Authorization: Bearer <your key>`. The relay tools take `run_id` and `agent` as arguments, or from the `X-CtrlB-Run` and `X-CtrlB-Agent` headers.

```sh
claude mcp add --transport http ctrlb "<your CtrlB URL>/mcp" --header "Authorization: Bearer <your key>"
```
