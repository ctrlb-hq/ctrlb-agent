---
name: relay
description: Join a CtrlB Relay run and follow its three rules (peers, post, claim), and share briefs of what you read, while working beside other agents
argument-hint: <run id>
---

You are joining CtrlB Relay run `$ARGUMENTS`. Other agents in this run work in the same repository at the same time and share the run's record with you through the ctrlb tools: `peers`, `post`, `claim`, `release`, `brief`, `briefs`, `tasks`.

Pass `run_id="$ARGUMENTS"` on every one of those calls. Pass `agent` as the agent id your session rule gives you (the line that starts "CtrlB Relay: your run id is ..."). If you have no such line, use your session id.

Follow these three rules for the rest of the session.

1. Other agents are working in this same directory right now. Before starting a task, look at the task's files to see whether it is already done or in progress, and pick one that is not.
2. Other agents are working in this same directory right now and share a relay with you (the ctrlb tools). Before starting a task, call `peers` to see what others have taken or finished, then `post` the task you are taking, do it, and `post` when it is done. Never take a task another agent has posted.
3. Other agents are working in this same directory right now and share a relay with you (the ctrlb tools). Claim the task by the name your orchestrator registered (for example `claim(resource="task 03 median")`; `tasks` lists them). Its files come with it. If `held` is false, pick another task. Work only while you hold it. When the task is done, release it with `finished=true`. A file that belongs to no task is claimed by its path.

Share what you read with briefs:

- Before reading a file or document that other agents of the run may also need (a spec, a shared module, a config), call `briefs` with it as `resources` (for example `briefs(resources="src/app.py")`). If a brief comes back, read it first; open the file only for what the brief does not answer.
- After reading a file you will edit, or one that others will need, post a `brief` for it: `brief(resource="src/app.py", text="...")`, up to a few hundred words. Say what it is for, what it exports or decides, and what not to touch.
- A brief is not a claim and reserves nothing. Anyone can write one; the newest brief of a file is the one `briefs` returns.

How to read the answers:

- `claim` answers `held: true` with the `key` it stored, for example `name:task 03 median`, and the files that came with it in `paths`, for example `["path:src/median.py"]`. You hold all of them.
- `held: false` with `reason: unknown_task` means the run has registered tasks and none has that name. Claim again with the `suggestion`, or pick a name from `tasks`.
- `held: false` with `held_by` means another agent is on it now. Pick another task; do not retry the same resource in a loop.
- `held: false` with `finished_by` and `finished_at` means the task is done. Pick another task, unless you were asked to do that task again: then claim its name with `reopen=true`, which starts a new round of work on it.
- Finish tasks, just release files. A file is never finished: `release` hands it back, so you or another agent can edit it again later. When a task is done, release the task with `finished=true` (the default): its files are handed back with it. The finished task is what stops another agent from doing it again.
- `release` with `finished=false` hands a task back for others to take.
- Claims are advisory unless the workspace turns on `enforce`. In `enforce` mode an edit to a file another agent holds is refused, with the claim call to make.
