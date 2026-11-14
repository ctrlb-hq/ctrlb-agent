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

Do steps 1 and 2 yourself, or save the key in a file and let Claude Code do them: see [Set up with a prompt](#set-up-with-a-prompt).

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

## Set up with a prompt

Claude Code can do steps 1 and 2 for you. You do one thing yourself: save the key in a file. Do not give the key to the agent. What the agent reads goes into its transcript, and the transcript goes to the model provider and, with this plugin, to the session store.

**1. Save the key, in a terminal.** The key is not shown when you paste it:

```sh
mkdir -p ~/.ctrlb && chmod 700 ~/.ctrlb
( umask 077; printf 'Paste your CtrlB MCP key: '; stty -echo; IFS= read -r k; stty echo; echo; printf '%s' "$k" > ~/.ctrlb/key )
```

**2. Give Claude Code this prompt.** Replace `<your CtrlB URL>` first.

```text
Set up CtrlB for Claude Code on this machine. My CtrlB URL is <your CtrlB URL>.
My MCP key is in ~/.ctrlb/key. Never read, cat, print or copy that file, and never
write the key's value into a command, file or message. Refer to it only as
$(cat ~/.ctrlb/key). To check that the file exists, run: test -s ~/.ctrlb/key && echo present

1. Run: claude plugin marketplace add ctrlb-hq/ctrlb-agent
   Then: claude plugin install ctrlb-agent@ctrlb-agent
2. In the profile my $SHELL reads (~/.zshrc or ~/.bashrc), add this block. If a
   block marked "# ctrlb" is already there, replace it:
     # ctrlb
     export CTRLB_URL="<your CtrlB URL>"
     export CTRLB_MCP_URL="<your CtrlB URL>/mcp"
     export CTRLB_KEY="$(cat ~/.ctrlb/key)"
     # end ctrlb
3. Check the key without showing it:
     curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $(cat ~/.ctrlb/key)" "<your CtrlB URL>/ai/relay/agent_sessions/runs?limit=1"
   200 means the key works. 401 or 403 means the key is wrong, expired or revoked:
   tell me to mint a new one and save it again.
4. Tell me to open a new terminal, start claude, and run /ctrlb-agent:setup.
```

The plugin loads only in a new session, so the restart in step 4 is necessary. `/ctrlb-agent:setup` then checks the connection and offers to upload your past sessions.

If your organization already installs the plugin through managed settings, remove step 1 from the prompt. To change the key later, run the command in step 1 again and open a new terminal.

## 3. Runs

A session is its own run, and its subagents join it. To put several sessions in one run, give them the same `CTRLB_RUN`:

```sh
CTRLB_RUN=nightly-refactor-1 claude -p "Fix the failing tests in src/parser"
```

The agent id is the session id. Set `CTRLB_AGENT` to use a label instead, for example `CTRLB_AGENT=builder-1`. When neither is set, the MCP server gets empty `X-CtrlB-Run` and `X-CtrlB-Agent` headers, and the agent passes `run_id` and `agent` to the relay tools from its session-start note.

`CTRLB_USER` is still read, as the agent label when `CTRLB_AGENT` is unset; `user_name` on the rows is always the key's user.

## Keep the key out of your profile

Clone the repository and run the configure step yourself, in a terminal, not through an agent:

```sh
git clone https://github.com/ctrlb-hq/ctrlb-agent.git ~/ctrlb-agent
python3 -I ~/ctrlb-agent/bin/configure.py
```

It asks for the URL, the sessions dataset, an optional name and run id, and the key. The key prompt hides what you type. It writes `~/.ctrlb/config.json` with mode 0600, and checks that CtrlB answers. The hooks read that file when `CTRLB_URL` or `CTRLB_KEY` is not set. The MCP server still reads `CTRLB_MCP_URL` and `CTRLB_KEY` from the environment, because Claude Code expands them in `.mcp.json`.

## Project instructions (optional)

At session start the plugin tells the agent when to search the record: before the first grep, file read or shell command for a question about earlier work, and after a command fails. Every session with the plugin gets this rule.

For earlier work in general, the rule calls `search_sessions` without `session_id` to search this user's sessions. The current session id is used only for this session's own turns before compaction; a subagent uses the parent's session id when searching the parent's work.

To keep the rule in the project itself, for example for sessions where the hooks are off, write it into the project's `CLAUDE.md` from a clone of the repository. The block names the CtrlB tools, and only the plugin gives a session those tools, so every teammate still needs the plugin installed.

Run the last two commands from your project's root directory:

```sh
git clone https://github.com/ctrlb-hq/ctrlb-agent.git ~/ctrlb-agent
cd your-project
python3 -I ~/ctrlb-agent/install.py init --claude --dry-run
python3 -I ~/ctrlb-agent/install.py init --claude
```

The block sits between `<!-- ctrlb:begin -->` and `<!-- ctrlb:end -->`. Text outside the markers is not changed. A file that does not exist is created; a run that would change nothing writes nothing; before a change, the old file is copied to `CLAUDE.md.ctrlb-backup-<time>`. `--dry-run` prints the diff and writes nothing, and `--remove` deletes the block. Add `--codex` to write `AGENTS.md` for Codex as well.

## Edit checks

Before an edit, the hook sends the file path, relative to the session's working directory, to CtrlB. The workspace setting `enforce` decides the answer:

| `enforce` | When another agent in the run holds the file |
|---|---|
| `off` | Nothing happens. |
| `advisory` (default) | The agent gets a note naming the holder and the `claim` call to make. The edit goes ahead. |
| `enforce` | The edit is refused. The reason names the holder and the `claim` call to make. |

The check gives up after two seconds. If CtrlB does not answer, the edit goes ahead and the error is written to `~/.ctrlb/hooks.log`. Claims are advisory unless the workspace turns on `enforce`. `CTRLB_ENFORCE=off` skips the check on one machine.

In a relay run the plugin also registers a task for each sub-agent it spawns, keeps small text files from the session's scratch directories in the record, starts each session with the run's open tasks, contracts and handoffs, and claims shared files such as `REGISTRY.md` around each edit (`CTRLB_AUTO_SLOTS`, `CTRLB_SCRATCH_DIR`). See [Relay hooks](relay-hooks.md).

### When the session ends, and when the agent calls done

- **Session end.** In a relay run (`CTRLB_RUN` set, or the agent called a relay tool), the `SessionEnd` hook hands off every hold the agent still has, in one write. Tasks go to nobody: the next agent to claim one gets it, with a note naming the files edited in this session, the last test command and its exit code, and the agent's last note. Paths and slots are released.
  - `SessionEnd` runs on a normal exit, and on SIGTERM or SIGINT of `claude -p`. It does not run on SIGKILL; a stalled hold is then taken over (`claim` with `takeover=true`).
  - Claude Code gives `SessionEnd` hooks 1.5 s in all, and a plugin's own `timeout` does not raise that. The handoff gives up after 0.8 s; if CtrlB does not answer, nothing is written. `CLAUDE_CODE_SESSIONEND_HOOKS_TIMEOUT_MS` raises the budget for the transcript upload that follows.
- **Witnessed done.** The hooks record every Bash command of the session with its exit code and last output line. When the agent calls `done` with evidence, the hook checks it against that record. If this session ran the command with that exit code, the call goes out with `witnessed: true`; otherwise it is refused with what the hook saw. A witness is only as strong as the machine the hook runs on.

## Check and remove

- Status: run `/ctrlb-agent:setup` in Claude Code.
- Log: `~/.ctrlb/hooks.log`.
- Off for one session: `CTRLB_OFF=1 claude`.
- Remove: `claude plugin uninstall ctrlb-agent@ctrlb-agent`, then delete `~/.ctrlb`. Rows already in CtrlB stay there. To remove the `CLAUDE.md` block, run `python3 -I ~/ctrlb-agent/install.py init --claude --remove` in the project.
