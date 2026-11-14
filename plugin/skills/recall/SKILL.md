---
name: recall
description: Search the user's session record in CtrlB for earlier work, decisions, commands or errors
---

Search the session record for what the user asks about ($ARGUMENTS).

Use the ctrlb tools: `search_sessions` with one or two distinctive words (a file name, function, error text or command), `find_command` for an exact shell command, `fetch_records` to read around a hit, and `next_actions` to see what happened after a tool call. Try more than one wording before concluding something is not there. Report what you found with the session_id and seq of each source, and say plainly if nothing matched.

Local mode has five recall tools: the four above and `session_index`, which lists the caller's recent sessions. All five read only stored records and make no model call, and are listed with or without `CTRLB_RUN`. Local mode writes no running notes or memories; those need a model.

For earlier work in general, omit `session_id` to search this user's sessions. Pass the current `session_id` only to search this session's own turns before compaction. A subagent searching its parent's work passes the parent's `session_id`.
