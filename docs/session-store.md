---
title: The session store and CtrlB Relay
description: What CtrlB keeps of every agent session, and how agents in one run coordinate through that record with posts and claims.
slug: session-store
---

# The session store and CtrlB Relay

CtrlB keeps every agent session as rows in your workspace. That record is the **session store**. **CtrlB Relay** is what agents working at the same time do with it: read what the others did, leave notes, and claim work before they start it.

## Concepts

| Term | Meaning |
|---|---|
| **Session** | One Claude Code or Codex conversation, including its subagents. Each message, thinking block, tool call and tool result is one row in the sessions dataset, `agent_sessions` by default. |
| **Run** | A set of agents that share a run id. A session with no `CTRLB_RUN` is its own run, and its subagents join it. A script that starts several agents sets `CTRLB_RUN` once, and they share it. |
| **Agent** | One session in a run. Its id is the session id, or the `CTRLB_AGENT` label when that is set. |
| **Relay** | The run's shared record: posts, claims, reopens, releases and refusals, one row each, in `_ctrlb_relay_agent_sessions`. Agents read it with `peers`. |
| **Post** | A short note from one agent to the others in the run (`post`). |
| **Resource** | What an agent claims: a file path or a task name. |
| **Key** | The normalised name a claim is decided on. A path becomes `path:<path>`, relative to the agent's working directory. Anything else becomes `name:<lower-cased name>`. `./src//app.py` and `src/app.py` are one key. |
| **Claim** | An exclusive hold on a key (`claim`). The answer is `held: true` or `held: false` with the holder. |
| **Lease** | How long a claim lasts unless released: 10 minutes by default. A crashed agent's claim ends when its lease runs out. |
| **Done** | A task name released with `finished=true`. A done task is refused to later claims in the run, with `finished_by`, until a claim passes `reopen=true`. A file path is never done: releasing it always hands it back. |
| **Round** | One stretch of work on a task name, from its first claim, or from a `REOPEN`, to its `DONE`. A claim with `reopen=true` on a done task starts the next round. |
| **Check** | Before an edit, the plugin's hook asks whether another agent holds the file. The answer depends on the workspace's `enforce` setting. |

Claims are advisory unless the workspace sets `enforce` to `enforce`. Relay records who holds what and tells agents. Only the edit hook, in `enforce` mode, refuses an edit, and only for the edit tools it watches. See [Limits](limits.md).

## How rows get there

- The Claude Code plugin and the Codex hooks upload session rows to CtrlB at the end of each turn, before compaction and at session end. Secrets are redacted first. See [Connect Claude Code](connect-claude-code.md) and [Connect Codex](connect-codex.md).
- Relay rows are written by CtrlB when an agent calls `post`, `claim`, `release`, or when an edit check finds a conflict. Each row is written through to the lake before CtrlB answers, so the next read by any agent sees it.
- Nothing is updated or deleted. A release is a new row; a claim's state is read from the rows.
- Both datasets are ordinary logs datasets. Data Explorer, alerts and SQL see them.

## Getting the agent to search

Having the recall tools is not enough: an agent often greps and reads files instead of searching the record. CtrlB gives it a rule that names the moment to search:

- **At session start.** The Claude Code plugin and the Codex hooks add the rule to the session's context, with the session id: before the first grep, file read or shell command for a question about earlier work, call `search_sessions`; after a command fails, call `find_command` or `next_actions` before changing it. Subagents get the parent's session id and the same rule.
- **In the project's files.** `python3 -I install.py init --codex --claude` writes the same rule, without the session id, into the project's `AGENTS.md` (Codex reads it) and `CLAUDE.md` (Claude Code reads it), between `<!-- ctrlb:begin -->` and `<!-- ctrlb:end -->`. Text outside the markers is never changed. Use it when not every teammate has the hooks installed, and for Codex, which runs no hook until a person approves it. See [Connect Codex](connect-codex.md#3-add-the-ctrlb-block-to-agentsmd).

The rule asks the agent to search. It does not force a search. See [Limits](limits.md#steering).

## The write-first claim

Two agents can claim the same key at the same moment. If each read the record first and wrote a hold only when it saw none, both would read nothing and both would write: both would think they hold it. A claim in CtrlB writes first and decides on a second read:

```text
agent-a                        relay dataset                        agent-b
   |                                 |                                 |
   |--- 0. read the key's rows ----->|<----- 0. read the key's rows ---|
   |    (a done task: refuse)        |       (a done task: refuse)     |
   |                                 |                                 |
   |--- 1. write HOLD, nonce a ----->|<------ 1. write HOLD, nonce b --|
   |                                 |                                 |
   |    2. wait waitMs (20 ms)       |       2. wait waitMs (20 ms)    |
   |                                 |                                 |
   |--- 3. read the live holds ----->|<----- 3. read the live holds ---|
   |<-- HOLD a, HOLD b --------------|-------------- HOLD a, HOLD b -->|
   |                                 |                                 |
   |    4. first live hold is a:     |       4. first live hold is     |
   |       held: true                |          not mine:              |
   |                                 |<-- write RELEASE b, REFUSED ----|
   |                                 |       held: false,              |
   |                                 |       held_by: agent-a          |
```

- A live hold is a `HOLD` (or `REOPEN`) row of the key's current round with no `RELEASE` or `DONE` row for its nonce, younger than the lease.
- Holds are ordered by time, then agent, then nonce. Every reader sees the same rows in the same order, so both agents agree on the first.
- The wait covers the time a written row takes to become readable. On the lake that is a few milliseconds; the default wait is 20 ms.
- `release` writes `DONE` (`finished=true` on a task name) or `RELEASE` (`finished=false`, and always on a file path) for the caller's live holds on the key.
- A claim with `reopen=true` on a done task writes a `REOPEN` row instead of a `HOLD` and follows the same steps, so two agents that reopen at once agree on one holder. Only rows from that `REOPEN` on decide the key; the earlier rounds stay in the record.
- `check` is step 3 for each path, with no hold written.

In the research behind this page, 480 contended write-first claims produced no double holder, against 30 of 30 trials for read-then-write. Four agents on one repository did 27 of 96 tasks twice with no shared record, 19 when they could post, and 1 when they could claim.

## Research

The two papers behind the session store and Relay are not public yet. They are:

- *A Session Store for Long-Running Coding Agents: Keeping the Full Record Searchable Across Compactions* (`session-store-long-running.tex`): one agent, and a record that survives compaction.
- *A Session Store for Swarms of Coding Agents: Coordinating Through a Shared, Searchable Record* (`session-store-swarms.tex`): many agents, one record they coordinate through, the claim protocol and its measurements.

## Next

- [Connect Claude Code](connect-claude-code.md), [Connect Codex](connect-codex.md), [Connect with OTEL only](connect-otel.md), [Connect over HTTP](connect-http.md)
- [Relay tools](relay-tools.md): the ten MCP tools and the agent rules
- [Run view](run-view.md): runs, timelines and transcripts in CtrlB
- [Limits](limits.md)
