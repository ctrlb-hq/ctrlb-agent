---
title: Run view
description: The Runs and Connect pages in CtrlB, the run timeline, the resources panel, the detectors and the session drawer.
slug: run-view
---

<!-- Written from the Relay plan, section 7 (Orion target layout), before the Orion pages were built. Check names and layout against the shipped pages. -->

# Run view

The run view is where people see what a run's agents did. It is under **AI** in CtrlB, with two pages: **Runs** (`/ai/runs`, and one run at `/ai/runs/<run id>`) and **Connect** (`/ai/connect`). The pages show when the workspace has logs. They use the time range of the other AI pages.

The pages read CtrlB's run endpoints, `GET /ai/relay/<dataset>/runs` and `GET /ai/relay/<dataset>/runs/<run id>`, and the session endpoints for transcripts. You need the `relay.read` permission and access to the sessions dataset, and `sessions.read` to open a transcript. You see your own user's agents unless your role has `relay.read_all`.

## Runs

A table of runs in the time range, newest started first.

| Column | Meaning |
|---|---|
| Run id | `CTRLB_RUN`, or the session id of a session that is its own run |
| Agents | Agents with a row in the run |
| Started, duration | From the run's first row to its last |
| Posts | `post` calls |
| Claims held / refused | Claims answered `held: true`, and claims refused |
| Done | Task names released with `finished=true` (file paths are never done) |
| Open claims | Holds live now: not released, not done, lease not run out |
| Duplicate edits | Keys edited by more than one agent |
| Silent agents | Agents with no row for a while during a live run |
| Cost | Sum of `cost_usd`, when the rows carry it (Claude Code sessions) |

- The status filter shows live, done or all runs. A run is live while one of its agents wrote in the last 60 seconds. A live run has a pulsing dot.
- Sort by cost or by duplicate edits to find the expensive runs and the ones where agents did the same work.
- Click a row to open the run.
- With no runs, the page links to Connect.

## One run

### KPI strip

Agents, tasks done, duplicate edits, refused claims and cost. The detector counts link to the rows they found.

### Timeline

One lane per agent on a shared time axis.

- A claim is a bar from its `HOLD` (or `REOPEN`) to its end: green when a task ended `DONE`, grey when it was released. A file path's claim always ends released.
- A refused claim is a red tick on the lane of the agent that was refused.
- A post is a marker. Hover it to read the text.
- Tool calls are a thin density strip under the lane.
- Click anything on a lane to open that agent's session at that point.

### Resources

A table of every key the run claimed or edited: the key, its holder, who finished it, how many claims on it were refused, and the agents that edited it. A key edited by two or more agents carries the duplicate badge.

### Detectors

Both are computed when the page reads the run. There are no alerts on them.

| Detector | Fires when |
|---|---|
| Duplicate edit | Two or more agents have an edit on the same key. Edits are session rows of `Edit`, `Write`, `MultiEdit`, `NotebookEdit` or `apply_patch`, keyed from their `paths` the way claims are keyed. |
| Silent agent | The run is live and an agent has written nothing for `silentAfterSeconds` (120 by default, a workspace setting). |
| Stalled task | A registered task is held (or its holder's lease ran out) and not finished, and its holder has written no relay row on the task or its files and edited none of its files for `stallAfterSeconds` (300 by default). Computed for finished runs too. The API answers it as `detectors.stalled` and `stalled_tasks`; the page's Stalled badge is not built yet. |

Edits made from a shell (`sed -i`, a redirect, `git checkout`) have no `paths`, so the duplicate-edit detector does not see them.

### Live runs

While the run is live, the page reads it again every 2 seconds, and less often while the browser tab is hidden. Relay rows appear at once. Session rows appear when the agent's hook uploads them, at the end of each turn, so the tool-call strip lags the claims by up to one turn.

A run's events are capped at the newest 5,000. A longer run shows a note that older events are left out.

## Session drawer

The drawer shows one session's transcript:

- Rows in `seq` order: text, thinking (collapsed), tool calls with their command or file paths, tool results with their error state.
- A search box over this session's rows, the same search as `search_sessions`.
- **Open in Data Explorer**, with the sessions dataset and the session id filled in.

## Connect

The Connect page holds what an agent needs to reach CtrlB:

- Your MCP key: mint it, rotate it, copy it. The key acts as you, with your roles and dataset grants.
- Four copy blocks, filled with your workspace's URL: [Claude Code](connect-claude-code.md), [Codex](connect-codex.md), [OTEL only](connect-otel.md), and [HTTP](connect-http.md) for anything else. Each block says how to set the run id.

## Settings

The workspace's Relay settings, read with `GET /ai/relay/settings` and changed with `PUT /ai/relay/settings` (needs `relay.manage`):

| Setting | Default | What it does |
|---|---|---|
| `sessionsDataset` | `agent_sessions` | Where session rows go. Relay rows go to `_ctrlb_relay_<sessionsDataset>`. |
| `waitMs` | 20 | The wait between a claim's write and its second read |
| `leaseSeconds` | 600 | How long a claim lasts unless released (60 to 86,400) |
| `enforce` | `advisory` | `off`, `advisory` or `enforce`: what the edit hook does when another agent holds the file. See [Limits](limits.md). |
| `silentAfterSeconds` | 120 | When an agent of a live run counts as silent |
| `stallAfterSeconds` | 300 | When a held, unfinished task with no progress from its holder counts as stalled |
| `evidenceRequired` | `false` | `done` on a task (and a finished release) is refused unless it names the command that was run |
