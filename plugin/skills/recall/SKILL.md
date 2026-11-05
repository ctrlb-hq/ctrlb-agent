---
name: recall
description: Search the user's session record in CtrlB for earlier work, decisions, commands or errors
---

Search the session record for what the user asks about ($ARGUMENTS).

Use the ctrlb tools: `search_sessions` with one or two distinctive words (a file name, function, error text or command), `find_command` for an exact shell command, `fetch_records` to read around a hit, and `next_actions` to see what happened after a tool call. Try more than one wording before concluding something is not there. Report what you found with the session_id and seq of each source, and say plainly if nothing matched.
