---
title: Limits
description: What CtrlB Relay and the session store do not do, and the sizes and timings they work within.
slug: limits
---

# Limits

## Claims are advisory unless the workspace turns on enforce

A claim is a row in the run's record. It tells other agents, and the people watching, who is working on what. It does not stop a process from writing a file.

The workspace setting `enforce` decides what the edit hook does when an agent is about to edit a file another agent in its run holds:

| `enforce` | Effect |
|---|---|
| `off` | Nothing. The hook does not call CtrlB. |
| `advisory` (default) | The agent gets a note naming the holder and the `claim` call to make. The edit goes ahead. |
| `enforce` | The hook refuses the edit. The reason names the holder and the `claim` call to make. |

Even in `enforce` mode:

- Only these tools are checked: `Edit`, `Write`, `MultiEdit` and `NotebookEdit` in Claude Code, `apply_patch` in Codex. A shell command that writes a file (`sed -i`, `>`, `git checkout`, a script) is not checked.
- The check fails open. If CtrlB does not answer within two seconds, or answers with an error, the edit goes ahead and the error is written to `~/.ctrlb/hooks.log`.
- A key without the `relay.write` permission (a Viewer) gets `403` from the check, and the edit goes ahead.
- `CTRLB_ENFORCE=off` on a machine skips the check there. `CTRLB_OFF=1` turns every hook off.
- Agents connected only through OTEL, or through HTTP without a hook, are not checked.
- Codex skips new or changed hooks until you trust them with `/hooks`.

## One claim per key

- The first live hold on a key wins. Every other claim on it is refused with `held_by`, until the holder releases it or its lease runs out.
- An agent that claims a key it already holds renews it: `held: true, renewed: true`, with the `lease_until` of the new hold.
- Only a task name can be done. A task released with `finished=true` is refused to every later claim with `finished_by` and `finished_at`, until a claim passes `reopen=true` and starts a new round of work on it.
- A file path is never done. Releasing it always hands it back, whatever `finished` says, so it can be edited again in a later round. A `DONE` row already on a path, from before this rule, blocks nothing.
- Claims are decided on the key, not on the text you pass. Paths are keyed relative to `cwd` and keep their case; names are lower-cased with their whitespace collapsed. A file with no extension, such as `Makefile`, is a name unless you claim `path:Makefile`.
- Two spellings of one task are two keys. Claim the task name as the task list gives it, then the file path the task names, and hold both while you work.
- Claims in one run cover every user's agents in it, even when each user sees only their own rows.

## Lease: 10 minutes by default

- A claim lasts `leaseSeconds` (600 by default, 60 to 86,400) from its `HOLD` row, unless released. A claim's answer gives the end as `lease_until`.
- Claiming the key again before the lease runs out renews it. When the lease runs out the claim is gone, and another agent can claim the key.
- A `release` after the lease ran out writes nothing: it answers `released: 0`, and the key is not done. Claim it again, then release it.
- A crashed or killed agent's claim lasts until its lease runs out.
- For work that takes longer than the lease, raise `leaseSeconds` in the workspace's Relay settings.

## Sizes

| What | Limit |
|---|---|
| Rows per records upload | 500. More is refused with `400`. |
| Bytes per records upload | 4 MiB. More is refused with `413`. |
| `body` of a session row | 64 KiB |
| Post text | 2,000 characters |
| Post text in `peers` | 300 characters |
| `peers` events | 40 by default, at most 500 |
| Paths per check | 64 |
| Run id, agent id, resource | 256 bytes each |
| Events in one run's detail | The newest 5,000 |
| Search hits | 10 by default, at most 25; over the newest (or oldest) 500 candidate rows |
| `fetch_records` | 50 rows, each body cut at 4,000 characters |

## Time

- Relay rows are written through before CtrlB answers, so the next read by any agent sees them.
- Session rows are uploaded by the hooks at the end of each turn, before compaction and at session end. A tool call is in the session store after the turn that made it ends.
- Relay reads look back 30 days. Claims and `DONE` rows older than that are forgotten.
- A run is live while one of its agents wrote in the last 60 seconds.

## Who sees what

- Each call acts as the key's user, with that user's roles and dataset grants.
- Session searches, `peers`, the runs list and the run detail show only your own user's rows, unless your role has `relay.read_all`.
- `post`, `claim`, `release`, the edit check and records uploads need `relay.write`. Changing the Relay settings needs `relay.manage`.
- The MCP server registers `post`, `claim` and `release` only when it runs with write tools enabled.
- A records row naming another user is refused with `403`, and nothing in that upload is written.

## What the run view does not do

- The detectors (duplicate edits, silent agents) are computed when the page reads the run. There are no alerts on them.
- Edits made from a shell have no `paths` column, so the duplicate-edit detector does not see them.
- Cost is shown for Claude Code sessions, from their `cost_usd`. Codex sessions record tokens only.

## Redaction

The plugin and the Codex hooks replace text that looks like a secret with `[REDACTED]` before upload. The patterns match common key and token shapes; they are not a guarantee. Rows you upload over HTTP, and OTEL records, are not redacted by the package.

## What OTEL export does not carry

Claude Code's and Codex's own OTEL export lands in the dataset you point it at, with the harness's own columns. Compared with the plugin and the Codex hooks, it has:

- no session store rows, so the recall tools, the run view and the session drawer do not read it;
- no `run_id` or `agent` columns (in Claude Code you can tag a run with `OTEL_RESOURCE_ATTRIBUTES`);
- no thinking blocks: records are per event, not per content block;
- no `paths` column, so no duplicate-edit detection;
- no relay tools and no edit check;
- no redaction;
- no past sessions.

[Connect with OTEL only](connect-otel.md) has the details.
