# Relay: shared context and coordination for AI agents, from the session store (plan, 2026-11-05)

This is the build plan for shipping the swarm work of 2026-11-04 as a CtrlB feature. The research
behind it is two papers, `~/benchmarks/paper/session-store-long-running.tex` (one agent, a record
that survives compaction) and `~/benchmarks/paper/session-store-swarms.tex` (many agents, one
record they coordinate through), and the working code under `~/benchmarks/swarm/` and
`~/benchmarks/ctrlb-memory-plugin/`. Everything measured there ran against the lake with no lake or
Seeker change. The product work is therefore packaging, one Seeker service, one Orion page and the
connect path for customers who run Claude Code or Codex.

Names. CtrlB is positioned as the AI Agent Operations Platform (owner's choice, 2026-11-05); its
capabilities are AI Observability, Evals, Guard, Relay and Self-Improving Agents (Skill Optimization
inside it). This plan builds **CtrlB Relay**, descriptor "Shared context and coordination for AI
agents"; the mechanism is CtrlB's agent coordination protocol. Underneath it is the **session
store**: every agent's record (messages, tool calls, results) as rows in the customer's lake,
searchable by the agent itself and by people. Relay is what agents in one run do with that record:
`peers`, `post`, `claim`, `release`. A **run** is a set of agents that share
a run id; an **agent** is one session in a run; a **post** is a note on the relay; a **claim** is an
exclusive hold on a **resource** (a file path or a task name) with a **lease**; a finished claim is
**done**. The **run view** is the Orion page. "Dataset" means a stream everywhere in this codebase.
Do not use "blackboard", "board", "swarm" or "lock" in code, UI or docs; "swarm" is fine in
marketing copy. Claims are described as advisory unless the workspace turns on `enforce`; never
write that Relay mechanically prevents conflicting edits.

## 1. Why

Teams now run several coding agents on one repository at once: Claude Code subagents and agent
teams, scripts that launch `claude -p` or `codex exec` in parallel, and background agents from
IDEs. With no shared record, four agents on one repo did 27 of 96 tasks twice. With a relay they
could post to, 19 of 96. With a relay they could claim from, 1 of 96, and each run was 16% cheaper
and 18% faster (8 trials per arm, `~/benchmarks/swarm/results/paper-numbers.txt`). The mechanism
is an append-only record on the lake: a row is visible to every other agent 2.8 ms after it is
written, a write-first claim never collides (0 of 480 contended claims, against 30 of 30 for
read-then-write), and the two relay queries answer in under 9 ms at a million rows. CtrlB already
holds the record; Relay is the two queries and the claim protocol, served from Seeker and shown in
Orion.

## 2. What exists (read these first)

Research code (read-only reference; nothing is moved from here by path, it is ported):

- `~/benchmarks/swarm/swarm_mcp.py`: the relay as a stdio MCP server. `holds()`, `claim()`,
  `release()`, `peers()`, `post()` are the protocol, 140 lines, standard library. Port the
  semantics exactly (section 4 "Claims").
- `~/benchmarks/swarm/run_swarm.py`: the experiment harness (arms `none`, `relay`, `claim`; four
  `claude -p` Haiku agents on `~/benchmarks/swarm/template/`, twelve tasks with unit tests). Task 15
  reruns it through the product path.
- `~/benchmarks/swarm/lock_contention.py`, `scale_queries.py`, `otel_latency.py`,
  `otel_probe.sh`: the measurements and the exact Claude Code OTEL environment that exports
  straight to the lake's OTLP endpoint (`/api/{org}/{stream}/_otel/v1/logs`).
- `~/benchmarks/paper/session-store-swarms.tex`: sections 3 (system), 4 (setup) and the appendix
  (the three agent rules, verbatim).

The plugin (the base of the customer-facing package):

- `~/benchmarks/ctrlb-memory-plugin/ctrlb-memory/`: Claude Code plugin, standard-library Python.
  `.claude-plugin/plugin.json`, `hooks/hooks.json` and `hooks/hook.py` (SessionStart rule,
  SubagentStart parent id, PreCompact/SessionEnd flush, Stop/SubagentStop async upload),
  `lib/common.py` (config, HTTP client, file state), `lib/rows.py` (transcript to rows, redaction,
  incremental upload; the row columns are in section 5), `server/recall_mcp.py` (four recall tools
  over ILIKE), `bin/{configure,status,backfill}.py`, `skills/{setup,recall}/SKILL.md`,
  `README.txt`, tests in `../tests/run_tests.py`.

Seeker (`~/CtrlB/seeker`, branch `feat/self-improving-agents`, `AGENTS.md` first):

- `docs/agent-read-api.md`, `docs/mcp.md`, `docs/labels.md`, `docs/findings.md`,
  `docs/findings-report.md` (the report shape to match).
- `internal/mcp/specs.go` (`toolSpec`, classes, `toolSpecs()`), `internal/mcp/toolsets.go`
  (`toolsetAgents`), `internal/mcp/agent_tools.go` (`agentSpecs()`, `requireAgentTraces`,
  `compactResult`), `internal/mcp/tools_fixes.go` (`fixLoopSpecs()`: how write tools are declared
  with `classCreate` and gated by `WriteToolsEnabled`), `internal/mcp/server.go`.
- `internal/api/router.go` (the `/ai` subrouter at line 264; labels, findings, trace sets as the
  handler → service → repository pattern), `internal/api/handlers/{labels,findings}.go`,
  `internal/services/labels.go` (`IngestLogsToOrg` write-through to a `_ctrlb_eval_<dataset>`
  logs dataset: the pattern for `_ctrlb_relay_<dataset>`), `internal/db/lake.go`
  (`IngestLogsToOrg`, `QueryLogLakeSQLQueryWithOrg`, `CreateDataset`, `GetAttributes`),
  `internal/db/labels_sql.go` (`escapeSQLLiteral`, schema guards), `internal/middleware/mcpAuth.go`
  and `/api/v1/integrations/mcp-api-key` (the per-user MCP key; the agent token in section 3),
  `internal/rbac/{registry,systemroles}.go`, `internal/pkg/derivedlabels/rules.go` (schema-guarded
  SQL on a tick; the detectors of section 4 reuse its style but run on read).
- `internal/models/guard.go` and `docs/guard-plan.md` (Guard is a plan; section 3 decision 8 says
  how Relay's enforcement hands over to it).

Artemis (`~/CtrlB/artemis`, `feat/self-improving-agents`): `internal/authz/grammar.go`
(`ResFindings`), `internal/authz/{registry,systemroles,validate}.go`, `internal/openfga/tuples.go`;
add `relay`.

Orion (`~/CtrlB/orion`, `feat/agent-system-map`; `.claude/CLAUDE.md`,
`.claude/guidelines/engineering.md`, `design-system/project/README.md` first):

- `src/routes.tsx` lines 285 to 325 (the `/ai` route with sub-pages overview, traces, evals),
  `src/pages/dashboard/Ai/AiPage.tsx` (picks the view from the URL, stays mounted, fetchers are
  module constants), `AiOverviewView.tsx`, `AiTracesView.tsx`, `AiEvalsView.tsx`.
- `src/components/Ai/` (`AiTabs`, `AiKpiStrip`, `AiBarList`, `AiTracesTable`, `AiTraceDrawer`,
  `AiMessageCard`, `AiAgentMap` on ReactFlow, `aiTheme.ts`), `src/hooks/ai/` (`aiFetchers.ts`
  sources: `runSql`, datasets, attributes, trace; `useAiFanOut`, `aiTimeStore`, `useAiAgentMap`,
  `evals/` with its own fetchers, fixtures and service `src/services/aiEvalsService.ts`).
- `.claude/plans/ai-observability.md` (the page's rules: no page scroll, one dataset at a time,
  capability-gated columns), `.claude/plans/ai-kickoff.md` (the kickoff shape), and the agent map
  commits `496b4405`, `f4f96308`.

Claude Code and Codex facts (verified 2026-11-04 and 2026-11-05):

- Claude Code 2.1.318. OTEL export to the lake with no collector: `CLAUDE_CODE_ENABLE_TELEMETRY=1`,
  `OTEL_LOGS_EXPORTER=otlp`, `OTEL_EXPORTER_OTLP_PROTOCOL=http/json`,
  `OTEL_EXPORTER_OTLP_LOGS_ENDPOINT=<lake>/api/<org>/<stream>/_otel/v1/logs`,
  `OTEL_LOG_USER_PROMPTS=1`, `OTEL_LOG_ASSISTANT_RESPONSES=1`, `OTEL_LOG_TOOL_DETAILS=1`,
  `OTEL_LOGS_EXPORT_INTERVAL=<ms>` (default 5000; lag equals it), `OTEL_RESOURCE_ATTRIBUTES=key=val`,
  `OTEL_EXPORTER_OTLP_HEADERS=Authorization=<value>`. Hooks: `PreToolUse` with `matcher`, JSON on
  stdin (`session_id`, `transcript_path`, `cwd`, `tool_name`, `tool_input`), JSON out
  `hookSpecificOutput.permissionDecision` allow/deny with `permissionDecisionReason`, or
  `additionalContext`. `${VAR}` expands in `.mcp.json` `url`, `headers`, `env`.
- Codex CLI 0.166. Hooks are stable and on by default: 14 events including `PreToolUse`,
  `PostToolUse`, `SessionStart`, `SessionEnd`, `Stop`, `PreCompact`, `SubagentStart`,
  `SubagentStop`; inline in `config.toml` as `[[hooks.PreToolUse]]` with `matcher` and
  `[[hooks.PreToolUse.hooks]]` `type = "command"`, `command`, `timeout`; stdin carries
  `session_id`, `transcript_path`, `cwd`, `hook_event_name`, `tool_name` (`Bash`, `apply_patch`,
  `mcp__<server>__<tool>`), `tool_input`; deny by exit code 2 with the reason on stderr, or JSON
  `hookSpecificOutput` with `permissionDecision: "deny"`; `ask` is not supported. New or changed
  hooks are skipped until trusted with `/hooks`. MCP: `[mcp_servers.<name>]` with `url` plus
  `bearer_token_env_var` or `http_headers`, or `command`/`args`/`env`; `codex mcp add <name> --url`.
  OTEL: `[otel]` with `exporter` (`otlp-http`, `endpoint`, `protocol = "json"`, `headers`),
  `log_user_prompt`, `log_agent_responses`, `tool_result.max_bytes`; the exact TOML layout of the
  exporter table must be confirmed against the installed version in Task 0 (the reference lists it
  both as a value and as a table). Transcripts: `~/.codex/sessions/<yyyy>/<mm>/<dd>/rollout-*.jsonl`,
  one JSON object per line with `type` in `session_meta`, `response_item` (payload `type`
  `message`, `reasoning`, `custom_tool_call`, `custom_tool_call_output`, `function_call`,
  `function_call_output`), `event_msg`, `turn_context`, `token_usage_record`.

## 3. Decisions

1. **Agents talk to Seeker, never to the lake.** The plugin and the Codex hooks send session rows
   and relay calls to Seeker with the user's MCP key (`Authorization: Bearer <key>`, the key minted
   at `/api/v1/integrations/mcp-api-key`). Seeker writes to the lake with `IngestLogsToOrg` and
   reads with `QueryLogLakeSQLQueryWithOrg`, under the dataset grants the key resolves to. The OTEL
   path is the exception by design: it is the customer's existing ingest credentials to the lake's
   OTLP endpoint, the same way their services send telemetry today. The research plugin's direct
   lake client (`CTRLB_URL` + `CTRLB_AUTH` to `_json_evolving`) stays only in `~/benchmarks`.
2. **One hosted MCP server, no stdio server in the product.** The recall tools (`search_sessions`,
   `find_command`, `next_actions`, `fetch_records`) and the relay tools (`peers`, `post`, `claim`,
   `release`) are specs in Seeker's `agents` toolset at `/mcp`. The plugin's `.mcp.json` points at
   it. Codex gets it with `codex mcp add ctrlb --url`. The plugin keeps only hooks and the installer.
3. **Datasets.** Sessions go to one logs dataset per workspace, default `agent_sessions`, a
   workspace setting (`sessionsDataset`). Relay rows go to `_ctrlb_relay_<sessions dataset>`,
   Seeker-owned, created on first write with `CreateDataset`, the eval-dataset convention. Both are
   ordinary datasets: Data Explorer, alerts and `run_sql` see them.
4. **Run id and agent id.** `run_id` is `CTRLB_RUN` when set. When unset, a Claude Code session is
   its own run and its subagents join it (the SubagentStart hook already knows the parent id), so
   every session with subagents is a run with no configuration. A script that launches several
   `claude -p` or `codex exec` processes sets `CTRLB_RUN` once. `agent` is the session id, with
   `CTRLB_AGENT` as an optional label. Relay tools take `run_id` and `agent` as arguments; when a
   request carries `X-CtrlB-Run` and `X-CtrlB-Agent` headers (set from `${CTRLB_RUN}` and
   `${CTRLB_AGENT}` in `.mcp.json`, `http_headers` in Codex) the arguments may be omitted. The
   SessionStart and SubagentStart hooks tell the agent its run id and agent id in the injected rule.
5. **Claims are decided in Seeker, write-first, exactly as measured.** Section 4 "Claims" is the
   protocol. The wait after the HOLD write (`waitMs`, default 20) and the lease (`leaseSeconds`,
   default 600) are workspace settings. A claim on a finished resource is refused with
   `finished_by`. A resource is normalised before it is keyed (decision 6).
6. **Resources are keyed on normalised paths when they look like paths.** The one duplicate in 96
   came from two names for one task ("task 01 slugify" versus "slugify"). A resource containing a
   `/` or ending in a file extension is a path: strip `./`, collapse `//` and `/./`, resolve `..`
   segments, drop a leading `/<cwd>/` prefix when the call carries `cwd`, key as `path:<p>`.
   Anything else is lower-cased, whitespace-collapsed, keyed as `name:<n>`. The response echoes the
   key so the agent sees what it holds. The injected rule tells agents to claim the file path the
   task names.
7. **Enforcement is a plugin hook calling one Seeker endpoint, with three modes.** `PreToolUse` on
   `Edit|Write|MultiEdit|NotebookEdit` (Claude Code) and `apply_patch` (Codex, paths parsed from
   the patch's `*** Update File:` / `*** Add File:` / `*** Delete File:` lines) posts the paths to
   `check`. Workspace setting `enforce`: `off` (hook exits 0 with no output), `advisory` (the
   default: `additionalContext` naming the holder), `enforce` (`permissionDecision: "deny"` with
   the holder and the claim call to make). The hook fails open on any error, two-second timeout,
   logged to `~/.ctrlb/hooks.log`.
8. **Guard takes enforcement over when it exists; nothing is built twice.** `check` is the only
   policy logic in Relay. When Guard lands, `held_path` becomes one Guard rule kind that calls the
   same `relay.Check` function, the plugin's `PreToolUse` hook switches to Guard's Claude Code hook
   endpoint, and the `enforce` setting moves into the policy. Keep `internal/pkg/relay/check.go`
   free of HTTP so Guard can call it.
9. **The run view reads Seeker endpoints, not raw SQL from Orion.** The AI pages fan SQL out from
   the browser (`runSql`), which is right for spans. Relay rows carry protocol semantics (a live
   hold is a HOLD with no matching RELEASE or DONE younger than the lease) that belong in one
   place, so Orion calls `GET /ai/relay/{dataset}/runs` and `/runs/{run}`. The detectors
   (duplicate edit, silent agent) are computed in that read for now; a derived-rule version that
   writes labels is a non-goal until a customer wants alerts on them.
10. **Live means polling at two seconds.** While a run is live (an agent wrote within the last 60 s)
    the run page refetches every 2 s; the lake's memtable makes each read a few milliseconds.
11. **Codex is a first-class harness.** Sessions upload from Codex hooks (`Stop`, `PreCompact`,
    `SessionEnd`, `SubagentStop`, each with `transcript_path`), parsed by a Codex rollout parser
    beside the Claude Code one; the same `harness` column distinguishes them. Enforcement and the
    injected rule use the same hook shapes. OTEL export is the alternative for both harnesses and
    is documented, not required.
12. **Connecting is one page and one command per harness.** Orion gets `/ai/connect`: the user's
    MCP key (minted or rotated there), and copy blocks for Claude Code (plugin install plus two
    environment lines), Codex (one installer command that writes the `config.toml` tables, with a
    backup), OTEL only (both harnesses), and anything else (the HTTP ingest and relay calls as
    `curl`). The Claude Code plugin installs from a marketplace repo
    (`claude plugin marketplace add <org>/ctrlb-agent`, `/plugin install ctrlb-agent`), the Codex
    path is `python3 -I install.py --codex` from the same repo.
13. **Secrets.** The key is read from `CTRLB_KEY` or `~/.ctrlb/config.json` (mode 0600, written by
    the configure step, never by the agent). The plugin's redaction patterns run on every session
    row before upload, as today. Nothing in the repo, docs or snippets contains a real key or URL;
    snippets are rendered by Orion from the workspace's values.
14. **Reuse the research numbers as acceptance criteria.** Task 15 reruns
    `~/benchmarks/swarm/run_swarm.py` with the relay tools served by Seeker's `/mcp` (not the stdio
    server): the `claim` arm must complete 96 of 96 tasks with at most 2 duplicated task files over
    8 trials, and the `none` arm is the control. Claim latency through Seeker (p50, p95) is
    recorded in the report.

## 4. Contract (authoritative; Orion mirrors it)

All routes under the `/ai` subrouter, JSON, workspace from the request context, dataset grants as
labels and findings have them. `{dataset}` is the sessions dataset. Times are microseconds since
the epoch as the lake writes them. Cursor paging as findings.

### Sessions

- `POST /ai/sessions/{dataset}/records`: body `[row, …]` (section 5 columns), at most 500 rows or
  4 MB; writes through with `IngestLogsToOrg`; `202 {accepted: n}`. Seeker sets `user_name` from
  the key's user when the row omits it and refuses a row naming another user (`403`).
- `GET /ai/sessions/{dataset}/search?term=&session_id=&tool=&order=newest|oldest&one_per_session=&limit=&offset=`:
  the `search_sessions` semantics of `server/recall_mcp.py` (ILIKE on `body` and `command`, a
  snippet of 300 characters around the hit, scope `user_name = <key's user>` unless the key has
  `relay.read_all`).
- `GET /ai/sessions/{dataset}/{session_id}/records?seq_from=&seq_to=&limit=`: rows in `seq` order.
- `GET /ai/sessions/{dataset}/commands?command=&limit=&offset=`: `find_command`.
- `GET /ai/sessions/{dataset}/{session_id}/next?seq=&k=&tool=`: `next_actions`.

### Relay

- `POST /ai/relay/{dataset}/runs/{run}/posts`: `{agent, text}` (text cut at 2 000); `201`.
- `GET /ai/relay/{dataset}/runs/{run}/peers?limit=40`: newest first, each
  `{agent, kind, resource, key, text, age_s, ts}`.
- `POST /ai/relay/{dataset}/runs/{run}/claims`: `{agent, resource, cwd?}` → `200`
  `{held: true, resource, key, lease_until}` or `{held: false, resource, key, held_by | finished_by,
  message}`. Never `409`: a refusal is a normal answer the agent reads.
- `DELETE /ai/relay/{dataset}/runs/{run}/claims`: `{agent, resource, finished: true}` → `{released:
  n, finished: bool}`; `finished: false` hands the resource back.
- `POST /ai/relay/{dataset}/runs/{run}/check`: `{agent, paths: [..], cwd?}` → `{mode, allowed:
  bool, conflicts: [{path, key, held_by, lease_until}]}`. `allowed` is false only in `enforce`
  mode with at least one conflict.
- `GET /ai/relay/{dataset}/runs?status=live|done|all&limit=&cursor=`: `{runs: [{run_id, agents,
  started, last_write, duration_s, live, posts, claims_held, claims_refused, done, open_claims,
  duplicate_edits, silent_agents, cost_usd?}], next_cursor}`. Cost is present when the session
  rows carry `cost_usd` (Claude Code's `result` record does; Codex's `token_usage_record` gives
  tokens, cost needs a price table and is a non-goal).
- `GET /ai/relay/{dataset}/runs/{run}`: `{run, agents: [{agent, session_id, label, first, last,
  turns, tool_calls, posts, claims_held, claims_refused, cost_usd?, silent_s}], events: [{ts,
  agent, kind: note|hold|release|done|refused|tool|edit, resource, key, text, session_id, seq}],
  resources: [{key, resource, holder, finished_by, holds: [{agent, from, to, outcome}], refused:
  [{agent, ts}], editors: [agent]}], detectors: {duplicate_edits: [{key, agents}], silent_agents:
  [{agent, since}]}}`. `events` is capped at 5 000 newest with `truncated: true`; `edit` events
  come from session rows whose `tool_name` is an edit tool, keyed by the same normalisation.
- `GET /ai/relay/settings`, `PUT /ai/relay/settings`: `{sessionsDataset, waitMs, leaseSeconds,
  enforce: off|advisory|enforce, silentAfterSeconds (default 120)}`; Mongo, per workspace.

### Claims (the protocol, from `swarm_mcp.py`; keep it exactly)

0. Read the resource's rows (`kind IN ('HOLD','RELEASE','DONE')`, ordered by `_timestamp`, `agent`).
   If any is DONE, answer `held: false, finished_by`.
1. Write one HOLD row `{run_id, agent, kind: HOLD, key, resource, nonce}` write-through.
2. Sleep `waitMs`.
3. Read again. Live holds are HOLD rows with no RELEASE or DONE row of the same nonce and age under
   `leaseSeconds`.
4. Held if and only if the first live hold is ours and no DONE appeared. Otherwise write a RELEASE
   for our nonce and answer `held: false` with the first live holder.

`release` writes DONE (`finished: true`) or RELEASE for each of the caller's live holds on the key.
`check` is step 3 for each path, without the write. Nothing is ever deleted or updated.

### MCP tools (toolset `agents`)

Reads (`classRead`): `search_sessions(term, session_id?, tool?, order?, one_per_session?, limit?,
offset?)`, `find_command(command, limit?, offset?)`, `next_actions(session_id, seq, k?, tool?)`,
`fetch_records(session_id, seq_from, seq_to)`, `peers(run_id?, agent?, limit?)`. Writes
(`classCreate`, registered only with `WriteToolsEnabled`): `post(text, run_id?, agent?)`,
`claim(resource, run_id?, agent?, cwd?)`, `release(resource, finished?, run_id?, agent?)`. The
dataset is the workspace's `sessionsDataset`; `dataset` is an optional argument. Descriptions are
the ones in `swarm_mcp.py` `ALL` and `recall_mcp.py` `TOOLS`, with the key echo added to `claim`.
Answers are compact JSON (`compactResult`).

### Authorisation

Artemis resource `relay` with `read`, `write`, `read_all`, `manage`. Session and relay reads need
`relay.read` and a grant on the dataset; a user sees their own `user_name` rows unless
`relay.read_all`; posts, claims, releases, records need `relay.write`; settings need
`relay.manage`. MCP keys inherit their user's roles as today.

## 5. Records written to the lake

Sessions dataset (`agent_sessions` by default), one row per content block, the plugin's columns
today plus three: `_timestamp`, `user_name`, `host`, `harness` (`claude` | `codex`), `session_id`,
`parent_session`, `agent_id`, `run_id` (new), `agent` (new; the session id or `CTRLB_AGENT`),
`seq`, `block_idx`, `role`, `block_type` (`text`, `thinking`, `tool_use`, `tool_result`, `result`),
`tool_name`, `tool_use_id`, `command`, `paths` (new; the file paths an edit tool touched, JSON
array text), `body` (cut at 64 KB), `is_error`, `is_summary`, `cwd`, `git_branch`, `cost_usd`
(Claude Code `result` rows), `input_tokens`, `output_tokens` (Codex `token_usage_record`, Claude
Code usage). Column types are fixed by the first value the lake sees (`docs/agent-read-api.md`),
so numbers are written as floats with a decimal point and booleans as booleans from the first row.

Relay dataset (`_ctrlb_relay_<dataset>`): `_timestamp`, `run_id`, `agent`, `session_id`,
`user_name`, `kind` (`NOTE`, `HOLD`, `RELEASE`, `DONE`, `REFUSED`, `CHECK`), `key`, `resource`,
`resource_kind` (`path` | `name`), `nonce`, `text`, `held_by` (on REFUSED and CHECK), `mode` (on
CHECK). REFUSED and CHECK rows are the audit trail the run view's refusal ticks and the Guard
handover read; they are written write-through like the rest.

## 6. Code layout

### Branching rule

Seeker, Artemis and Orion: `git fetch origin`; create `feat/relay` from
`feat/self-improving-agents` (Seeker and Orion already carry `origin/dev` merged in); first commit
merges `origin/dev` into `feat/relay`. If `feat/self-improving-agents` has been merged into
`dev`, branch from `origin/dev`. Never touch `main`, `staging`, `dev` or the stale `evals` branch.
Pull requests target `dev`. Commits need approval; never push without being asked.

### Agent package (new repo `~/CtrlB/ctrlb-agent`, `git init`, no remote)

```
.claude-plugin/marketplace.json        one entry: plugin ctrlb-agent at ./plugin
plugin/.claude-plugin/plugin.json      name ctrlb-agent, version 0.2.0
plugin/hooks/hooks.json                SessionStart, SubagentStart, PreCompact, SessionEnd, Stop,
                                       SubagentStop (as today) + PreToolUse matcher Edit|Write|MultiEdit|NotebookEdit
plugin/hooks/hook.py                   modes: session-start, subagent-start, flush, upload, pre-tool
plugin/.mcp.json                       {"ctrlb": {"url": "${CTRLB_MCP_URL}", "headers": {"Authorization": "Bearer ${CTRLB_KEY}",
                                       "X-CtrlB-Run": "${CTRLB_RUN}", "X-CtrlB-Agent": "${CTRLB_AGENT}"}}}
plugin/skills/setup/SKILL.md           status, configure instructions, backfill offer (as today)
plugin/skills/recall/SKILL.md          as today
plugin/skills/relay/SKILL.md           the three agent rules from the paper appendix, as /ctrlb-agent:relay <run id>
codex/install.py                       writes [mcp_servers.ctrlb], [[hooks.*]] tables and optional [otel] into
                                       ~/.codex/config.toml (backup first, idempotent, --dry-run prints the diff)
codex/hook.py                          same modes as plugin/hooks/hook.py, Codex stdin and output shapes
lib/common.py                          config (~/.ctrlb/config.json: url, key, user, run), Seeker HTTP client, FileState, log
lib/rows_claude.py                     today's rows.py + run_id, agent, paths, cost_usd, tokens
lib/rows_codex.py                      rollout jsonl → the same columns (session_meta → ids and cwd; response_item
                                       message/reasoning/custom_tool_call/custom_tool_call_output/function_call*;
                                       token_usage_record → tokens; apply_patch paths from the patch text)
lib/redact.py                          the REDACT patterns, shared
lib/paths.py                           resource normalisation, identical to Seeker's (table test shared as JSON)
bin/configure.py, bin/status.py, bin/backfill.py   as today, --codex flag on backfill
tests/run_tests.py                     today's tests + Codex parser fixtures + pre-tool fixtures (allow, advisory, deny, timeout)
tests/fixtures/                        one Claude transcript, one Codex rollout (redacted), hook inputs for both harnesses
docs/                                  the docs-site sources (section 8 Task 14)
README.md
```

Standard library only, `python3 -I` everywhere, every hook exits 0, every error to `~/.ctrlb/hooks.log`.

### Seeker (`feat/relay`)

- `internal/models/relay.go`: `RelaySettings`, `RelayRow`, `ClaimRequest/Response`,
  `CheckRequest/Response`, `RunSummary`, `RunDetail`, `SessionRecord`; `ValidateSessionRecord`;
  the `_ctrlb_relay_` prefix constant.
- `internal/pkg/relay/keys.go` (normalisation, decision 6), `claim.go` (`Claim`, `Release`,
  `Check`, `LiveHolds` over an interface of read and write-through), `runs.go` (summaries, detail,
  detectors), `keys_test.go` (the shared JSON table), `claim_test.go` (a fake lake with an
  injectable delay; 16 goroutines claiming one key must yield one holder; a released claim is
  re-claimable; DONE refuses).
- `internal/db/relay_sql.go` (holds, peers, runs list, run detail, sessions search; every column
  schema-guarded as `labels_sql.go` does, literals through `escapeSQLLiteral`),
  `internal/db/relay_repository.go` (Mongo `relaySettings`).
- `internal/services/relay.go` (orchestration, dataset creation on first write, settings,
  `user_name` scoping), `internal/services/sessions.go` (records ingest, search, commands, next,
  records), `internal/services/relay_lake_test.go` behind `LABELS_LAKE_URL`.
- `internal/api/handlers/relay.go`, `internal/api/handlers/sessions.go`, `internal/api/router.go`
  (modified), `internal/rbac/{registry,systemroles}.go` (modified: `ResRelay`),
  `internal/config/config.go` (modified: `RELAY_WAIT_MS_MAX`, `RELAY_LEASE_MAX_SECONDS`,
  `RELAY_RECORDS_MAX_ROWS`, `RELAY_RECORDS_MAX_BYTES`).
- `internal/mcp/tools_relay.go` (`relaySpecs()`, added to `toolSpecs()`), `internal/mcp/tools_relay_test.go`.
- `docs/relay.md` (feature doc in the shape of `docs/findings.md`), `docs/relay-plan.md` (this),
  `docs/relay-report.md`, `docs/openapi/openapi.yaml` (modified).

### Artemis (`feat/relay`)

`internal/authz/grammar.go` `ResRelay`, relations `read`, `write`, `read_all`, `manage`; system
roles (Viewer: read; Editor: read, write; Admin: all); `ContractVersion` bump; tests.

### Orion (`feat/relay`)

Section 7.

## 7. Orion target layout

Sub-pages added to the `/ai` route: `/ai/runs`, `/ai/runs/:runId` and `/ai/connect` (sidebar
names "Runs" and "Connect", hidden like the others, `AiPage` picks the view). Gate: the workspace
has logs. Time range from `aiTimeStore` as the other views.

- `src/pages/dashboard/Ai/AiRunsView.tsx`: a `DataTable` of runs (run id, agents, started,
  duration, posts, claims held / refused, done, open claims, duplicate edits, silent agents, cost
  when present), status filter live / done / all, sorted by started desc, sortable by cost and
  duplicate edits; a live run row shows a pulsing dot; row click opens the run. Empty state links
  to Connect.
- `src/pages/dashboard/Ai/AiRunView.tsx`: top: KPI strip (`AiKpiStrip`: agents, tasks done,
  duplicate edits, refused claims, cost); middle, `flex-1 min-h-0`: `AiRunTimeline` (one swimlane
  per agent on a shared time axis; holds as bars from HOLD to DONE / RELEASE, DONE in `green-600`,
  RELEASE in `gray-400`, refusals as `red-450` ticks on the refused agent's lane, posts as
  markers, tool calls as a thin density strip under the lane; hover tooltip with the text; click
  opens `AiSessionDrawer` at that session and seq) and, beside it, `AiResourcesPanel` (a
  `DataTable` of resources: key, holder, finished by, refused count, editors; rows with two editors
  carry the duplicate badge). Detector badges render in the KPI strip and link to the rows. The
  page polls every 2 s while `live`.
- `src/components/Ai/AiSessionDrawer.tsx`: the transcript viewer for one session: records in
  `seq` order rendered with `AiMessageCard` (text, thinking collapsed, tool call with its command
  or paths, tool result with error state), a search box over `GET …/search` scoped to the session,
  and "Open in Data Explorer" with the dataset and session id. This is the first paper's feature
  and lands here.
- `src/pages/dashboard/Ai/AiConnectView.tsx`: the user's MCP key (mint, rotate, copy; existing
  `/api/v1/integrations/mcp-api-key`), then four copy blocks rendered from the workspace's URL and
  the key placeholder: Claude Code, Codex, OTEL only, HTTP. Each block has the run id line
  explained in one sentence.
- `src/hooks/ai/relay/` (`relayTypes.ts`, `relayFetchers.ts` over `src/services/aiRelayService.ts`,
  `useAiRuns.ts`, `useAiRun.ts` with the 2 s poll, `useAiSession.ts`), fixtures in
  `relayFixtures.ts` from a recorded run of Task 15, tests beside them.
- Agent map data source: a run's agents as nodes and refusals as edges, behind the existing
  `useAiAgentMap` shape, is a non-goal for this run (section 9); the detail page links to the map.

Rules from `.claude/plans/ai-observability.md` hold: no page scroll (`scrollHeight ===
clientHeight` at 1280×720 and 1920×1080), design-system tokens only, fetchers as module
constants, fixtures in DEV behind a localStorage switch.

## 8. Tasks

Seeker (orchestrator runs them; one builder agent each, one reviewer each):

- **Task 0, Branch and baseline.** Branching rule; build, vet, test → `docs/relay-baseline.md`.
  Copy `docs/relay-plan.md`. Confirm the Codex `[otel]` TOML layout against `codex --version`
  0.166 (`codex config` help or a dry run) and record it in the plan copy.
- **Task 1, Models, settings, RBAC, config.** `internal/models/relay.go`, Mongo settings
  repository, `ResRelay`, `RELAY_*` config with validation. Tests.
- **Task 2, Keys and claims.** `internal/pkg/relay/{keys,claim}.go` with the fake-lake race test
  and the shared normalisation table (`tests/fixtures/keys.json`, copied to the agent repo in Task
  9). No HTTP in this package.
- **Task 3, Lake SQL and services.** `internal/db/relay_sql.go`, `internal/services/{relay,
  sessions}.go`: write-through ingest, dataset creation, holds, peers, runs list, run detail with
  detectors, sessions search / records / commands / next. `relay_lake_test.go` behind
  `LABELS_LAKE_URL`: ingest a run of three agents, claim the same key from two, see one holder,
  release with finished, see the refusal, list and detail match.
- **Task 4, HTTP API.** Handlers, router, OpenAPI, `docs/relay.md`. Fixture tests for every route
  in section 4 including the `403` on another user's rows.
- **Task 5, MCP tools.** `tools_relay.go`: the eight specs, header fallback for run and agent,
  `compactResult`, a test that lists them under the `agents` toolset with writes gated by
  `WriteToolsEnabled`.
- **Task 6, Artemis.** Resource `relay`, roles, contract version, tests.

Agent package (new repo; one builder, one reviewer; parallel with Seeker Tasks 2 to 5 using
recorded Seeker responses, then live against the Task 4 server):

- **Task 7, Repo and Claude Code plugin.** Move the plugin in, rename, marketplace file, Seeker
  client in `lib/common.py`, `run_id` / `agent` / `paths` / `cost_usd` / tokens in
  `rows_claude.py`, the `.mcp.json` of section 6, the relay skill, `pre-tool` mode with the three
  modes and fail-open, SessionStart and SubagentStart rules naming run id and agent id. Tests.
- **Task 8, Codex.** `rows_codex.py` on a redacted real rollout fixture, `codex/hook.py`,
  `codex/install.py` (backup, idempotent, `--dry-run`, `--otel`), `backfill.py --codex`. A test
  that `install.py` run twice leaves `config.toml` unchanged.
- **Task 9, Shared normalisation and connect docs.** `lib/paths.py` passing Seeker's
  `keys.json`; `docs/` sources (section 8, Task 14 lists the pages).

Orion (after Seeker Task 4; builders per view, `design-system-reviewer` and `regression-guard`
on every diff):

- **Task 10, Service, hooks, fixtures.** `aiRelayService.ts`, the `relay/` hooks, fixtures recorded
  from a live run, routes and `AiPage` wiring, breadcrumbs, navbar time pill for the new paths.
- **Task 11, Runs list and Connect.** `AiRunsView`, `AiConnectView`.
- **Task 12, Run detail and session drawer.** `AiRunView`, `AiRunTimeline`, `AiResourcesPanel`,
  `AiSessionDrawer`, the 2 s poll with backoff when the tab is hidden.
- **Task 13, Gates.** No page scroll at both sizes, oxlint clean on new files, tests, screenshots
  of a live run, a finished run with a duplicate, an empty workspace, and Connect.

Docs and the end-to-end run:

- **Task 14, Docs.** In `ctrlb-agent/docs/` as Markdown with front matter for the docs site:
  `session-store.md` (concepts: session, run, agent, relay, claim, lease, done; one diagram of the
  write-first claim), `connect-claude-code.md`, `connect-codex.md`, `connect-otel.md`,
  `connect-http.md` (the `curl` calls), `relay-tools.md` (the eight tools, arguments, answers, the
  three agent rules as copy-paste text), `run-view.md`, `limits.md` (advisory unless `enforce`,
  one claim per key, lease 10 min, 500 rows per records call, what OTEL export does not carry).
  Every snippet uses `<your CtrlB URL>` and `<your key>` placeholders. The two papers are linked
  from `session-store.md`.
- **Task 15, Live run and report.** Against a running lake, Seeker with `MCP_ENABLED=true` and
  `MCP_WRITE_TOOLS_ENABLED=true`, Artemis for keys, and the Task 4 build: (a) connect Claude Code
  through the plugin and Codex through `install.py` on one machine; run one session each with a
  subagent and see the rows, the run and the transcript in Orion; (b) rerun
  `~/benchmarks/swarm/run_swarm.py` arms `none` and `claim` for 8 trials each with the agents'
  MCP config pointing at Seeker's `/mcp` (a small adapter in the harness, kept in `~/benchmarks`),
  and report completed, duplicated, cost and wall beside the 2026-11-04 numbers; (c) measure claim
  latency through Seeker (p50, p95 over 1 000 claims, 1 and 16 contenders) and the records ingest
  round trip; (d) `enforce` mode: an Edit on a path another agent holds is denied in Claude Code
  and an `apply_patch` in Codex, with the fixture inputs recorded. Write `docs/relay-report.md` in
  the shape of `docs/findings-report.md`.

Order: 0 → 1 → 2 and 3 in parallel (7 and 8 start here) → 4 and 5 and 6 in parallel (9 after 2) →
10 → 11 and 12 in parallel → 13 → 14 (can start after 4) → 15.

## 9. Non-goals

A stdio MCP server in the product; a Seeker-owned OTLP proxy (customers' ingest credentials
already work); derived-label detectors and alerts on them; the agent map data source; cost for
Codex (needs a price table); Guard rules (Guard's plan carries `held_path`); a VS Code or Cursor
extension; any lake change; any change to compaction in either harness.

## 10. Asks and decisions needing the owner (defaults applied if silent)

1. **Sessions dataset name.** Default `agent_sessions`; the research plugin used `claude_sessions`.
   Existing rows in `claude_sessions` on staging are left where they are.
2. **Default `enforce` mode.** Default `advisory`. `enforce` changes an agent's behaviour on a
   customer's repo and should be a choice they make on the settings page.
3. **Where docs.ctrlb.ai sources live.** Task 14 writes Markdown with front matter in the agent
   repo; moving it into the docs site's repo is a copy once the owner names it.
4. **Marketplace repo location.** `ctrlb-agent` needs a public Git remote for
   `claude plugin marketplace add <org>/ctrlb-agent` to work for customers; until then the
   Connect page shows the `--plugin-dir` install from a zip Orion serves from `/ai/connect`.
5. **Plugin name.** Default `ctrlb-agent` (tools appear as `mcp__ctrlb__peers`); `ctrlb-memory` is
   retired with a note in its README pointing here.
6. **Cost column for Codex.** Tokens are recorded; a price table is a later task if a customer asks.
