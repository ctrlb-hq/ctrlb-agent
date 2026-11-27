# Coding Standards

Review rules for a diff against its ticket. Every rule is a judgement call; flag each violation with the line and the smaller alternative.

## Minimal diff

The diff is the smallest change that meets the ticket's acceptance criteria.

- A new type, file, trait or helper earns its place only when a plain line at the call site cannot do the job. Prefer a log line over a logging type, a closure over a helper, an existing function over a new one.
- Every changed line traces to an acceptance criterion. Rewrites, renames and tidy-ups of surrounding code belong in their own ticket.
- Scope is the ticket. A bug found along the way becomes a new ticket, not a change in this diff.

## No comments

Added code carries no comments of any kind: line, block, doc (`///`) or module (`//!`). Names, types and tests carry the meaning. Existing comments in the codebase are not precedent for new ones.

## Tests are the spec

Each test pins one acceptance criterion of the ticket, written as the behaviour a caller observes.

- One test per criterion. A second test for the same criterion needs a distinct failure it alone catches.
- Test our behaviour, not our dependencies: no tests that restate what tokio, DataFusion, actix or another crate already guarantees.
- Test through the seam the ticket names, not the internals of a helper the implementation introduced.
