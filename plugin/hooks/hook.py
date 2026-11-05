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
from hooks_shared import CHECK_S, RECALL, SUBAGENT, RELAY, context as _context, decide  # noqa: E402

SYNC_S, ASYNC_S = 5, 90


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


def pre_tool(cfg, inp):
    """Decision 7. Returns the hook output, or None for no output (allowed, off, or nothing to check)."""
    if cfg['enforce'] == 'off':
        return None
    paths = rows_claude.edit_paths(inp.get('tool_name'), inp.get('tool_input'))
    if not paths:
        return None
    sid = inp.get('session_id') or ''
    return decide(cfg, common.run_id(cfg, sid), common.agent_name(cfg, sid, inp.get('agent_id') or ''), paths,
                  inp.get('cwd') or '')


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
