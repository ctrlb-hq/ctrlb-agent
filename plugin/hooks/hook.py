"""Every ctrlb-agent hook for Claude Code. Usage: hook.py <session-start|subagent-start|flush|upload|session-end|pre-tool|
post-tool|post-tool-failure>, with the hook's JSON on stdin. A hook never fails or blocks a session: every error goes to ~/.ctrlb/hooks.log and the hook
exits 0. pre-tool fails open (no output) on any error and gives up after two seconds."""
import json, os, signal, sys, time
sys.dont_write_bytecode = True
_HERE = os.path.dirname(os.path.abspath(__file__))
for _lib in (os.path.join(_HERE, '..', 'lib'), os.path.join(_HERE, '..', '..', 'lib')):
    if os.path.isfile(os.path.join(_lib, 'common.py')):
        sys.path.insert(0, _lib)
        break
import common, rows_claude  # noqa: E402
from hooks_shared import (CHECK_S, RECALL, SUBAGENT, RELAY, context as _context, decide, record_tokens, _tool_answer,  # noqa: E402
                          bash_result, evidence_hint, is_test_command, record_test, record_bash, record_edit, record_note,
                          mark_relay, handoff_text, witness)
from paths import edit_paths as normalise_edit_paths  # noqa: E402

SYNC_S, ASYNC_S = 5, 90
# SessionEnd hooks share a 1.5 s budget (a plugin hook's own timeout does not raise it): the open handoff goes first and
# gives up after HANDOFF_S, so the transcript flush still gets the rest.
HANDOFF_S = 0.8
DONE_TOOLS = ('mcp__ctrlb__done', 'mcp__plugin_ctrlb-agent_ctrlb__done')


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


def session_end(cfg, inp):
    """Seeker decision 36: on SessionEnd (a normal end, SIGTERM or SIGINT; never SIGKILL) hand off every hold the agent
    still has, in one write: each task openly (to nobody) with a text built from this session's record, each path
    and slot released. Sent only in a relay run (CTRLB_RUN set, or a ctrlb relay tool was called in this session).
    Seeker unreachable or answering an error: nothing is written (a takeover covers the rest)."""
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
    try:
        ans = common.handoff_all(cfg, run, agent, text, inp.get('cwd') or '', timeout=HANDOFF_S)
    except Exception as e:
        common.log(f"session-end handoff {run}/{agent}: {type(e).__name__}: {str(e)[:200]}")
        return None
    common.log(f"session-end handoff {run}/{agent}: handed={ans.get('handed')} released={ans.get('released')}")
    return ans


def pre_tool(cfg, inp):
    """Decision 7 on an edit; decision 38 (the witness) on a done call. Returns the hook output, or None for no output
    (allowed, off, or nothing to check)."""
    if (inp.get('tool_name') or '') in DONE_TOOLS:
        run, agent, sid = _who(cfg, inp)
        return witness(common.SessionRecord(run, agent).get(sid), inp.get('tool_input'))
    if cfg['enforce'] == 'off':
        return None
    paths = rows_claude.edit_paths(inp.get('tool_name'), inp.get('tool_input'))
    if not paths:
        return None
    sid = inp.get('session_id') or ''
    return decide(cfg, common.run_id(cfg, sid), common.agent_name(cfg, sid, inp.get('agent_id') or ''), paths,
                  inp.get('cwd') or '')


def post_tool(cfg, inp):
    """Decision 21: after a ctrlb claim, handoff, release or done, keep the agent's hold tokens for the pre-tool check.
    Decision 30: after a test-looking Bash command, remember it, its exit code and last output line; after a done (or
    a finished release) refused for evidence, return the context that quotes that command so the agent can call done
    with it. Returns the hook output, or None (nothing printed)."""
    tool = inp.get('tool_name') or ''
    args = inp.get('tool_input') if isinstance(inp.get('tool_input'), dict) else {}
    own_run, own_agent, sid = _who(cfg, inp)
    if tool == 'Bash':
        # Decision 38: every command, with its exit code and last output line, for the done witness.
        failed = inp.get('hook_event_name') == 'PostToolUseFailure' or 'error' in inp and 'tool_response' not in inp
        code, line = bash_result(inp.get('error') if failed else inp.get('tool_response'))
        record_bash(own_run, own_agent, sid, args.get('command'), code, line)
        if is_test_command(args.get('command')):
            record_test(own_run, own_agent, args['command'], code, line)
        return None
    if tool in rows_claude.EDIT_TOOLS:
        record_edit(own_run, own_agent, sid, normalise_edit_paths(rows_claude.edit_paths(tool, args), inp.get('cwd') or ''))
        return None
    name = tool.rsplit('__', 1)[-1] if tool.startswith('mcp__') else ''
    if name == 'post':
        record_note(own_run, own_agent, sid, args.get('text'))
        mark_relay(own_run, own_agent, sid)
        return None
    if name not in ('claim', 'handoff', 'release', 'done'):
        return None
    mark_relay(own_run, own_agent, sid)
    run = args.get('run_id') or own_run
    agent = args.get('agent') or own_agent
    answer = _tool_answer(inp.get('tool_response'))
    record_tokens(run, agent, tool, answer)
    return evidence_hint(run, agent, answer, inp.get('transcript_path') or '', keys=[(own_run, own_agent)])


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
    elif mode == 'session-end':
        try:
            session_end(cfg, inp)
        except Exception as e:
            common.log(f"session-end: {type(e).__name__}: {str(e)[:200]}")
        upload(cfg, inp, SYNC_S)
    elif mode == 'upload':
        upload(cfg, inp, ASYNC_S)
    elif mode in ('post-tool', 'post-tool-failure'):
        try:
            out = post_tool(cfg, inp)
        except Exception as e:
            common.log(f"post-tool: {type(e).__name__}: {str(e)[:200]}")
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
