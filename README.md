# ctrlb-agent

CtrlB's agent package for Claude Code and Codex. It does three things:

- It uploads every session, including subagents, to the session store in CtrlB. One row per message, thinking block, tool call and tool result, and one per session title (every rename kept), with secrets redacted first.
- It tells each agent its CtrlB Relay run id and agent id at session start. The relay tools (`peers`, `post`, `claim`, `release`) and the recall tools (`search_sessions`, `find_command`, `next_actions`, `fetch_records`) come from CtrlB's hosted MCP server, at the CtrlB URL plus `/mcp`.
- It checks each edit against the run's claims before the edit runs. Claims are advisory unless the workspace turns on `enforce`.

Everything goes to the CtrlB URL with your MCP key. Nothing talks to the lake directly. Standard library Python 3.9 or newer; nothing else is installed.

Use an HTTPS CtrlB URL. The hooks and `bin/configure.py` allow plain HTTP for loopback (`localhost`, `127.0.0.0/8`, `::1`). For a self-hosted server on another host, set `CTRLB_ALLOW_HTTP=1` in the environment when configuring and running your agent to opt into plain HTTP; the key and session text then travel unencrypted. API requests reject redirects, so configure the final server URL.

For Recall and Relay on one machine with a standalone binary and SQLite, see [Local mode](docs/local-mode.md). It includes installation, agent setup, coordinating several agents, limits and moving your records to hosted CtrlB.

Local mode has five recall tools: `search_sessions`, `find_command`, `next_actions`, `fetch_records` and `session_index`. `session_index` lists the caller's recent sessions. All five read only stored records and make no model call, and are listed with or without `CTRLB_RUN`. Local mode writes no running notes or memories; those need a model.

## Install in Claude Code

Mint a key on the Connect page in CtrlB. Put these three lines in your shell profile:

```sh
export CTRLB_URL="<your CtrlB URL>"
export CTRLB_MCP_URL="<your CtrlB URL>/mcp"
export CTRLB_KEY="<your key>"
```

Install the plugin:

```sh
claude plugin marketplace add ctrlb-hq/ctrlb-agent
claude plugin install ctrlb-agent@ctrlb-agent
```

Start Claude Code and run `/ctrlb-agent:setup`. It checks the connection and offers to upload past sessions.

Claude Code can also do this setup for you: save the key in a file yourself, then give it the prompt in [docs/connect-claude-code.md](docs/connect-claude-code.md#set-up-with-a-prompt). The agent never reads the key.

To keep the key out of your shell profile, clone this repo and run `python3 -I bin/configure.py` in a terminal. It asks for the key with hidden input and writes `~/.ctrlb/config.json` with mode 0600. The hooks read that file. The MCP server still needs `CTRLB_MCP_URL` and `CTRLB_KEY` in the environment, because Claude Code expands them in `plugin/.mcp.json`.

## Install in Codex

Put the same three lines in your shell profile, clone this repo, and run the installer from the clone:

```sh
export CTRLB_URL="<your CtrlB URL>"
export CTRLB_MCP_URL="<your CtrlB URL>/mcp"
export CTRLB_KEY="<your key>"
python3 -I install.py --codex --dry-run
python3 -I install.py --codex
```

`python3 -I install.py --codex` runs `codex/install.py` with the options that follow; either command works.

`--dry-run` prints the change to `~/.codex/config.toml` and writes nothing. The installer then writes one block between two marker comments:

- `[mcp_servers.ctrlb]` with the CtrlB URL plus `/mcp` and `bearer_token_env_var = "CTRLB_KEY"`. Codex reads the key from the environment; the key is never written into the file. `env_http_headers` sends `CTRLB_RUN` and `CTRLB_AGENT` as `X-CtrlB-Run` and `X-CtrlB-Agent` when they are set.
- `[[hooks.<Event>]]` tables for `SessionStart`, `SubagentStart`, `PreCompact`, `SessionEnd`, `Stop`, `SubagentStop` and `PreToolUse` (matcher `apply_patch`). Each calls `codex/hook.py` in the clone, so keep the clone where it is.

It copies the old file to `config.toml.ctrlb-backup-<time>` before it writes. Running it again changes nothing. It refuses to run when `config.toml` already has an MCP server named `ctrlb`. Codex skips new or changed hooks until you trust them: start Codex and run `/hooks` once.

`--codex-home <dir>` or `CODEX_HOME` picks another Codex home; `--config <file>` names the file. `--uninstall` removes the block.

To upload past Codex sessions: `python3 -I bin/backfill.py --codex`.

## OTEL export instead

Both harnesses can also send their own OpenTelemetry logs straight to the lake's OTLP endpoint with your ingest credentials. For Codex, `--otel` adds an `[otel]` table to the block:

```sh
python3 -I codex/install.py --otel \
  --otel-endpoint "<your CtrlB URL>/api/<org>/<dataset>/_otel/v1/logs" \
  --otel-header "Authorization=<your ingest credentials>"
```

The header value is stored in `config.toml` as given. The installer refuses to write your CtrlB key there.

## Runs and agents

A run is the set of agents that share a run id. An agent is one session in a run.

- With no setting, each Claude Code or Codex session is its own run, and its subagents join it. Such a session is not told to claim a task; it does the work it was asked to do.
- To put several sessions in one run, set `CTRLB_RUN` once for all of them:

  ```sh
  CTRLB_RUN=nightly-refactor-1 claude -p "Fix the failing tests in src/parser"
  ```

- The agent id is the session id. Set `CTRLB_AGENT` to use a label instead. A Claude Code subagent's id is `agent-<id>`, or `<label>:agent-<id>` when a label is set. A Codex subagent's id is its thread id, or `<label>:<thread id>`.
- `/ctrlb-agent:relay <run id>` joins a named run from inside a session and loads the three relay rules. The session's hooks and the agents it starts from a shell keep the run the session started with; to put a started agent in the named run, give it `CTRLB_RUN` and `CTRLB_AGENT`.

## Edit checks

A `PreToolUse` hook runs before `Edit`, `Write`, `MultiEdit` and `NotebookEdit` in Claude Code, and before `apply_patch` in Codex. For Codex the paths come from the patch's `*** Update File:`, `*** Add File:`, `*** Delete File:` and `*** Move to:` lines. The hook sends the file paths to `POST /ai/relay/{dataset}/runs/{run}/check`. The workspace setting `enforce` decides what happens:

| Mode | When another agent holds the file |
|---|---|
| `off` | Nothing. The hook prints nothing. |
| `advisory` (default) | The agent gets a note naming the holder and the claim call to make. The edit goes ahead. |
| `enforce` | The edit is refused. The reason names the holder and the claim call to make. |

The hook gives up after two seconds. On a timeout, CtrlB not reachable, or any other error, the edit goes ahead and the error goes to `~/.ctrlb/hooks.log`. Set `CTRLB_ENFORCE=off` to skip edit checks, including automatic slot claims, on one machine. Done evidence witnessing still runs and can refuse unmatched evidence.

## What is uploaded

Rows go to `POST /ai/sessions/{dataset}/records`, at most 500 per call. The dataset is `agent_sessions` unless `CTRLB_DATASET` says otherwise. Uploads happen at the end of each turn, before each compaction, when a subagent starts or stops, and when the session ends. Each upload sends only the transcript lines added since the last one, and, for Codex, the thread names given since the last one sent (from `~/.codex/session_index.jsonl`).

Each row carries the session, run and agent ids, the role and block type, the tool name, the shell command, the paths an edit tool touched, the text (cut at 64 KB), error and summary flags, cwd, git branch, token counts and cost. `harness` is `claude` or `codex`.

### Cost

A Claude Code session's cost is the sum of `cost_usd` over its rows, which is on the `result` rows made from the transcript's `cost-state` records. Each record holds a running total, its cost ledger, named by the record's `startTime`. A session that continues another one in a new transcript (a fork in the desktop app) starts from the total the other one had, so its first record also holds the earlier session's cost. A resumed session goes on from its own saved total under the same `startTime`. The plugin keeps each ledger's last total on this machine, across transcripts (`~/.ctrlb/state/cost-ledgers.json`), and charges each record only the increase over it. A session then pays only its own spend.

A cost record has no time of its own. It takes the time of the row before it plus 1 µs, and "time order" below means that time.

- A ledger not seen before counts its first record whole (less the transcript's own earlier record of it, if any). A new `startTime` is a new ledger, also inside one transcript.
- A total at or above the ledger's last, in time order, counts what is above it, or above the transcript's own earlier record if that is higher. The records above a ledger's last total add up to the highest total it reached, so nothing is counted twice.
- A total below the ledger's last, in time order, is a restart and counts whole, unless it continues the transcript's own earlier record (a session resumed from its own saved total), which counts from there. This assumes a ledger's total goes down only on a restart or a resume. Claude Code writes a cost record when it saves a session, with that session's own total, so a record written later into a transcript that was idle repeats its total.
- A record that arrives after a later record of its ledger (two sessions uploaded out of order) counts only what is above both the ledger's last total and the transcript's own earlier record, never less than zero, and the ledger never moves back. Nothing is counted twice, but that spend can land on the later session. `backfill.py` avoids this by putting every transcript's cost records in the ledgers in time order before it uploads.
- When two transcripts of one ledger take turns, each from its own total (a session resumed after a fork of it went on), a record that passes the other one's total is charged only what is above it. That spend is counted too little, never twice.
- Two sessions started in the same millisecond (the desktop app can start several at once) get the same `startTime` but keep separate totals. The later one is then charged only what its total is above the other's.
- A retried upload gets the amounts the first try computed, however late it comes: the transcript's upload state keeps a record's amounts until CtrlB accepts its row. After that only the ledger's note that the record was counted is kept, and the record counts 0 if it is read again (a transcript uploaded again from its start): CtrlB already has its row, and the session's cost is the sum over its rows. This holds unless the ledger file could not be used the first time.
- Records without a `startTime` (stream-json `result` records) are counted from the transcript's own previous record. While the ledger file cannot be used, a record is counted as for a ledger not seen before (from the transcript's own earlier record of the same ledger), with a line in `~/.ctrlb/hooks.log`. A file that is not valid JSON, or holds a value this version cannot use, is set aside and a new one is started; a file that cannot be read or written (a full disk, a permission error) is left as it was.
- The ledger file keeps at most 1,000 ledgers, dropping those whose last record is oldest (never the one being written), plus those holding amounts `backfill.py` planned for the records it is about to upload. A planned amount is kept until its record uploads, or 30 days after the last backfill that planned it; a record uploaded after that counts 0.

Each `cost-state` row's `body` keeps the record and adds the amounts: `"ctrlb_cost": {"ledger": "<host>:<startTime>", "usd": <cost_usd>, "models": {"<model>": {"costUSD", "inputTokens", "outputTokens", "cacheReadInputTokens", "cacheCreationInputTokens"}}}`. `ledger` is `""` without a `startTime`. A stream-json `result` row's `body` is its result text, with no `ctrlb_cost`. Each model value is the increase over the same base, each kind at least 0, so a session's per-model sums can be below its `cost_usd` sum. When the base has no per-model figures (the record before it had no `modelUsage`, or plugin 0.3.0 or earlier counted it and its line is not found), `models` is empty. The `input_tokens` and `output_tokens` columns stay 0 on cost rows. A session's tokens are on its assistant rows, so add one or the other, never both.

Sessions uploaded by plugin 0.3.0 or earlier keep the cost they were uploaded with, which may include the cost of the session they continue: rows are never replaced. A session in progress at the upgrade is read once more up to where it was uploaded, so its next cost record is counted from its last one, per model too. After upgrading, run `python3 -I bin/backfill.py --cost-ledger` once. It puts the cost records CtrlB already has, from the transcripts on this machine, into the ledgers in time order, and sends nothing. A session that continues one of those is then charged only its own spend. Records not uploaded yet are left out and counted when they upload; `backfill.py` without `--cost-ledger` plans them first and uploads them. If the ledger file cannot be used, `--cost-ledger` says so and exits with 1; an upload backfill says so and uploads anyway, each cost record counted as it is uploaded. Either exits with 1 when a transcript could not be read. Codex rollouts carry no cost.

### Session titles

A session's title is a row with `role` `system`, `block_type` `title` and the title as text. Every title a session gets is kept, so its renames stay in the record. The title shown for a session is its title row with the highest `seq`, then the highest `block_idx`.

Claude Code writes two kinds of title lines into the transcript, with no time and repeated many times: `custom-title`, the title the user sets, and `ai-title`, the one Claude Code generates. A custom title wins: once a transcript has had one, later `ai-title` lines are ignored. The plugin writes a row when a transcript's title first appears and each time it changes, not for a repeat. The row's `seq` is the title line's own and its `block_idx` is 0. Its time is that of the line before it that counts, plus 1 µs: a user, assistant or system message line with a `message` object (even one whose blocks make no row), a cost record or another title row. Other lines do not count, even with a timestamp. A title before any such line with a time (often the first line) waits, keeping its `seq`, and takes the time of the first one. A transcript whose lines were uploaded by a version without titles (an upgrade, or a downgrade and an upgrade again) is checked once for a custom title in those lines, so a generated title after the upgrade cannot replace it. Claude Code writes no title into subagent transcripts.

Title rows go in a call of their own, before the transcript rows. A title row whose call CtrlB answered `202` is recorded at once and is not sent again, even when the transcript call after it fails. A CtrlB older than session titles refuses them with `400`: they are skipped, one line goes to `~/.ctrlb/hooks.log`, and the rest of the session uploads as usual. A refusal counts as that only when the call held title rows alone and the answer's JSON `error` holds that CtrlB's text for an unknown block type, `block_type must be one of …` and `got "title"`. Any other `400`, even one that names `block_type`, stops the upload without moving the offset, as other failures do.

Sessions uploaded before title rows, or while CtrlB refused them, get their titles with `python3 -I bin/backfill.py --titles` (run it after upgrading CtrlB). It reads each uploaded transcript from the start and sends only the title rows CtrlB has not accepted yet, at their own `seq`, without uploading anything else again. `--count` shows how many files and titles it would send, `--project TEXT` limits it to matching project folders, and running it twice sends nothing new.

These cases leave extra title rows:

- A session in progress when the plugin is upgraded from a version without titles writes its unchanged title once more at the first title line after the upgrade, and `--titles` also sends the line where it first appeared. The title shown does not change.
- A title call CtrlB stored but whose answer never arrived (a timeout, a dropped connection) is sent again, as transcript rows are: a Claude Code title at the same key, a Codex name with the next upload, at that upload's last line. The title shown does not change.
- Past 500 accepted title rows in one transcript, the oldest are no longer recorded, and `--titles` sends them again at their own keys. The title shown does not change.
- A transcript or rollout that gets shorter (Claude Code and Codex only append) is uploaded again from the start, and its title rows from before stay in CtrlB. One with a higher `seq` than the new ones, or at the same `seq`, can then still be the title shown.
- After a downgrade to a version without titles, the plugin notices when that version started a transcript over (it got shorter) and starts its title tracking over too. If the transcript then grew back to or past where the plugin had stopped before the downgrade, it cannot tell. The tracking can then be stale: a title row at a `seq` sent before is not sent again, a custom title from before keeps later generated ones out, and the next cost record may be counted from the old content's last one.

Codex rows come from the rollout files in `~/.codex/sessions`. User and assistant messages, reasoning summaries, tool calls and tool results become rows. Developer messages and encrypted reasoning do not. Each `token_usage_record` becomes one `result` row with the token counts. Codex rows carry no cost.

A Codex thread's names are not in its rollout: Codex appends each name and rename to `~/.codex/session_index.jsonl` (`$CODEX_HOME`) with its `updated_at`. Every rename is kept. After an upload that sent new lines of a main thread (not a subagent thread), each name the thread got since the last one sent is a `title` row, oldest first, in the file's order; a name equal to the one before it is left out. A line whose `updated_at` cannot be read, or is earlier than a line before it for the thread, still counts as the newest so far. Each row has the `seq` of the last uploaded line, `block_idx` 1000 plus the number of title rows already written for that rollout (so names after the same line keep distinct, increasing keys, and the newest has the highest) and the last line's time plus 1 µs and its place in the batch, never the rename's own time. A rename with no new lines since waits for the thread's next upload with new lines. A CtrlB older than session titles refuses them with `400`: the rows are skipped with one line in `~/.ctrlb/hooks.log`, their keys, cwd, branch, run and agent are kept, and the names count as sent. `python3 -I bin/backfill.py --codex --titles`, run after upgrading CtrlB, sends those refused rows as they were, at their own keys (below every later name's, so the newest name stays the title shown), then each uploaded main thread's names given since the last one sent. `--count` works as above; `--project TEXT` keeps threads whose working directory contains TEXT.

Before upload, text that looks like a secret is replaced with `[REDACTED]`. That covers private keys, cloud and API keys, tokens, JWTs, Authorization and Bearer values, credential headers such as `X-API-Key`, `X-Auth-Token` and `Cookie`, flags such as `--token`, `--api-key` and `--password`, assignments to names ending in SECRET, TOKEN, PASSWORD or KEY, and passwords in URLs. Pattern matching cannot catch every secret. Do not paste real credentials into a session.

## Configuration

| Variable | `~/.ctrlb/config.json` key | Meaning |
|---|---|---|
| `CTRLB_URL` | `url` | The CtrlB URL (the Connect page shows it) |
| `CTRLB_ALLOW_HTTP=1` | | allow plain HTTP to a non-loopback server; HTTPS is required otherwise |
| `CTRLB_KEY` | `key` | your MCP key |
| `CTRLB_MCP_URL` | `mcp_url` | The CtrlB URL plus `/mcp`, for the MCP server |
| `CTRLB_RUN` | `run` | run id for every session (default: each session is its own run) |
| `CTRLB_AGENT` | | agent label (default: the session id) |
| | `user` | the key's user (its `email` claim), written by `bin/configure.py`; `user_name` on the rows is always this user, decoded from the key in use |
| `CTRLB_USER` | | read for compatibility: the agent label when `CTRLB_AGENT` is unset; never `user_name` |
| `CTRLB_DATASET` | `dataset` | sessions dataset (default `agent_sessions`) |
| `CTRLB_ENFORCE` | `enforce` | `off` skips edit checks and automatic slot claims on this machine; done evidence witnessing still runs |
| `CTRLB_OFF=1` | | turns every hook off |
| `CTRLB_HOME` | | where the config, log and upload state live (default `~/.ctrlb`) |

Environment variables win over the file.

## Turn off or remove

- Off for one session: `CTRLB_OFF=1 claude`
- Status: `/ctrlb-agent:setup`, or `python3 -I bin/status.py` from a clone
- Log: `~/.ctrlb/hooks.log`
- Remove: `claude plugin uninstall ctrlb-agent@ctrlb-agent`, then delete `~/.ctrlb`. Rows already in CtrlB stay there.
- Codex: `CTRLB_OFF=1 codex` for one session; `python3 -I codex/install.py --uninstall` to remove the block.

## Layout

```
.claude-plugin/marketplace.json   the marketplace: one plugin, ctrlb-agent, at ./plugin
plugin/                           the Claude Code plugin (hooks, skills, .mcp.json)
plugin/lib, plugin/bin            symlinks to lib/ and bin/; a plugin install copies their content
install.py                        python3 -I install.py --codex runs codex/install.py with the options that follow
codex/install.py                  writes the Codex config.toml block
codex/hook.py                     the Codex hooks
lib/common.py                     config, the CtrlB client, upload state, log
lib/rows_claude.py                Claude Code transcript to session rows
lib/rows_codex.py                 Codex rollout to session rows
lib/hooks_shared.py               the injected rules and the edit check, shared by both hooks
lib/redact.py                     the redaction patterns
lib/paths.py                      resource keys, identical to CtrlB's (tests/fixtures/keys.json); edit paths sent relative to cwd
bin/                              configure.py, status.py, backfill.py
tests/run_tests.py                python3 -I tests/run_tests.py
docs/connect-*.md                 the docs-site pages: Claude Code, Codex, OTEL only, HTTP
```

## Tests

```sh
python3 -I tests/run_tests.py
```

The tests run against a local fake CtrlB server that answers with the responses in `tests/fixtures/seeker/`. Those responses are synthetic: they are written by hand, not recorded. When `claude` is installed, the tests also run `claude plugin validate` on the marketplace and the plugin. When `codex` is installed, they load the installer's output with `codex mcp list` in a temporary Codex home. The tests never touch `~/.codex/config.toml`.

See [Contributing](CONTRIBUTING.md) for development and testing, [Releases](CONTRIBUTING.md#releases) for versioning and plugin updates, and [Security](SECURITY.md) for private vulnerability reporting.
