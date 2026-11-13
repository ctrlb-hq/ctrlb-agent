---
title: Connect Codex
description: One installer command writes CtrlB's MCP server and hooks into Codex's config.toml, with a backup.
slug: connect-codex
---

# Connect Codex

The installer writes CtrlB into Codex's `config.toml`. After that, Codex does what Claude Code does with the plugin:

- Every session, including subagent threads, is uploaded to the session store from the rollout files in `~/.codex/sessions`. Secrets are redacted before upload.
- CtrlB's MCP server gives the agent the recall tools and the relay tools (`peers`, `post`, `claim`, `release`).
- At session start the agent is told its run id and agent id. Before each `apply_patch`, the files the patch names are checked against the run's claims.

It needs Codex CLI 0.166 or newer and Python 3.9 or newer. Nothing else is installed.

## 1. Set the environment

Mint a key on the Connect page in CtrlB. Add these lines to your shell profile and open a new terminal:

```sh
export CTRLB_URL="<your CtrlB URL>"
export CTRLB_MCP_URL="<your CtrlB URL>/mcp"
export CTRLB_KEY="<your key>"
```

Codex reads the key from `CTRLB_KEY` each time it starts. The key is never written into `config.toml`.

## 2. Run the installer

The installer runs from a clone of the repository. The hooks it writes call `codex/hook.py` in that clone, so keep the clone where it is.

```sh
git clone https://github.com/ctrlb-hq/ctrlb-agent.git ~/ctrlb-agent
cd ~/ctrlb-agent
python3 -I install.py --codex --dry-run
python3 -I install.py --codex
```

`--dry-run` prints the change to `~/.codex/config.toml` as a diff and writes nothing. The second command writes it.

Then start Codex and run `/hooks` once. Codex runs no hook until a person approves it with `/hooks`, and it skips a hook again after the hook changes. Until you approve them, sessions are not uploaded, edits are not checked and the session-start rule is not sent.

## 3. Add the CtrlB block to AGENTS.md

Codex reads `AGENTS.md` in your project at the start of every session, with no approval step. Because hooks run only after `/hooks`, the `AGENTS.md` block is what reaches Codex by default. It tells the agent when to search the record: before the first grep, file read or shell command for a question about earlier work, and after a command fails.

Run this in your project:

```sh
python3 -I ~/ctrlb-agent/install.py init --codex --dry-run
python3 -I ~/ctrlb-agent/install.py init --codex
```

`--dry-run` prints the change as a diff and writes nothing. The block sits between `<!-- ctrlb:begin -->` and `<!-- ctrlb:end -->`:

- If `AGENTS.md` does not exist, it is created.
- If it exists, the block is added at the end. Your text is not changed.
- If the block is already there, only the text between the markers is replaced. A run that would change nothing writes nothing.
- Before a change, the old file is copied to `AGENTS.md.ctrlb-backup-<time>` beside it.
- `--remove` deletes the block and nothing else. `--dir` names another project directory.

Commit `AGENTS.md` so everyone who opens the project in Codex gets the block.

## What it writes

The installer writes one block between two marker comments, `# >>> ctrlb-agent` and `# <<< ctrlb-agent`. Everything else in the file stays as it was.

```toml
[mcp_servers.ctrlb]
url = "<your CtrlB URL>/mcp"
bearer_token_env_var = "CTRLB_KEY"
env_http_headers = { "X-CtrlB-Run" = "CTRLB_RUN", "X-CtrlB-Agent" = "CTRLB_AGENT" }
```

- `url` is `CTRLB_MCP_URL`, or `CTRLB_URL` plus `/mcp`, or `--mcp-url`.
- `bearer_token_env_var` names the variable Codex reads the key from. Codex sends it as `Authorization: Bearer`.
- `env_http_headers` sends `CTRLB_RUN` and `CTRLB_AGENT` as headers when they are set.

Then one `[[hooks.<Event>]]` table per event. Each runs `python3 -I <clone>/codex/hook.py <mode>`:

| Event | Mode | What it does |
|---|---|---|
| `SessionStart` | `session-start` | Tells the agent its run id and agent id, and how to search its record |
| `SubagentStart` | `subagent-start` | Uploads the parent's new lines, tells the subagent its parent and ids |
| `PreCompact` | `flush` | Uploads the lines added since the last upload |
| `SessionEnd` | `session-end` | In a relay run (`CTRLB_RUN` set), hands off every hold the agent still has to nobody, in one write (tasks open for the next claimer, paths and slots released), then uploads like `flush`. Codex has no hook after a tool call, so the handoff's text cannot list edits or tests, and `done` carries no witness |
| `Stop`, `SubagentStop` | `upload` | The same, in the background (`async = true`) |
| `PreToolUse`, matcher `apply_patch` | `pre-tool` | Sends the patch's file paths to the edit check |

For example, the `PreToolUse` table:

```toml
[[hooks.PreToolUse]]
matcher = "apply_patch"
[[hooks.PreToolUse.hooks]]
type = "command"
command = "python3 -I \"/home/you/ctrlb-agent/codex/hook.py\" pre-tool"
timeout = 5
```

Every hook exits 0 and never blocks a session. Errors go to `~/.ctrlb/hooks.log`. The edit check gives up after two seconds and lets the patch through.

## Backup and running it again

Before it writes, the installer copies the old file to `config.toml.ctrlb-backup-<time>` beside it. Running it again with the same settings changes nothing and makes no backup. With a different URL, it replaces the block; `--dry-run` shows the changed lines first.

It refuses to write, and changes nothing, when `config.toml` already has an MCP server named `ctrlb` outside the block. Remove that one with `codex mcp remove ctrlb` and run the installer again.

Options, all passed after `--codex`:

| Option | Meaning |
|---|---|
| `--dry-run` | Print the diff, write nothing |
| `--mcp-url URL` | The MCP URL to write |
| `--codex-home DIR` | Another Codex home (default `$CODEX_HOME`, else `~/.codex`) |
| `--config FILE` | Edit this file instead of `<codex home>/config.toml` |
| `--otel` | Also write Codex's OTEL export (see Connect with OTEL) |
| `--uninstall` | Remove the block |

To try it without touching your Codex setup, point it at an empty home:

```sh
cd ~/ctrlb-agent
CODEX_HOME="$(mktemp -d)" python3 -I install.py --codex --mcp-url "<your CtrlB URL>/mcp" --dry-run
```

## Runs

A Codex session is its own run, and its subagent threads join it. To put several sessions in one run, give them the same `CTRLB_RUN`:

```sh
CTRLB_RUN=nightly-refactor-1 codex exec "Fix the failing tests in src/parser"
```

The agent id is the thread id. Set `CTRLB_AGENT` to use a label instead.

`CTRLB_USER` is still read, as the agent label when `CTRLB_AGENT` is unset; `user_name` on the rows is always the key's user.

## Past sessions and removal

- Upload past sessions: `python3 -I ~/ctrlb-agent/bin/backfill.py --codex`. It lists what it would send and asks first; `--count` only lists.
- Off for one session: `CTRLB_OFF=1 codex`.
- Remove: `python3 -I ~/ctrlb-agent/install.py --codex --uninstall`. It backs the file up first, as an install does. To remove the `AGENTS.md` block, run `python3 -I ~/ctrlb-agent/install.py init --codex --remove` in the project.
