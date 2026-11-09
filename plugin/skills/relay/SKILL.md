---
name: relay
description: Join a CtrlB Relay run and follow its three rules (peers, post, claim), and share briefs of what you read, while working beside other agents
argument-hint: <run id>
---

You are joining CtrlB Relay run `$ARGUMENTS`. Other agents in this run work in the same repository at the same time and share the run's record with you through the ctrlb tools: `peers`, `post`, `claim`, `release`, `handoff`, `ask`, `answer`, `brief`, `briefs`, `tasks`, `contract`, `contracts`, and `done`.

Pass `run_id="$ARGUMENTS"` on every one of those calls. Pass `agent` as the agent id your session rule gives you (the line that starts "CtrlB Relay: your run id is ..."). If you have no such line, use your session id.

Follow these three rules for the rest of the session.

1. Other agents are working in this same directory right now. Before starting a task, look at the task's files to see whether it is already done or in progress, and pick one that is not.
2. Other agents are working in this same directory right now and share a relay with you (the ctrlb tools). Before starting a task, call `peers` to see what others have taken or finished, then `post` the task you are taking, do it, and `post` when it is done. Never take a task another agent has posted.
3. Other agents are working in this same directory right now and share a relay with you (the ctrlb tools). Claim the task by the name your orchestrator registered (for example `claim(resource="task 03 median")`; `tasks` lists them). Its files come with it. If `held` is false, pick another task. Work only while you hold it. When the task is done, release it with `finished=true`. When the task's test passes, call `done` on the task name with the command you ran and its result. A task is finished only when the record shows what was checked. A file that belongs to no task is claimed by its path.

Share what you read with briefs:

- Before reading a file or document that other agents of the run may also need (a spec, a shared module, a config), call `briefs` with it as `resources` (for example `briefs(resources="src/app.py")`). If a brief comes back, read it first; open the file only for what the brief does not answer.
- After reading a file you will edit, or one that others will need, post a `brief` for it: `brief(resource="src/app.py", text="...")`, up to a few hundred words. Say what it is for, what it exports or decides, and what not to touch.
- A brief is not a claim and reserves nothing. Anyone can write one; the newest brief of a file is the one `briefs` returns.

How to read the answers:

- `claim` answers `held: true` with the `key` it stored, for example `name:task 03 median`, and the files that came with it in `paths`, for example `["path:src/median.py"]`. You hold all of them.
- `held: false` with `reason: unknown_task` means the run has registered tasks and none has that name. Claim again with the `suggestion`, or pick a name from `tasks`.
- `held: false` with `held_by` means another agent is on it now. Pick another task; do not retry the same resource in a loop.
- If `tasks` shows a task you need as `stalled`, claim it with `takeover=true`, read its evidence and notes, and continue it. Never wait in line on a stalled task. If a file you need is held by an agent that has stopped, the hold shows as stalled; take it over the same way: `claim(resource="REGISTRY.md", takeover=true)`.
  - A takeover answers `held: true` with `taken_over_from` (the agent that stopped) and `stall` (its reading: `holder`, `last_activity`, `idle_s`, `stall_after_s`); a task's files come with it. The stopped agent's next edit of them is refused.
  - `held: false` with `reason: not_stalled` means the holder is still working (`stall.idle_s` is under `stall.stall_after_s`): pick other work. `reason: other_user` means another user's agent holds it: only that user's agents can take it over.
  - A free task, or one whose holder's lease ran out, is claimed as usual.
- `held: false` with `reason: timed_out`, `held_by` and your `position` in line is the answer of a claim that waited (`wait_s`) and was not served in time.
- `held: false` with `reason: belongs_to_task` means the file comes with a registered task (`task` in the answer): claim that task instead, and its files come with it.
- A held claim of a task may answer `ask_about`: facts or questions the task's owner wants you to ask about. `ask` them before deciding them yourself.
- A held claim of a task (and `tasks`) may show `test`: the command that tests the task, for example `"test": "python3 -m pytest tests/test_median.py"`. That is the check to run before `done`.
- `held: false` with `finished_by` and `finished_at` means the task is done. Pick another task, unless you were asked to do that task again: then claim its name with `reopen=true`, which starts a new round of work on it.
- Finish tasks, just release files. A file is never finished: `release` hands it back, so you or another agent can edit it again later. When a task is done, release the task with `finished=true` (the default): its files are handed back with it. The finished task is what stops another agent from doing it again.
- Run the task's test command (`tasks` and `claim` show it as `test`); when it passes, call `done` on the task name with that command, its exit code and its last output line as the `evidence`: `done(resource="task 03 median", evidence={"command": "python3 -m pytest tests/test_median.py", "exit_code": 0, "summary": "4 passed in 0.03s"})`. When the task has no test command, give the check you ran. Report only a check you ran: a smoke script of your own is not the task's test. `done` replaces `release` with `finished=true`, which answers `deprecated`.
- `done: false` with `reason: evidence_required` means the run requires evidence: call `done` again with the command you ran, its exit code and one line of its output. When the refusal carries `test`, that is the task's test command: run it and give it. Otherwise the plugin quotes your last test command after such a refusal; run the task's test first if you have not.
- `release` with `finished=false` hands a task back for others to take.
- Use the coordination tools before a prose note (`post`), each for its one purpose:
  - `handoff` when you must leave a task you hold: it moves to the agent you name in one step, with its files.
  - `ask` when you are blocked on something only another agent or the orchestrator can decide. Do not post a QUESTION note.
  - `answer`, with the question's id, when you can settle an open question; `peers(kind="question", open=true)` lists the open ones.
  - `contract` when you finish an interface others will build on (a type, a route, a config shape): its name, the shape in a few lines, and the files that define it. Do not post a CONTRACT note.
  - `contracts` before you build on another agent's interface; its `changed_since` lists the files changed since its text was written, and `changed_since_read` says it changed since you last read the contracts: read it again before building on it.
  - `peers(kind="answer", since=<ts>, wait_s=30)` waits for an answer (or `kind="handoff"` for a handoff) instead of a sleep loop.

  Post a note only for what none of them covers.
- A shared thing that is not a file, such as the dev server or a port, is claimed as `slot:<name>` and released when you are done with it: `claim(resource="slot:devserver")` before `npm run dev`, `release(resource="slot:devserver")` after. A slot's claim lasts two minutes; claim it again to keep it longer.
- Claims are advisory unless the workspace turns on `enforce`. In `enforce` mode an edit to a file another agent holds is refused, with the claim call to make.
