"""Every ctrlb-agent hook for Claude Code. Usage: hook.py <session-start|subagent-start|flush|upload|pre-tool>, with
the hook's JSON on stdin. A hook never fails or blocks a session: every error goes to ~/.ctrlb/hooks.log and the hook
exits 0. pre-tool fails open (no output) on any error and gives up after two seconds."""
import json, os, signal, sys, time
sys.dont_write_bytecode = True
_HERE = os.path.dirname(os.path.abspath(__file__))
for _lib in (os.path.join(_HERE, '..', 'lib'), os.path.join(_HERE, '..', '..', 'lib')):
    if os.path.isfile(os.path.join(_lib, 'common.py')):
        sys.path.insert(0, _lib)
        break
import common, rows_claude  # noqa: E402

SYNC_S, ASYNC_S = 5, 90
CHECK_S = 2.0

RECALL = ("Your earlier turns in this session are not all in your context: compaction leaves details out, including "
          "instructions the user gave, files you read, commands that failed and decisions you made. The full record of "
          "this session (session_id {sid}) and of this user's earlier sessions is searchable with the ctrlb tools "
          "(search_sessions, find_command, next_actions, fetch_records). Before relying on anything from earlier work, "
          "search it. Do not say something was not said or not done until you have searched. When you start a "
          "subagent, put this session_id in its prompt and tell it to search the record before exploring.")

SUBAGENT = ("You are a subagent of session {sid}. That session's full record, including what it already read, ran and "
            "decided, is searchable with the ctrlb tools (search_sessions with session_id {sid}, fetch_records, "
            "next_actions). Search it before exploring files or rerunning commands.")

RELAY = ("CtrlB Relay: your run id is {run} and your agent id is {agent}. Other agents in this run may be working in "
         "the same files. Pass run_id=\"{run}\" and agent=\"{agent}\" to the ctrlb relay tools (peers, post, claim, "
         "release). Before starting a task, call claim with the file path the task names (for example "
         "claim(resource=\"src/app.py\")), not a description of the task; work on it only if held is true, otherwise "
         "pick other work. When the work on it is finished, call release with finished=true.")


def upload(cfg, inp, budget):
    deadline = time.time() + budget
    paths = [inp['agent_transcript_path']] if inp.get('agent_transcript_path') else rows_claude.session_files(inp.get('transcript_path'))
    total = dict(rows=0, lines=0, redacted=0)
    for p in paths:
        if time.time() > deadline:
            break
        try:
            c = rows_claude.upload_file(cfg, p, deadline)
            for k in total:
                total[k] += c[k]
        except Exception as e:
            common.log(f"upload {os.path.basename(p)}: {type(e).__name__}: {str(e)[:200]}")
    if total['rows'] or total['redacted']:
        common.log(f"{inp.get('hook_event_name', '?')} {inp.get('session_id', '')}: {total}")


def _context(event, text):
    return {'hookSpecificOutput': {'hookEventName': event, 'additionalContext': text}}


def _resource(c):
    key = c.get('key') or ''
    return key[len('path:'):] if key.startswith('path:') else (c.get('path') or key)


def _lease(c):
    try:
        mins = (float(c['lease_until']) / 1e6 - time.time()) / 60
        return f' (lease ends in about {max(1, round(mins))} min)' if mins > 0 else ''
    except Exception:
        return ''


def pre_tool(cfg, inp):
    """Decision 7. Returns the hook output, or None for no output (allowed, off, or nothing to check)."""
    if cfg['enforce'] == 'off':
        return None
    paths = rows_claude.edit_paths(inp.get('tool_name'), inp.get('tool_input'))
    if not paths:
        return None
    sid = inp.get('session_id') or ''
    run = common.run_id(cfg, sid)
    agent = common.agent_name(cfg, sid, inp.get('agent_id') or '')
    if not run or not agent:
        return None
    res = common.check(cfg, run, agent, paths, inp.get('cwd') or '', timeout=CHECK_S)
    mode = res.get('mode') or 'advisory'
    conflicts = [c for c in res.get('conflicts') or [] if isinstance(c, dict) and c.get('held_by') and c['held_by'] != agent]
    if mode == 'off' or not conflicts:
        return None
    held = '; '.join(f"{_resource(c)} is claimed by {c['held_by']}{_lease(c)}" for c in conflicts)
    first = _resource(conflicts[0])
    if mode == 'enforce' and res.get('allowed') is False:
        reason = (f"CtrlB Relay (run {run}): {held}. Do not edit it. Call claim(resource=\"{first}\", run_id=\"{run}\", "
                  f"agent=\"{agent}\") and edit only if held is true; otherwise pick other work or call peers to see "
                  f"what {conflicts[0]['held_by']} is doing.")
        return {'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'permissionDecision': 'deny',
                                       'permissionDecisionReason': reason}}
    text = (f"CtrlB Relay (run {run}): {held}. Another agent is working on this file. Before editing it, call "
            f"claim(resource=\"{first}\", run_id=\"{run}\", agent=\"{agent}\"); if held is false, leave the file to "
            f"its holder and pick other work.")
    return _context('PreToolUse', text)


def _alarm(*_):
    raise TimeoutError(f'pre-tool over {CHECK_S}s')


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ''
    if mode == 'pre-tool':
        signal.signal(signal.SIGALRM, _alarm)
        signal.setitimer(signal.ITIMER_REAL, CHECK_S)
    try:
        inp = json.load(sys.stdin)
    except Exception:
        inp = {}
    if not isinstance(inp, dict):
        inp = {}
    cfg = common.config()
    if cfg['off']:
        return
    sid = inp.get('session_id') or ''
    out = None
    if mode == 'session-start':
        relay = RELAY.format(run=common.run_id(cfg, sid), agent=common.agent_name(cfg, sid))
        out = _context('SessionStart', RECALL.format(sid=sid) + '\n\n' + relay)
    elif mode == 'subagent-start':
        upload(cfg, dict(inp, agent_transcript_path=None), SYNC_S - 1)
        relay = RELAY.format(run=common.run_id(cfg, sid), agent=common.agent_name(cfg, sid, inp.get('agent_id') or ''))
        out = _context('SubagentStart', SUBAGENT.format(sid=sid) + '\n\n' + relay)
    elif mode == 'flush':
        upload(cfg, inp, SYNC_S)
    elif mode == 'upload':
        upload(cfg, inp, ASYNC_S)
    elif mode == 'pre-tool':
        try:
            out = pre_tool(cfg, inp)
        except BaseException as e:      # fail open: the edit goes ahead
            common.log(f"pre-tool fail open: {type(e).__name__}: {str(e)[:200]}")
            out = None
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
    if out is not None:
        print(json.dumps(out))


if __name__ == '__main__':
    try:
        main()
    except BaseException as e:
        if not isinstance(e, SystemExit):
            common.log(f"hook {sys.argv[1:]}: {type(e).__name__}: {str(e)[:200]}")
    sys.exit(0)
