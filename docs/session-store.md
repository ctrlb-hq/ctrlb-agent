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

## The session index

At the start of each session the plugin and the Codex hooks put a short index of your recent sessions in this project into the agent's context, under the recall rule. The agent knows what happened before without searching. CtrlB builds the index from the record. No model is called.

- **Which sessions:** your own recent sessions whose working directory is the session's directory or under it. A session on the same git branch also counts, unless the branch is a default branch such as `main` or `master`. With `relay.read_all`, every user's sessions count, and a line names its user. Subagent sessions are not listed.
- **One line each, newest first, at most 10:** the session id, the start time, the duration, the cost, the `seq` range, the first user message and the last assistant message (120 characters each), up to 8 edited files, and the last command. A line is at most 340 characters, so 10 lines stay under about 1,000 tokens.
- **Expand a line** with `fetch_records(session_id, seq_from, seq_to)`, or search one session with `search_sessions(term, session_id)`.
- **A subagent** gets only its parent session's line.
- **The first session in a project** gets no index: there is nothing earlier in the record.
- **A slow or unreachable CtrlB:** the hook waits at most 2 seconds. When CtrlB does not answer in time or answers an error, the session starts without the index.

The same index is the `session_index` tool (`cwd`, `branch`, `limit`, `since`, `session_id`) and `GET /ai/sessions/{dataset}/index`. It answers `{sessions: [{session_id, user_name?, started, duration_s, seq_from, seq_to, first_message, files_edited, last_command, last_message, cost_usd, note_seq?, line}], note?, expand, text}`. `text` is the index as the hooks inject it.

## The running note

When the workspace's `sessions.runningNote` setting is on (the default), CtrlB keeps a short hand-off note for each session while it runs: every 120,000 characters of new records, the workspace's model updates a note of at most 500 words with the task, the current state, the next step, and the key decisions, dropped approaches and files, each pointing into the record by `seq`. The note is a row of the session (`block_type` `note`), so it is searchable.

- **Before compaction** (Claude Code): the PreCompact hook gives the latest note to the compaction as its instructions, so the summary is written with the note in view. Codex ignores a PreCompact hook's output.
- **After compaction, or when you resume a session** (both harnesses): the session starts with its latest note above the session index.
- **No note yet**, or CtrlB slow or unreachable: nothing is added.

**When the note is updated.** Seeker updates the note in three cases. First, while the session runs, each time it has 120,000 characters of new records. Second, before a compaction (Claude Code): the PreCompact hook asks Seeker to bring the note up to date now, over the records since the last update even when they are fewer, and waits up to 43 seconds for it, so the note given to the compaction covers the whole session. Third, when a session ends (Claude Code, and a Codex main thread): the hook asks for the same update and does not wait, so the next session starts from a current note. Between these, the note lags the session by up to 120,000 characters. Search the record for anything after it. A Codex session that compacts still starts from the latest note, which can lag the same way, because Codex ignores PreCompact output.

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
