---
title: Connect Claude Code
description: Install the ctrlb-agent plugin so every Claude Code session goes to the session store and joins CtrlB Relay.
slug: connect-claude-code
---

# Connect Claude Code

The `ctrlb-agent` plugin does three things in Claude Code:

- It uploads every session, including subagents, to the session store in CtrlB. Each message, thinking block, tool call and tool result is one row. Text that looks like a secret is replaced with `[REDACTED]` before upload.
- It adds CtrlB's MCP server. The agent gets the recall tools (`search_sessions`, `find_command`, `next_actions`, `fetch_records`) and the relay tools (`peers`, `post`, `claim`, `release`).
- At session start it tells the agent its run id and agent id. Before each `Edit`, `Write`, `MultiEdit` or `NotebookEdit` it checks the file against the run's claims.

Everything goes to CtrlB with your MCP key. Nothing else is installed: the hooks are standard-library Python 3.9 or newer.

## 1. Set the environment

Mint a key on the Connect page in CtrlB. Add these lines to your shell profile (`~/.zshrc` or `~/.bashrc`) and open a new terminal:

```sh
export CTRLB_URL="<your CtrlB URL>"
export CTRLB_MCP_URL="<your CtrlB URL>/mcp"
export CTRLB_KEY="<your key>"
```

`CTRLB_URL` and `CTRLB_KEY` are what the hooks use to upload rows and check edits. `CTRLB_MCP_URL` is the MCP server's address. Claude Code expands it, and the key, in the plugin's `.mcp.json` when it starts.

## 2. Install the plugin

```sh
claude plugin marketplace add ctrlb-hq/ctrlb-agent
claude plugin install ctrlb-agent@ctrlb-agent
```

The first command adds the marketplace from the `ctrlb-hq/ctrlb-agent` repository. The second installs the plugin for your user. Start Claude Code and run `/ctrlb-agent:setup`. It checks the connection and offers to upload your past sessions.

### Without the marketplace

If you cannot add a marketplace, load the plugin from a clone for each session:

```sh
git clone https://github.com/ctrlb-hq/ctrlb-agent.git ~/ctrlb-agent
claude --plugin-dir ~/ctrlb-agent/plugin
```

`--plugin-dir` loads the plugin for that session only. Pass it every time, or put it in a shell alias.

## 3. Runs

A session is its own run, and its subagents join it. To put several sessions in one run, give them the same `CTRLB_RUN`:

```sh
CTRLB_RUN=nightly-refactor-1 claude -p "Fix the failing tests in src/parser"
```

The agent id is the session id. Set `CTRLB_AGENT` to use a label instead, for example `CTRLB_AGENT=builder-1`. When neither is set, the MCP server gets empty `X-CtrlB-Run` and `X-CtrlB-Agent` headers, and the agent passes `run_id` and `agent` to the relay tools from its session-start note.

## Keep the key out of your profile

Clone the repository and run the configure step yourself, in a terminal, not through an agent:

```sh
git clone https://github.com/ctrlb-hq/ctrlb-agent.git ~/ctrlb-agent
python3 -I ~/ctrlb-agent/bin/configure.py
```

It asks for the URL, the sessions dataset, an optional name and run id, and the key. The key prompt hides what you type. It writes `~/.ctrlb/config.json` with mode 0600, and checks that CtrlB answers. The hooks read that file when `CTRLB_URL` or `CTRLB_KEY` is not set. The MCP server still reads `CTRLB_MCP_URL` and `CTRLB_KEY` from the environment, because Claude Code expands them in `.mcp.json`.

## Edit checks

Before an edit, the hook sends the file path, relative to the session's working directory, to CtrlB. The workspace setting `enforce` decides the answer:

| `enforce` | When another agent in the run holds the file |
|---|---|
| `off` | Nothing happens. |
| `advisory` (default) | The agent gets a note naming the holder and the `claim` call to make. The edit goes ahead. |
| `enforce` | The edit is refused. The reason names the holder and the `claim` call to make. |

The check gives up after two seconds. If CtrlB does not answer, the edit goes ahead and the error is written to `~/.ctrlb/hooks.log`. Claims are advisory unless the workspace turns on `enforce`. `CTRLB_ENFORCE=off` skips the check on one machine.

## Check and remove

- Status: run `/ctrlb-agent:setup` in Claude Code.
- Log: `~/.ctrlb/hooks.log`.
- Off for one session: `CTRLB_OFF=1 claude`.
- Remove: `claude plugin uninstall ctrlb-agent@ctrlb-agent`, then delete `~/.ctrlb`. Rows already in CtrlB stay there.
