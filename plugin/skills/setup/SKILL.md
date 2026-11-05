---
name: setup
description: Check ctrlb-agent's configuration and connection to CtrlB, and offer to upload past sessions
disable-model-invocation: true
allowed-tools: Bash(python3 -I *)
---

Set up or check ctrlb-agent for the user.

1. Run `python3 -I "${CLAUDE_PLUGIN_ROOT}/bin/status.py"` and show the user its output.
2. If it says ctrlb-agent is OFF or not configured, tell the user to run this themselves in a separate terminal. It asks for the key with hidden input, so it must not go through you:
   `python3 -I "${CLAUDE_PLUGIN_ROOT}/bin/configure.py"`
   Never ask the user to paste a key, token, password or Authorization value into this chat.
3. If status.py says `CTRLB_MCP_URL` or `CTRLB_KEY` is not set in the environment, tell the user the ctrlb tools need both in the shell that starts Claude Code, and show these two lines with the placeholders left in:
   `export CTRLB_MCP_URL="<your CtrlB URL>/mcp"`
   `export CTRLB_KEY="<your key>"`
4. If CtrlB answered, run `python3 -I "${CLAUDE_PLUGIN_ROOT}/bin/backfill.py" --count` and show the user how many past sessions it found. Ask whether to upload them. Only if they say yes, run it with `--yes` (add `--project <text>` if they want only some projects).
5. Finish by running status.py again and telling the user what it reports.
