"""Every ctrlb-agent hook for Codex. Usage: hook.py <session-start|subagent-start|flush|upload|session-end|pre-tool>, with the
hook's JSON on stdin (Codex 0.166: session_id, transcript_path, cwd, hook_event_name; agent_id and
agent_transcript_path on subagent events and on tool calls inside a subagent; tool_name and tool_input on PreToolUse).
codex/install.py writes the [[hooks.*]] tables that call it. Output shapes are Claude Code's, which Codex accepts:
hookSpecificOutput.additionalContext, and on PreToolUse permissionDecision "deny" with a permissionDecisionReason.

A hook never fails or blocks a session: every error goes to ~/.ctrlb/hooks.log and the hook exits 0. pre-tool fails
open (no output) on any error and gives up after two seconds.

Round 8 (docs/relay-hooks.md): session-start creates the run's scratch directory and, in a relay run (CTRLB_RUN), adds
the record block (decision 45). Codex gets no spawn registration (decision 43), no artefact posting (decision 44) and
no autoSlots (decision 47): its hooks have no sub-agent tool input to read and no hook after a tool call, so a slot
claimed before a patch could not be released after it."""
import json, os, signal, sys, time
sys.dont_write_bytecode = True
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'lib'))
import common, relay_hooks, rows_codex  # noqa: E402
from hooks_shared import (CHECK_S, RECALL, SUBAGENT, RELAY, context, decide, index_block, parent_line,  # noqa: E402
                          end_flush, END_FLUSH_S)

SYNC_S, ASYNC_S = 5, 90
HANDOFF_S = 0.8
# Codex has no hook after a tool call, so the hook keeps no record of edits or tests: its open handoff says so.
CODEX_HANDOFF_TEXT = 'Session ended (Codex: the hook keeps no record of edits or tests; read the task\'s files and notes).'


def session_end(cfg, inp):
    """Seeker decision 36 for Codex: in a relay run (CTRLB_RUN set), hand off every hold the agent still has, in one
    write (common.handoff_all). Seeker unreachable or an error: nothing is written."""
    if not cfg.get('run'):
        return None
    sid = inp.get('session_id') or ''
    run, parent = session_run(cfg, sid, inp.get('transcript_path'))
    agent = rows_codex.agent_for(cfg, sid, parent)
    if not run or not agent:
        return None
    try:
        ans = common.handoff_all(cfg, run, agent, CODEX_HANDOFF_TEXT, inp.get('cwd') or '', timeout=HANDOFF_S)
    except Exception as e:
        common.log(f"codex session-end handoff {run}/{agent}: {type(e).__name__}: {str(e)[:200]}")
        return None
    common.log(f"codex session-end handoff {run}/{agent}: handed={ans.get('handed')} released={ans.get('released')}")
    return ans


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


def session_run(cfg, sid, transcript):
    """(run id, parent thread id) of a Codex session. A thread whose rollout names a parent_thread_id is a subagent
    and joins its parent's run."""
    parent = str(rows_codex.read_meta(transcript).get('parent_thread_id') or '') if transcript else ''
    return common.run_id(cfg, sid, parent), parent


def pre_tool(cfg, inp):
    """Decision 7 for apply_patch: the paths come from the patch's *** Update File / Add File / Delete File lines."""
    if cfg['enforce'] == 'off':
        return None
    paths = rows_codex.input_paths(inp.get('tool_input'))
    if not paths:
        return None
    sid = inp.get('session_id') or ''
    run, parent = session_run(cfg, sid, inp.get('transcript_path'))
    sub = inp.get('agent_id') or ''
    agent = rows_codex.agent_for(cfg, sub, sid) if sub else rows_codex.agent_for(cfg, sid, parent)
    return decide(cfg, run, agent, paths, inp.get('cwd') or '')


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
    event = inp.get('hook_event_name') or mode
    out = None
    if mode == 'session-start':
        run, parent = session_run(cfg, sid, inp.get('transcript_path'))
        head = SUBAGENT.format(sid=parent) if parent else RECALL.format(sid=sid)
        cwd = inp.get('cwd') or ''
        # item 2: a thread starts with the session index under the recall rule; a subagent thread with its parent's line
        index = parent_line(cfg, cwd, parent) if parent else index_block(cfg, cwd, sid, inp.get('source') or '')
        head += '\n\n' + index if index else ''
        agent = rows_codex.agent_for(cfg, sid, parent)
        scratch = relay_hooks.make_scratch(cfg, run) or common.scratch_dir(cfg, run)
        text = head + '\n\n' + RELAY.format(run=run, agent=agent, scratch=scratch)
        try:      # decision 45: a relay run's session starts with the record
            block = relay_hooks.session_start_block(cfg, run, agent, inp.get('source') or '')
        except Exception as e:
            common.log(f"codex session-start record: {type(e).__name__}: {str(e)[:200]}")
            block = ''
        out = context('SessionStart', text + ('\n\n' + block if block else ''))
    elif mode == 'subagent-start':
        upload(cfg, [inp.get('transcript_path')], SYNC_S - 1, event, sid)
        run, _ = session_run(cfg, sid, inp.get('transcript_path'))
        sub = inp.get('agent_id') or ''
        agent = rows_codex.agent_for(cfg, sub, sid) if sub else rows_codex.agent_for(cfg, sid)
        line = parent_line(cfg, inp.get('cwd') or '', sid)
        out = context('SubagentStart', SUBAGENT.format(sid=sid) + ('\n\n' + line if line else '') + '\n\n'
                      + RELAY.format(run=run, agent=agent, scratch=common.scratch_dir(cfg, run)))
    elif mode == 'flush':
        upload(cfg, [inp.get('agent_transcript_path'), inp.get('transcript_path')], SYNC_S, event, sid)
    elif mode == 'session-end':
        try:
            session_end(cfg, inp)
        except Exception as e:
            common.log(f"codex session-end: {type(e).__name__}: {str(e)[:200]}")
        upload(cfg, [inp.get('agent_transcript_path'), inp.get('transcript_path')], SYNC_S - END_FLUSH_S, event, sid)
        # item 3: a main thread's next session starts from a current note (subagent threads keep none)
        if not session_run(cfg, sid, inp.get('transcript_path'))[1]:
            end_flush(cfg, sid)
    elif mode == 'upload':
        upload(cfg, [inp.get('agent_transcript_path') or inp.get('transcript_path')], ASYNC_S, event, sid)
    elif mode == 'pre-tool':
        try:
            out = pre_tool(cfg, inp)
        except BaseException as e:      # fail open: the patch goes ahead
            common.log(f"codex pre-tool fail open: {type(e).__name__}: {str(e)[:200]}")
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
