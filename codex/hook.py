"""Every ctrlb-agent hook for Codex. Usage: hook.py <session-start|subagent-start|flush|upload|session-end|pre-tool|pre-bash>, with the
hook's JSON on stdin (Codex 0.166: session_id, transcript_path, cwd, hook_event_name; agent_id and
agent_transcript_path on subagent events and on tool calls inside a subagent; tool_name and tool_input on PreToolUse).
codex/install.py writes the [[hooks.*]] tables that call it. Output shapes are Claude Code's, which Codex accepts:
hookSpecificOutput.additionalContext, and on PreToolUse permissionDecision "deny" with a permissionDecisionReason.

A hook never fails or blocks a session: every error goes to ~/.ctrlb/hooks.log and the hook exits 0. pre-tool fails
open (no output) on any error and gives up after two seconds.

As described in docs/relay-hooks.md, session-start creates the run's scratch directory and, in a relay run (CTRLB_RUN), adds
the record block, and a session started from another agent's shell finds that agent (lib/spawn.py: a process record,
since Codex has no environment file). Codex gets no spawn registration, no artefact posting and
no autoSlots: its hooks have no sub-agent tool input to read and no hook after a tool call, so a slot
claimed before a patch could not be released after it."""
import json, os, signal, sys, time
sys.dont_write_bytecode = True
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'lib'))
import common, model_call_guard, relay_hooks, rows_codex, spawn  # noqa: E402
from hooks_shared import CHECK_S, RECALL, SUBAGENT, RELAY, relay_text, start_context, spawned_text, decide, _deny  # noqa: E402

SYNC_S, ASYNC_S = 5, 90
HANDOFF_S = 0.8
# Codex has no hook after a tool call, so the hook keeps no record of edits or tests: its open handoff says so.
CODEX_HANDOFF_TEXT = 'Session ended (Codex: the hook keeps no record of edits or tests; read the task\'s files and notes).'


def session_end(cfg, inp, deadline=None):
    """For Codex, in a relay run (CTRLB_RUN set), hand off every hold the agent still has, in one
    write (common.handoff_all). CtrlB unreachable or an error: nothing is written."""
    if common.relay_off(cfg) or not cfg.get('run'):
        return None
    sid = inp.get('session_id') or ''
    run, parent = session_run(cfg, sid, inp.get('transcript_path'))
    agent = rows_codex.agent_for(cfg, sid, parent)
    if not run or not agent:
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


def in_relay(cfg, run, agent, sid, parent, transcript):
    """Whether a Codex session or thread is in a relay run: CTRLB_RUN set (or joined), or its root session's rollout
    shows a ctrlb relay tool call that writes to a run. Codex has no hook after a tool call, so the rollout is read."""
    if relay_hooks.in_relay(cfg, run, agent, sid):
        return True
    try:
        rollout = rows_codex.root_rollout(sid, parent, transcript or '')[1]
        return bool(rollout and rows_codex.relay_call(rollout))
    except Exception as e:
        common.log(f"codex relay check {sid}: {type(e).__name__}: {str(e)[:200]}")
        return False


def session_run(cfg, sid, transcript):
    """(run id, parent thread id) of a Codex session. A thread whose rollout names a parent_thread_id is a subagent
    and joins its parent's run."""
    parent = str(rows_codex.read_meta(transcript).get('parent_thread_id') or '') if transcript else ''
    return rows_codex.session_run(cfg, sid, parent, transcript), parent


def pre_tool(cfg, inp):
    """For apply_patch: the paths come from the patch's *** Update File / Add File / Delete File lines."""
    if common.relay_off(cfg) or cfg['enforce'] == 'off':
        return None
    paths = rows_codex.input_paths(inp.get('tool_input'))
    if not paths:
        return None
    sid = inp.get('session_id') or ''
    run, parent = session_run(cfg, sid, inp.get('transcript_path'))
    sub = inp.get('agent_id') or ''
    agent = rows_codex.agent_for(cfg, sub, sid) if sub else rows_codex.agent_for(cfg, sid, parent)
    return decide(cfg, run, agent, paths, inp.get('cwd') or '')


def pre_bash(cfg, inp):
    """guardModelCalls: refuse a shell command that starts a model CLI without a time limit. Codex matches its shell
    tools as Bash, with the command in tool_input.command."""
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
        spawn.apply(cfg, sid)      # a session that joined its parent's run at start keeps that run and agent name
    if mode == 'session-start':
        common.start_local_server(cfg)
        _, parent = session_run(cfg, sid, inp.get('transcript_path'))
        started = {}
        if parent:      # a subagent thread: it takes the decision its root session made at its own start
            try:
                spawn.link(sid, rows_codex.root_session(sid, parent, inp.get('transcript_path') or ''))
            except Exception as e:
                common.log(f"codex spawn link {sid}: {type(e).__name__}: {str(e)[:200]}")
            spawn.apply(cfg, sid)
        else:           # Codex has no environment file: '' keeps a CLAUDE_ENV_FILE inherited from a parent's shell unused
            started = spawn.session_start(cfg, sid, inp.get('source') or '', env_file='',
                                          transcript=inp.get('transcript_path') or '')
        run, parent = session_run(cfg, sid, inp.get('transcript_path'))
        head = SUBAGENT.format(sid=parent) if parent else RECALL.format(sid=sid)
        if common.relay_off(cfg):
            print(json.dumps(start_context(cfg, 'SessionStart', head + spawned_text(started, '', '', ''))))
            return
        agent = rows_codex.agent_for(cfg, sid, parent)
        scratch = relay_hooks.make_scratch(cfg, run) or common.scratch_dir(cfg, run)
        if started.get('joined'):
            text, block = head + spawned_text(started, run, agent, scratch), ''
        else:
            in_run = in_relay(cfg, run, agent, sid, parent, inp.get('transcript_path'))
            text = head + spawned_text(started, '', '', '') + '\n\n' + relay_text(in_run, run, agent, scratch)
            block = ''
            if in_run:
                try:      # a relay run's session starts with the record
                    block = relay_hooks.session_start_block(cfg, run, agent, inp.get('source') or '')
                except Exception as e:
                    common.log(f"codex session-start record: {type(e).__name__}: {str(e)[:200]}")
        out = start_context(cfg, 'SessionStart', text + ('\n\n' + block if block else ''))
    elif mode == 'subagent-start':
        upload(cfg, [inp.get('transcript_path')], SYNC_S - 1, event, sid)
        if common.relay_off(cfg):
            print(json.dumps(start_context(cfg, 'SubagentStart', SUBAGENT.format(sid=sid))))
            return
        run, sparent = session_run(cfg, sid, inp.get('transcript_path'))
        sub = inp.get('agent_id') or ''
        agent = rows_codex.agent_for(cfg, sub, sid) if sub else rows_codex.agent_for(cfg, sid)
        out = start_context(cfg, 'SubagentStart', SUBAGENT.format(sid=sid) + '\n\n'
                      + relay_text(in_relay(cfg, run, agent, sid, sparent, inp.get('transcript_path')), run, agent,
                                   common.scratch_dir(cfg, run)))
    elif mode == 'flush':
        upload(cfg, [inp.get('agent_transcript_path'), inp.get('transcript_path')], SYNC_S, event, sid)
    elif mode == 'session-end':
        deadline = time.monotonic() + SYNC_S
        try:
            session_end(cfg, inp, deadline)
        except Exception as e:
            common.log(f"codex session-end: {type(e).__name__}: {str(e)[:200]}")
        upload(cfg, [inp.get('agent_transcript_path'), inp.get('transcript_path')], max(0, deadline - time.monotonic()), event, sid)
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
    elif mode == 'pre-bash':
        try:
            out = pre_bash(cfg, inp)
        except BaseException as e:      # fail open: the command runs
            common.log(f"codex pre-bash fail open: {type(e).__name__}: {str(e)[:200]}")
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
