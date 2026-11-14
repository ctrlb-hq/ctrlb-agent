# Local mode

Local mode runs Recall with one `ctrlb` binary and a SQLite database on this machine. It needs no CtrlB account or other service. Once installed, session uploads and recall work without a network connection.

The agent gets four recall tools:

- `search_sessions`: find words or phrases in past sessions.
- `find_command`: find shell commands from past sessions.
- `next_actions`: read what happened after a matching action.
- `fetch_records`: read a range of records from a session.

Relay is available in hosted CtrlB only. Local mode turns the plugin's Relay hooks off.

## Install

Use Python 3.9 or newer on macOS or Linux, with an arm64 or amd64 processor. From a clone of this repository:

```sh
python3 -I install.py --local --version "<version>" --download-url "<download address>"
```

The release download address has not been published yet. Supply it with `--download-url` or `CTRLB_DOWNLOAD_URL` when it is available; without either, the installer stops with a clear error. The flag wins over the environment variable. The address is the full release base URL; no version is appended to it. The base must contain `SHA256SUMS` and the raw binary named `ctrlb-<os>-<arch>`, where the OS is `darwin` or `linux` and the architecture is `arm64` or `amd64`.

If the supplied address contains the literal `<version>`, `--version V` replaces it with `V`; omitting that flag uses `latest`. An address without `<version>` is used as given.

The installer verifies the binary's SHA-256 before replacing anything, installs it with mode 0755 as `~/.ctrlb/bin/ctrlb`, then runs `ctrlb init`. A checksum mismatch or missing checksum refuses the installation. Downloads use HTTPS; `file://` and HTTP on `127.0.0.1` or `localhost` are also accepted for local testing.

`CTRLB_HOME` changes the location of the binary, config and database. `--port N` chooses the local server port (default 7531). Add the `bin` folder under your CtrlB home to your shell's `PATH` to run `ctrlb` directly. Initialization saves the local config with mode 0600; an existing config is backed up before it is replaced.

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

## Limits

Local mode is for one machine and one user. It has no Relay, team search, search by meaning, or continuous sync with hosted CtrlB.

## Move to hosted CtrlB

When you have a hosted CtrlB URL and key, run this in your terminal:

```sh
ctrlb push --url "<hosted>" --key "<hosted key>" --dataset "<name>"
```

This copies the stored session rows, keeping their timestamps, then backs up the local config and switches it to hosted CtrlB. The local SQLite file stays on this machine. Interrupted uploads can be resumed by running the same command again. `--dry-run` shows the tables and counts without uploading.

Reload the two export lines above after the switch and restart your agent so its MCP connection uses hosted CtrlB. Remove any `CTRLB_MODE=local` override. The plugin's upload state is kept, so the next session uploads new records without uploading old transcripts again.
