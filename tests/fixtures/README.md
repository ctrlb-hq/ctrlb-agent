# Test fixtures

- `claude/session.jsonl` and `claude/session/subagents/agent-abc123.jsonl`: one Claude Code session and one of its subagents, in the line format Claude Code 2.1.318 writes to `~/.claude/projects`. The content is made up for the tests and holds no real prompt, path, account or secret. The tests add secret-shaped strings at run time to check redaction, so none is stored here.
- `hooks/claude.json`: hook inputs (stdin JSON) for every mode of `plugin/hooks/hook.py`.
- `seeker/*.json`: Seeker's answers for the routes the package calls. They are **synthetic**: written by hand from section 4 of `docs/relay-plan.md`, not recorded from a running Seeker. Each file says so in its `synthetic` field. Replace them with recordings once Seeker serves the routes. The fixture tests must keep passing after that.
