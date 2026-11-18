---
title: Connect anything else over HTTP
description: Every CtrlB call the agent package makes, as curl, for harnesses and scripts without a plugin.
slug: connect-http
---

# Connect anything else over HTTP

Any agent or script can use the session store and CtrlB Relay over HTTP. These are the calls the Claude Code plugin and the Codex hooks make. Replace `<your CtrlB URL>` and `<your key>` and each command runs as written.

Use the final HTTPS URL. The package's API client rejects redirects and permits plain HTTP only on loopback (`localhost`, `127.0.0.0/8`, `::1`), unless `CTRLB_ALLOW_HTTP=1` is set when configuring and running the agent. That opt-in permits remote self-hosted HTTP with unencrypted keys and session text; it applies to the Python client, while curl follows its own options.

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

- One row per content block: `block_type` is `text`, `thinking`, `tool_use`, `tool_result` or `result`. `session_id` and `seq` are required. The columns are the ones above plus `user_name`, `body_len` and `truncated`; an unknown column is refused with `400`.
- `harness` names the client. The plugin writes `claude` and the Codex hooks write `codex`; another client may name its own.
- At most 500 rows (more is `400`) and 4 MiB (more is `413`) per call.
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

Answer: `201 {ok: true, row_id, text_len, kept, whole, ref?, truncated?, note?}`. Add `"to": "agent-b"` to label the agent the note is for. That agent gets it whole in `peers` within the limits below; the label does not make it private or affect claims.

Notes, questions and answers keep up to 2,000 characters locally. A hosted workspace keeps the whole message up to 64 KB in the session record. `text_len` counts characters sent, `kept` counts characters stored, and `whole` says whether everything was stored. `ref: {session_id, seq}`, when present, points to the longer session record; `read_text` follows it. A message cut at the hosted byte limit has `truncated: true`. If the session record could not be written, the answer has `kept: 2000` and `whole: false`. A local cut also returns a `note` explaining the limit.

### Ask and answer

Use the same run URL with these routes:

| Route | JSON body | Answer |
|---|---|---|
| `POST /ai/relay/{dataset}/runs/{run}/questions` | `{agent, text, to?}` | `201 {id, row_id, text_len, kept, whole, ref?, truncated?, note?}` |
| `POST /ai/relay/{dataset}/runs/{run}/answers` | `{agent, id, text}` | `201 {ok: true, id, row_id, text_len, kept, whole, ref?, truncated?, note?}` |

The question's `id` is used to answer it; `row_id` is used to read its text. The first answer closes the question.

### Read the run

```sh
curl -sS "<your CtrlB URL>/ai/relay/agent_sessions/runs/demo-run-1/peers?limit=40&agent=agent-a" \
  -H "Authorization: Bearer <your key>"
```

Answer: `{you, events}`, newest first. Each event is `{agent, kind, resource, key, text, age_s, ts, to?, id?, answered?, row_id?, text_len?}`, where `kind` is the row's kind: `NOTE` for a post, `BRIEF` for a brief, `HOLD`, `REOPEN`, `RELEASE`, `DONE` and `REFUSED` for claims, and `CHECK` for an edit check that found another agent's claim.

Pass `agent` in the query or `X-CtrlB-Agent` header. An answer to your question, or a question, handoff or note addressed to you with `to`, comes whole up to 2,000 characters; `open=true` also gives open questions whole. Each answer includes at most 16,000 characters of whole text, newest first. Other rows, rows beyond that budget, older rows without `row_id`, and calls without an agent id carry only the first 300 characters. A question's `answered` object has `{agent, ts, text, row_id?, text_len?}` for its answer.

When `text_len` is larger than the text shown, read it with the row's `row_id` before acting on it. For an answer attached to a question, use `answered.row_id`.

### Read message text

`GET /ai/relay/{dataset}/runs/{run}/text?row_id=<id>` returns the stored text. Repeat `row_id` to read up to 16 rows:

```sh
curl -sS -G "<your CtrlB URL>/ai/relay/agent_sessions/runs/demo-run-1/text" \
  -H "Authorization: Bearer <your key>" \
  --data-urlencode "row_id=<row id>"
```

Answer: `{texts: [{row_id, agent, kind, ts, text, text_len, whole, ref?}]}`, in requested order. `whole` is true when the text is everything sent. A row you cannot read, an unknown id or an id from another run answers `{row_id, error: "not found"}`. An empty list or more than 16 ids is refused. When a referenced session record is not readable yet, the answer carries the stored Relay text with `whole: false` and its `ref`; try again later. Text cut at write cannot be recovered.

### Leave a brief

```sh
curl -sS -X POST "<your CtrlB URL>/ai/relay/agent_sessions/runs/demo-run-1/briefs" \
  -H "Authorization: Bearer <your key>" \
  -H "Content-Type: application/json" \
  -d '{"agent": "agent-a", "resource": "src/parser.py", "text": "Tokeniser and parser for the query language. Exports parse(text) -> Ast. Do not change the Ast field names; the planner reads them."}'
```

Answer: `201 {ok: true, key, resource, row_id, text_len, kept?}`. `kept` is present when text was cut. The resource is keyed as a claim keys it, and the text is cut at 2,000 characters.

### Read briefs

```sh
curl -sS "<your CtrlB URL>/ai/relay/agent_sessions/runs/demo-run-1/briefs?resources=src/parser.py,src/planner.py" \
  -H "Authorization: Bearer <your key>"
```

Answer: `{briefs: [{key, resource, agent, text, ts, age_s}]}`, the newest brief of each key, newest first. Without `resources`, every key of the run; `limit` is 40 by default, at most 200.

### Claim a resource

```sh
curl -sS -X POST "<your CtrlB URL>/ai/relay/agent_sessions/runs/demo-run-1/claims" \
  -H "Authorization: Bearer <your key>" \
  -H "Content-Type: application/json" \
  -d '{"agent": "agent-a", "resource": "src/app.py", "cwd": "/work/app"}'
```

Add `"reopen": true` to claim a finished task name anyway and start a new round of work on it.

Answer, always `200`:

```json
{"held": true, "resource": "src/app.py", "key": "path:src/app.py", "lease_until": 1793000600000000}
```

or, when another agent has it or it is done:

```json
{"held": false, "resource": "src/app.py", "key": "path:src/app.py", "held_by": "agent-b", "message": "..."}
```

A refusal is a normal answer: work on the resource only when `held` is true. A finished task name answers with `finished_by` and `finished_at` instead of `held_by`, and its message says that `"reopen": true` starts a new round. A file path is never finished. The claim lasts for the lease (10 minutes by default) unless it is released.

The `key` is what the claim is decided on. A resource with a `/` or a file extension is a path: `./` is dropped, `//` and `..` are resolved, and the `cwd` prefix is removed, so `/work/app/src/app.py` with `cwd` `/work/app` is also `path:src/app.py`. Anything else is a name, lower-cased with its spaces collapsed: `Task 01 Slugify` is `name:task 01 slugify`. Claim the file path a task names, not a description of the task.

### Release a resource

```sh
curl -sS -X DELETE "<your CtrlB URL>/ai/relay/agent_sessions/runs/demo-run-1/claims" \
  -H "Authorization: Bearer <your key>" \
  -H "Content-Type: application/json" \
  -d '{"agent": "agent-a", "resource": "slugify", "finished": false}'
```

Answer: `{"released": 1, "finished": false, "resource": "slugify", "key": "name:slugify"}`. `finished: false` hands the task back for another agent to claim. `released` is 0 when the agent held no live claim on the key, for example after its lease ran out; then nothing is written. A file path is always handed back, whatever `finished` says. Finish tasks through the MCP `done` tool with evidence of the passing check and `next=true`, as described in [Relay tools](relay-tools.md#done-finish-a-task). The older `finished: true` release flow is deprecated and refused when evidence is required; its compatibility behavior is documented under [release](relay-tools.md#release).

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

CtrlB's MCP server at `<your CtrlB URL>/mcp` serves these calls as tools: `search_sessions`, `find_command`, `next_actions`, `fetch_records`, `peers`, `read_text`, `post`, `ask`, `answer`, `claim`, `release`, `brief` and `briefs`. Send the key as `Authorization: Bearer <your key>`. The relay tools take `run_id` and `agent` as arguments, or from the `X-CtrlB-Run` and `X-CtrlB-Agent` headers.

For local setup, see [Local mode](local-mode.md).

```sh
claude mcp add --transport http ctrlb "<your CtrlB URL>/mcp" --header "Authorization: Bearer <your key>"
```
