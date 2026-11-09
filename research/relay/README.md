# Relay research code

This directory is research code, not the plugin. Nothing in `plugin/`, `lib/`, `bin/` or `codex/` imports it, and installing ctrlb-agent does not install it.

`swarm_mcp.py` is the research prototype of CtrlB Relay: a stdio MCP server that gives each agent of a multi-agent run a shared set of rows on a lake stream (`swarm_board`). The claim protocol was first measured here, then ported to Seeker (`internal/pkg/relay`). Seeker's rules have since grown, and the prototype follows them so that both can be checked against one set of fixtures. The tests compare it with Seeker's fixtures and run it against a lake. It talks to the lake directly; the product talks to Seeker.

| File | What it is |
|---|---|
| `swarm_mcp.py` | the prototype: `peers`, `post`, `claim`, `release`, `done`, `handoff`, `ask`, `answer`, `brief`, `briefs`, `contract`, `contracts`, `task`, `tasks` |
| `relay_paths.py` | resource keys, a copy of `lib/paths.py` that reads the kinds table from `lib/keys.json` |
| `lib_lake.py` | a small lake client: write rows and run SQL |
| `test_swarm_mcp.py` | the parity runner |
| `relay_proof.py` | the benchmark harness: Claude Code agents on a toy or DevBench task set, against Seeker, one trial at a time, per arm |
| `run_swarm.py` | the earlier harness, which runs agents against the prototype; `relay_proof.py` reads its template, rules and `bash_writes` |
| `stall_replay.py` | replays finished runs through Seeker's run view, to see the stall detector (decision 31) |
| `run_benchmark.py` | the full benchmark (relay-benchmark-2026-11-09) as one queue of single `relay_proof.py` trials: groups of identical Relay settings, two lanes each, STATUS posts and a cost projection |
| `bench_analyse.py` | the benchmark's tables from its trial files, with the duplicated-edit recount that leaves out Bash writes into a scratch copy |
| `run_round5.py` | round 5 (relay-stall-takeover-2026-11-09): the takeover proof (`handoff1-control`), the path-hold proof (`handoff1-registry`, agent-1 stopped while it holds `REGISTRY.md`), Sonnet `pair-relay` readtime with rule text v8, and the corrected contracts cells (`contracts-reader`, `contracts-provider` and their controls), as single trials in lanes of identical settings |
| `round5_analyse.py` | round 5's tables (takeovers, path holds, contracts beside the benchmark's, Sonnet turns and cost, cost and wall) from its trial files |
| `overhead.py` | the overhead accounting of the benchmark's pair cells: extra turns (and cost) per tool, Relay minus no Relay |
| `test_round5.py` | offline tests of round 5's measures (takeovers, contract triggers and reads, question duplicates by `ask_about` entry) |
| `run_round6.py` | round 6 (relay-redirect-2026-11-09): redirect and swarm sizing (`pair-relay` hone and readtime, `quad-relay` hone, Sonnet `pair-relay` readtime), witnessed evidence (`pair-relay-witness`, `false-evidence`), handoff by the SessionEnd hook (`handoff1-stop` with SIGTERM, `handoff1-kill` with SIGKILL) and stale contracts (`contracts-provider`, `contracts-provider-staleoff`), one trial at a time in groups of identical settings, every task registered with a test command (`--every-task-test`; readtime's packages in a venv of its own), with a cost projection after the first cell and a stop above $40 |
| `round6_analyse.py` | round 6's tables (A/E redirect, B hook handoffs, C stale contracts, D witnessed done, F REGISTRY.md turns, the overhead accounting on the Sonnet cell, cost and wall) beside the benchmark's and round 5's |
| `test_round6.py` | offline tests of round 6's measures and tables on synthetic transcripts, stream logs and trial files |
| `history_latency.py` | round 7 (relay-record-2026-11-10): times `GET history` for one resource with the index and the scan predicates, interleaved (decision 40) |
| `forecast_eval.py` | round 7: the contention forecast on the held-out runs of rounds 5 and 6 from the history before them: hot-key precision and recall against the base rate, sizing, bundles against pair-none double edits (decision 41) |
| `patterns_gate.py` | round 7: the patterns gate, per-group turns from Seeker's patterns against the overhead accounting, the hot key, and the round 5 wait-in-line loop (decision 42) |
| `post_status.py` | posts a status note to a run on the lake, or reads the last notes back with `--tail` |
| `dup_fix.py` | recounts duplicated work in finished `relay_proof.py` trials with `run_swarm.bash_writes` |
| `test_bash_writes.py` | unit tests of `run_swarm.bash_writes` |
| `devbench/` | DevBench task sets: `tasksets.py` builds, loads and scores them; `contracts.py` holds the contracts arm's judges; `features.py` holds the feature arms' data |

## What the prototype's claim does

These rules follow Seeker's `docs/relay.md` (Claim protocol, State machine, Waiting, Registered tasks and bundled claims):

- `read_key` is the same fold as Seeker's `ReadKey`. Both pass `tests/fixtures/key_states.json`.
- `claim(resource, reopen?, paths?, wait_s?)` writes a `HOLD` on the resource and on each of `paths` in one write, waits, and reads every key again. The claim is held only when its hold is the first live one on every deciding key. A read that does not show the claimer's own row decides nothing (`not_visible`).
- `wait_s` comes from decision 25 and is capped at 55 s. A losing claim keeps its `HOLD` live as its place in line. It reads the keys again every 5 × `SWARM_WAIT_MS`. The claim ends in one of three ways:
  - `held: true` with `waited_ms`;
  - refused with `finished_by`, when a `DONE` finishes the round;
  - `reason: "timed_out"` with `held_by` and `position`, after a `RELEASE` (text `timed_out`) and a `REFUSED` row.
- Registered tasks and `belongs_to_task` come from decisions 18 and 27:
  - `task(name, text, paths)` registers a task with its files.
  - A claim of the task takes those files with it.
  - A file registered to that task alone is decided on the task's key. Its `HOLD` is still written.
  - A bare claim of such a file is refused with `reason: "belongs_to_task"` and `task`. It is refused before anything is written, unless the caller holds the task.
  - Releasing the task hands its files back.
- `done(resource, evidence?)` comes from decision 30. It finishes a task name with the evidence of its check, `{command, exit_code, summary}`, which is written as the `DONE` row's text. `read_key` never reads that text. `release(finished=true)` stays as an alias of `done` with no evidence. With `SWARM_EVIDENCE_REQUIRED=1`, both `done` without a command and a finished `release` are refused with `reason: "evidence_required"`.
- `claim(resource, takeover=true)` comes from decision 33. When the first live hold of the key is another agent's and its holder wrote no row on the key or its files for `SWARM_STALL_S`, one write hands it to the caller: a `HANDOFF` with the stalled holder as giver (text `stalled: taken over by <agent>`) on each key the holder is first on, a `RELEASE` of the holder's other live holds, and the caller's `HOLD` where the holder is not first. The claim then waits, reads and decides like any claim; `read_key` does not change. A holder that is still working is refused with `reason: "not_stalled"` and nothing is written. `takeover_plan` is the decision as a pure function, `check_state` the edit check, and `tasks` marks a held task `stalled`. The prototype has no edit stream, so only relay rows count as progress.
- Not ported: `unknown_task` and its suggestion, user names (agents are told apart by agent id only), fencing tokens on edits, and the session store.

## Running the parity runner

You need Python 3.10 or later, with the standard library only. Run:

```sh
FIXTURES=<seeker checkout>/tests/fixtures python3 -I research/relay/test_swarm_mcp.py
```

- **Parity** runs `read_key` on every case of `key_states.json`. It also runs the `belongs_to_task` decision on the cases that carry a `claim`, and `relay_paths.key` on every case of `keys.json`. It needs no lake. When `FIXTURES` is not set, the runner looks in `../../../seeker/tests/fixtures`, which is a Seeker checkout beside this repo. When neither has the files, it skips these tests.
- **Evidence** checks that the `DONE` rows that carry evidence fold as they would without it. It needs a fixture from decision 30 on (Seeker `feat/relay-done-stall`), and is skipped with an older one.
- **Takeover** runs `takeover_plan` on the cases that carry a `takeover` and `check_state` on those that carry a `check`. It needs a fixture from decision 33 on (Seeker `feat/relay-stall-takeover`), and is skipped with an older one.
- **Live** runs the following against a lake, each test under a fresh run id: claims, releases, rounds, the read-your-write guard, handoff, questions, contracts, waiting in line (a waiter is served after a release, and a second waiter times out with its position), registered tasks, `done` with evidence, and the takeover of a stalled task with its file. When the lake does not answer, the live tests are skipped.

## Environment

| Variable | Default | Used for |
|---|---|---|
| `CTRLB_URL` | `http://127.0.0.1:5160` | the lake |
| `CTRLB_ORG` | `default` | the lake org |
| `CTRLB_AUTH` | empty | the whole `Authorization` header value; leave it empty for an unauthenticated local lake |
| `FIXTURES` | `../../../seeker/tests/fixtures` | Seeker's fixtures |
| `RELAY_KEYS_JSON` | `../../lib/keys.json` | the kinds table |
| `SWARM_RUN`, `SWARM_AGENT` | empty | the run and the agent of one server process |
| `SWARM_TOOLS` | `peers,post,claim,release` | the tools the server exposes |
| `SWARM_WAIT_MS`, `SWARM_LEASE_S` | `20`, `600` | the claim's wait and the lease |
| `SWARM_STALL_S` | `300` | a hold with no progress for this long can be taken over (`takeover=true`) |

## The benchmark harness

The harness runs real Claude Code agents (`claude -p`). It needs `claude` on the `PATH`, and it costs model usage. Importing a script starts nothing, and each script prints its usage with `--help`. Run the unit tests of the edit counter with:

```sh
python3 -I research/relay/test_bash_writes.py
```

`relay_proof.py` talks to Seeker as an orchestrator: it registers tasks, answers questions, and sets and restores the workspace's Relay settings. It loads this repo's `plugin/` into the agents. The keys come from the environment (`SEEKER_MCP_KEY`, `CTRLB_KEY`, and `SEEKER_JWT` for the settings). They go into request headers only, and are never printed or written.

| Option | What it does |
|---|---|
| `--first K` | numbers the trials from K |
| `--lane NAME` | gives this run its own lane |
| `--evidence-required` | sets `evidenceRequired` (decision 30) and allows the `done` tool |
| `--task-tests` | registers each task with its test command (decision 32) |

To run two lanes side by side, give each its own `--lane`, give the pair disjoint `--first` numbers, and set `RELAY_PROOF_NO_RESTORE=1` on both. Only arms with identical settings can run as two lanes at once.

None of the harness's data or results is shipped. `.gitignore` keeps the default locations out of git. Point the harness at the data with these variables:

| Variable | Default | What it is |
|---|---|---|
| `RELAY_SEEKER` | `http://127.0.0.1:8096` | Seeker |
| `RELAY_SESSIONS_DATASET` | `agent_sessions` | the workspace's sessions dataset |
| `RELAY_PLUGIN` | `../../plugin` | the ctrlb-agent plugin the Relay arms load |
| `RELAY_PROOF_OUT` | `results/relay-proof` | trial results, which `dup_fix.py` and `stall_replay.py` also read |
| `RELAY_PROOF_NO_RESTORE` | unset | `1`: a lane leaves the settings in place for the queue to restore |
| `RELAY_PROOF_FEATURES` | `relay_proof_features.json` | the feature arms' data per project (questions, contracts). `devbench/features.py` builds it from the downloaded projects. Without it, those arms exit. |
| `RELAY_TEMPLATE` | `template/` | the toy task template: `TASKS.md`, with `src/` and `tests/` |
| `DEVBENCH_SRC` | `devbench-src/benchmark_data/python` | a DevBench checkout's Python projects |
| `DEVBENCH_VENV_PY` | `devbench-venv/bin/python` | the scoring Python, with pytest and the projects' packages |
| `DEVBENCH_SETS` | `devbench/sets` | the built task sets: `python3 -I research/relay/devbench/tasksets.py build readtime hone` |
| `SWARM_OUT`, `STALL_REPLAY_OUT` | `results/...` | the results of `run_swarm.py` and `stall_replay.py` |
| `FIELD_RULES` | `../../lib/hooks_shared.py` | the field rule that `run_swarm.py`'s `claim-files` arm injects |
| `CLAUDE_PROJECTS` | `~/.claude/projects` | where Claude Code writes transcripts |

Relative defaults are relative to `research/relay/`.

## Serving the prototype

To serve the prototype to an agent, register this as an MCP stdio server:

```sh
python3 -I research/relay/swarm_mcp.py
```

Give each agent its own `SWARM_AGENT` and one shared `SWARM_RUN`.

## Third-party material

`devbench/contracts.py` and `devbench/features.py` quote a few lines of the readtime and hone projects' source, docs, tests and sample data from [DevBench](https://github.com/open-compass/DevBench). DevBench's code is licensed under the Apache License 2.0, and its data (the projects' docs, tests and sample data, which is what these lines quote) under CC BY 4.0. Those lines are used only to build and judge the benchmark's feature arms. DevBench itself is not included; set `DEVBENCH_SRC` to a checkout.
