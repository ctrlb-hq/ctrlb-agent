---
title: Connect with OTEL only
description: Send Claude Code's or Codex's own OpenTelemetry logs to CtrlB, with no plugin or hooks, and what that export leaves out.
slug: connect-otel
---

# Connect with OTEL only

Claude Code and Codex can both export their own events as OpenTelemetry (OTLP) logs. CtrlB accepts OTLP logs on each dataset's ingest endpoint, so the harness can send straight to CtrlB with no collector in between. Nothing from this repository is installed.

This path uses your workspace's ingest credentials, the ones your services already send telemetry with. It does not use your MCP key. You need two values from the Connect page in CtrlB:

| Value | What it is |
|---|---|
| `<your OTLP logs endpoint>` | The OTLP logs URL of the dataset to write to. It ends in `/api/<org>/<dataset>/_otel/v1/logs`. |
| `<your ingest credentials>` | The `Authorization` header value for that endpoint, for example `Basic ...`. |

Replace both in the snippets below.

## Claude Code

Add these lines to your shell profile, or put them in front of one `claude` command:

```sh
export CLAUDE_CODE_ENABLE_TELEMETRY=1
export OTEL_LOGS_EXPORTER=otlp
export OTEL_EXPORTER_OTLP_PROTOCOL=http/json
export OTEL_EXPORTER_OTLP_LOGS_ENDPOINT="<your OTLP logs endpoint>"
export OTEL_EXPORTER_OTLP_HEADERS="Authorization=<your ingest credentials>"
export OTEL_LOG_USER_PROMPTS=1
export OTEL_LOG_ASSISTANT_RESPONSES=1
export OTEL_LOG_TOOL_DETAILS=1
export OTEL_LOGS_EXPORT_INTERVAL=5000
```

- The three `OTEL_LOG_*` lines add the prompt text, the response text and the tool arguments to the records. Without them, the records carry event names, tool names, timings, token counts and cost, but no text.
- `OTEL_LOGS_EXPORT_INTERVAL` is how often, in milliseconds, the batch is sent. Records arrive in CtrlB about that long after the event.

To tag every record of a run, add a resource attribute. Every record carries it as a column:

```sh
OTEL_RESOURCE_ATTRIBUTES=ctrlb.run=nightly-refactor-1 claude -p "Fix the failing tests in src/parser"
```

## Codex

Codex reads its OTEL settings from `config.toml`. The installer writes them for you, inside the same marked block as the MCP server and the hooks:

```sh
cd ~/ctrlb-agent
python3 -I install.py --codex --otel \
  --otel-endpoint "<your OTLP logs endpoint>" \
  --otel-header "Authorization=<your ingest credentials>" \
  --dry-run
```

The dry run shows header values as `"<hidden>"`. Drop `--dry-run` to write it. The installer refuses to write your CtrlB MCP key into an OTEL header. The header value is stored in `config.toml` as given, so the file holds your ingest credentials; keep its mode at 0600.

If you want the export without the hooks or the MCP server, add the tables yourself. This is the layout Codex 0.166 accepts:

```toml
[otel]
log_user_prompt = true
log_agent_responses = true

[otel.exporter.otlp-http]
endpoint = "<your OTLP logs endpoint>"
protocol = "json"
headers = { "Authorization" = "<your ingest credentials>" }
```

- `exporter` is a table here, not a string. `exporter = "otlp-http"` is rejected when Codex loads the file.
- `endpoint` and `protocol` are required. `protocol` is `"json"` or `"binary"`.
- `headers` is a map, not a string.
- `tool_result = { max_bytes = 65536 }` under `[otel]` caps the tool output in each record.

Run `codex mcp list` after editing. Codex loads the whole file for every command and names the key it rejects.

## What OTEL export does not carry

The export is the harness's event log. The agent package writes different rows and does more:

- **No session store rows.** OTEL records land in the dataset you point them at, with the harness's own attribute columns. The recall tools (`search_sessions`, `find_command`, `next_actions`, `fetch_records`), the run view and the session drawer read the session store rows the plugin and the Codex hooks write. They do not read OTEL records.
- **No run id or agent id columns.** A run is only what you tag with `OTEL_RESOURCE_ATTRIBUTES` in Claude Code.
- **No thinking blocks.** Records are per event (a prompt, a tool decision, a tool result, an API request, a response), not per content block.
- **No `paths` column.** Edits are tool records with their arguments, not keyed file paths, so the run view's duplicate-edit detector does not see them.
- **No CtrlB Relay.** The agent is not told its run id, has no `peers`, `post`, `claim` or `release` tools, and edits are not checked. Add the MCP server (Connect Claude Code or Connect Codex) for those.
- **No redaction.** The package's secret patterns run only on rows it uploads. With `OTEL_LOG_USER_PROMPTS` and `OTEL_LOG_ASSISTANT_RESPONSES`, prompt and response text is exported as written.
- **No past sessions.** Only events after the variables are set are exported. `bin/backfill.py` uploads past sessions as session store rows.
