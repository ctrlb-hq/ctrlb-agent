# ctrlb-agent

CtrlB's agent package for Claude Code and Codex. It does three things:

- It uploads every session, including subagents, to the session store in CtrlB. One row per message, thinking block, tool call and tool result, with secrets redacted first.
- It tells each agent its CtrlB Relay run id and agent id at session start. The relay tools (`peers`, `post`, `claim`, `release`) and the recall tools (`search_sessions`, `find_command`, `next_actions`, `fetch_records`) come from Seeker's hosted MCP server at `/mcp`.
- It checks each edit against the run's claims before the edit runs. Claims are advisory unless the workspace turns on `enforce`.

Everything goes to Seeker with your MCP key. Nothing talks to the lake directly. Standard library Python 3.9 or newer; nothing else is installed.

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

- `[mcp_servers.ctrlb]` with Seeker's `/mcp` URL and `bearer_token_env_var = "CTRLB_KEY"`. Codex reads the key from the environment; the key is never written into the file. `env_http_headers` sends `CTRLB_RUN` and `CTRLB_AGENT` as `X-CtrlB-Run` and `X-CtrlB-Agent` when they are set.
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

- With no setting, each Claude Code or Codex session is its own run, and its subagents join it.
- To put several sessions in one run, set `CTRLB_RUN` once for all of them:

  ```sh
  CTRLB_RUN=nightly-refactor-1 claude -p "Fix the failing tests in src/parser"
  ```

- The agent id is the session id. Set `CTRLB_AGENT` to use a label instead. A Claude Code subagent's id is `agent-<id>`, or `<label>:agent-<id>` when a label is set. A Codex subagent's id is its thread id, or `<label>:<thread id>`.
- `/ctrlb-agent:relay <run id>` joins a named run from inside a session and loads the three relay rules.

## Edit checks

A `PreToolUse` hook runs before `Edit`, `Write`, `MultiEdit` and `NotebookEdit` in Claude Code, and before `apply_patch` in Codex. For Codex the paths come from the patch's `*** Update File:`, `*** Add File:`, `*** Delete File:` and `*** Move to:` lines. The hook sends the file paths to `POST /ai/relay/{dataset}/runs/{run}/check`. The workspace setting `enforce` decides what happens:

| Mode | When another agent holds the file |
|---|---|
| `off` | Nothing. The hook prints nothing. |
| `advisory` (default) | The agent gets a note naming the holder and the claim call to make. The edit goes ahead. |
| `enforce` | The edit is refused. The reason names the holder and the claim call to make. |

The hook gives up after two seconds. On a timeout, an unreachable Seeker or any other error, the edit goes ahead and the error goes to `~/.ctrlb/hooks.log`. Set `CTRLB_ENFORCE=off` to skip the check on one machine.

## What is uploaded

Rows go to `POST /ai/sessions/{dataset}/records`, at most 500 per call. The dataset is `agent_sessions` unless `CTRLB_DATASET` says otherwise. Uploads happen at the end of each turn, before each compaction, when a subagent starts or stops, and when the session ends. Each upload sends only the transcript lines added since the last one.

Each row carries the session, run and agent ids, the role and block type, the tool name, the shell command, the paths an edit tool touched, the text (cut at 64 KB), error and summary flags, cwd, git branch, token counts and cost. Section 5 of `docs/relay-plan.md` lists the columns. `harness` is `claude` or `codex`.

Codex rows come from the rollout files in `~/.codex/sessions`. User and assistant messages, reasoning summaries, tool calls and tool results become rows. Developer messages and encrypted reasoning do not. Each `token_usage_record` becomes one `result` row with the token counts. Codex rows carry no cost.

Before upload, text that looks like a secret is replaced with `[REDACTED]`. That covers private keys, cloud and API keys, tokens, JWTs, Authorization and Bearer values, assignments to names ending in SECRET, TOKEN, PASSWORD or KEY, and passwords in URLs. Pattern matching cannot catch every secret. Do not paste real credentials into a session.

## Configuration

| Variable | `~/.ctrlb/config.json` key | Meaning |
|---|---|---|
| `CTRLB_URL` | `url` | Seeker's URL |
| `CTRLB_KEY` | `key` | your MCP key |
| `CTRLB_MCP_URL` | `mcp_url` | Seeker's `/mcp` URL, for the MCP server |
| `CTRLB_RUN` | `run` | run id for every session (default: each session is its own run) |
| `CTRLB_AGENT` | | agent label (default: the session id) |
| | `user` | the key's user (its `email` claim), written by `bin/configure.py`; `user_name` on the rows is always this user, decoded from the key in use |
| `CTRLB_USER` | | read for compatibility: the agent label when `CTRLB_AGENT` is unset; never `user_name` |
| `CTRLB_DATASET` | `dataset` | sessions dataset (default `agent_sessions`) |
| `CTRLB_ENFORCE` | `enforce` | `off` skips the edit check on this machine |
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
lib/common.py                     config, the Seeker client, upload state, log
lib/rows_claude.py                Claude Code transcript to session rows
lib/rows_codex.py                 Codex rollout to session rows
lib/hooks_shared.py               the injected rules and the edit check, shared by both hooks
lib/redact.py                     the redaction patterns
lib/paths.py                      resource keys, identical to Seeker's (tests/fixtures/keys.json); edit paths sent relative to cwd
bin/                              configure.py, status.py, backfill.py
tests/run_tests.py                python3 -I tests/run_tests.py
docs/connect-*.md                 the docs-site pages: Claude Code, Codex, OTEL only, HTTP
docs/relay-plan.md                the build plan
research/relay/                   the research prototype of Relay and its parity runner (research code, not the plugin)
```

## Tests

```sh
python3 -I tests/run_tests.py
```

The tests run against a local fake Seeker that answers with the responses in `tests/fixtures/seeker/`. Those responses are synthetic: they are built from section 4 of the plan, not recorded. When `claude` is installed, the tests also run `claude plugin validate` on the marketplace and the plugin. When `codex` is installed, they load the installer's output with `codex mcp list` in a temporary Codex home. The tests never touch `~/.codex/config.toml`.
