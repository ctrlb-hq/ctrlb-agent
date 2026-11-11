"""Search before act (an experiment, search-before-act-test.md): after a session resumes or compacts, the agent reads
its record before it edits, runs a command or ends its turn.

Setting `searchFirst` (CTRLB_SEARCH_FIRST=on, or "searchFirst": "on" in ~/.ctrlb/config.json), default off. With it on,
in a main session (never a subagent):

- arm: SessionStart with source resume or compact, and PreCompact (Claude Code). The session's state file
  (common.SessionRecord) gets search_first {armed: true, since, stop_blocked: 0}.
- clear: PostToolUse of a recall tool, any MCP server's search_sessions, fetch_records, find_command, next_actions or
  session_index (mcp__<server>__<tool>). armed: false.
- while armed: PreToolUse of an edit or a command (Claude Code Edit, Write, MultiEdit, NotebookEdit, Bash; Codex
  apply_patch, Bash) is denied with REASON; every other tool goes through.
- while armed: the Stop hook blocks the turn end once ({"decision": "block", "reason": REASON}) and sets
  stop_blocked: 1; a later Stop goes through even while armed, so a session can never loop on it.

No Seeker call: the state is the per-session file. CTRLB_SEARCH_FIRST_ARM_ON_START=1 arms on every SessionStart, for a
harness that starts a fresh session where the product would see a resume (it is the one difference from the product
path; not for use in a real session)."""
import os
import re
import time
import common

REASON = ("CtrlB Relay: search this session's record before you act. Nothing has been read from it since the "
          "session resumed.")
RECALL_TOOLS = ('search_sessions', 'fetch_records', 'find_command', 'next_actions', 'session_index')
RECALL_RE = re.compile(r'^mcp__.+__(' + '|'.join(RECALL_TOOLS) + r')$')
GATED = {'claude': ('Edit', 'Write', 'MultiEdit', 'NotebookEdit', 'Bash'), 'codex': ('apply_patch', 'Bash')}
ARM_SOURCES = ('resume', 'compact')


def enabled(cfg):
    return str(cfg.get('search_first') or '').lower() == 'on'


def _record(cfg, sid):
    return common.SessionRecord(common.run_id(cfg, sid), common.agent_name(cfg, sid))


def state(cfg, sid):
    return dict(_record(cfg, sid).get(sid).get('search_first') or {})


def _set(cfg, sid, fn):
    def go(d):
        sf = dict(d.get('search_first') or {})
        fn(sf)
        d['search_first'] = sf
    _record(cfg, sid).update(sid, go)


def arm(cfg, sid):
    _set(cfg, sid, lambda sf: sf.update(armed=True, since=time.time(), stop_blocked=0))


def is_recall(tool):
    return bool(RECALL_RE.match(str(tool or '')))


def hook(cfg, inp, harness='claude', subagent=False):
    """The gate for one hook event. Returns the hook's output (a dict) or None. Off, no session id or a subagent:
    None and nothing written."""
    sid = inp.get('session_id') or ''
    if not enabled(cfg) or not sid or subagent or inp.get('agent_id'):
        return None
    event = inp.get('hook_event_name') or ''
    if event == 'SessionStart':
        if inp.get('source') in ARM_SOURCES or os.environ.get('CTRLB_SEARCH_FIRST_ARM_ON_START') == '1':
            arm(cfg, sid)
            common.log(f"search first {sid}: armed ({inp.get('source') or 'start'})")
        return None
    if event == 'PreCompact':
        arm(cfg, sid)
        common.log(f'search first {sid}: armed (PreCompact)')
        return None
    if event == 'PostToolUse':
        if is_recall(inp.get('tool_name')) and state(cfg, sid).get('armed'):
            _set(cfg, sid, lambda sf: sf.update(armed=False, cleared_by=inp.get('tool_name'), cleared=time.time()))
            common.log(f"search first {sid}: cleared by {inp.get('tool_name')}")
        return None
    if event == 'PreToolUse':
        if inp.get('tool_name') in GATED[harness] and state(cfg, sid).get('armed'):
            _set(cfg, sid, lambda sf: sf.update(denied=int(sf.get('denied') or 0) + 1))
            return {'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'permissionDecision': 'deny',
                                           'permissionDecisionReason': REASON}}
        return None
    if event == 'Stop':
        sf = state(cfg, sid)
        if sf.get('armed') and not sf.get('stop_blocked'):
            _set(cfg, sid, lambda s: s.update(stop_blocked=1))
            common.log(f'search first {sid}: blocked the turn end once')
            return {'decision': 'block', 'reason': REASON}
        return None
    return None
