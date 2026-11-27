
import json, os, signal, sys, time
sys.dont_write_bytecode = True
_HERE = os.path.dirname(os.path.abspath(__file__))
for _lib in (os.path.join(_HERE, '..', 'lib'), os.path.join(_HERE, '..', '..', 'lib')):
    if os.path.isfile(os.path.join(_lib, 'common.py')):
        sys.path.insert(0, _lib)
        break
import model_call_guard
if __name__ == '__main__' and sys.argv[1:2] == ['pre-bash'] and model_call_guard.off_before_load():
    sys.exit(0)
import common, launch, relay_hooks, rows_claude, spawn
from hooks_shared import (CHECK_S, RECALL, SUBAGENT, RELAY, session_start_text, relay_text, launch_text, start_context, spawned_text, decide, starters, record_tokens, _tool_answer, _tool_error,
                          bash_result, evidence_hint, is_test_command, record_test, record_bash, record_edit, record_note,
                          mark_relay, handoff_text, witness, _deny)
from paths import edit_paths as normalise_edit_paths

SYNC_S, ASYNC_S = 5, 90


HANDOFF_S = 0.8
DONE_TOOLS = ('mcp__ctrlb__done', 'mcp__plugin_ctrlb-agent_ctrlb__done')

CTRLB_TOOL_PREFIXES = ('mcp__ctrlb__', 'mcp__plugin_ctrlb-agent_ctrlb__')
AGENT_TOOLS = ('Agent', 'Task')
AGENT_ALARM_S = relay_hooks.REGISTER_S + 0.5


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


def _who(cfg, inp):
    sid = inp.get('session_id') or ''
    return common.run_id(cfg, sid), common.agent_name(cfg, sid, inp.get('agent_id') or ''), sid


def session_end(cfg, inp, deadline=None):

    if common.relay_off(cfg):
        return None
    run, agent, sid = _who(cfg, inp)
    if not run or not agent:
        return None
    rec = common.SessionRecord(run, agent).get(sid)
    if not cfg.get('run') and not rec.get('relay'):
        return None
    try:
        last = common.LastTest(run, agent).get()
    except Exception:
        last = None
    text = handoff_text(rec, last)
    remaining = min(HANDOFF_S, deadline - time.monotonic()) if deadline is not None else HANDOFF_S
    if remaining <= 0:
        return None
    try:
        ans = common.handoff_all(cfg, run, agent, text, inp.get('cwd') or '', timeout=remaining)
    except Exception as e:
        common.log(f"session-end handoff {run}/{agent}: {type(e).__name__}: {str(e)[:200]}")
        return None
    common.log(f"session-end handoff {run}/{agent}: handed={ans.get('handed')} released={ans.get('released')}")
    return ans


def pre_tool(cfg, inp):

    if common.relay_off(cfg):
        return None
    if (inp.get('tool_name') or '') in DONE_TOOLS:
        run, agent, sid = _who(cfg, inp)
        return witness(common.SessionRecord(run, agent).get(sid), inp.get('tool_input'))
    if cfg['enforce'] == 'off':
        return None
    paths = rows_claude.edit_paths(inp.get('tool_name'), inp.get('tool_input'))
    if not paths:
        return None
    run, agent, sid = _who(cfg, inp)
    cwd = inp.get('cwd') or ''
    slots = relay_hooks.slot_paths(cfg, paths, cwd)
    if slots and relay_hooks.in_relay(cfg, run, agent, sid):


        try:
            denied, _ = relay_hooks.claim_slots(cfg, run, agent, slots, cwd)
        except BaseException as e:
            common.log(f"auto slot fail open {run}/{agent}: {type(e).__name__}: {str(e)[:200]}")
            return None
        if denied:
            return _deny(denied)
        paths = [p for p in normalise_edit_paths(paths, cwd) if p not in slots]
        if not paths:
            return None

    return decide(cfg, run, agent, paths, cwd,
                  lambda: starters(sid, run, common.agent_name(cfg, sid) if inp.get('agent_id') else ''))


def pre_agent(cfg, inp):

    if common.relay_off(cfg) or (inp.get('tool_name') or '') not in AGENT_TOOLS:
        return None
    run, agent, sid = _who(cfg, inp)
    if not relay_hooks.in_relay(cfg, run, agent, sid):
        return None
    if launch.current():
        return None
    return relay_hooks.register_spawn(cfg, run, agent, inp.get('tool_input'), inp.get('cwd') or '')


def pre_bash(cfg, inp):

    if not model_call_guard.enabled(cfg) or (inp.get('tool_name') or '') != 'Bash':
        return None
    reason = model_call_guard.check(cfg, (inp.get('tool_input') or {}).get('command'))
    if reason:
        common.log(f"pre-bash: model CLI call refused ({reason.split('`')[1]})")
        return _deny(reason)
    return None


def post_tool(cfg, inp):

    if common.relay_off(cfg):
        return None
    tool = inp.get('tool_name') or ''
    args = inp.get('tool_input') if isinstance(inp.get('tool_input'), dict) else {}
    own_run, own_agent, sid = _who(cfg, inp)
    if tool == 'Bash':

        failed = inp.get('hook_event_name') == 'PostToolUseFailure' or 'error' in inp and 'tool_response' not in inp
        code, line = bash_result(inp.get('error') if failed else inp.get('tool_response'))
        record_bash(own_run, own_agent, sid, args.get('command'), code, line)
        if is_test_command(args.get('command')):
            record_test(own_run, own_agent, args['command'], code, line)
        return None
    if tool in rows_claude.EDIT_TOOLS:
        cwd = inp.get('cwd') or ''
        paths = rows_claude.edit_paths(tool, args)
        failed = inp.get('hook_event_name') == 'PostToolUseFailure'
        if not failed:
            record_edit(own_run, own_agent, sid, normalise_edit_paths(paths, cwd))
        if not relay_hooks.in_relay(cfg, own_run, own_agent, sid):
            return None
        slots = relay_hooks.slot_paths(cfg, paths, cwd)
        if slots:
            try:
                relay_hooks.release_slots(cfg, own_run, own_agent, slots, cwd)
            except Exception as e:
                common.log(f"auto slot release {own_run}/{own_agent}: {type(e).__name__}: {str(e)[:200]} (the slot lapses)")
        if not failed:
            for p in paths:
                try:
                    relay_hooks.post_artifact(cfg, own_run, own_agent, p, inp)
                except Exception as e:
                    common.log(f"artifact: {type(e).__name__}: {str(e)[:200]}")
        return None
    name = _ctrlb_tool(tool)
    if name not in ('post', 'claim', 'handoff', 'release', 'done', 'task', 'brief', 'ask', 'answer', 'contract'):
        return None
    mark_relay(own_run, own_agent, sid)
    if name == 'post':
        record_note(own_run, own_agent, sid, args.get('text'))
        return None
    if name not in ('claim', 'handoff', 'release', 'done'):
        return None
    run = args.get('run_id') or own_run
    agent = args.get('agent') or own_agent
    answer = _tool_answer(inp.get('tool_response'))
    record_tokens(run, agent, tool, answer)
    if (name == 'claim' and inp.get('hook_event_name') != 'PostToolUseFailure' and not inp.get('agent_id')
            and not _tool_error(inp.get('tool_response'))):

        info = launch.current()
        if info:
            try:
                launch.record_claim(info, sid, args, answer, own_run, own_agent)
            except Exception as e:
                common.log(f"launch result: {type(e).__name__}: {str(e)[:200]}")
    return evidence_hint(run, agent, answer, inp.get('transcript_path') or '', keys=[(own_run, own_agent)])


def _ctrlb_tool(tool):

    for prefix in CTRLB_TOOL_PREFIXES:
        if tool.startswith(prefix) and '__' not in tool[len(prefix):]:
            return tool[len(prefix):]
    return ''


def _alarm(*_):
    raise TimeoutError('hook over its budget')


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ''
    if mode in ('pre-tool', 'pre-agent', 'pre-bash'):
        signal.signal(signal.SIGALRM, _alarm)
        signal.setitimer(signal.ITIMER_REAL, AGENT_ALARM_S if mode == 'pre-agent' else CHECK_S)
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
    if mode != 'session-start':
        spawn.apply(cfg, sid)
    if mode == 'session-start':
        common.start_local_server(cfg)

        started = spawn.session_start(cfg, sid, inp.get('source') or '', transcript=inp.get('transcript_path') or '')
        if common.relay_off(cfg):
            print(json.dumps(start_context(cfg, 'SessionStart', RECALL.format(sid=sid) + spawned_text(started, '', '', ''))))
            return
        run, agent = common.run_id(cfg, sid), common.agent_name(cfg, sid)
        scratch = relay_hooks.make_scratch(cfg, run) or common.scratch_dir(cfg, run)
        info = launch.current()
        rule = launch_text(info, launch.role(info, sid), run, agent, scratch) if info else ''
        text = session_start_text(cfg, RECALL.format(sid=sid), started, rule, run, agent, scratch,
                                  inp.get('source') or '', lambda: relay_hooks.in_relay(cfg, run, agent, sid),
                                  'session-start record')
        out = start_context(cfg, 'SessionStart', text)
    elif mode == 'subagent-start':
        upload(cfg, dict(inp, agent_transcript_path=None), SYNC_S - 1)
        if common.relay_off(cfg):
            print(json.dumps(start_context(cfg, 'SubagentStart', SUBAGENT.format(sid=sid))))
            return
        run = common.run_id(cfg, sid)
        in_run = relay_hooks.in_relay(cfg, run, common.agent_name(cfg, sid), sid)
        sub = common.agent_name(cfg, sid, inp.get('agent_id') or '')
        info = launch.current()
        relay = (launch_text(info, launch.DESCENDANT, run, sub, common.scratch_dir(cfg, run)) if info else
                 relay_text(in_run, run, sub, common.scratch_dir(cfg, run)))
        out = start_context(cfg, 'SubagentStart', SUBAGENT.format(sid=sid) + '\n\n' + relay)
    elif mode == 'flush':
        upload(cfg, inp, SYNC_S)
    elif mode == 'session-end':
        deadline = time.monotonic() + 1.4
        try:
            session_end(cfg, inp, deadline)
        except Exception as e:
            common.log(f"session-end: {type(e).__name__}: {str(e)[:200]}")
        upload(cfg, inp, max(0, deadline - time.monotonic()))
    elif mode == 'upload':
        upload(cfg, inp, ASYNC_S)
    elif mode in ('post-tool', 'post-tool-failure'):
        try:
            out = post_tool(cfg, inp)
        except Exception as e:
            common.log(f"post-tool: {type(e).__name__}: {str(e)[:200]}")
    elif mode in ('pre-tool', 'pre-bash'):
        try:
            out = pre_tool(cfg, inp) if mode == 'pre-tool' else pre_bash(cfg, inp)
        except BaseException as e:
            common.log(f"{mode} fail open: {type(e).__name__}: {str(e)[:200]}")
            out = None
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
    elif mode == 'pre-agent':
        try:
            pre_agent(cfg, inp)
        except BaseException as e:
            common.log(f"pre-agent: {type(e).__name__}: {str(e)[:200]}")
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
