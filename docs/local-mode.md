# Local mode

Local mode runs Recall and Relay with one `ctrlb` binary and a SQLite database on this machine. It needs no CtrlB account or other service. Once installed, session uploads, recall and coordination work without a network connection.

The agent gets five recall tools:

- `search_sessions`: find words or phrases in past sessions.
- `find_command`: find shell commands from past sessions.
- `next_actions`: read what happened after a matching action.
- `fetch_records`: read a range of records from a session.
- `session_index`: list the caller's recent sessions.

All five tools read only stored records and make no model call. They are listed with or without `CTRLB_RUN` in the agent's environment. With no `CTRLB_RUN`, local mode lists only these five tools and leaves Relay hooks off. Set `CTRLB_RUN` before starting agents to coordinate them in one run.

Local mode writes no running notes or memories; those need a model.

## Install

Use Python 3.9 or newer on macOS or Linux, with an arm64 or amd64 processor. From a clone of this repository:

```sh
python3 -I install.py --local --version "<version>" --download-url "<download address>"
```

The release download address has not been published yet. Supply it with `--download-url` or `CTRLB_DOWNLOAD_URL` when it is available; without either, the installer stops with a clear error. The flag wins over the environment variable. The address is the full release base URL; no version is appended to it. The base must contain `SHA256SUMS` and the raw binary named `ctrlb-<os>-<arch>`, where the OS is `darwin` or `linux` and the architecture is `arm64` or `amd64`.

If the supplied address contains the literal `<version>`, `--version V` replaces it with `V`; omitting that flag uses `latest`. An address without `<version>` is used as given.

The installer verifies the binary's SHA-256 before replacing anything, installs it with mode 0755 as `~/.ctrlb/bin/ctrlb`, then runs `ctrlb init`. A checksum mismatch or missing checksum refuses the installation. Downloads use HTTPS; `file://` and HTTP on `127.0.0.1` or `localhost` are also accepted for local testing.

`CTRLB_HOME` changes the location of the binary, config and database. `--port N` chooses the local server port (default 7531). Add the `bin` folder under your CtrlB home to your shell's `PATH` to run `ctrlb` directly. Initialization saves the local config with mode 0600; an existing config is backed up before it is replaced.

Already have the binary? Run `python3 -I bin/configure.py --local` from this clone to initialize it and print the same config-reading exports shown below. Add `--port N` to choose the port. Set `CTRLB_BIN` to the executable's path to override binary discovery for configuration, session start and `bin/status.py`; otherwise they look in `<CTRLB_HOME>/bin/ctrlb`, then absolute entries in `PATH`.

## Connect your agent

For Claude Code, install the plugin:

```sh
claude plugin marketplace add ctrlb-hq/ctrlb-agent
claude plugin install ctrlb-agent@ctrlb-agent
```

Put these two lines in your shell profile and load them before starting your agent. They read the saved config when the shell runs them; they contain no key value:

```sh
export CTRLB_MCP_URL="$(python3 -I -c 'import json, os; print(json.load(open(os.path.join(os.environ.get("CTRLB_HOME") or os.path.expanduser("~/.ctrlb"), "config.json")))["mcp_url"])')"
export CTRLB_KEY="$(python3 -I -c 'import json, os; print(json.load(open(os.path.join(os.environ.get("CTRLB_HOME") or os.path.expanduser("~/.ctrlb"), "config.json")))["key"])')"
```

If you set `CTRLB_HOME`, set it before these lines. Remove old hosted `CTRLB_URL`, `CTRLB_MCP_URL`, `CTRLB_KEY` and `CTRLB_MODE` settings before loading the local values: environment variables take precedence over the config file.

For Codex, run from the clone:

```sh
python3 -I install.py --codex
```

Keep the clone where it is; Codex's hooks call scripts in it. Start Codex and run `/hooks` once to trust the new hooks.

## Start and check

Start a new Claude Code or Codex session. The session-start hook starts `ctrlb serve` if the local server is down, then new session records upload to SQLite. The local database is `~/.ctrlb/ctrlb.db`, or `ctrlb.db` under `CTRLB_HOME`.

```sh
ctrlb status
```

Status shows the version, database size, datasets, row and session counts, and newest record time. You can also start the server yourself with `ctrlb serve`.

## Several agents on one laptop

After connecting your agent above, start two or three agents from the same repository in separate terminals. Give them the same `CTRLB_RUN` and a different `CTRLB_AGENT` each. For Claude Code:

```sh
CTRLB_RUN=parser-fix CTRLB_AGENT=parser claude
CTRLB_RUN=parser-fix CTRLB_AGENT=tests claude
CTRLB_RUN=parser-fix CTRLB_AGENT=review claude
```

For Codex, use the same environment variables:

```sh
CTRLB_RUN=parser-fix CTRLB_AGENT=parser codex
CTRLB_RUN=parser-fix CTRLB_AGENT=tests codex
CTRLB_RUN=parser-fix CTRLB_AGENT=review codex
```

The agents share Relay tasks, claims, notes, questions, contracts and handoffs through the local server. Their session rules name the run and agent, their edit hooks check claims, and session end hands off remaining holds. Claude Code also registers tasks from subagent prompts and records tool evidence. Recall and session uploads continue to use the same local database.

The Relay tools are listed only when the MCP session connects with a run. To join or change a local run, set `CTRLB_RUN` and `CTRLB_AGENT` in each agent's environment and restart the agents. Local session context says "CtrlB is local on this machine; the run is fixed when the session starts." If the session has no run or a different run, `/ctrlb-agent:relay <run>` gives restart instructions and stops before calling Relay tools for that run. If the run already matches, it continues normally. A run saved only in `config.json` does not enable local Relay; an empty value or an unresolved `${...}` placeholder does not enable it either.

This setup is for several agents on one machine, used by one user.

## Limits

Local mode is for one machine and one user. It has no team search, search by meaning, or continuous sync with hosted CtrlB.

## Move to hosted CtrlB

When you have a hosted CtrlB URL and key, read the key with a hidden prompt, then run this in your terminal. The key stays out of command arguments and shell history:

```sh
export CTRLB_HOSTED_KEY="$(python3 -I -c 'import getpass; print(getpass.getpass("Hosted CtrlB key: "))')"
ctrlb push --url "<hosted>" --dataset "<name>"
unset CTRLB_HOSTED_KEY
```

`ctrlb push` reads `CTRLB_HOSTED_KEY`, or you can supply `--key-file PATH` instead. The file must not be group/world readable (use mode 0600). For a key already saved in such a file:

```sh
ctrlb push --url "<hosted>" --key-file "$HOME/.ctrlb/hosted-key" --dataset "<name>"
```

This copies the stored session rows, keeping their timestamps, then backs up the local config and switches it to hosted CtrlB. The local SQLite file stays on this machine. Interrupted uploads can be resumed by running the same command again. `--dry-run` shows the tables and counts without uploading.

Reload the two export lines above after the switch and restart your agent so its MCP connection uses hosted CtrlB. Remove any `CTRLB_MODE=local` override. The plugin's upload state is kept, so the next session uploads new records without uploading old transcripts again.
