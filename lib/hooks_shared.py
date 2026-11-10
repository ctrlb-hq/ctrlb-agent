"""What the Claude Code hook (plugin/hooks/hook.py) and the Codex hook (codex/hook.py) share: the injected rules and
the edit check of decision 7. Both harnesses take the same output shapes: hookSpecificOutput with additionalContext
(SessionStart, SubagentStart, PreToolUse) and, on PreToolUse, permissionDecision "deny" with a non-empty
permissionDecisionReason."""
import datetime
import os
import json
import re
import time
import common
from redact import clean as _clean
from paths import PATH, edit_paths as normalise_edit_paths, key as resource_key

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

# Rule text v10 (round 8): claim(next=true) first, done with next=true, peers only to wait; the REGISTRY.md sentence
# is gone (the hooks claim autoSlots files, decision 47); helper files go to the run's scratch directory (decision D).
RELAY = ("CtrlB Relay: your run id is {run} and your agent id is {agent}. Other agents in this run may be working in "
         "the same files. Pass run_id=\"{run}\" and agent=\"{agent}\" to the ctrlb relay tools (peers, post, claim, "
         "release, handoff, ask, answer, brief, briefs, tasks, contract, contracts). Start with `claim(next=true)`. When "
         "your test passes, call `done` with the evidence and `next=true`. Call `peers` only when you are waiting for an "
         "answer or a handoff. Claim the task by the name your orchestrator registered (for example "
         "claim(resource=\"task 03 median\"); tasks lists them). Its files come with it. If held is false with "
         "finished_by, the task is done; if held is false with held_by, another agent has it: pick another. If a claim "
         "is refused and the answer lists free tasks, claim one of them next. If the list is empty, post a note saying "
         "you have no task and stop. If tasks shows a task you need as stalled, claim it with takeover=true, read its "
         "evidence and notes, and continue it. Never wait in line on a stalled task. If a file you need is held by an "
         "agent that has stopped, the hold shows as stalled; take it over the same way. If reason is unknown_task, "
         "claim again with the suggestion. If reason is belongs_to_task, the file comes with a registered task: claim "
         "that task (task in the answer) instead. If the claim answers ask_about, ask the task's owner about those "
         "before deciding them yourself. Work only while you hold it. When the task is done, release "
         "it with finished=true. Run the task's test command (tasks and claim show it as test); when it passes, call "
         "done on the task name with that command, its exit code and its last output line as the evidence. When the "
         "task has no test command, give the check you ran. A task is finished only when the record shows what was "
         "checked. A file that belongs to no task is claimed by its path. A shared thing that is not a "
         "file, such as the dev server or a port, is claimed as slot:<name> and released when you are done with it. Use "
         "the coordination tools before a prose note: handoff when you must leave a task you hold, so it moves to the "
         "agent you name in one step; ask when you are blocked on something only another agent or the orchestrator "
         "can decide; answer, with the question's id, when you can settle an open question; contract when you finish "
         "an interface others will build on (a type, a route, a config shape), with its name, the shape in a few "
         "lines and the files that define it. Before you build on a contract, read it with `contracts`. If it is "
         "stale, read the file it names and check your assumptions against it. Post a note "
         "only for what none of them covers. If you were asked to do a finished task again, claim its name with "
         "reopen=true. Put helper scripts and binaries in the scratch directory {scratch}, not in /tmp.")


# Seeker decision 33: a hold whose holder has stopped is taken over, not waited out.
STALLED_HINT = ("If the holder has stopped, the hold shows as stalled (tasks lists stalled tasks); claim it with "
                "takeover=true instead of waiting.")


def context(event, text):
    return {'hookSpecificOutput': {'hookEventName': event, 'additionalContext': text}}


def _resource(c):
    """What to name in the claim call: the conflict's path without the key prefix when claiming that names the same key
    ("src/app.py"), else the key itself ("path:Makefile", since a bare "Makefile" is keyed as a name)."""
    key = c.get('key') or ''
    if not key.startswith(PATH):
        return c.get('path') or key
    bare = key[len(PATH):]
    return bare if resource_key(bare)[0] == key else key


def _lease(c):
    try:
        mins = (float(c['lease_until']) / 1e6 - time.time()) / 60
        return f' (lease ends in about {max(1, round(mins))} min)' if mins > 0 else ''
    except Exception:
        return ''


def decide(cfg, run, agent, paths, cwd=''):
    """Decision 7: post the paths an edit is about to touch to Seeker's check and turn the answer into the hook
    output, or None for no output (allowed, mode off, or no conflict). Raises on any Seeker error; the caller fails
    open. The paths are sent as paths.edit_paths gives them (relative to cwd, once per key), so Seeker keys them as it
    keys claims and as it keys the paths column of the session rows. The tokens the agent's claims answered are sent
    with them (decision 21), so Seeker can tell a hold of an earlier round of the file's task."""
    paths = normalise_edit_paths(paths, cwd or '')
    if not paths or not run or not agent:
        return None
    keys = {p: resource_key(p, cwd or '')[0] for p in paths}
    try:
        held = common.TokenState(run, agent).get(sorted(set(keys.values())))
    except Exception:
        held = {}
    tokens = {p: held[k] for p, k in keys.items() if k in held}
    res = common.check(cfg, run, agent, paths, cwd or '', timeout=CHECK_S, tokens=tokens)
    mode = res.get('mode') or 'advisory'
    conflicts = [c for c in res.get('conflicts') or [] if isinstance(c, dict)
                 and (c.get('reason') in ('lapsed', 'unheld', 'stale_round') or c.get('held_by') and c['held_by'] != agent)]
    if mode == 'off' or not conflicts:
        return None
    first = _resource(conflicts[0])
    call = f'claim(resource="{first}", run_id="{run}", agent="{agent}")'
    reasons = {c.get('reason') or 'held' for c in conflicts}
    said = '; '.join(_said(c) for c in conflicts)
    if reasons == {'held'}:
        holder = conflicts[0]['held_by']
        if mode == 'enforce' and res.get('allowed') is False:
            reason = (f"CtrlB Relay (run {run}): {said}. Do not edit it. Call {call} and edit only if held is true; "
                      f"otherwise pick other work or call peers to see what {holder} is doing. {STALLED_HINT}")
            return _deny(reason)
        return context('PreToolUse', f"CtrlB Relay (run {run}): {said}. Another agent is working on this file. Before "
                                     f"editing it, call {call}; if held is false, leave the file to its holder and pick "
                                     f"other work. {STALLED_HINT}")
    # Fencing (holdRequired): the agent does not hold the file now.
    text = (f"CtrlB Relay (run {run}): {said}. Call {call} and edit only if held is true; if held is false, leave the "
            f"file to its holder and pick other work.")
    if mode == 'enforce' and res.get('allowed') is False:
        return _deny(text)
    return context('PreToolUse', text)


def _said(c):
    """One conflict in words: the holder (held), or Seeker's message for lapsed, unheld and stale_round."""
    reason = c.get('reason') or 'held'
    if reason == 'held':
        return f"{_resource(c)} is claimed by {c['held_by']}{_lease(c)}"
    msg = c.get('message') or {'lapsed': f'your lease on {_resource(c)} ran out',
                               'unheld': f'{_resource(c)} is not held; claim it before editing'}.get(reason, reason)
    return f"{msg} ({reason})"


def _deny(reason):
    return {'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'permissionDecision': 'deny',
                                   'permissionDecisionReason': reason}}


def _tool_answer(resp):
    """The JSON answer of an MCP tool from a PostToolUse tool_response: a dict, a list of content blocks, a dict with
    content, or text."""
    if isinstance(resp, dict) and ('held' in resp or 'handed' in resp or 'released' in resp or 'done' in resp):
        return resp
    if isinstance(resp, dict):
        resp = resp.get('content', resp.get('text', ''))
    if isinstance(resp, list):
        resp = ''.join(b.get('text', '') if isinstance(b, dict) else str(b) for b in resp)
    if not isinstance(resp, str):
        return {}
    try:
        out = json.loads(resp)
    except ValueError:
        return {}
    return out if isinstance(out, dict) else {}


def record_tokens(run, agent, tool, answer):
    """Keep the agent's hold tokens (decision 21): a held claim's token for its key and its bundle's keys; a handoff
    or a release drops them. Returns the keys changed."""
    if not run or not agent or not isinstance(answer, dict):
        return []
    keys = [k for k in [answer.get('key')] + list(answer.get('paths') or []) if isinstance(k, str) and k]
    if not keys:
        return []
    state = common.TokenState(run, agent)
    name = tool.rsplit('__', 1)[-1]
    if name == 'claim' and answer.get('held') is True and answer.get('token'):
        state.put(keys, answer['token'])
        return keys
    if (name == 'handoff' and answer.get('handed') is True or name == 'release' and answer.get('released')
            or name == 'done' and answer.get('done') is True):
        state.drop(keys)
        return keys
    return []


# ---------------------------------------------------------------- done and its evidence (Seeker decision 30)

# A test-looking command: pytest, python -m pytest / unittest, go test, npm (run) test, cargo test, make test.
TEST_COMMAND = re.compile(r'(?:^|[\s;&|(`])(?:pytest|py\.test|python[0-9.]*\s+-m\s+(?:pytest|unittest)|go\s+test|npm\s+(?:run\s+)?test'
                          r'|cargo\s+test|make\s+(?:\S+\s+)*test)(?:$|[\s;&|)`])')
EXIT_CODE = re.compile(r'^\s*(?:Error:\s*)?Exit code (-?\d+)')
SUMMARY_MAX = 300
COMMAND_MAX = 500


# Installing a test tool is not running a test.
INSTALL_COMMAND = re.compile(r'\b(?:pip[0-9.]*|uv|poetry|pipx|apt(?:-get)?|brew|npm|yarn|pnpm|cargo|go)\s+(?:install|add|i|get)\b')


def is_test_command(cmd):
    return isinstance(cmd, str) and bool(TEST_COMMAND.search(cmd)) and not INSTALL_COMMAND.search(cmd)


def _last_line(text):
    for ln in reversed((text or '').splitlines()):
        if ln.strip():
            return ln.strip()[:SUMMARY_MAX]
    return ''


def bash_result(resp):
    """(exit code, last output line) of a Bash tool_response: an object with stdout and stderr (the command exited
    0; an explicit exit code field wins), or the error text of a failed call, whose first line is "Exit code N"."""
    if isinstance(resp, list):
        resp = ''.join(b.get('text', '') if isinstance(b, dict) else str(b) for b in resp)
    if isinstance(resp, dict):
        code = 0
        for k in ('exit_code', 'exitCode', 'returnCode', 'code'):
            if isinstance(resp.get(k), int) and not isinstance(resp.get(k), bool):
                code = resp[k]
                break
        return code, _last_line(resp.get('stdout') or '') or _last_line(resp.get('stderr') or '')
    if isinstance(resp, str):
        m = EXIT_CODE.match(resp)
        rest = resp[m.end():] if m else resp
        return (int(m.group(1)) if m else None), _last_line(rest)
    return None, ''


def record_test(run, agent, command, code, line):
    """Remember the agent's last test command, its exit code and last output line (per run and agent)."""
    if not run or not agent or not is_test_command(command):
        return None
    rec = dict(command=command.strip()[:COMMAND_MAX], exit_code=code, summary=line, ts=time.time())
    common.LastTest(run, agent).put(rec)
    return rec


def last_test_in_transcript(path):
    """The newest test-looking Bash call of a transcript and its result: {command, exit_code, summary, ts}, or None.
    A failed call's result is an error whose first line is "Exit code N"."""
    if not path:
        return None
    calls, best = {}, None
    try:
        fh = open(path, errors='replace')
    except OSError:
        return None
    with fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            msg = rec.get('message') if isinstance(rec, dict) else None
            if not isinstance(msg, dict) or not isinstance(msg.get('content'), list):
                continue
            for b in msg['content']:
                if not isinstance(b, dict):
                    continue
                if b.get('type') == 'tool_use' and b.get('name') == 'Bash':
                    cmd = (b.get('input') or {}).get('command') or ''
                    if is_test_command(cmd):
                        calls[b.get('id')] = cmd
                elif b.get('type') == 'tool_result' and b.get('tool_use_id') in calls:
                    c = b.get('content')
                    text = c if isinstance(c, str) else ''.join(x.get('text', '') for x in c or [] if isinstance(x, dict))
                    m = EXIT_CODE.match(text)
                    code = int(m.group(1)) if m else (None if b.get('is_error') else 0)
                    best = dict(command=calls[b['tool_use_id']].strip()[:COMMAND_MAX], exit_code=code,
                                summary=_last_line(text[m.end():] if m else text), ts=_epoch(rec.get('timestamp')))
    return best


def _epoch(ts):
    try:
        return datetime.datetime.fromisoformat(str(ts).replace('Z', '+00:00')).timestamp()
    except ValueError:
        return 0.0


# Seeker decision 32: an evidence_required refusal of a task with a test command carries it as "test", and its message
# ends with this marker and the command.
TASK_TEST_MARKER = "; the task's test command is: "


def refusal_test(answer):
    """The task's test command an evidence_required refusal quotes: its "test", else the end of its message after
    TASK_TEST_MARKER. '' when it quotes none."""
    test = answer.get('test')
    if isinstance(test, str) and test.strip():
        return test.strip()
    msg = answer.get('message')
    if isinstance(msg, str) and TASK_TEST_MARKER in msg:
        return msg.rsplit(TASK_TEST_MARKER, 1)[1].strip()
    return ''


def names_test(command, test):
    """Whether a command names the task's test (Seeker's rule: the test, each run of white space as one space,
    appears in the command so spaced)."""
    t = ' '.join((test or '').split())
    return bool(t) and t in ' '.join((command or '').split())


def evidence_hint(run, agent, answer, transcript='', keys=()):
    """When done (or a finished release) was refused for evidence, the PostToolUse output that shows the agent the
    task's test command when the refusal quotes one, else the last test command it ran, so it can call done again
    with it. Never fills evidence in. None otherwise."""
    if not isinstance(answer, dict) or answer.get('reason') != 'evidence_required':
        return None
    found = None
    for r, a in [(run, agent)] + list(keys):
        if r and a:
            try:
                found = common.LastTest(r, a).get()
            except Exception:
                found = None
            if found:
                break
    # The transcript has failed runs too (a failing Bash call may not reach PostToolUse): the newer of the two wins.
    seen = last_test_in_transcript(transcript)
    if seen and (not found or (seen.get('ts') or 0) >= (found.get('ts') or 0)):
        found = seen
    resource = answer.get('resource') or answer.get('key') or '<the task name>'
    head = f'CtrlB Relay (run {run}): {resource} was not marked finished: this run requires evidence.'
    test = refusal_test(answer)
    if test and not (found and names_test(found.get('command'), test)):
        # The task's own test wins over whatever the agent ran last.
        ran = f" The last test command you ran, `{found['command']}`, is not it." if found else ''
        return context('PostToolUse', f'{head} The task\'s test command is `{test}`.{ran} Run it; when it passes, call '
                                      f'done(resource="{resource}", run_id="{run}", agent="{agent}", evidence={{"command": '
                                      f'{json.dumps(test)}, "exit_code": <its exit code>, "summary": <its last output line>}}). '
                                      f'Report only a check you ran.')
    if not found:
        return context('PostToolUse', f'{head} Run the task\'s test, then call done(resource="{resource}", run_id="{run}", '
                                      f'agent="{agent}", evidence={{"command": <the command>, "exit_code": <its exit code>, '
                                      f'"summary": <one line of its output>}}).')
    ev = {'command': found['command'], 'exit_code': found.get('exit_code'), 'summary': found.get('summary') or ''}
    said = f"`{found['command']}` (exit code {found.get('exit_code') if found.get('exit_code') is not None else 'unknown'})"
    if test:
        return context('PostToolUse', f'{head} The last test command you ran was {said}, the task\'s test. If it passed, '
                                      f'call done(resource="{resource}", run_id="{run}", agent="{agent}", '
                                      f'evidence={json.dumps(ev)}); otherwise fix the task and run it again. Report only '
                                      f'a check you ran.')
    return context('PostToolUse', f'{head} The last test command you ran was {said}. If that is the task\'s check and it '
                                  f'passed, call done(resource="{resource}", run_id="{run}", agent="{agent}", '
                                  f'evidence={json.dumps(ev)}); otherwise run the task\'s test first. Report only a check '
                                  f'you ran.')

# ---------------------------------------------------------------- the session record (Seeker decisions 36 and 38)

HANDOFF_TEXT_MAX = 1500
LINE_MAX = 200


def _norm(cmd):
    return ' '.join(str(cmd or '').split())


def record_bash(run, agent, sid, command, code, line):
    """Remember one Bash command of this session, its exit code (None when unknown) and last output line: every
    command, not only tests (the done witness checks evidence against them)."""
    if not run or not agent or not isinstance(command, str) or not command.strip():
        return None
    rec = dict(command=command.strip()[:COMMAND_MAX], exit_code=code, summary=(line or '')[:SUMMARY_MAX], ts=time.time())
    common.SessionRecord(run, agent).update(sid, lambda d: d['bash'].append(rec))
    return rec


def record_edit(run, agent, sid, paths):
    """Remember the files an applied edit tool call wrote in this session (in order, once each)."""
    paths = [p for p in paths or [] if isinstance(p, str) and p]
    if not run or not agent or not paths:
        return []

    def fn(d):
        for p in paths:
            if p in d['edited']:
                d['edited'].remove(p)
            d['edited'].append(p)
    common.SessionRecord(run, agent).update(sid, fn)
    return paths


def record_note(run, agent, sid, text):
    """Remember the agent's newest note (the ctrlb post tool's text)."""
    if not run or not agent or not isinstance(text, str) or not text.strip():
        return None
    note = dict(text=text.strip()[:LINE_MAX * 2], ts=time.time())
    common.SessionRecord(run, agent).update(sid, lambda d: d.update(note=note))
    return note


def mark_relay(run, agent, sid):
    """Remember that the agent used the ctrlb relay tools in this session (the session-end handoff is then sent)."""
    if run and agent:
        common.SessionRecord(run, agent).update(sid, lambda d: d.update(relay=True))


def _cut(s, n):
    s = ' '.join(str(s or '').split())
    return s if len(s) <= n else s[:n - 3] + '...'


def handoff_text(rec, last_test=None):
    """The open handoff's text (decision 36), one paragraph from the hook's own record: the files edited in this
    session, the last test command it saw and its exit code, and the agent's last note. Each piece is cleaned
    (private tags, secrets) on its own, so an unclosed private tag in one never hides the others."""
    def c(t):
        return _clean(t or '')[0]
    parts = ['Session ended.']
    edited = [c(os.path.basename(p) if len(p) > 80 else p) for p in (rec.get('edited') or [])]
    if edited:
        shown = edited[-12:]
        more = f' and {len(edited) - len(shown)} more' if len(edited) > len(shown) else ''
        parts.append('Edited: ' + ', '.join(shown) + more + '.')
    else:
        parts.append('Edited: nothing.')
    tests = [b for b in rec.get('bash') or [] if is_test_command(b.get('command'))]
    test = tests[-1] if tests else None
    if last_test and (not test or (last_test.get('ts') or 0) > (test.get('ts') or 0)):
        test = last_test
    if test:
        code = test.get('exit_code')
        line = f" (last line: {_cut(c(test.get('summary')), LINE_MAX)})" if test.get('summary') else ''
        parts.append(f"Last test: `{_cut(c(test.get('command')), 300)}` exit {code if code is not None else 'unknown'}{line}.")
    else:
        parts.append('Last test: none run.')
    note = rec.get('note') or {}
    if note.get('text'):
        parts.append('Last note: ' + _cut(c(note['text']), 400))
    return _cut(' '.join(parts), HANDOFF_TEXT_MAX)


def _evidence_of(args):
    """The done call's evidence as a dict, or None (free text, or nothing)."""
    ev = args.get('evidence')
    if isinstance(ev, str):
        try:
            ev = json.loads(ev)
        except ValueError:
            return None
    return dict(ev) if isinstance(ev, dict) else None


def _code(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def witness(rec, tool_input):
    """Decision 38: the PreToolUse output for a done call, checked against this session's Bash record. A command
    containing the evidence command (white space collapsed), with an equal exit code (newest first): allow, with
    witnessed true (and the exit code filled in from the record when the call left it out). No such command: deny,
    naming what the hook saw. No evidence command: None, unless the agent set witnessed itself (then it is dropped).
    The hook asserts only what it saw; it is as strong as the host it runs on."""
    args = dict(tool_input) if isinstance(tool_input, dict) else {}
    ev = _evidence_of(args)
    cmd = _norm(ev.get('command')) if ev else ''
    if not cmd:
        if 'witnessed' in args:
            args.pop('witnessed')
            return _allow(args)
        return None
    want = _code(ev.get('exit_code')) if ev.get('exit_code') is not None else None
    bash = rec.get('bash') or []
    seen = [b for b in reversed(bash) if cmd in _norm(b.get('command'))]
    for b in seen:
        code = _code(b.get('exit_code'))
        if code is None:
            continue
        if want is None or code == want:
            ev['exit_code'] = code
            args['evidence'] = ev
            args['witnessed'] = True
            return _allow(args)
    run = args.get('run_id') or ''
    head = f"CtrlB Relay{f' (run {run})' if run else ''}: done was not sent: the evidence names `{_cut(cmd, 300)}`"
    head += f' with exit code {want}' if want is not None else ''
    if seen:
        b = seen[0]
        saw = (f"; this session ran it with exit code {b.get('exit_code') if b.get('exit_code') is not None else 'unknown'}"
               + (f" (last line: {_cut(b.get('summary'), LINE_MAX)})" if b.get('summary') else ''))
    elif bash:
        tests = [b for b in reversed(bash) if is_test_command(b.get('command'))]
        b = tests[0] if tests else bash[-1]
        saw = (f"; this session never ran it. The last {'test ' if tests else ''}command it ran was "
               f"`{_cut(b.get('command'), 300)}` (exit code {b.get('exit_code') if b.get('exit_code') is not None else 'unknown'})")
    else:
        saw = '; there is no Bash command in this session'
    return _deny(head + saw + '. Run the task\'s test, then call done with the command, its exit code and its last '
                 'output line as you saw them.')


def _allow(args):
    return {'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'permissionDecision': 'allow', 'updatedInput': args}}
