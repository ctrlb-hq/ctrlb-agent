
import json, os, signal, sys, time
sys.dont_write_bytecode = True
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'lib'))
import common, launch, model_call_guard, relay_hooks, rows_codex, spawn
from hooks_shared import CHECK_S, RECALL, SUBAGENT, RELAY, session_start_text, relay_text, launch_text, start_context, spawned_text, decide, starters, _deny

SYNC_S, ASYNC_S = 5, 90
HANDOFF_S = 0.8

CODEX_HANDOFF_TEXT = 'Session ended (Codex: the hook keeps no record of edits or tests; read the task\'s files and notes).'


def session_end(cfg, inp, deadline=None):

    if common.relay_off(cfg):
        return None
    sid = inp.get('session_id') or ''
    run, parent = session_run(cfg, sid, inp.get('transcript_path'))
    agent = rows_codex.agent_for(cfg, sid, parent)
    if not run or not agent or not in_relay(cfg, run, agent, sid, inp.get('transcript_path')):
        return None
    remaining = min(HANDOFF_S, deadline - time.monotonic()) if deadline is not None else HANDOFF_S
    if remaining <= 0:
        return None
    try:
        ans = common.handoff_all(cfg, run, agent, CODEX_HANDOFF_TEXT, inp.get('cwd') or '', timeout=remaining)
    except Exception as e:
        common.log(f"codex session-end handoff {run}/{agent}: {type(e).__name__}: {str(e)[:200]}")
        return None
    common.log(f"codex session-end handoff {run}/{agent}: handed={ans.get('handed')} released={ans.get('released')}")
    return ans


def launch_result(cfg, inp):

    info = launch.current()
    sid = inp.get('session_id') or ''
    transcript = inp.get('transcript_path') or ''
    if not info or not sid or not transcript or inp.get('agent_id') or common.relay_off(cfg):
        return None
    if rows_codex.read_meta(transcript).get('parent_thread_id') or not launch.is_worker(info, sid):
        return None
    run, agent = rows_codex.session_run(cfg, sid), rows_codex.agent_for(cfg, sid)
    found = None
    for args, answer in rows_codex.claim_calls(transcript, sid):
        claim = launch.claim_outcome(info, args, answer, run, agent)
        if claim == 'held' or claim and not found:
            found = claim
    return launch.write_result(info, found) if found else None


def upload(cfg, paths, budget, event, sid):
    deadline = time.time() + budget
    total = dict(rows=0, lines=0, redacted=0)
    for p in paths:
        if not p or time.time() > deadline:
            continue
        try:
            c = rows_codex.upload_file(cfg, p, deadline)
            for k in total:
                total[k] += c[k]
        except Exception as e:
            common.log(f"codex upload {os.path.basename(p)}: {type(e).__name__}: {str(e)[:200]}")
    if total['rows'] or total['redacted']:
        common.log(f"codex {event} {sid}: {total}")


def in_relay(cfg, run, agent, sid, transcript):

    if relay_hooks.in_relay(cfg, run, agent, sid):
        return True
    try:
        parent = str(rows_codex.read_meta(transcript).get('parent_thread_id') or '') if transcript else ''
        rollout = rows_codex.root_rollout(sid, parent, transcript or '')[1]
        if rollout and rows_codex.relay_call(rollout):
            return True
        return bool(transcript and transcript != rollout and rows_codex.relay_call(transcript))
    except Exception as e:
        common.log(f"codex relay check {sid}: {type(e).__name__}: {str(e)[:200]}")
        return False


def session_run(cfg, sid, transcript):

    parent = str(rows_codex.read_meta(transcript).get('parent_thread_id') or '') if transcript else ''
    return rows_codex.session_run(cfg, sid, parent, transcript), parent


def pre_tool(cfg, inp):

    if common.relay_off(cfg) or cfg['enforce'] == 'off':
        return None
    paths = rows_codex.input_paths(inp.get('tool_input'))
    if not paths:
        return None
    sid = inp.get('session_id') or ''
    run, parent = session_run(cfg, sid, inp.get('transcript_path'))
    sub = inp.get('agent_id') or ''
    agent = rows_codex.agent_for(cfg, sub, sid) if sub else rows_codex.agent_for(cfg, sid, parent)

    return decide(cfg, run, agent, paths, inp.get('cwd') or '',
                  lambda: _starters(cfg, sid, run, parent, sub, inp.get('transcript_path') or ''))


def _starters(cfg, sid, run, parent, sub, transcript):

    starter = ''
    if sub:
        starter = rows_codex.agent_for(cfg, sid, parent)
    elif parent and rows_codex.root_session(sid, parent, transcript) == parent:
        starter = rows_codex.agent_for(cfg, parent, '')
    return starters(sid, run, starter)


def pre_bash(cfg, inp):

    if not model_call_guard.enabled(cfg):
        return None
    ti = inp.get('tool_input')
    command = ti.get('command') if isinstance(ti, dict) else ti
    if not command:
        return None
    reason = model_call_guard.check(cfg, command)
    if reason:
        common.log(f"codex pre-bash: model CLI call refused ({reason.split('`')[1]})")
        return _deny(reason)
    return None


def _alarm(*_):
    raise TimeoutError(f'pre-tool over {CHECK_S}s')


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ''
    if mode in ('pre-tool', 'pre-bash'):
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
    event = inp.get('hook_event_name') or mode
    out = None
    if mode != 'session-start':
        spawn.apply(cfg, sid)
    if mode == 'session-start':
        common.start_local_server(cfg)
        _, parent = session_run(cfg, sid, inp.get('transcript_path'))
        started = {}
        if parent:
            try:
                spawn.link(sid, rows_codex.root_session(sid, parent, inp.get('transcript_path') or ''))
            except Exception as e:
                common.log(f"codex spawn link {sid}: {type(e).__name__}: {str(e)[:200]}")
            spawn.apply(cfg, sid)
        else:
            started = spawn.session_start(cfg, sid, inp.get('source') or '', env_file='',
                                          transcript=inp.get('transcript_path') or '')
        run, parent = session_run(cfg, sid, inp.get('transcript_path'))
        head = SUBAGENT.format(sid=parent) if parent else RECALL.format(sid=sid)
        if common.relay_off(cfg):
            print(json.dumps(start_context(cfg, 'SessionStart', head + spawned_text(started, '', '', ''))))
            return
        agent = rows_codex.agent_for(cfg, sid, parent)
        scratch = relay_hooks.make_scratch(cfg, run) or common.scratch_dir(cfg, run)

        info = launch.current()
        rule = launch_text(info, launch.role(info, sid, subagent=bool(parent)), run, agent, scratch) if info else ''
        text = session_start_text(cfg, head, started, rule, run, agent, scratch, inp.get('source') or '',
                                  lambda: in_relay(cfg, run, agent, sid, inp.get('transcript_path')),
                                  'codex session-start record')
        out = start_context(cfg, 'SessionStart', text)
    elif mode == 'subagent-start':
        upload(cfg, [inp.get('transcript_path')], SYNC_S - 1, event, sid)
        if common.relay_off(cfg):
            print(json.dumps(start_context(cfg, 'SubagentStart', SUBAGENT.format(sid=sid))))
            return
        run, _ = session_run(cfg, sid, inp.get('transcript_path'))
        sub = inp.get('agent_id') or ''
        agent = rows_codex.agent_for(cfg, sub, sid) if sub else rows_codex.agent_for(cfg, sid)
        in_run = in_relay(cfg, run, agent, sid, inp.get('transcript_path'))
        info = launch.current()
        out = start_context(cfg, 'SubagentStart', SUBAGENT.format(sid=sid) + '\n\n'
                      + (launch_text(info, launch.DESCENDANT, run, agent, common.scratch_dir(cfg, run)) if info else
                         relay_text(in_run, run, agent, common.scratch_dir(cfg, run))))
    elif mode == 'flush':
        try:
            launch_result(cfg, inp)
        except Exception as e:
            common.log(f"codex launch result: {type(e).__name__}: {str(e)[:200]}")
        upload(cfg, [inp.get('agent_transcript_path'), inp.get('transcript_path')], SYNC_S, event, sid)
    elif mode == 'session-end':
        deadline = time.monotonic() + SYNC_S
        try:
            launch_result(cfg, inp)
        except Exception as e:
            common.log(f"codex launch result: {type(e).__name__}: {str(e)[:200]}")
        try:
            session_end(cfg, inp, deadline)
        except Exception as e:
            common.log(f"codex session-end: {type(e).__name__}: {str(e)[:200]}")
        upload(cfg, [inp.get('agent_transcript_path'), inp.get('transcript_path')], max(0, deadline - time.monotonic()), event, sid)
    elif mode == 'upload':
        try:
            launch_result(cfg, inp)
        except Exception as e:
            common.log(f"codex launch result: {type(e).__name__}: {str(e)[:200]}")
        upload(cfg, [inp.get('agent_transcript_path') or inp.get('transcript_path')], ASYNC_S, event, sid)
    elif mode in ('pre-tool', 'pre-bash'):
        try:
            out = pre_tool(cfg, inp) if mode == 'pre-tool' else pre_bash(cfg, inp)
        except BaseException as e:
            common.log(f"codex {mode} fail open: {type(e).__name__}: {str(e)[:200]}")
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
            common.log(f"codex hook {sys.argv[1:]}: {type(e).__name__}: {str(e)[:200]}")
    sys.exit(0)
