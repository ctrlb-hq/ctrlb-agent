"""Every ctrlb-agent hook for Codex. Usage: hook.py <session-start|subagent-start|flush|upload|session-end|pre-tool|
post-tool>, with the hook's JSON on stdin (Codex 0.166: session_id, transcript_path, cwd, hook_event_name; agent_id and
agent_transcript_path on subagent events and on tool calls inside a subagent; tool_name and tool_input on PreToolUse;
tool_name, tool_input and tool_response on PostToolUse, whose shell calls are named Bash).
codex/install.py writes the [[hooks.*]] tables that call it. Output shapes are Claude Code's, which Codex accepts:
hookSpecificOutput.additionalContext, and on PreToolUse permissionDecision "deny" with a permissionDecisionReason.

A hook never fails or blocks a session: every error goes to ~/.ctrlb/hooks.log and the hook exits 0. pre-tool fails
open (no output) on any error and gives up after two seconds.

The hooks protocol (docs/relay-hooks.md, "The agent-side state machine"), in a run (CTRLB_RUN): session-start claims
the next task and names it; pre-tool checks a patch's files, claims a file nobody holds (or its task), renews near the
lease end and names the holder of a file another agent holds; post-tool on a Bash call that runs a held task's test
and exits 0 sends done with that run as witnessed evidence and claims the next task. A Codex sub-agent's machine is
keyed by its own thread id; SubagentStart only joins (the sub-agent's own SessionStart claims). If no PostToolUse
reached the hook in a session (hooks not trusted for it, or a Codex without it), the Stop hook reads the rollout's
shell calls for a passing test instead. Codex gets no spawn registration (decision 43), no artefact posting (decision
44) and no autoSlots (decision 47)."""
import json, os, signal, sys, time
sys.dont_write_bytecode = True
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'lib'))
import common, redact, relay_hooks, rows_codex  # noqa: E402
from hooks_shared import PRE_TOOL_ALARM_S, RECALL, SUBAGENT, Machine, relay_text, context, decide  # noqa: E402
from paths import edit_paths as normalise_edit_paths  # noqa: E402

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
    m = machine(cfg, inp)
    if m:
        return m.pre_edit(normalise_edit_paths(paths, inp.get('cwd') or ''))
    return decide(cfg, run, agent, paths, inp.get('cwd') or '')


SHELL_NAMES = ('Bash',) + rows_codex.SHELL_TOOLS


def _ids(cfg, inp):
    """(run, agent, machine key) of the thread a hook fires in: a tool call inside a sub-agent carries the parent's
    session_id and the sub-agent's agent_id (its thread id)."""
    sid = inp.get('session_id') or ''
    run, parent = session_run(cfg, sid, inp.get('transcript_path'))
    sub = inp.get('agent_id') or ''
    agent = rows_codex.agent_for(cfg, sub, sid) if sub else rows_codex.agent_for(cfg, sid, parent)
    return run, agent, sub or sid


def machine(cfg, inp):
    """The thread's protocol machine in a run (CTRLB_RUN or the config's run), else None."""
    if not cfg.get('run'):
        return None
    run, agent, key = _ids(cfg, inp)
    if not run or not agent or not key:
        return None
    return Machine(cfg, run, agent, key, inp.get('cwd') or '')


def post_tool(cfg, inp):
    """PostToolUse on a shell call: the machine's done on a passing test. In code mode (Codex 0.166: the model's
    tools run inside one JavaScript exec) the call's text holds several tools.exec_command({cmd}) calls and its result
    one JSON chunk per call: each command is paired with its own exit code. The call id is kept, so the Stop and
    SessionEnd scans of the rollout never count the same run again. Returns the hook output or None."""
    if (inp.get('tool_name') or '') not in SHELL_NAMES:
        return None
    m = machine(cfg, inp)
    if not m:
        return None
    call_id = inp.get('tool_use_id') or ''
    m.change(lambda d: d.update(post_seen=True, seen_calls=(d.get('seen_calls') or [])[-499:] + ([call_id] if call_id else [])))
    args = inp.get('tool_input')
    cmd = rows_codex.shell_command(args) if isinstance(args, dict) else args if isinstance(args, str) else ''
    texts = [x for x in rows_codex.strings_in(args)] if not rows_codex.is_code_mode(cmd) else []
    code_js = cmd if rows_codex.is_code_mode(cmd) else next((x for x in texts if rows_codex.is_code_mode(x)), '')
    if code_js:
        runs = rows_codex.code_mode_runs(code_js, inp.get('tool_response'))
    else:
        runs = [(cmd,) + tuple(rows_codex.shell_result(inp.get('tool_response')))]
    said = []
    for c, code, line in runs:
        o = m.post_bash(c, code, line)
        if o:
            said.append(o['hookSpecificOutput']['additionalContext'])
    return context('PostToolUse', '\n'.join(said)) if said else None


def stop_fallback(cfg, inp):
    """The Stop hook, and SessionEnd before its handoff: the rollout's shell calls since the last scan that no
    PostToolUse handled (its call id is not in the state's seen_calls), in order, each through the machine's Bash rule
    (a passing run of a held task's test sends done). Covers a session whose PostToolUse did not fire or could not
    read the exit code."""
    m = machine(cfg, inp)
    path = inp.get('agent_transcript_path') or inp.get('transcript_path')
    if not m or not path:
        return
    st = m.read()
    if not st['tasks']:
        return
    runs, offset = rows_codex.shell_runs(path, int(st.get('rollout_offset') or 0))
    m.change(lambda d: d.update(rollout_offset=offset))
    seen = set(st.get('seen_calls') or [])
    for r in runs:
        if r['call_id'] not in seen:
            m.post_bash(r['command'], r['exit_code'], r['summary'])


def end_machine(cfg, inp):
    m = machine(cfg, inp)
    if m:
        m.end()


def _alarm(*_):
    raise TimeoutError(f'pre-tool over {PRE_TOOL_ALARM_S}s')


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ''
    if mode == 'pre-tool':
        signal.signal(signal.SIGALRM, _alarm)
        signal.setitimer(signal.ITIMER_REAL, PRE_TOOL_ALARM_S)
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
    if os.environ.get('CTRLB_HOOK_DUMP'):      # diagnosis: what this Codex sends each hook (secrets redacted)
        try:
            with open(os.environ['CTRLB_HOOK_DUMP'], 'a') as fh:
                fh.write(redact.redact(json.dumps(dict(mode=mode, input=inp)))[0][:20000] + '\n')
        except Exception as e:
            common.log(f"codex hook dump: {type(e).__name__}")
    out = None
    if mode == 'session-start':
        run, parent = session_run(cfg, sid, inp.get('transcript_path'))
        head = SUBAGENT.format(sid=parent) if parent else RECALL.format(sid=sid)
        agent = rows_codex.agent_for(cfg, sid, parent)
        scratch = relay_hooks.make_scratch(cfg, run) or common.scratch_dir(cfg, run)
        text = head + '\n\n' + relay_text(cfg, run, agent, scratch)
        m = machine(cfg, inp)
        try:      # the hooks protocol: claim(next=true), and the task's brief when one is held
            brief = m.start(inp.get('source') or '') if m else ''
        except Exception as e:
            common.log(f"codex session-start claim: {type(e).__name__}: {str(e)[:200]}")
            brief = ''
        try:      # decision 45, now only the agent's artefacts written back to the scratch directory
            block = relay_hooks.artefacts_line(cfg, run, agent, inp.get('source') or '', machine=m)
        except Exception as e:
            common.log(f"codex session-start artefacts: {type(e).__name__}: {str(e)[:200]}")
            block = ''
        out = context('SessionStart', '\n\n'.join(x for x in (text, brief, block) if x))
    elif mode == 'subagent-start':
        upload(cfg, [inp.get('transcript_path')], SYNC_S - 1, event, sid)
        run, _ = session_run(cfg, sid, inp.get('transcript_path'))
        sub = inp.get('agent_id') or ''
        agent = rows_codex.agent_for(cfg, sub, sid) if sub else rows_codex.agent_for(cfg, sid)
        out = context('SubagentStart', SUBAGENT.format(sid=sid) + '\n\n'
                      + relay_text(cfg, run, agent, common.scratch_dir(cfg, run)))
    elif mode == 'flush':
        upload(cfg, [inp.get('agent_transcript_path'), inp.get('transcript_path')], SYNC_S, event, sid)
    elif mode == 'session-end':
        try:      # a passing test the PostToolUse hook missed is finished before the handoff releases the task
            stop_fallback(cfg, inp)
        except Exception as e:
            common.log(f"codex session-end scan: {type(e).__name__}: {str(e)[:200]}")
        try:
            session_end(cfg, inp)
        except Exception as e:
            common.log(f"codex session-end: {type(e).__name__}: {str(e)[:200]}")
        try:
            end_machine(cfg, inp)
        except Exception as e:
            common.log(f"codex session-end machine: {type(e).__name__}: {str(e)[:200]}")
        upload(cfg, [inp.get('agent_transcript_path'), inp.get('transcript_path')], SYNC_S, event, sid)
    elif mode == 'upload':
        try:
            stop_fallback(cfg, inp)
        except Exception as e:
            common.log(f"codex stop fallback: {type(e).__name__}: {str(e)[:200]}")
        upload(cfg, [inp.get('agent_transcript_path') or inp.get('transcript_path')], ASYNC_S, event, sid)
    elif mode == 'post-tool':
        try:
            out = post_tool(cfg, inp)
        except Exception as e:
            common.log(f"codex post-tool: {type(e).__name__}: {str(e)[:200]}")
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
