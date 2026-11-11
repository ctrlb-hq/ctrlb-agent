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

# The injected Relay text (the hooks protocol, docs/relay-hooks.md): who the agent is, that the hooks hold files and
# tasks for it, that a blocked edit names the holder, and how to talk to peers. Nothing about claim, release, renew or
# done: the hooks do those. The round 8 rule text (v10), for a harness without hooks, is in docs/relay-tools.md.
RELAY_IDS = 'CtrlB Relay: your run id is {run} and your agent id is {agent}.'
RELAY_HOOKS = ("Relay holds files and tasks for you through the plugin's hooks, so never claim or release anything by "
               "hand. If an edit is blocked, the message names the agent that holds the file: leave that file to it, "
               "or ask it.")
RELAY_PEERS = ('To talk to the other agents, pass run_id="{run}" and agent="{agent}" to the ctrlb tools peers (what the '
               'others are doing), post (a note to all), ask (a question to one agent) and answer (with the '
               "question's id).")
RELAY_SCRATCH = 'Put helper scripts and binaries in the scratch directory {scratch}, not in /tmp.'
RELAY = ' '.join([RELAY_IDS, RELAY_HOOKS, RELAY_PEERS, RELAY_SCRATCH])
RELAY_NO_RUN = ' '.join([RELAY_IDS, RELAY_PEERS, RELAY_SCRATCH])


def relay_text(cfg, run, agent, scratch):
    """The injected Relay text: with a run id (CTRLB_RUN or the config's run) the hooks line too; without one the
    hooks never claim, so only the ids, the peers line and the scratch directory."""
    return (RELAY if cfg.get('run') else RELAY_NO_RUN).format(run=run, agent=agent, scratch=scratch)


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
    """Whether a command names the task's test: the test, each run of white space as one space, appears in the
    command so spaced. Naming it is not running it alone (test_alone): `test | tail -8` names the test."""
    t = ' '.join((test or '').split())
    return bool(t) and t in ' '.join((command or '').split())


TEST_ALONE_SUFFIX = ' 2>&1'


def test_alone(command, test):
    """Whether a command is the task's test run alone, so that its exit code is the test's (Seeker's rule,
    models.IsTestAlone; docs/relay-hooks.md): with each run of white space as one space, exactly the test, allowing
    only a leading `cd <one path> &&` (or `cd <one path>;`) and a trailing `2>&1`. Any other pipe, `;`, `&&`, `||`,
    backtick or `$(` makes the exit code another command's. The one rule for post_bash, the Codex rollout scan
    (which calls post_bash) and the done witness."""
    t, c = ' '.join((test or '').split()), ' '.join((command or '').split())
    if not t or not c:
        return False
    if c in (t, t + TEST_ALONE_SUFFIX):
        return True
    rest = _cut_cd(c)
    return rest is not None and rest in (t, t + TEST_ALONE_SUFFIX)


def _cut_cd(c):
    """c (spaced) without a leading `cd <path> && ` or `cd <path>; `, the path one word with no shell operator; None
    when c does not start so."""
    if not c.startswith('cd '):
        return None
    path, sep, rest = c[3:].partition(' ')
    if not sep:
        return None
    if path.endswith(';'):
        path = path[:-1]
    elif rest.startswith('&& '):
        rest = rest[3:]
    elif rest.startswith('; '):
        rest = rest[2:]
    else:
        return None
    if not path or any(ch in path for ch in ';&|`<>()$'):
        return None
    return rest or None


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
    if test and not (found and test_alone(found.get('command'), test)):
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
    session, the last test command it saw and its exit code, and the agent's last note."""
    parts = ['Session ended.']
    edited = [os.path.basename(p) if len(p) > 80 else p for p in (rec.get('edited') or [])]
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
        line = f" (last line: {_cut(test.get('summary'), LINE_MAX)})" if test.get('summary') else ''
        parts.append(f"Last test: `{_cut(test.get('command'), 300)}` exit {code if code is not None else 'unknown'}{line}.")
    else:
        parts.append('Last test: none run.')
    note = rec.get('note') or {}
    if note.get('text'):
        parts.append('Last note: ' + _cut(note['text'], 400))
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
    that is the evidence command run alone (test_alone: white space collapsed, at most a leading cd and a trailing
    2>&1; a pipe or another command makes the exit code not its own), with an equal exit code (newest first): allow, with
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
    seen = [b for b in reversed(bash) if test_alone(b.get('command'), cmd)]
    inside = [b for b in reversed(bash) if cmd in _norm(b.get('command'))] if not seen else []
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
    elif inside:
        b = inside[0]
        saw = (f"; this session ran it only inside `{_cut(b.get('command'), 300)}`, whose exit code is not the test's "
               f"(a pipe or another command around it). Run it alone, exactly as written, on its own line")
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


# ---------------------------------------------------------------- the hooks protocol (docs/relay-hooks.md)

CLAIM_S = 2.0           # a claim on write, after the edit check's CHECK_S (the pre-tool alarm is PRE_TOOL_ALARM_S)
PRE_TOOL_ALARM_S = 4.5  # inside the PreToolUse hook's 5 s timeout
NEXT_S = 5.0            # SessionStart's claim(next=true), inside the hook's 10 s timeout
DONE_S = 3.0            # the done a passing test sends, inside the PostToolUse hook's 5 s timeout
RENEW_AT = 0.2          # renew a hold when an edit comes within this share of its lease of the lease end
READ_ONLY_AGENTS = ('Explore', 'Plan', 'claude-code-guide', 'statusline-setup')
BRIEF_PATHS = 12


def _now_us():
    return int(time.time() * 1e6)


def _lease_s(until_us, at=None):
    """The length of a lease that ends at until_us (µs), from the time of the answer that carried it."""
    try:
        if not until_us:
            return 0
        return max(1, round(float(until_us) / 1e6 - (at if at is not None else time.time())))
    except (TypeError, ValueError):
        return 0


def _bare(k):
    return k[len(PATH):] if isinstance(k, str) and k.startswith(PATH) else str(k)


def brief_text(task, lead=''):
    """The context that names a held task: its name, files, the tasks it comes after, test command (to run as
    written) and ask_about."""
    files = [_bare(p) for p in task.get('paths') or []]
    shown = ', '.join(files[:BRIEF_PATHS]) + (f' and {len(files) - BRIEF_PATHS} more' if len(files) > BRIEF_PATHS else '')
    parts = [f'{lead}CtrlB Relay: you hold task "{task.get("name")}".']
    after = [a for a in task.get('after') or [] if isinstance(a, str) and a.strip()]
    if after:
        parts.append(f'It comes after {", ".join(after)}, which are finished.')
    if task.get('taken_over_from'):
        parts.append(f'You took it over from {task["taken_over_from"]}, who stopped; read its files and notes first.')
    if shown:
        parts.append(f'Its files: {shown}.')
    if task.get('test'):
        parts.append(f"When your change is ready, run its test exactly as written, on its own line, with no pipe, tail or "
                     f"other command before or after it: `{task['test']}`. When it passes, the hooks record it and hand "
                     "you your next task.")
    asks = [a for a in task.get('ask_about') or [] if isinstance(a, str) and a.strip()]
    if asks:
        parts.append('Ask about these before deciding them yourself: ' + '; '.join(asks) + '.')
    return ' '.join(parts)


class Machine:
    """One agent session's protocol state (docs/relay-hooks.md, "The agent-side state machine"): a JSON file under
    <CTRLB_HOME>/state/, written by the hooks only, held exclusively while it is read and written (never across a Seeker call).
    key names the file: the session id, or <session id>.<agent id> for a Claude Code sub-agent."""

    def __init__(self, cfg, run, agent, key, cwd=''):
        self.cfg, self.run, self.agent, self.cwd = cfg, run, agent, cwd or ''
        d = os.path.join(common.HOME, 'state')
        os.makedirs(d, exist_ok=True)
        self.file = os.path.join(d, common.safe_segment(key) + '.json')

    # -- the state file

    def _blank(self):
        return dict(agent=self.agent, run=self.run, state='idle', tasks={}, holds={}, pending_done=None,
                    calls=dict(claim=0, check=0, done=0, read=0))

    def _locked(self, fn):
        import fcntl
        with open(self.file + '.mutex', 'w') as mx:
            fcntl.flock(mx, fcntl.LOCK_EX)
            try:
                try:
                    with open(self.file) as fh:
                        d = json.load(fh)
                except Exception:
                    d = {}
                if not isinstance(d, dict) or d.get('run') != self.run or d.get('agent') != self.agent:
                    d = self._blank()
                for k, v in self._blank().items():
                    d.setdefault(k, v)
                out = fn(d)
                d['state'] = d['state'] if d['state'] == 'ended' else ('holding' if d['tasks'] or d['holds'] else
                                                                       d['state'] if d['state'] == 'idle' else 'free')
                tmp = self.file + '.tmp'
                with open(tmp, 'w') as fh:
                    json.dump(d, fh)
                os.replace(tmp, self.file)
                return out
            finally:
                fcntl.flock(mx, fcntl.LOCK_UN)

    def read(self):
        return self._locked(lambda d: json.loads(json.dumps(d)))

    def change(self, fn):
        return self._locked(fn)

    def count(self, name):
        """Count one hook call to Seeker that is not a claim, check or done (the session-start read)."""
        self.change(lambda d: d['calls'].update({name: d['calls'].get(name, 0) + 1}))

    def _call(self, name, what, fn):
        """One Seeker call by the hook: counted in the state file and logged with its answer. Raises what fn raises."""
        self.change(lambda d: d['calls'].update({name: d['calls'].get(name, 0) + 1}))
        try:
            ans = fn()
        except BaseException as e:
            common.log(f"relay-hook {self.agent} {name} {what} -> {type(e).__name__}: {str(e)[:160]}")
            raise
        brief = {k: ans.get(k) for k in ('held', 'done', 'reason', 'held_by', 'finished_by', 'task', 'renewed', 'mode')
                 if isinstance(ans, dict) and ans.get(k) not in (None, '', False)} if isinstance(ans, dict) else ans
        common.log(f"relay-hook {self.agent} {name} {what} -> {json.dumps(brief)[:240]}")
        return ans if isinstance(ans, dict) else {}

    # -- what the state keeps from Seeker's answers

    def _take_task(self, d, ans, name=None, at=None):
        """Keep a held task from a claim answer (a claim of the task by name, or claim(next=true) with next_task)."""
        nt = ans.get('next_task') if isinstance(ans.get('next_task'), dict) else {}
        name = nt.get('name') or name or ans.get('resource') or ''
        paths = [p for p in (nt.get('paths') or ans.get('paths') or []) if isinstance(p, str)]
        until = ans.get('lease_until') or 0
        task = dict(name=name, key=nt.get('key') or ans.get('key') or '', paths=paths,
                    test=nt.get('test') or ans.get('test') or '', ask_about=nt.get('ask_about') or ans.get('ask_about') or [],
                    lease_until=until, lease_s=_lease_s(until, at))
        after = nt.get('after') or ans.get('after') or []
        if after:
            task['after'] = [a for a in after if isinstance(a, str)]
        if ans.get('taken_over_from'):
            task['taken_over_from'] = ans['taken_over_from']
        d['tasks'][name] = task
        for k in [task['key']] + paths:
            if k:
                d['holds'][k] = dict(resource=name, task=name, lease_until=until, lease_s=task['lease_s'])
        return task

    def _take_hold(self, d, resource, ans, at=None):
        until = ans.get('lease_until') or 0
        k = ans.get('key') or resource_key(resource, self.cwd)[0]
        d['holds'][k] = dict(resource=resource, task=None, lease_until=until, lease_s=_lease_s(until, at))
        if ans.get('after_done'):
            d['holds'][k]['after_done'] = True      # the finisher's hold of its task's file

    def _drop_task(self, d, name):
        t = d['tasks'].pop(name, None) or {}
        for k in [t.get('key')] + list(t.get('paths') or []):
            if k and (d['holds'].get(k) or {}).get('task') == name:
                d['holds'].pop(k, None)

    # -- events

    def start(self, source='', agent_type=''):
        """SessionStart or SubagentStart: the idle rule. Returns the brief text to inject ('' when nothing is held).
        After a compaction, resume or clear of a session that holds tasks, the briefs again, with no call."""
        st = self.read()
        if source in ('compact', 'resume', 'clear') and st['tasks'] and st['state'] != 'ended':
            return '\n'.join(brief_text(t) for t in st['tasks'].values())
        if source == 'compact':
            return ''      # the same session goes on: it ran its start already
        if st['state'] == 'ended':
            self.change(lambda d: d.update(self._blank()))
        if not self.cfg.get('run') or not self.run or not self.agent:
            return ''
        if agent_type in READ_ONLY_AGENTS:
            self.change(lambda d: d.update(state='free'))
            return ''
        at = time.time()
        try:
            ans = self._call('claim', 'next=true', lambda: common.claim_next(self.cfg, self.run, self.agent, self.cwd,
                                                                             timeout=NEXT_S))
        except Exception:
            self.change(lambda d: d.update(state='free'))
            return ''
        if ans.get('held') is True:
            record_tokens(self.run, self.agent, 'claim', ans)
            task = self.change(lambda d: self._take_task(d, ans, at=at))
            return brief_text(task)
        self.change(lambda d: d.update(state='free'))
        return ''

    def end(self):
        """SessionEnd: the open handoff (hook.py) released every hold; the machine ends."""
        self.change(lambda d: d.update(state='ended', tasks={}, holds={}, pending_done=None))

    def pre_edit(self, paths):
        """PreToolUse on an edit of paths (as paths.edit_paths gives them). Returns the hook output or None (the edit
        goes ahead with nothing said). Raises when the edit check itself fails (the caller fails open)."""
        run, agent, cwd = self.run, self.agent, self.cwd
        if not paths or not run or not agent:
            return None
        keys = {p: resource_key(p, cwd)[0] for p in paths}
        try:
            held = common.TokenState(run, agent).get(sorted(set(keys.values())))
        except Exception:
            held = {}
        tokens = {p: held[k] for p, k in keys.items() if k in held}
        res = self._call('check', ','.join(paths)[:200],
                         lambda: common.check(self.cfg, run, agent, paths, cwd, timeout=CHECK_S, tokens=tokens))
        mode = res.get('mode') or 'advisory'
        if mode == 'off':
            return None
        has_own = isinstance(res.get('own'), list)
        own = {o.get('path'): o for o in res.get('own') or [] if isinstance(o, dict)}
        conflicts = {c.get('path'): c for c in res.get('conflicts') or [] if isinstance(c, dict)}
        st = self.read()
        blocked, briefs, failed, waiting, claimed_tasks, renewed = [], [], [], [], set(), set()
        for p in paths:
            c = conflicts.get(p)
            if c and c.get('held_by') and c['held_by'] != agent:
                blocked.append((p, c['held_by'], _said(c)))
                continue
            if p in own:
                self._renew(st, p, keys[p], own[p], renewed)
                continue
            if c or has_own:
                # nobody holds it (or the caller's lease lapsed): claim it on write
                try:
                    out = self._claim_on_write(p, claimed_tasks)
                except BaseException as e:
                    if isinstance(e, KeyboardInterrupt):
                        raise
                    failed.append(p)
                    continue
                kind, val = out
                if kind == 'brief':
                    briefs.append(val)
                elif kind == 'blocked':
                    blocked.append(val)
                elif kind == 'waiting':
                    waiting.append(val)
                elif kind == 'failed':
                    failed.append(p)
        enforce = mode == 'enforce'
        lines = []
        for p, holder, said in blocked:
            if enforce:
                lines.append(f'{said}. Do not edit it: leave it to {holder}, or ask {holder} with ask(to="{holder}", '
                             f'run_id="{run}", agent="{agent}").')
            else:
                lines.append(f'{said}. Another agent is working on this file: leave it to {holder}, or ask {holder} '
                             f'before you change it.')
        for p, name, on in waiting:
            # a waiting_on answer is treated like another agent's hold: named in advisory, denied in enforce
            said = f'{p} belongs to task "{name}", which waits on {", ".join(on)}: not finished yet'
            lines.append(f'{said}. Do not edit it until they are finished.' if enforce else
                         f'{said}. Leave it until they are finished.')
        if failed:
            lines.append(('Relay did not answer in time, so ' + ', '.join(failed) + ' could not be held for you'
                          + ('; the edit was not made. Try the edit again.' if enforce else '.')))
        if enforce and (blocked or failed or waiting):
            return _deny(f'CtrlB Relay (run {run}): ' + ' '.join(lines))
        if not lines and not briefs:
            return None
        text = ' '.join(([f'CtrlB Relay (run {run}): ' + ' '.join(lines)] if lines else []) + briefs)
        return context('PreToolUse', text)

    def _claim_on_write(self, p, claimed_tasks):
        """claim(resource=p) for this agent; on belongs_to_task the task instead. ('held', None), ('brief', text),
        ('blocked', (path, holder, said)), ('waiting', (path, task, [unfinished task names])) or ('failed', None)."""
        at = time.time()
        ans = self._call('claim', p, lambda: common.claim(self.cfg, self.run, self.agent, p, cwd=self.cwd, timeout=CLAIM_S))
        if ans.get('held') is True:
            record_tokens(self.run, self.agent, 'claim', ans)
            self.change(lambda d: self._take_hold(d, p, ans, at))
            return 'held', None
        if ans.get('reason') == 'belongs_to_task' and ans.get('task'):
            name = ans['task']
            if name in claimed_tasks:
                return 'held', None
            claimed_tasks.add(name)
            at = time.time()
            t = self._call('claim', name, lambda: common.claim(self.cfg, self.run, self.agent, name, cwd=self.cwd,
                                                                timeout=CLAIM_S))
            if t.get('held') is True:
                record_tokens(self.run, self.agent, 'claim', t)
                task = self.change(lambda d: self._take_task(d, t, name=name, at=at))
                return 'brief', brief_text(task)
            if t.get('reason') == 'waiting_on':
                on = [w for w in t.get('waiting_on') or [] if isinstance(w, str)] or ['other tasks']
                return 'waiting', (p, name, on)
            if t.get('finished_by') == self.agent and not t.get('held_by'):
                # the finisher keeps its files (docs/relay-hooks.md): a plain path hold, and the edit goes ahead
                return self._finisher_hold(p)
            holder = t.get('held_by') or t.get('finished_by') or ans.get('held_by') or ''
            if holder:
                said = (f'{p} belongs to task "{name}", finished by {holder}' if t.get('finished_by') and not t.get('held_by')
                        else f'{p} belongs to task "{name}", held by {holder}')
                return 'blocked', (p, holder, said)
            return 'failed', None
        if ans.get('held_by') or ans.get('finished_by'):
            holder = ans.get('held_by') or ans.get('finished_by')
            return 'blocked', (p, holder, f'{p} is held by {holder}')
        return 'failed', None

    def _finisher_hold(self, p):
        """This agent finished the task p belongs to: claim resource=p, a plain path hold (a Seeker with the
        after-done rule answers held, after_done). The edit goes ahead unless another agent holds p; with an older
        Seeker that refuses the path again, it goes ahead with no hold."""
        at = time.time()
        try:
            ans = self._call('claim', f'{p} (after done)', lambda: common.claim(self.cfg, self.run, self.agent, p,
                                                                                 cwd=self.cwd, timeout=CLAIM_S))
        except BaseException as e:
            if isinstance(e, KeyboardInterrupt):
                raise
            return 'held', None
        if ans.get('held') is True:
            record_tokens(self.run, self.agent, 'claim', ans)
            self.change(lambda d: self._take_hold(d, p, ans, at))
        elif ans.get('held_by') and ans['held_by'] != self.agent:
            return 'blocked', (p, ans['held_by'], f'{p} is held by {ans["held_by"]}')
        return 'held', None

    def _renew(self, st, p, key, own, renewed):
        """Own hold within RENEW_AT of its lease end: claim again, which renews (the task for a file of a held task,
        else the path). The lease end is the earlier of check's and the held task's; no injection."""
        hold = st['holds'].get(key) or {}
        task = st['tasks'].get(hold.get('task') or '') if hold.get('task') else None
        ends = [own.get('lease_until') or 0]
        lease = own.get('lease_s') or hold.get('lease_s') or 0
        if task:
            ends.append(task.get('lease_until') or 0)
            lease = task.get('lease_s') or lease
        ends = [e for e in ends if e]
        until = min(ends) if ends else 0
        resource = task['name'] if task else p
        if not until or not lease or resource in renewed:
            return
        if _now_us() < until - RENEW_AT * lease * 1e6:
            return
        renewed.add(resource)
        at = time.time()
        try:
            ans = self._call('claim', f'{resource} (renew)', lambda: common.claim(self.cfg, self.run, self.agent, resource,
                                                                                   cwd=self.cwd, timeout=CLAIM_S))
        except BaseException as e:
            if isinstance(e, KeyboardInterrupt):
                raise
            return
        if ans.get('held') is True:
            record_tokens(self.run, self.agent, 'claim', ans)
            if task:
                self.change(lambda d: self._take_task(d, ans, name=task['name'], at=at))
            else:
                self.change(lambda d: self._take_hold(d, p, ans, at))

    def post_bash(self, command, code, line):
        """PostToolUse (or PostToolUseFailure) on Bash: send a done that failed earlier, then, when the command names
        a held task's test and exited 0, done with that run as witnessed evidence and next=true. Returns the hook
        output or None."""
        if not self.cfg.get('run') or not self.run or not self.agent:
            return None
        st = self.read()
        texts = []
        pending = st.get('pending_done')
        if pending and pending.get('task') in st['tasks']:
            texts.append(self._done(pending['task'], pending['evidence']))
        elif pending:
            self.change(lambda d: d.update(pending_done=None))
        if isinstance(command, str):
            for name, t in self.read()['tasks'].items():
                if not (t.get('test') and names_test(command, t['test'])):
                    continue
                if not test_alone(command, t['test']):
                    # its exit code may be another command's (`test | tail -8` exits 0 on a failing test): no done,
                    # whatever the exit code; said once per task per session
                    texts.append(self._nudge(name, t['test']))
                elif code == 0:
                    ev = dict(command=command.strip()[:COMMAND_MAX], exit_code=0, summary=(line or '')[:SUMMARY_MAX])
                    texts.append(self._done(name, ev))
                break
        texts = [t for t in texts if t]
        return context('PostToolUse', '\n'.join(texts)) if texts else None

    def _nudge(self, name, test):
        """The one line for a run that named the task's test but did not run it alone; '' after the first time for
        this task in this session (the state file's nudged list)."""
        def fn(d):
            if name in (d.get('nudged') or []):
                return False
            d['nudged'] = (d.get('nudged') or []) + [name]
            return True
        if not self.change(fn):
            return ''
        return (f'CtrlB Relay: that run did not count as the test of task "{name}" because the test was not run alone. '
                f'Run it exactly as written, on its own line: {test}')

    def _done(self, name, ev):
        """done(resource=name, evidence=ev, witnessed=true, next=true). Returns the text to inject ('' for none). A
        failed call is kept as pending_done and sent again on the next Bash event."""
        at = time.time()
        try:
            ans = self._call('done', name, lambda: common.done(self.cfg, self.run, self.agent, name, ev, cwd=self.cwd,
                                                                witnessed=True, next_=True, timeout=DONE_S))
        except BaseException as e:
            if isinstance(e, KeyboardInterrupt):
                raise
            self.change(lambda d: d.update(pending_done=dict(task=name, evidence=ev)))
            return ''
        self.change(lambda d: d.update(pending_done=None, last_done_ts=time.time()))
        if ans.get('done') is not True:
            msg = ans.get('message') or ans.get('reason') or 'no reason given'
            return f'CtrlB Relay (run {self.run}): task "{name}" was not marked finished: {msg}'
        record_tokens(self.run, self.agent, 'done', ans)
        nxt = ans.get('next') if isinstance(ans.get('next'), dict) else {}

        def fn(d):
            self._drop_task(d, name)
            if nxt.get('held') is True:
                return self._take_task(d, nxt, at=at)
            return None
        task = self.change(fn)
        head = f'CtrlB Relay: task "{name}" is finished, with your test run as the record.'
        if task:
            record_tokens(self.run, self.agent, 'claim', nxt)
            return brief_text(task, lead=head + ' Next: ')
        if nxt.get('reason') == 'awaiting_answers':
            return head + ' The free tasks wait for answers to open questions: ' + (nxt.get('message') or '')
        if nxt.get('reason') == 'waiting_on':
            on = [w for w in nxt.get('waiting_on') or [] if isinstance(w, str)]
            return (head + ' No other task is free now: the open tasks wait on ' + (', '.join(on) or 'other tasks')
                    + ', not finished yet; the agent that finishes the last of them is handed the next one.')
        return head + ' No other task is free in this run.'
