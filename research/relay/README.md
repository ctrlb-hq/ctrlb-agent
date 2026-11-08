# Relay research code

This directory is research code, not the plugin. Nothing in `plugin/`, `lib/`, `bin/` or `codex/` imports it, and installing ctrlb-agent does not install it.

`swarm_mcp.py` is the research prototype of CtrlB Relay: a stdio MCP server that gives each agent of a multi-agent run a shared set of rows on a lake stream (`swarm_board`). The claim protocol was first measured here, then ported to Seeker (`internal/pkg/relay`). Seeker's rules have since grown, and the prototype follows them so that both can be checked against one set of fixtures. The tests compare it with Seeker's fixtures and run it against a lake. It talks to the lake directly; the product talks to Seeker.

| File | What it is |
|---|---|
| `swarm_mcp.py` | the prototype: `peers`, `post`, `claim`, `release`, `handoff`, `ask`, `answer`, `brief`, `briefs`, `contract`, `contracts`, `task`, `tasks` |
| `relay_paths.py` | resource keys, a copy of `lib/paths.py` that reads the kinds table from `lib/keys.json` |
| `lib_lake.py` | a small lake client: write rows and run SQL |
| `test_swarm_mcp.py` | the parity runner |

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
- Not ported: `unknown_task` and its suggestion, user names (agents are told apart by agent id only), fencing tokens on edits, and the session store.

## Running the parity runner

You need Python 3.10 or later, with the standard library only. Run:

```sh
FIXTURES=<seeker checkout>/tests/fixtures python3 -I research/relay/test_swarm_mcp.py
```

- **Parity** runs `read_key` on every case of `key_states.json`. It also runs the `belongs_to_task` decision on the cases that carry a `claim`, and `relay_paths.key` on every case of `keys.json`. It needs no lake. When `FIXTURES` is not set, the runner looks in `../../../seeker/tests/fixtures`, which is a Seeker checkout beside this repo. When neither has the files, it skips these tests.
- **Live** runs the following against a lake, each test under a fresh run id: claims, releases, rounds, the read-your-write guard, handoff, questions, contracts, waiting in line (a waiter is served after a release, and a second waiter times out with its position) and registered tasks. When the lake does not answer, the live tests are skipped.

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

To serve the prototype to an agent, register this as an MCP stdio server:

```sh
python3 -I research/relay/swarm_mcp.py
```

Give each agent its own `SWARM_AGENT` and one shared `SWARM_RUN`.
