"""Tests for ctrlb-agent. Run: python3 -I tests/run_tests.py

CtrlB is a local fake that answers with the synthetic responses in tests/fixtures/seeker/.
Nothing is sent anywhere else, Claude is never called, and no real config is read: every CTRLB_* variable from the
calling environment is dropped and CTRLB_HOME points into tests/work/."""
import base64, glob, hashlib, http.server, json, os, re, shutil, signal, socket, socketserver, stat, subprocess, sys, textwrap, threading, time, traceback
from unittest import mock
sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FIX = os.path.join(HERE, 'fixtures')
WORK = os.path.join(HERE, 'work')
HOOK = os.path.join(ROOT, 'plugin', 'hooks', 'hook.py')
CODEX_HOOK = os.path.join(ROOT, 'codex', 'hook.py')
INSTALL = os.path.join(ROOT, 'codex', 'install.py')
SID = '5f0c7a52-0000-4000-8000-000000000001'
CSID = '0190f0a0-1111-7000-8000-000000000001'        # the thread id of tests/fixtures/codex-rollout.jsonl
CSUB = '01a0aaaa-0000-7000-8000-000000000002'        # a subagent thread of it, written by the tests
KEY = 'test-key-not-a-real-key'


def fake_jwt(claims):
    """A JWT-shaped key built here from made-up claims, with a made-up signature: never a real key."""
    def enc(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip('=')
    return enc(dict(alg='RS256', typ='JWT')) + '.' + enc(claims) + '.' + enc(dict(sig='made-up'))


KEY_USER = 'dev.one@example.test'
JWT_KEY = fake_jwt(dict(sub='user-0001', email=KEY_USER, typ='mcp', workspaceId='ws-test', perm_version=1, role='member'))
JWT_NOCLAIM = fake_jwt(dict(sub='user-0002', typ='mcp', workspaceId='ws-test', perm_version=1, role='member'))
# the fake CtrlB's keys and the user each one belongs to; a row naming any other user_name is refused with 403
KEYS = {KEY: 'fixture-owner@example.test', JWT_KEY: KEY_USER, JWT_NOCLAIM: 'user-two@example.test'}
sys.path.insert(0, os.path.join(ROOT, 'lib'))
os.environ['CTRLB_HOME'] = os.path.join(WORK, 'home-import')
import common  # noqa: E402
import redact  # noqa: E402
import rows_claude  # noqa: E402

results = []


def check(name, ok, detail=''):
    results.append((name, bool(ok), detail))
    print(('PASS ' if ok else 'FAIL ') + name + (f'  ({detail})' if detail else ''))


def run_test(test, *args):
    try:
        test(*args)
    except Exception:
        check(test.__name__ + ': unexpected exception', False, traceback.format_exc(limit=8).strip())


def test_runner_failure_reporting():
    import contextlib
    import io

    def broken_check():
        check('probe', common.local_health(dict(url='http://127.0.0.1', key=KEY)) == 200)

    def later_check():
        check('later check', True)

    recorded, output = [], io.StringIO()
    with mock.patch.dict(run_test.__globals__, results=recorded), contextlib.redirect_stdout(output), \
            mock.patch.object(common, 'local_health', side_effect=common.urllib.error.URLError('timed out')):
        run_test(broken_check)
        run_test(later_check)
    check('test runner: a probe exception records FAIL with a traceback and the next test runs',
          len(recorded) == 2 and not recorded[0][1] and recorded[1][1]
          and 'FAIL broken_check' in output.getvalue() and 'Traceback' in recorded[0][2]
          and 'URLError' in recorded[0][2] and 'timed out' in recorded[0][2])


def load(name):
    with open(os.path.join(FIX, name)) as fh:
        return json.load(fh)


# ---------------------------------------------------------------- fake CtrlB

class StubHTTPServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 64

    def server_bind(self):
        # HTTPServer resolves the host before listen(); loopback fixtures need no reverse DNS.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


class StubRequestHandler(http.server.BaseHTTPRequestHandler):
    timeout = 2


class Seeker:
    """Answers the routes ctrlb-agent calls from tests/fixtures/seeker/. state['check'] picks the check answer;
    'slow' sleeps 5 s before answering."""

    def __init__(self):
        self.records, self.check_fx, self.runs_fx = load('seeker/records.json'), load('seeker/check.json'), load('seeker/runs.json')
        self.calls, self.rows, self.refused, self.state = [], [], [], dict(check='allow', handoff='ok')
        # Relay routes: each answers 'ok', or 'slow' (sleeps SLOW_S, longer than any hook budget), 'error' (500),
        # 'missing' (404, a CtrlB without the route); claims also 'held' (another agent holds it).
        self.state.update(briefs='ok', claims='ok', release='ok', posts='ok', tasks='ok', contracts='ok', peers='ok',
                          artifacts='ok')
        outer = self

        class H(StubRequestHandler):
            def log_message(self, *a):
                pass

            def _answer(self, status, body):
                raw = body if isinstance(body, bytes) else json.dumps(body).encode()
                try:
                    self.send_response(status)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def _handle(self, method):
                n = int(self.headers.get('Content-Length') or 0)
                raw = self.rfile.read(n) if n else b''
                body = json.loads(raw) if raw else None
                outer.calls.append(dict(method=method, path=self.path, auth=self.headers.get('Authorization'), body=body,
                                        size=len(raw)))
                auth = self.headers.get('Authorization') or ''
                owner = KEYS.get(auth[len('Bearer '):]) if auth.startswith('Bearer ') else None
                if owner is None:
                    return self._answer(**outer.records['responses']['no_key'])
                if self.path == '/healthz':
                    health = outer.state.get('health', 'ok')
                    if health == 'slow':
                        time.sleep(5)
                    return self._answer(503 if health == 'down' else 200, {'status': 'ok'})
                m = re.match(r'^/ai/sessions/([A-Za-z0-9_]+)/records$', self.path)
                if m and method == 'POST':
                    lim = outer.records['limits']
                    if not isinstance(body, list) or len(body) > lim['max_rows'] or len(raw) > lim['max_bytes']:
                        return self._answer(**outer.records['responses']['too_many_rows'])
                    if any(isinstance(r, dict) and r.get('user_name') not in (None, '', owner) for r in body):
                        outer.refused.append(body)
                        return self._answer(**outer.records['responses']['other_user'])
                    if outer.state.get('records_answer'):
                        status, answer = outer.state['records_answer'](body)
                        if status == 202 and isinstance(answer, dict) and answer.get('accepted') == len(body):
                            outer.rows.extend(body)
                        return self._answer(status, answer)
                    outer.rows.extend(body)
                    return self._answer(202, dict(outer.records['responses']['accepted']['body'], accepted=len(body)))
                m = re.match(r'^/ai/relay/([A-Za-z0-9_]+)/runs/([^/]+)/check$', self.path)
                if m and method == 'POST':
                    s = outer.state['check']
                    if s == 'slow':
                        time.sleep(5)
                        s = 'allow'
                    return self._answer(**outer.check_fx['responses'][s])
                m = re.match(r'^/ai/relay/([A-Za-z0-9_]+)/runs/([^/]+)/handoffs$', self.path)
                if m and method == 'POST':
                    s = outer.state['handoff']
                    if s == 'slow':
                        time.sleep(5)
                    if s == 'error':
                        return self._answer(500, {'error': 'boom'})
                    return self._answer(200, {'handed': True, 'open': True, 'released': ['name:task 07 latency', 'path:src/lat.py'],
                                              'tasks': ['task 07 latency'], 'message': 'handed off 1 task, released 1 key'})
                if self.path.startswith('/ai/relay/') and self.path.split('?')[0].endswith('/runs') and method == 'GET':
                    return self._answer(**outer.runs_fx['responses']['ok'])
                m = re.match(r'^/ai/relay/([A-Za-z0-9_]+)/runs/([^/?]+)/(briefs|claims|posts|tasks|contracts|peers|artifacts)(?:\?(.*))?$', self.path)
                if m:
                    route = 'release' if m.group(3) == 'claims' and method == 'DELETE' else m.group(3)
                    s = outer.state.get(route, 'ok')
                    if s == 'slow':
                        time.sleep(SLOW_S)
                    if s == 'error':
                        return self._answer(500, {'error': 'boom'})
                    if s == 'missing':
                        return self._answer(404, {'error': 'no route'})
                    return self._answer(*r8_answer(route, method, body, m.group(4) or '', s))
                return self._answer(404, {'error': 'no route'})

            def do_POST(self):
                self._handle('POST')

            def do_GET(self):
                self._handle('GET')

            def do_DELETE(self):
                self._handle('DELETE')

        self.httpd = StubHTTPServer(('127.0.0.1', 0), H)
        self.url = f'http://127.0.0.1:{self.httpd.server_address[1]}'
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def reset(self):
        self.calls.clear()
        self.rows.clear()
        self.refused.clear()


SLOW_S = 5
R8_ARTIFACT = '# plan\nstep 1: parse\n'


def r8_answer(route, method, body, query, state):
    """(status, body) of the fake Relay routes."""
    body = body if isinstance(body, dict) else {}
    if route == 'briefs' and method == 'POST':
        res = body.get('resource') or ''
        return 201, {'ok': True, 'key': res if res.startswith('contract:') else 'name:' + ' '.join(res.lower().split()),
                     'resource': res, 'paths': ['path:' + p for p in body.get('paths') or []]}
    if route == 'claims':
        res = body.get('resource') or ''
        k = 'slot:' + res[len('slot:'):].lower() if res.startswith('slot:') else res
        if state == 'held':
            return 200, {'held': False, 'resource': res, 'key': k, 'held_by': 'agent-b', 'message': f'{res} is already held by agent-b'}
        return 200, {'held': True, 'resource': res, 'key': k, 'paths': ['path:' + p for p in body.get('paths') or []],
                     'token': 'tok-slot', 'lease_until': 1793000120000000}
    if route == 'release':
        res = body.get('resource') or ''
        return 200, {'released': 1, 'finished': False, 'resource': res, 'key': 'slot:' + res[len('slot:'):].lower(),
                     'paths': ['path:REGISTRY.md']}
    if route == 'posts':
        return 201, {'ok': True, 'key': body.get('key', '')}
    if route == 'tasks':
        return 200, {'tasks': [
            {'name': 'task 01 parse', 'key': 'name:task 01 parse', 'state': 'held', 'held_by': 'agent-b', 'stalled': True,
             'test': 'python3 -m pytest tests/test_parse.py', 'paths': ['path:src/parse.py']},
            {'name': 'task 02 emit', 'key': 'name:task 02 emit', 'state': 'open', 'handed_off': True,
             'handoff_text': 'Session ended. Edited: src/emit.py. Last test: none run.', 'handoff_by': 'agent-c'},
            {'name': 'task 00 setup', 'key': 'name:task 00 setup', 'state': 'finished', 'finished_by': 'agent-b'}],
            'free_tasks': 1}
    if route == 'contracts':
        return 200, {'contracts': [{'name': 'parse api', 'key': 'contract:parse api', 'text': 'parse(s) -> Tree', 'paths': ['path:src/parse.py']}]}
    if route == 'peers':
        if 'kind=handoff' in query:
            return 200, {'you': '', 'events': [{'agent': 'agent-c', 'kind': 'HANDOFF', 'resource': 'task 04 render', 'key': 'name:task 04 render',
                                                'to': 'agent-1', 'text': 'render: tests pass, docs left'},
                                               {'agent': 'agent-c', 'kind': 'HANDOFF', 'resource': 'task 09 other', 'to': 'agent-z', 'text': 'not yours'}]}
        return 200, {'you': '', 'events': [{'agent': 'agent-1', 'kind': 'NOTE', 'text': 'parse: tokenizer done', 'key': ''},
                                           {'agent': 'agent-b', 'kind': 'NOTE', 'text': 'someone else', 'key': ''},
                                           {'agent': 'agent-1', 'kind': 'NOTE', 'text': 'artifact body', 'key': 'artifact:x.md'}]}
    if route == 'artifacts':
        return 200, {'artifacts': [{'key': 'artifact:plan.md', 'path': 'plan.md', 'agent': 'agent-1',
                                    'hash': 'sha256:' + hashlib.sha256(R8_ARTIFACT.encode()).hexdigest(), 'text': R8_ARTIFACT, 'ts': 1},
                                   {'key': 'artifact:../escape.md', 'path': '../escape.md', 'agent': 'agent-1', 'text': 'no', 'ts': 1}]}
    return 404, {'error': 'no route'}


def closed_port():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ---------------------------------------------------------------- helpers

def base_env(url, home, **extra):
    env = {k: v for k, v in os.environ.items() if not k.startswith('CTRLB_') and k != 'CLAUDE_ENV_FILE'}
    env.update(CTRLB_HOME=home, CTRLB_URL=url, CTRLB_KEY=KEY)
    env.update(extra)
    return env


def hook(mode, inp, env, timeout=20, script=HOOK):
    t = time.monotonic()
    p = subprocess.run([sys.executable, '-I', script, mode], input=inp if isinstance(inp, str) else json.dumps(inp),
                       capture_output=True, text=True, env=env, timeout=timeout)
    return p, time.monotonic() - t


def chook(mode, inp, env, timeout=20):
    return hook(mode, inp, env, timeout, CODEX_HOOK)


def out_of(p):
    try:
        return json.loads(p.stdout or '{}').get('hookSpecificOutput', {})
    except ValueError:
        return {'_unparsed': p.stdout}


def setup_transcripts():
    shutil.rmtree(WORK, ignore_errors=True)
    proj = os.path.join(WORK, 'projects', '-work-dash')
    os.makedirs(os.path.join(proj, SID, 'subagents'))
    main = os.path.join(proj, SID + '.jsonl')
    sub = os.path.join(proj, SID, 'subagents', 'agent-abc123.jsonl')
    shutil.copy(os.path.join(FIX, 'claude', 'session.jsonl'), main)
    shutil.copy(os.path.join(FIX, 'claude', 'session', 'subagents', 'agent-abc123.jsonl'), sub)
    return main, sub


def inputs(main, sub):
    raw = json.dumps(load('hooks/claude.json')).replace('{transcript}', main).replace('{subagent_transcript}', sub)
    return json.loads(raw)


def line(t, content, ts, extra=None):
    r = dict(type=t, message=dict(role=t, content=content), timestamp=ts, sessionId=SID, cwd='/work/dash', gitBranch='feat/dash')
    r.update(extra or {})
    return json.dumps(r) + '\n'


def planted():
    """Secret-shaped strings, built here so that no file in the repo holds one."""
    return ['AKIA' + 'ABCDEFGHIJKLMNOP', 'sk-' + 'ant-api03-abcdefghijklmnopqrstuvwx', 'gh' + 'p_abcdefghijklmnopqrstuvwxyz0123',
            'xo' + 'xb-1234567890-abcdefghij', 'hunter2' + 'pass', 'Zm9vYmFy' + 'YmF6cXV4', 's3cr3t' + 'value99',
            'ctrlbkey' + 'value0000']


# ---------------------------------------------------------------- tests

def task_claim_first(text, name_claim, file_claim):
    """The rule text names the task-name claim before the file claim, and both exist."""
    i, j = text.find(name_claim), text.find(file_claim)
    return 0 <= i < j


REGISTERED = 'Claim the task by the name your orchestrator registered'


def registered_rule(text, md=False):
    """Claim the registered task and its files together, finish with evidence, and release unfinished work.
    A file of no task is claimed by its path; no separate claim of the task's file."""
    text = text.replace('`', '')
    return (REGISTERED in text and 'Its files come with it' in text and 'Work only while you hold it.' in text
            and 'call done on the task name' in text and 'as the evidence, and next=true' in text
            and 'release with finished=false to give a task back' in text
            and 'release it with finished=true' not in text and 'A file that belongs to no task is claimed by its path' in text
            and 'Then claim the file path the task names' not in text and 'Work only while you hold both' not in text)


def recall_rule(text, sid):
    """The session-start recall rule: search before the first grep, file read or shell command about earlier work,
    find_command after a failed command, then next_actions on what it returns, and the session id to pass on to subagents."""
    return ("call `search_sessions` without session_id to search this user's sessions." in text
            and f"Pass session_id {sid} only to search this session's own turns before compaction." in text
            and 'Before your first grep, file read or shell command' in text
            and 'After a command fails, call `find_command` with the command before changing it, then `next_actions` with a session_id and seq it returns to see what followed.' in text
            and 'Pass this session_id to every subagent you start' in text)


def test_layout():
    pj = load('../../plugin/.claude-plugin/plugin.json')
    check('plugin.json: name ctrlb-agent, version 0.2.2', pj.get('name') == 'ctrlb-agent' and pj.get('version') == '0.2.2')
    mk = load('../../.claude-plugin/marketplace.json')
    ent = mk.get('plugins') or []
    check('marketplace.json: one entry, ctrlb-agent at ./plugin', len(ent) == 1 and ent[0]['name'] == 'ctrlb-agent'
          and ent[0]['source'] == './plugin')
    hk = load('../../plugin/hooks/hooks.json')['hooks']
    events = {'SessionStart': 'session-start', 'SubagentStart': 'subagent-start', 'PreCompact': 'flush', 'SessionEnd': 'session-end',
              'Stop': 'upload', 'SubagentStop': 'upload', 'PreToolUse': 'pre-tool', 'PostToolUse': 'post-tool',
              'PostToolUseFailure': 'post-tool-failure'}
    ok = set(hk) == set(events) and all(hk[e][0]['hooks'][0]['command'].endswith('hook.py" ' + m) for e, m in events.items())
    check('hooks.json: the six hooks plus PreToolUse, PostToolUse and PostToolUseFailure, each with its mode (SessionEnd: session-end)',
          ok, ','.join(sorted(hk)))
    check('hooks.json: PostToolUse matcher is the ctrlb claim, handoff and release tools, under both server names',
          hk['PostToolUse'][0].get('matcher') == 'mcp__ctrlb__claim|mcp__plugin_ctrlb-agent_ctrlb__claim|mcp__ctrlb__handoff|'
          'mcp__plugin_ctrlb-agent_ctrlb__handoff|mcp__ctrlb__release|mcp__plugin_ctrlb-agent_ctrlb__release')
    # A plugin install names the tools mcp__plugin_ctrlb-agent_ctrlb__<tool>; a server added by hand, mcp__ctrlb__<tool>.
    # Every matcher that names one must name the other, or the hook never runs on that kind of install.
    twins = []
    for e in hk.values():
        for g in e:
            names = (g.get('matcher') or '').split('|')
            for n in names:
                for short, long_ in (('mcp__ctrlb__', 'mcp__plugin_ctrlb-agent_ctrlb__'),
                                     ('mcp__plugin_ctrlb-agent_ctrlb__', 'mcp__ctrlb__')):
                    if n.startswith(short) and long_ + n[len(short):] not in names:
                        twins.append(n)
    check('hooks.json: every ctrlb tool in a matcher is listed under both server names', not twins, ','.join(twins))
    check('hooks.json: PreToolUse matcher Edit|Write|MultiEdit|NotebookEdit',
          hk['PreToolUse'][0].get('matcher') == 'Edit|Write|MultiEdit|NotebookEdit')
    check('hooks.json: every command runs python3 -I', all(h['command'].startswith('python3 -I ') for e in hk.values() for g in e for h in g['hooks']))
    mcp = load('../../plugin/.mcp.json')
    srv = (mcp.get('mcpServers') or mcp).get('ctrlb', {})
    check('.mcp.json: ctrlb http server, section 6 url and headers; run and agent default to empty when unset',
          'mcpServers' in mcp and srv.get('type') == 'http' and srv.get('url') == '${CTRLB_MCP_URL}' and srv.get('headers') == {
              'Authorization': 'Bearer ${CTRLB_KEY}', 'X-CtrlB-Run': '${CTRLB_RUN:-}', 'X-CtrlB-Agent': '${CTRLB_AGENT:-}'},
          json.dumps(srv.get('headers')))
    skills = {n: open(os.path.join(ROOT, 'plugin', 'skills', n, 'SKILL.md')).read() for n in ('setup', 'recall', 'relay')}
    check('skills setup, recall, relay have front matter with their names',
          all(s.startswith('---\n') and f'\nname: {n}\n' in s for n, s in skills.items()))
    relay = skills['relay']
    check('relay skill: run id argument and the three rules', '$ARGUMENTS' in relay and all(f'\n{i}. ' in relay for i in (1, 2, 3)))
    check('relay skill: rule 3 claims the registered task, its files come with it', registered_rule(relay, md=True))
    check('relay skill: files handed back with the task; unknown_task; reopen a finished task',
          'its files are handed back with it' in relay and 'Finish tasks with `done`, just release files' in relay
          and 'A file is never finished' in relay and '`reopen=true`' in relay and 'call `release` with `finished=true`' not in relay
          and '`reason: unknown_task`' in relay and '`suggestion`' in relay and '`paths`' in relay)
    check('no stdio recall server shipped', not glob.glob(os.path.join(ROOT, '**', 'recall_mcp.py'), recursive=True))
    check('plugin/lib and plugin/bin point at the shared lib and bin',
          os.path.realpath(os.path.join(ROOT, 'plugin', 'lib')) == os.path.realpath(os.path.join(ROOT, 'lib'))
          and os.path.realpath(os.path.join(ROOT, 'plugin', 'bin')) == os.path.realpath(os.path.join(ROOT, 'bin')))


def test_credential_redaction(seeker):
    import copy
    import rows_codex
    marker = 'synthetic-' + 'credential-value-001'
    shapes = [name + '=' + marker for name in ('TOKEN', 'KEY', 'SECRET', 'PASSWORD', 'token')]
    shapes += [json.dumps({name: value}) for name, value in
               [('token', marker), ('Authorization', 'Bearer ' + marker), ('x-api-key', marker)]]
    shapes += ['password="prefix ' + marker + ' suffix"', "SECRET='prefix " + marker + " suffix'",
               'cli --password "prefix ' + marker + ' suffix"', 'TOKEN=' + marker + '\n',
               'password=' + json.dumps('prefix "' + marker + '" suffix'),
               'PASSWORD="prefix ' + marker, 'Authorization: "prefix \'middle\' ' + marker + ' suffix"',
               'password="[REDACTED] ' + marker + '"', 'password="sk-' + ('x' * 20) + ' ' + marker + '"',
               'headers: ' + json.dumps({'Cookie': 'session=' + marker + '; theme=dark'})]
    check('credential assignments, JSON headers and complete quoted values are redacted',
          all(marker not in redact.redact(text)[0] and redact.redact(text)[1] for text in shapes))
    keep = ['max_tokens=5', 'token_count = 1234', 'KEY_RE = re.compile("pattern")',
            'input_tokens=1234', 'TOKEN=$VALUE', 'KEY=<your key>', 'token_count: 1234']
    check('redaction preserves code identifiers, counts, placeholders and variable references',
          all(redact.redact(text) == (text, 0) for text in keep))
    nested = {'items': [{'token': 'prefix "' + marker + '" suffix', 'headers':
                        {'Authorization': 'Bearer ' + marker, 'x-api-key': marker}}],
              'input_tokens': 1234, 'token_count': 5, 'enabled': True, 'text': shapes[0]}
    original = copy.deepcopy(nested)
    cleaned, count = redact.redact(nested)
    check('parsed credential dictionaries are redacted recursively without mutating the input',
          marker not in json.dumps(cleaned) and count == 4 and nested == original
          and cleaned['input_tokens'] == 1234 and cleaned['token_count'] == 5 and cleaned['enabled'] is True)
    check('redaction is idempotent for structured and quoted credentials',
          redact.redact(cleaned) == (cleaned, 0)
          and all(redact.redact(redact.redact(text)[0])[1] == 0 for text in shapes))
    home = os.path.join(WORK, 'home-credential-redaction')
    shutil.rmtree(home, ignore_errors=True)
    os.makedirs(home, exist_ok=True)
    cfg = dict(url=seeker.url, key=KEY, dataset=common.DEFAULT_DATASET, run='', agent='')
    claude = os.path.join(home, 'claude.jsonl')
    with open(claude, 'w') as fh:
        for i, text in enumerate(shapes):
            fh.write(line('user', [dict(type='tool_result', tool_use_id=str(i), content=text)], '2026-11-03T10:00:00Z'))
        fh.write(line('assistant', [dict(type='tool_use', id='write', name='Write', input=nested)], '2026-11-03T10:00:01Z'))
        fh.write(line('assistant', [dict(type='tool_use', id='bash', name='Bash', input={'command': shapes[0]})],
                      '2026-11-03T10:00:02Z'))
    codex = os.path.join(home, 'codex.jsonl')
    records = [dict(type='session_meta', payload={'id': 'credential-thread'})]
    records += [dict(type='response_item', payload=dict(type='function_call_output', call_id=str(i), output=text))
                for i, text in enumerate(shapes)]
    records += [dict(type='response_item', payload=dict(type='function_call', name='tool', arguments=json.dumps(nested))),
                dict(type='response_item', payload=dict(type='custom_tool_call', name='tool', input=nested)),
                dict(type='response_item', payload=dict(type='function_call_output', output=nested)),
                dict(type='response_item', payload=dict(type='function_call', name='exec_command',
                                                      arguments=json.dumps({'cmd': shapes[0]})))]
    with open(codex, 'w') as fh:
        fh.write(''.join(json.dumps(rec) + '\n' for rec in records))
    with mock.patch.object(common, 'HOME', home):
        for parser, path, label in ((rows_claude, claude, 'Claude'), (rows_codex, codex, 'Codex')):
            seeker.reset()
            counts = parser.upload_file(cfg, path, time.time() + 5)
            check(label + ' uploads redact credential text, command fields and parsed tool dictionaries',
                  counts['rows'] >= len(shapes) + 2 and counts['redacted'] >= len(shapes) + 4
                  and marker not in json.dumps(seeker.rows) and '[REDACTED]' in json.dumps(seeker.rows)
                  and any('"input_tokens": 1234' in r['body'] for r in seeker.rows))
    seeker.reset()


def test_redaction_literals():
    marker = 'abc' + '123456789'
    names = ('SECRET_KEY_BASE', 'DB_PASSWORD_PROD', 'GITHUB_TOKEN_ENTERPRISE', 'API_TOKEN_V2',
             'AWS_SECRET_ACCESS_KEY', 'DATABASE_PASSWORD', 'MY_API_KEY', 'CLIENT_SECRET', 'CTRLB_KEY')
    for name in names:
        text = name + '=' + marker
        check('credential assignment suffix and prefix: ' + name,
              redact.redact(text) == (name + '=[REDACTED]', 1))
    # The original positive formats, alongside the new bare-name cases.
    s = planted()
    positives = s[:4] + ['github_pat_' + ('a' * 24), 'eyJ' + ('a' * 12) + '.' + ('b' * 12) + '.' + ('c' * 12),
                        '-----BEGIN PRIVATE KEY-----\n' + marker + '\n-----END PRIVATE KEY-----',
                        'Authorization: Basic ' + s[5], 'Proxy-Authorization: Bearer ' + s[5],
                        'X-API-Key: alphabeticcredential', 'Cookie: session=' + marker + '; theme=dark',
                        'https://bob:' + marker + '@example.test', 'cli --token ' + marker,
                        'cli --api-key=' + marker, 'cli --password "prefix ' + marker + ' suffix"',
                        'aws_secret_access_key = "' + marker + '"', 'db_password: ' + marker]
    check('original credential formats remain redacted', all(redact.redact(t)[1] for t in positives))
    negatives = ['token = tokens[i]', 'self.token = token',
                 'csrf_token = request.form["csrf"]', '- token = get_token()', 'TOKEN = Token(kind, text)',
                 'PRIMARY_KEY = "id"', 'MONKEY = "banana"', 'HOTKEY = "ctrl+s"',
                 '{"KEY": "SORT"}', '{"token": null}', '{"cookie": true}',
                 'log {"token": false, "password": null, "cookie": true}']
    for text in negatives:
        check('ordinary code and data preserved: ' + text, redact.redact(text) == (text, 0))
    typed = {'token': None, 'cookie': True, 'password': False, 'SECRET': 1234, 'API_KEY': 1.5, 'KEY': 'SORT'}
    check('parsed credential keys mask numbers and preserve null, boolean and ordinary key values',
          redact.redact(typed) == (dict(typed, SECRET='[REDACTED]', API_KEY='[REDACTED]'), 2))
    for secret in (marker, 'S'):
        escaped = ['echo "TOKEN=\\"' + secret + '\\""',
                   'log {\\"password\\": \\"' + secret + '\\"}',
                   '{\\"x-api-key\\": \\"' + secret + '\\"}']
        for text in escaped:
            cleaned, n = redact.redact(text)
            check('escaped credential quotes are redacted: ' + text.split('=')[0].split(':')[0],
                  ('\\"' + secret + '\\"') not in cleaned and '\\"[REDACTED]\\"' in cleaned and n == 1
                  and redact.redact(cleaned) == (cleaned, 0))
    tail = '\nordinary code\nprint("keep this")\nend of file'
    for value in ("'it", '"it', '\\"it'):
        cleaned, n = redact.redact('PASSWORD=' + value + tail)
        check('unclosed credential quote preserves all following lines: ' + value,
              cleaned.endswith(tail) and 'it\n' not in cleaned and n == 1)


def test_redaction_assignment_safety():
    for text, expected, count in (
            ('{"password": 12345678}', '{"password": "[REDACTED]"}', 1),
            ('"password":12345678,', '"password":[REDACTED],', 1),
            ('{"db": {"password": 987654}}', '{"db": {"password": "[REDACTED]"}}', 1),
            ('"pin_password": 1234', '"pin_password": [REDACTED]', 1),
            ('log {"token": false, "password": 1234, "cookie": true}',
             'log {"token": false, "password": [REDACTED], "cookie": true}', 1)):
        check('numeric credential values are masked in JSON text and fragments',
              redact.redact(text) == (expected, count) and redact.redact(expected) == (expected, 0))
    original = {'db': {'password': 987654}, 'pin_password': 1234, 'input_tokens': 1234,
                'token': [0, -42, 1.5, None, True, False]}
    expected = {'db': {'password': '[REDACTED]'}, 'pin_password': '[REDACTED]', 'input_tokens': 1234,
                'token': ['[REDACTED]', '[REDACTED]', '[REDACTED]', None, True, False]}
    check('parsed numeric credentials are masked recursively without changing ordinary numbers or inputs',
          redact.redact(original) == (expected, 5) and original['db']['password'] == 987654
          and original['token'] == [0, -42, 1.5, None, True, False]
          and redact.redact(expected) == (expected, 0))
    for expression in ('resp.get("NextToken")', "resp.get('NextToken')"):
        text = 'next_token = ' + expression
        check('new credential names preserve spaced calls with quoted identifier arguments',
              redact.redact(text) == (text, 0))
        for prefix in ('next_token=', 'next_token: ', 'password = ', 'API_TOKEN = '):
            cleaned, n = redact.redact(prefix + expression)
            check('quoted call arguments do not exempt compact, YAML or legacy assignments',
                  cleaned == prefix + '[REDACTED]' + expression[len('resp.get('):] and n == 1)
    literals = ['DB_PASSWORD=Xy7!kq#2zz', 'POSTGRES_PASSWORD=S3cure@Pass', 'REDIS_PASSWORD=p@ssw0rd',
                'export API_TOKEN=abc@123!xyz', 'password: P@ssw0rd!',
                'spring.datasource.password=Xy7!kq#2', 'password=hunter', 'password: changeme',
                'db_password = correcthorse', 'api_key = AbCdEfGhIjKlMnOp',
                'SECRET_KEY = django_insecure_xyz', 'PASSWORD=foo.bar', 'SECRET=a.b.c',
                'API_KEY=sk_live_xxx(', 'token: string;', 'token = token', 'TOKEN = token',
                'token=get_token()', 'token=tokens[i]', 'token=request.token', 'A_KEY=alphabetic',
                'token = abc!@#$%^&*', 'password=abc;def', 'API_TOKEN=abc,def', 'API_TOKEN=abc}def',
                'password=abc\\def', 'foo..password=hunter', 'a.b.token = request.token',
                'service.private_key = keys[index]', 'MAX_TOKENS = 4096']
    for text in literals:
        prefix = re.split('[:=]', text, maxsplit=1)[0]
        separator = text[len(prefix):len(prefix) + 1]
        gap = ' ' if text[len(prefix) + 1:].startswith(' ') else ''
        cleaned, n = redact.redact(text)
        check('credential literals are masked regardless of punctuation or spacing: ' + prefix.strip(),
              cleaned == prefix + separator + gap + '[REDACTED]' and n == 1
              and redact.redact(cleaned) == (cleaned, 0))
    check('short generic key literals retain the original threshold with an incomplete quote',
          redact.redact('A_KEY="abcd') == ('A_KEY="[REDACTED]"', 1))
    check('token URL names retain conservative masking',
          redact.redact('TOKEN_URL = "https://example.test"') == ('TOKEN_URL = "[REDACTED]"', 1))
    for expression in ('get_token ()', 'tokens [i]'):
        check('ambiguous spaced expressions still mask the value',
              redact.redact('token = ' + expression) == ('token = [REDACTED] ' + expression.split(' ', 1)[1], 1))
    for name in ('password', 'api_key', 'db_password', 'API_TOKEN', 'PRIMARY_KEY'):
        for expression in ('get_token()', 'tokens[index]', 'request.token'):
            text = name + ' = ' + expression
            check('existing credential names mask code-shaped values: ' + name + ' ' + expression,
                  redact.redact(text) == (name + ' = [REDACTED]', 1))
    for name in ('token', 'csrf_token', 'private_key'):
        for expression in ('get_token()', 'tokens[index]', 'request.token', 'request.form["csrf"]'):
            text = name + ' = ' + expression
            check('new credential names preserve clearly spaced code: ' + name + ' ' + expression,
                  redact.redact(text) == (text, 0))
    tail = '\nordinary code\nprint("keep this")\nend of file'
    for separator in (':\n  ', '=\n\t', ' :\r\n  '):
        for value in ('"S"', "'S'", '\\"S\\"', '"S', "'S", '\\"S'):
            cleaned, n = redact.redact('password' + separator + value + tail)
            quote = '\\"' if value.startswith('\\"') else value[0]
            check('credential separators cross lines while quoted values stop at their line',
                  cleaned == 'password' + separator + quote + '[REDACTED]' + quote + tail and n == 1
                  and redact.redact(cleaned) == (cleaned, 0))
    for name in ('TOKEN', 'DB_PASSWORD', 'password', 'token', 'api_key', 'Authorization'):
        for operator in ('==', '!=', '<=', '>='):
            for text in ('if ' + name + ' ' + operator + ' "abc": pass', 'if ' + name + operator + 'abcdefgh: pass'):
                check('comparisons are preserved: ' + name + ' ' + operator, redact.redact(text) == (text, 0))
    for text in ('notauthorization: abcdefgh', 'Authorization: abcdefgh anotherword',
                 'Authorization: Negotiate abcdefgh', 'Authorization: Digest abcdefgh',
                 'X-Api-Key: $VARIABLE', 'X-Api-Key: <placeholder>'):
        cleaned, n = redact.redact(text)
        check('header coverage includes legacy shapes',
              n > 0 and 'abcdefgh' not in cleaned and '$VARIABLE' not in cleaned and '<placeholder>' not in cleaned)
    for key, value in (('token', ['s3cretvalue']), ('X-Api-Key', ['S']), ('token', {'v': 'S'}),
                       ('foo..password', 'hunter')):
        original = {key: value}
        expected = {key: ['[REDACTED]'] if isinstance(value, list) else
                    {'v': '[REDACTED]'} if isinstance(value, dict) else '[REDACTED]'}
        for text in (original, json.dumps(original)):
            cleaned, n = redact.redact(text)
            check('credential containers mask string leaves: ' + key,
                  (json.loads(cleaned) if isinstance(cleaned, str) else cleaned) == expected and n == 1
                  and redact.redact(cleaned) == (cleaned, 0) and original == {key: value})
    for key in ('Admin Password', 'db:password', '/api_key', '@password', '$TOKEN', 'Admin db_password',
                'db:/auth_token', '/private_key', '@client_secret', '$access_key'):
        for value in ('S', 'synthetic-value', 1234, {'nested': ['synthetic-value']}):
            original = {key: value}
            expected = {key: {'nested': ['[REDACTED]']} if isinstance(value, dict) else '[REDACTED]'}
            for text in (original, json.dumps(original)):
                clean, n = redact.redact(text)
                check('credential JSON keys accept spaces and punctuation before the name: ' + key,
                      (json.loads(clean) if isinstance(clean, str) else clean) == expected and n == 1
                      and redact.redact(clean) == (clean, 0) and original == {key: value})
    for name in ('DB_PASSWORD', 'API_TOKEN', 'SECRET_KEY_BASE', 'PRIMARY_KEY'):
        for value in ('abc,def', 'abc}def', 'abc]def', '[1,2]', 'abc;def', '$VARIABLE', '<placeholder>'):
            text = name + '=>' + value
            expected = name + '=>[REDACTED]'
            check('uppercase arrow assignments mask the complete shell value: ' + name,
                  redact.redact(text) == (expected, 1) and redact.redact(expected) == (expected, 0))
    for name in ('X-Auth-Token', 'X-Access-Token', 'Private-Token', 'X-Amz-Security-Token', 'X-Api-Key', 'Api-Key'):
        for value in ('abc}def', 'abc]def', '[1234]', 'abcdef,tail', '[1234,5678]'):
            for text in (name + ': ' + value, 'curl -H "' + name + ': ' + value + '" example.test'):
                clean, n = redact.redact(text)
                expected = text.replace(value, '[REDACTED]' + value[value.index(','):] if ',' in value else '[REDACTED]')
                check('credential headers retain complete brace and bracket values: ' + name,
                      clean == expected and n > 0 and redact.redact(clean) == (clean, 0))
    for text, expected in (('curl -H "password: null" example.test', 'curl -H "password: [REDACTED]" example.test'),
                           ("'SECRET_KEY=null", "'SECRET_KEY=[REDACTED]"),
                           ('TOKEN:=<placeholder>', 'TOKEN:=[REDACTED]'),
                           ('password=>$VARIABLE', 'password=>[REDACTED]'),
                           ('api_key=><placeholder>', 'api_key=>[REDACTED]')):
        check('non-JSON literals retain legacy redaction coverage', redact.redact(text) == (expected, 1))
    original = {'token': ['firstsecret', None, True, False, 1234, 1.5,
                          {'v': 'S', 'nested': ['lastsecret', False]}], 'ordinary': ['keep']}
    expected = {'token': ['[REDACTED]', None, True, False, '[REDACTED]', '[REDACTED]',
                          {'v': '[REDACTED]', 'nested': ['[REDACTED]', False]}], 'ordinary': ['keep']}
    for text in (original, json.dumps(original)):
        cleaned, n = redact.redact(text)
        check('credential containers preserve nulls, booleans and shape without mutating inputs',
              (json.loads(cleaned) if isinstance(cleaned, str) else cleaned) == expected and n == 5
              and original['token'][0] == 'firstsecret' and original['token'][-1]['v'] == 'S')
    original = {'password=hunter': 'ordinary', 'nested': {'API_TOKEN=abc@123!xyz': 1234}}
    expected = {'password=[REDACTED]': 'ordinary', 'nested': {'API_TOKEN=[REDACTED]': 1234}}
    for text in (original, json.dumps(original)):
        cleaned, n = redact.redact(text)
        check('embedded assignments in JSON keys are sanitized',
              (json.loads(cleaned) if isinstance(cleaned, str) else cleaned) == expected and n == 2
              and original['password=hunter'] == 'ordinary')


def test_codex_tool_input_preservation():
    import rows_codex
    cfg = dict(run='', agent='')
    ids = dict(sid='test-thread', parent='', agent_id='', run_id='', agent='test')
    st = dict(cwd='/work/example', branch='', names={}, prev_ts=0)

    def row(payload):
        rec = dict(type='response_item', payload=payload)
        r = rows_codex.rows_from(rec, 1, st, ids, cfg, 'test-host')[0]
        return rows_claude._finish(r, {'redacted': 0})

    raw = '{  "message" : "café 日本語", "count":2 }'
    r = row(dict(type='function_call', name='tool', arguments=raw))
    check('Codex function arguments retain original spacing and Unicode when unchanged', r['body'] == raw)
    marker = 'synthetic-' + 'tool-value-001'
    raw = '{  "message" : "café 日本語", "token":"' + marker + '" }'
    r = row(dict(type='function_call', name='tool', arguments=raw))
    check('Codex redacted function arguments keep Unicode characters',
          marker not in r['body'] and 'café 日本語' in r['body'] and json.loads(r['body'])['token'] == '[REDACTED]')
    patch = '*** Begin Patch\n*** Update File: src/café.py\n@@\n-x\n+y\n*** End Patch\n'
    command = 'pytest -q tests/'
    raw = {'nested': {'cmd': command, 'patch': patch}}
    r = row(dict(type='custom_tool_call', name='exec', input=raw))
    check('Codex dict custom input retains exec commands and nested patch paths',
          r['command'] == command and json.loads(r['paths']) == ['src/café.py'] and 'café.py' in r['body'])
    r = row(dict(type='custom_tool_call', name='apply_patch', input={'patch': patch}))
    check('Codex dict apply_patch input retains patch paths', json.loads(r['paths']) == ['src/café.py'])
    r = row(dict(type='custom_tool_call', name='exec', input={'cmd': command, 'patch': patch}))
    check('Codex dict custom input scans command fields as well as strings',
          r['command'] == command and json.loads(r['paths']) == ['src/café.py'])


def test_no_secrets_in_repo():
    # this file is left out: it builds its fake secrets by concatenation and passes fake keys to the hooks
    files = [os.path.join(d, n) for d, ds, ns in os.walk(ROOT) for n in ns]    # os.walk sees dot files; glob does not
    files = [f for f in files if os.path.isfile(f) and '/tests/work/' not in f and '/.git/' not in f and not f.endswith('.pyc')
             and os.path.abspath(f) != os.path.abspath(__file__)]
    hits = []
    for f in files:
        with open(f, errors='replace') as fh:
            text = fh.read()
        if redact.redact(text)[1]:
            hits.append(os.path.relpath(f, ROOT))
    check('no file in the repo matches a redaction pattern', not hits, ', '.join(hits))
    url_re = re.compile(r'https?://(?!127\.0\.0\.1|localhost|claude\.com|docs\.claude\.com|github\.com/ctrlb-hq)[A-Za-z0-9.-]+')
    urls = sorted({os.path.relpath(f, ROOT) for f in files for _ in [0] if url_re.search(open(f, errors='replace').read())})
    check('no real URL in the repo (placeholders only)', not urls, ', '.join(urls))
    words = ['black' + 'bo' + 'ard', 'bo' + 'ard', 'sw' + 'arm', 'lo' + 'ck', 'lo' + 'cks', 'lo' + 'cked']   # split: this file is scanned too
    banned = re.compile(r'(?i)\b(' + '|'.join(words) + r')\b')
    named = sorted({os.path.relpath(f, ROOT) for f in files + [os.path.abspath(__file__)]
                    if banned.search(open(f, errors='replace').read())})
    check('product names only: none of the retired research names', not named, ', '.join(named))


def test_hooks(seeker):
    main, sub = setup_transcripts()
    I = inputs(main, sub)
    home = os.path.join(WORK, 'home')
    env = base_env(seeker.url, home)

    # a session in a relay run gets the whole relay rule (CTRLB_RUN set; here the run id is the session id)
    p, _ = hook('session-start', I['session_start'], dict(env, CTRLB_RUN=SID))
    rctx = out_of(p).get('additionalContext', '')
    seeker.reset()
    # session-start: the rule names the session, run and agent ids
    p, dt = hook('session-start', I['session_start'], env)
    o = out_of(p)
    ctx = o.get('additionalContext', '')
    check('session-start: rule names session id, run id and agent id', o.get('hookEventName') == 'SessionStart'
          and f'session_id {SID}' in ctx and f'run id is {SID}' in ctx and f'agent id is {SID}' in ctx and p.returncode == 0)
    check('session-start with no run set: no task to find or stop rule, the ids for the relay tools',
          'no Relay tasks are waiting for you' in ctx and 'claim(next=true)' not in ctx and 'no task and stop' not in ctx
          and f'run_id="{SID}"' in ctx and 'helper scripts and binaries in the scratch directory' in ctx, ctx[-400:])
    check('session-start: claims the registered task, its files come with it, holds one claim', registered_rule(rctx))
    check('session-start: the recall rule names its moments (first grep, file read or shell command; a failed command)',
          recall_rule(ctx, SID))
    check('session-start: refusals, unknown_task, reopen a finished task',
          'held is false with finished_by, the task is done' in rctx and 'held is false with held_by, another agent has it' in rctx
          and 'If reason is unknown_task, claim again with the suggestion' in rctx and 'claim its name with reopen=true' in rctx)
    check('session-start: slots for things that are not files', 'is claimed as slot:<name> and released when you are done with it' in rctx
          and rctx.index('slot:<name>') < rctx.index('reopen=true'))
    check('relay skill and tools: slots, two-minute lease, never finished',
          'claimed as `slot:<name>` and released when you are done with it' in open(os.path.join(ROOT, 'plugin', 'skills', 'relay', 'SKILL.md')).read()
          and all(n in open(os.path.join(ROOT, 'docs', 'relay-tools.md')).read() for n in
                  ('## Slots', '`slot:devserver`', '`slot:port-3000`', '`slot:db-migrate`', '`slot:screenshots`', '`slot:commit`', 'lasts two minutes')))
    check('session-start: names the relay tools, contract and contracts among them',
          'relay tools (peers, post, claim, release, handoff, ask, answer, brief, briefs, tasks, contract, contracts, done)' in rctx)
    check('session-start: the coordination tools before a prose note, one sentence each',
          'Use the coordination tools before a prose note: handoff when you must leave a task you hold, so it moves to the agent '
          'you name in one step; ask when you are blocked on something only another agent or the orchestrator can decide; '
          "answer, with the question's id, when you can settle an open question; contract when you finish an interface others "
          'will build on (a type, a route, a config shape), with its name, the shape in a few lines and the files that define '
          "it. Before you build on a contract, read it with `contracts`. If it is stale, read the file it names and check your "
          "assumptions against it. Post a note only for what none of them covers." in rctx)
    check('session-start returns promptly', dt < 3.0, f'{dt:.2f}s')
    p, _ = hook('session-start', I['session_start_compact'], dict(env, CTRLB_RUN='relay-run-7', CTRLB_AGENT='builder-1'))
    ctx = out_of(p).get('additionalContext', '')
    check('session-start: CTRLB_RUN and CTRLB_AGENT win', 'run id is relay-run-7' in ctx and 'agent id is builder-1' in ctx
          and 'run_id="relay-run-7"' in ctx)
    check('session-start sends nothing to CtrlB', not seeker.calls)

    # Stop: main and subagent rows
    seeker.reset()
    p, dt = hook('upload', I['stop'], env)
    rows = list(seeker.rows)
    check('Stop upload sends main and subagent rows', len(rows) == 19 and p.returncode == 0, f'{len(rows)} rows, {dt:.2f}s')
    check('records go to POST /ai/sessions/agent_sessions/records with the Bearer key',
          seeker.calls and all(c['path'] == '/ai/sessions/agent_sessions/records' and c['auth'] == 'Bearer ' + KEY for c in seeker.calls))
    mainr = [r for r in rows if r['session_id'] == SID]
    subr = [r for r in rows if r['session_id'] == 'agent-abc123']
    by = {(r['seq'], r['block_idx']): r for r in mainr}
    check('run_id and agent default to the session id', all(r['run_id'] == SID and r['agent'] == SID for r in mainr))
    check('subagent rows: parent_session, agent_id, joins the parent run, agent is its own session id',
          len(subr) == 2 and all(r['parent_session'] == SID and r['agent_id'] == 'abc123' and r['run_id'] == SID
                                 and r['agent'] == 'agent-abc123' for r in subr))
    check('tool_result carries the tool name and error flag', by[(5, 0)]['tool_name'] == 'Bash' and by[(5, 0)]['is_error'] is True)
    check('Bash tool_use has the command', by[(4, 0)]['command'] == 'npm run build -- --filter=dash')
    paths = {r['tool_name']: r['paths'] for r in mainr if r['block_type'] == 'tool_use'}
    check('paths on Edit, Write, MultiEdit, NotebookEdit tool_use rows, relative to the row cwd', paths.get('Edit') == '["src/panels/Latency.tsx"]'
          and paths.get('Write') == '["src/panels/index.ts"]'
          and paths.get('MultiEdit') == '["src/panels/Latency.test.tsx"]'
          and paths.get('NotebookEdit') == '["notebooks/latency.ipynb"]', json.dumps(paths))
    check('paths empty on other rows', all(r['paths'] == '' for r in rows if r['tool_name'] not in rows_claude.EDIT_TOOLS
                                           or r['block_type'] != 'tool_use'))
    tin, tout = sum(r['input_tokens'] for r in mainr), sum(r['output_tokens'] for r in mainr)
    check('tokens counted once per message id (cache reads and writes in input)', tin == 10597.0 and tout == 750.0, f'{tin} in, {tout} out')
    check('subagent tokens', sum(r['input_tokens'] for r in subr) == 403.0 and sum(r['output_tokens'] for r in subr) == 15.0)
    cost = [r for r in mainr if r['block_type'] == 'result']
    check('cost-state becomes one result row with cost_usd', len(cost) == 1 and abs(cost[0]['cost_usd'] - 0.0123) < 1e-9
          and cost[0]['role'] == 'system')
    check('cost_usd, input_tokens, output_tokens are floats on every row',
          all(isinstance(r[k], float) for r in rows for k in ('cost_usd', 'input_tokens', 'output_tokens')))
    check('is_error and is_summary are booleans on every row', all(isinstance(r['is_error'], bool) and isinstance(r['is_summary'], bool) for r in rows))
    check('compaction summary flagged', any(r['is_summary'] for r in mainr))
    check('fields: harness, cwd, branch, host', all(r['harness'] == 'claude' and r['cwd'] == '/work/dash' and r['git_branch'] == 'feat/dash'
                                                    and r['host'] for r in rows))
    check('user_name left to CtrlB when the key carries no user', all('user_name' not in r for r in rows))
    cols = {'_timestamp', 'host', 'harness', 'session_id', 'parent_session', 'agent_id', 'run_id', 'agent', 'seq', 'block_idx',
            'role', 'block_type', 'tool_name', 'tool_use_id', 'command', 'paths', 'body', 'is_error', 'is_summary', 'cwd',
            'git_branch', 'cost_usd', 'input_tokens', 'output_tokens', 'body_len', 'truncated'}
    check('every row has the section 5 columns', all(set(r) == cols for r in rows), str(sorted(set(rows[0]) ^ cols)))

    # idempotent, append, partial line, cost delta
    seeker.reset()
    hook('upload', I['stop'], env)
    check('second upload sends nothing', not seeker.rows, f'{len(seeker.rows)} rows')
    with open(main, 'a') as fh:
        fh.write(line('assistant', [dict(type='text', text='appended pangolinstep line')], '2026-11-03T10:31:00Z'))
        fh.write(json.dumps(dict(type='cost-state', sessionId=SID, totalCostUSD=0.02)) + '\n')
        fh.write('{"type": "assistant", "partial')
    hook('flush', I['pre_compact'], env)
    new = list(seeker.rows)
    check('appended lines upload alone; partial line waits', len(new) == 2 and 'pangolinstep' in new[0]['body'], f'{len(new)} rows')
    check('cost_usd is the increase since the last cost record', len(new) == 2 and abs(new[1]['cost_usd'] - 0.0077) < 1e-9,
          str([r['cost_usd'] for r in new]))

    # redaction
    seeker.reset()
    s = planted()
    with open(main, 'a') as fh:
        fh.write('\n')   # completes the partial line (skipped: not JSON)
        fh.write(line('user', [dict(type='tool_result', tool_use_id='t1', is_error=True,
                                    content=f'export AWS_KEY={s[0]}\nCTRLB_KEY={s[7]}')], '2026-11-03T10:32:00Z'))
        fh.write(line('assistant', [dict(type='text', text=f'Secrets seen: {s[1]} {s[2]} {s[3]} PASSWORD={s[4]} '
                                                            f'Authorization: Basic {s[5]} https://bob:{s[6]}@db.example.com '
                                                            '{"input_tokens": 1234}')], '2026-11-03T10:32:05Z'))
        fh.write(line('assistant', [dict(type='tool_use', id='t20', name='Bash',
                                         input=dict(command=f'curl -H "Authorization: Bearer {s[7]}" x'))], '2026-11-03T10:32:09Z'))
    hook('upload', I['stop'], env)
    sent = json.dumps(seeker.rows)
    leaked = [x for x in s if x in sent]
    check('no planted secret reaches CtrlB (body and command)', not leaked and len(seeker.rows) == 3,
          f'leaked {leaked}' if leaked else f"{sent.count('[REDACTED]')} redactions")
    check('non-secret token counts kept', '\\"input_tokens\\": 1234' in sent)
    import redact as _redact
    pre = [_redact.redact(t)[1] for t in ('aws_secret_access_key = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"',
                                          'db_password: hunter2hunter', 'password=$PW', 'api_key=<your key>',
                                          'the access key rotation doc')]
    check('prefixed lower-case secret assignments are redacted (aws_secret_access_key = "...")', pre == [1, 1, 0, 0, 0], str(pre))

    # credential headers and flags, through the upload path: the Bash command field and the text body
    seeker.reset()
    v = ['hdrkey' + 'value0001', 'authtok' + 'value0002', 'cookie' + 'value0003', 'flagtok' + 'value0004',
         'apiflag' + 'value0005', 'pwflag' + 'value0006', 'digest' + 'value0007', 'proxy' + 'value0008']
    cmds = [f'curl -H "X-API-Key: {v[0]}" https://api.example.com/v1',
            f'http GET api.example.com X-Auth-Token:{v[1]}',
            f'curl --header "Cookie: session={v[2]}; theme=dark" https://app.example.com',
            f'gh auth login --token {v[3]} --hostname example.com',
            f'tool --api-key={v[4]} sync',
            f'mysql --password "{v[5]}" -u root',
            f'curl -H "Authorization: Digest {v[6]}" -H "Proxy-Authorization: Basic {v[7]}" https://example.com']
    with open(main, 'a') as fh:
        for i, c in enumerate(cmds):
            fh.write(line('assistant', [dict(type='tool_use', id=f't3{i}', name='Bash', input=dict(command=c))],
                          f'2026-11-03T10:33:{i:02d}Z'))
        fh.write(line('assistant', [dict(type='text', text='Ran: ' + ' ; '.join(cmds))], '2026-11-03T10:33:30Z'))
    hook('upload', I['stop'], env)
    sent = json.dumps(seeker.rows)
    leaked = [x for x in v if x in sent]
    rc = [r['command'] for r in seeker.rows if r.get('command')]
    check('credential headers (X-API-Key, X-Auth-Token, Cookie, Authorization with any scheme, Proxy-Authorization) and flags '
          '(--token, --api-key=, --password "...") never reach CtrlB, in the command field or the body',
          not leaked and len(rc) == len(cmds) and all('[REDACTED]' in c for c in rc), f'leaked {leaked}' if leaked else str(rc)[:300])
    keep = ['The cookie: it was tasty.', 'Set the api-key header in the request', 'grep -n token src/x.py',
            'cli --password=$PW', 'cli --token <your token>', 'docker login --password-stdin -u me', 'Authorization header missing']
    check('credential header and flag patterns leave ordinary text, variables, placeholders and -stdin flags alone',
          all(_redact.redact(t) == (t, 0) for t in keep), str([_redact.redact(t) for t in keep]))

    # user_name comes only from the key's own user (its email claim); CTRLB_USER is the agent label
    seeker.reset()
    hook('upload', I['stop'], dict(env, CTRLB_HOME=os.path.join(WORK, 'home-user'), CTRLB_KEY=JWT_KEY))
    check("a key from the environment with an email claim: rows carry that user as user_name",
          seeker.rows and all(r.get('user_name') == KEY_USER for r in seeker.rows) and not seeker.refused,
          f'{len(seeker.rows)} rows, {len(seeker.refused)} refused')
    seeker.reset()
    hook('upload', I['stop'], dict(env, CTRLB_HOME=os.path.join(WORK, 'home-user-noclaim'), CTRLB_KEY=JWT_NOCLAIM))
    check('a key with no email claim: rows carry no user_name, CtrlB fills it',
          seeker.rows and all('user_name' not in r for r in seeker.rows) and not seeker.refused)
    seeker.reset()
    hook('upload', I['stop'], dict(env, CTRLB_HOME=os.path.join(WORK, 'home-user-label'), CTRLB_USER='seeker-t15-build'))
    check('CTRLB_USER=seeker-t15-build sets agent and not user_name',
          seeker.rows and all(r['agent'] == ('seeker-t15-build:agent-' + r['agent_id'] if r['agent_id'] else 'seeker-t15-build')
                              and 'user_name' not in r for r in seeker.rows)
          and not seeker.refused, f'{len(seeker.refused)} refused')
    seeker.reset()
    hook('upload', I['stop'], dict(env, CTRLB_HOME=os.path.join(WORK, 'home-user-both'), CTRLB_USER='old-label',
                                   CTRLB_AGENT='builder-1', CTRLB_KEY=JWT_KEY))
    check('CTRLB_AGENT wins over CTRLB_USER for the label; user_name is still the key user',
          seeker.rows and all(r['agent'] == ('builder-1:agent-' + r['agent_id'] if r['agent_id'] else 'builder-1')
                              and r.get('user_name') == KEY_USER for r in seeker.rows))
    stale = os.path.join(WORK, 'home-user-stale')
    os.makedirs(stale, exist_ok=True)
    with open(os.path.join(stale, 'config.json'), 'w') as fh:
        json.dump(dict(user='a-label-not-the-key-user'), fh)   # a config.json from before: "user" written as a label
    seeker.reset()
    hook('upload', I['stop'], dict(env, CTRLB_HOME=stale))
    check('an old config.json "user" is never sent as user_name', seeker.rows and all('user_name' not in r for r in seeker.rows)
          and not seeker.refused)

    # subagent start flushes the parent and names run and agent
    seeker.reset()
    with open(main, 'a') as fh:
        fh.write(line('assistant', [dict(type='tool_use', id='t9', name='Agent', input=dict(prompt='explore'))], '2026-11-03T10:33:00Z'))
    p, dt = hook('subagent-start', I['subagent_start'], env)
    o = out_of(p)
    ctx = o.get('additionalContext', '')
    check('subagent-start: parent session id, parent run id, own agent id', o.get('hookEventName') == 'SubagentStart'
          and f'subagent of session {SID}' in ctx and f'run id is {SID}' in ctx and 'agent id is agent-def456' in ctx, f'{dt:.2f}s')
    check('subagent-start flushed the parent first', len(seeker.rows) == 1 and seeker.rows[0]['tool_name'] == 'Agent')
    p, _ = hook('subagent-start', I['subagent_start'], dict(env, CTRLB_AGENT='builder-1', CTRLB_RUN='relay-run-7'))
    ctx = out_of(p).get('additionalContext', '')
    check('subagent-start with a label: label:agent-<id>', 'agent id is builder-1:agent-def456' in ctx and 'run id is relay-run-7' in ctx)
    seeker.reset()
    p, _ = hook('upload', I['subagent_stop'], env)
    check('SubagentStop uploads only the subagent transcript, nothing new', p.returncode == 0 and not seeker.rows)
    p, _ = hook('flush', I['session_end'], env)
    check('SessionEnd flush exits 0', p.returncode == 0)


def test_batches(seeker):
    seeker.reset()
    home = os.path.join(WORK, 'home-batch')
    path = os.path.join(WORK, 'projects', '-work-big', 'big-session.jsonl')
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as fh:
        for i in range(1200):
            fh.write(line('user', f'walrus line {i} ' + 'x' * 3000, f'2026-11-03T11:{i // 60:02d}:{i % 60:02d}Z'))
    p, _ = hook('upload', dict(session_id='big-session', transcript_path=path, hook_event_name='Stop'), base_env(seeker.url, home))
    sizes = [len(c['body']) for c in seeker.calls if c['path'].endswith('/records')]
    check('uploads split into calls of at most 500 rows', len(seeker.rows) == 1200 and max(sizes) <= 500 and len(sizes) >= 3, str(sizes))
    check('no call over 4 MB', max(c['size'] for c in seeker.calls) <= 4_000_000)


def test_pre_tool(seeker):
    main, sub = os.path.join(WORK, 'projects', '-work-dash', SID + '.jsonl'), ''
    I = inputs(main, sub)
    home = os.path.join(WORK, 'home-pre')
    env = base_env(seeker.url, home)

    def run(state, name='pre_tool_edit', e=None):
        seeker.reset()
        seeker.state['check'] = state
        return hook('pre-tool', I[name], e or env)

    p, dt = run('allow')
    c = seeker.calls[0] if seeker.calls else {}
    check('pre-tool allow: exit 0, no output', p.returncode == 0 and not p.stdout.strip(), f'{dt:.2f}s')
    check('pre-tool posts paths, cwd and agent to /ai/relay/{dataset}/runs/{run}/check',
          c.get('path') == f'/ai/relay/agent_sessions/runs/{SID}/check' and c.get('body') == load('seeker/check.json')['request'],
          json.dumps(c.get('body')))
    p, _ = run('advisory')
    o = out_of(p)
    ctx = o.get('additionalContext', '')
    check('pre-tool advisory: additionalContext naming the holder', o.get('hookEventName') == 'PreToolUse' and 'agent-b' in ctx
          and 'src/panels/Latency.tsx' in ctx and 'permissionDecision' not in o, ctx[:120])
    p, _ = run('enforce')
    o = out_of(p)
    reason = o.get('permissionDecisionReason', '')
    check('pre-tool enforce: deny with the holder and the claim call', o.get('permissionDecision') == 'deny' and 'agent-b' in reason
          and f'claim(resource="src/panels/Latency.tsx", run_id="{SID}", agent="{SID}")' in reason, reason[:160])
    p, _ = run('enforce_allow')
    check('pre-tool enforce with no conflict: no output', p.returncode == 0 and not p.stdout.strip())
    p, _ = run('off')
    check('pre-tool off (CtrlB mode): exit 0, no output', p.returncode == 0 and not p.stdout.strip())
    p, _ = run('enforce', e=dict(env, CTRLB_ENFORCE='off'))
    check('pre-tool CTRLB_ENFORCE=off: no call, no output', not seeker.calls and not p.stdout.strip())
    p, _ = run('self')
    check('pre-tool: a hold of the caller itself is not a conflict', not p.stdout.strip())
    p, dt = run('slow')
    check('pre-tool timeout: fails open within budget plus scheduling headroom', p.returncode == 0 and not p.stdout.strip() and dt < 4.0, f'{dt:.2f}s')
    with open(os.path.join(home, 'hooks.log')) as fh:
        check('pre-tool timeout is logged to hooks.log', 'pre-tool fail open' in fh.read())
    p, dt = hook('pre-tool', I['pre_tool_edit'], dict(env, CTRLB_URL=f'http://127.0.0.1:{closed_port()}'))
    check('pre-tool CtrlB down: fails open', p.returncode == 0 and not p.stdout.strip() and dt < 4.0, f'{dt:.2f}s')
    p, _ = run('error')
    check('pre-tool CtrlB 500: fails open', p.returncode == 0 and not p.stdout.strip())
    p, _ = hook('pre-tool', I['pre_tool_edit'], dict(env, CTRLB_KEY='wrong'))
    check('pre-tool refused key (401): fails open', p.returncode == 0 and not p.stdout.strip())
    p, _ = run('enforce', 'pre_tool_read')
    check('pre-tool on a non-edit tool: no call', not seeker.calls and not p.stdout.strip())
    for name, want in (('pre_tool_write', 'src/panels/index.ts'), ('pre_tool_multiedit', 'src/panels/Latency.test.tsx'),
                       ('pre_tool_notebook', 'notebooks/latency.ipynb')):
        p, _ = run('enforce', name)
        check(f'pre-tool {name[9:]}: path checked and denied', seeker.calls and seeker.calls[0]['body']['paths'] == [want]
              and out_of(p).get('permissionDecision') == 'deny')
    # Fencing: lapsed, unheld and stale_round name the reason and the claim call.
    for state, said in (('enforce_lapsed', 'your lease on src/panels/Latency.tsx ran out at'),
                        ('enforce_unheld', 'src/panels/Latency.tsx is not held; claim it before editing (unheld)'),
                        ('enforce_stale_round', 'you held task 07 latency in round 1; round 2 belongs to agent-b (stale_round)')):
        p, _ = run(state)
        reason = out_of(p).get('permissionDecisionReason', '')
        check(f'pre-tool {state[8:]}: deny naming the reason and the claim call', out_of(p).get('permissionDecision') == 'deny'
              and said in reason and f'Call claim(resource="src/panels/Latency.tsx", run_id="{SID}", agent="{SID}")' in reason, reason[:200])
    p, _ = run('advisory_unheld')
    o = out_of(p)
    check('pre-tool advisory unheld: context, not a deny', 'permissionDecision' not in o and '(unheld)' in o.get('additionalContext', ''))
    # Tokens: a claim's answer seen after the tool is kept, sent with the check, dropped by the release.
    seeker.reset()
    p, _ = hook('post-tool', I['post_tool_claim'], env)
    check('post-tool claim: no output, no CtrlB call', p.returncode == 0 and not p.stdout.strip() and not seeker.calls)
    run('allow')
    body = seeker.calls[0]['body'] if seeker.calls else {}
    check('pre-tool sends the token of the held task for its file', body.get('tokens') == {'src/panels/Latency.tsx': '<hold nonce>'},
          json.dumps(body))
    hook('post-tool', I['post_tool_release'], env)
    run('allow')
    body = seeker.calls[0]['body'] if seeker.calls else {}
    check('post-tool release drops the tokens', 'tokens' not in body, json.dumps(body))
    p, _ = run('enforce', 'pre_tool_subagent_edit', dict(env, CTRLB_RUN='relay-run-7'))
    c = seeker.calls[0] if seeker.calls else {}
    check('pre-tool in a subagent: CTRLB_RUN in the route, agent-<id> as agent', c.get('path') == '/ai/relay/agent_sessions/runs/relay-run-7/check'
          and c.get('body', {}).get('agent') == 'agent-abc123' and 'agent="agent-abc123"' in out_of(p).get('permissionDecisionReason', ''))


def test_docs():
    """The four connect pages: front matter for the docs site, the two placeholders, no other placeholder in
    a snippet except the OTEL page's ingest values, and the commands they show match what the repo ships."""
    pages = {n: open(os.path.join(ROOT, 'docs', f'connect-{n}.md')).read() for n in ('claude-code', 'codex', 'otel', 'http')}
    check('docs: connect-claude-code, -codex, -otel, -http have front matter with title, description, slug',
          all(re.match(r'---\ntitle: .+\ndescription: .+\nslug: connect-' + n + r'\n---\n', s) for n, s in pages.items()))
    allowed = {'<your CtrlB URL>', '<your key>'}
    extra = {}
    for n, s in pages.items():
        for block in re.findall(r'```[a-z]*\n(.*?)```', s, re.S):
            found = set(re.findall(r'<[a-z][^<>\n]*>', block)) - allowed
            if n == 'otel':
                found -= {'<your OTLP logs endpoint>', '<your ingest credentials>'}
            if found:
                extra[n] = sorted(found)
    check('docs: snippets use only <your CtrlB URL> and <your key> (OTEL: also the ingest endpoint and credentials)', not extra, json.dumps(extra))
    cc = pages['claude-code']
    check('docs: Claude Code page has the marketplace install, the --plugin-dir fallback and CTRLB_RUN',
          'claude plugin marketplace add ctrlb-hq/ctrlb-agent' in cc and 'claude plugin install ctrlb-agent@ctrlb-agent' in cc
          and 'claude --plugin-dir ' in cc and 'CTRLB_RUN=' in cc)
    cx = pages['codex']
    check('docs: Codex page has install.py --codex, --dry-run, the backup and bearer_token_env_var',
          'python3 -I install.py --codex' in cx and '--dry-run' in cx and 'ctrlb-backup' in cx and 'bearer_token_env_var = "CTRLB_KEY"' in cx)
    ot = pages['otel']
    check('docs: OTEL page has the Claude Code environment, the Codex [otel] tables and what the export does not carry',
          'OTEL_EXPORTER_OTLP_LOGS_ENDPOINT' in ot and '[otel.exporter.otlp-http]' in ot and 'protocol = "json"' in ot
          and 'What OTEL export does not carry' in ot)
    routes = ['/ai/sessions/agent_sessions/records', '/ai/sessions/agent_sessions/search', '/ai/sessions/agent_sessions/sess-1/records',
              '/ai/sessions/agent_sessions/commands', '/ai/sessions/agent_sessions/sess-1/next', '/runs/demo-run-1/posts',
              '/runs/demo-run-1/peers', '-X POST "<your CtrlB URL>/ai/relay/agent_sessions/runs/demo-run-1/claims"',
              '-X DELETE "<your CtrlB URL>/ai/relay/agent_sessions/runs/demo-run-1/claims"', '/runs/demo-run-1/check',
              '/ai/relay/agent_sessions/runs?status=', '"<your CtrlB URL>/ai/relay/agent_sessions/runs/demo-run-1"',
              '"<your CtrlB URL>/ai/relay/settings"', '-X PUT "<your CtrlB URL>/ai/relay/settings"']
    missing = [r for r in routes if r not in pages['http']]
    check('docs: HTTP page has every section 4 route as curl', not missing and pages['http'].count('-H "Authorization: Bearer <your key>"') >= len(routes),
          ', '.join(missing))
    body = re.search(r"-d '(\[.*?\])'", pages['http'], re.S)
    rows = json.loads(body.group(1)) if body else []
    check('docs: the HTTP records example is valid JSON with the section 5 columns', rows and set(rows[0]) == COLS - {'body_len', 'truncated'},
          str(sorted(set(rows[0]) ^ COLS)) if rows else 'no body')


def test_release_wording():
    """The CtrlB server never finishes a file path: no agent-facing text may tell agents to release
    with finished=true as the rule for every claim, and the tool docs carry reopen."""
    old = re.compile(r'(call|then) `?release`? with `?finished=true`?', re.I)
    files = [f for f in glob.glob(os.path.join(ROOT, '**', '*'), recursive=True)
             if os.path.isfile(f) and f.endswith(('.md', '.py', '.json')) and '/tests/' not in f and '/.git/' not in f]
    bad = [os.path.relpath(f, ROOT) for f in files if old.search(open(f, errors='replace').read())]
    check('no agent-facing text says to release with finished=true for every claim', not bad, ', '.join(bad))
    tools = open(os.path.join(ROOT, 'docs', 'relay-tools.md')).read()
    check('relay-tools: claim documents reopen and release says files are always handed back',
          '| `reopen` |' in tools and '"reopened": true' in tools and 'A file path is always handed back' in tools)
    lim = open(os.path.join(ROOT, 'docs', 'limits.md')).read()
    check('limits: a file path is never done; reopen starts a new round; no "use a new run id"',
          'A file path is never done' in lim and 'reopen=true' in lim and 'use a new run id' not in lim)


def test_briefs_wording():
    """Briefs: the skill and the tool docs tell the agent to read the brief of a
    shared file before reading the file, and to leave one after reading a file it edits or others need."""
    skill = open(os.path.join(ROOT, 'plugin', 'skills', 'relay', 'SKILL.md')).read()
    check('relay skill: lists the relay tools, ask and answer among them; ask rather than a QUESTION note',
          '`peers`, `post`, `claim`, `release`, `handoff`, `ask`, `answer`, `brief`, `briefs`, `tasks`, `contract`, `contracts`' in skill
          and 'Use the coordination tools before a prose note (`post`)' in skill
          and '- `handoff` when you must leave a task you hold' in skill
          and '- `ask` when you are blocked on something only another agent or the orchestrator can decide. Do not post a QUESTION note.' in skill
          and "- `answer`, with the question's id, when you can settle an open question" in skill
          and '- `contract` when you finish an interface others will build on' in skill
          and "- `contracts` before you build on another agent's interface" in skill
          and 'Post a note only for what none of them covers.' in skill)
    check('relay-tools: the coordination tools come before a prose note',
          'The coordination tools come before a prose note (`post`)' in open(os.path.join(ROOT, 'docs', 'relay-tools.md')).read())
    check('relay skill: call briefs before reading a shared file, post a brief after reading one',
          'call `briefs` with it' in skill and 'post a `brief` for it' in skill and 'up to a few hundred words' in skill
          and 'what it exports or decides, and what not to touch' in skill and 'not a claim and reserves nothing' in skill)
    tools = open(os.path.join(ROOT, 'docs', 'relay-tools.md')).read()
    sections = re.findall(r'^### (\w+)$', tools, re.M)
    relay = sections[sections.index('peers'):] if 'peers' in sections else []
    check('relay-tools: fifteen relay tools, read_text after peers, handoff, ask and answer after release, then brief, briefs, task, tasks, history, contract, contracts',
          relay == ['peers', 'read_text', 'post', 'claim', 'release', 'handoff', 'ask', 'answer', 'brief', 'briefs', 'task', 'tasks', 'history', 'contract', 'contracts'],
          ','.join(relay))
    check('relay-tools: contract and contracts documented, instead of a CONTRACT note',
          '{contracts: [{name, key, agent, ts, age_s, text, paths, changed_since, text_since, text_changed_at?, changed_since_read?}], last_read?, read_at?}' in tools
          and 'instead of a `CONTRACT:` note' in tools
          and 'changed_since?}]}' in tools)
    check('relay-tools: peers kind and open, ask and answer documented',
          '| `open` |' in tools and 'peers(kind="question", open=true)' in tools and 'answered: {agent, ts, text, row_id?, text_len?}' in tools
          and 'Answer: `{id, run_id, agent, row_id, text_len, kept, whole, ref?, truncated?, note?}`' in tools and 'Do not post a QUESTION note' in tools)
    check('relay-tools: hold required, its four reasons and the claim token',
          '## Edits need a claim (hold required)' in tools and all(f'| `{r}` |' in tools for r in ('held', 'lapsed', 'unheld', 'stale_round'))
          and 'A held claim answers `token`' in tools)
    check('relay-tools: handoff documents to, handed and held_by',
          '| `to` |' in tools and '{handed: true, resource, key, to, lease_until, token, paths?}' in tools
          and '{handed: false, resource, key, to, held_by?, message}' in tools)
    check('relay-tools: nineteen tools, the writes listed, briefs answers the newest brief per key',
          'gives an agent nineteen tools' in tools and '`post`, `claim`, `release`, `handoff`, `ask`, `answer`, `brief`, `task` and `contract` write' in tools
          and '{briefs: [{key, resource, agent, text, ts, age_s, paths?, changed_since?}]}' in tools and '`BRIEF` for a brief' in tools)
    check('relay-tools: the briefs paragraph for the rules', 'call briefs for it and read the brief first' in tools)
    check('relay-tools: rule 3 claims the registered task with its files, and finishes it', registered_rule(tools))
    check('relay-tools: claim documents paths, conflict_key and unknown_task; task and tasks documented',
          '| `paths` |' in tools and '"conflict_key"' in tools and '"reason": "unknown_task"' in tools and '"suggestion"' in tools
          and '{tasks: [{name, key, text, paths, agent, ts, state, held_by?, finished_by?, round, ask_about?}]}' in tools)
    http = open(os.path.join(ROOT, 'docs', 'connect-http.md')).read()
    check('connect-http: both briefs routes as curl',
          '-X POST "<your CtrlB URL>/ai/relay/agent_sessions/runs/demo-run-1/briefs"' in http
          and '/runs/demo-run-1/briefs?resources=' in http and '`brief` and `briefs`' in http)


def test_keys():
    """lib/paths.py against CtrlB's shared table (tests/fixtures/keys.json), and the edit paths the hooks and
    parsers send against the same keys."""
    import paths
    cases = load('keys.json')['cases']
    bad = [c for c in cases if paths.key(c['resource'], c['cwd'])[0] != c['key']]
    check(f'paths.key passes CtrlB keys.json ({len(cases)} cases)', len(cases) >= 40 and not bad,
          '; '.join(f"{c['resource']!r},{c['cwd']!r} -> {paths.key(c['resource'], c['cwd'])[0]!r} want {c['key']!r}" for c in bad[:5]))
    check('paths.key: kind is the table row of the key\'s prefix ("" for an invalid resource)',
          all(paths.key(c['resource'], c['cwd'])[1] == (paths.kind_of(c['key'])['kind'] if c['key'] else '') for c in cases))
    check('paths.key is idempotent on its own keys', all(paths.key(paths.key(c['resource'], c['cwd'])[0], c['cwd'])[0] == c['key']
                                                         for c in cases if c['key']))
    check('keys.json: lib/keys.json (read at run time) is tests/fixtures/keys.json byte for byte',
          open(os.path.join(ROOT, 'lib', 'keys.json'), 'rb').read() == open(os.path.join(FIX, 'keys.json'), 'rb').read())
    kinds = {k['prefix']: k for k in paths.KINDS}
    check('kinds table: path, name, slot, contract with the decided columns',
          sorted(kinds) == ['contract:', 'name:', 'path:', 'slot:']
          and kinds['path:']['checked_by_hook'] and not kinds['path:']['finishable']
          and kinds['name:']['finishable'] and kinds['name:']['reopenable']
          and kinds['slot:']['leasable'] and kinds['slot:']['default_lease_s'] == 120 and not kinds['slot:']['finishable']
          and not kinds['contract:']['leasable'])
    check('kinds: slot lease 120 s, path and name the setting', paths.lease_seconds_for('slot:devserver', 600) == 120
          and paths.lease_seconds_for('path:a.go', 600) == 600 and paths.lease_seconds_for('name:t', 600) == 600)
    check('keys: slot:devserver, contract:..., Slot:X is a name, slot: is invalid',
          paths.key('slot:devserver') == ('slot:devserver', 'slot') and paths.key('contract:seeker-4 routes') == ('contract:seeker-4 routes', 'contract')
          and paths.key('Slot:X') == ('name:slot:x', 'name') and paths.key('slot:') == ('', ''))
    file_cases = [c for c in cases if c['key'].startswith('path:') and c['key'] != 'path:'
                  and not c['resource'].strip().startswith(('path:', 'name:'))]
    check('edit_path: CtrlB keys what the package sends as the key of the path itself',
          all(paths.key(paths.edit_path(c['resource'], c['cwd']), c['cwd'])[0] == c['key'] for c in file_cases))
    check('edit_path: a file with no extension is sent so it is keyed as a path',
          paths.edit_path('/work/dash/Makefile', '/work/dash') == './Makefile' and paths.edit_path('Makefile', '/work/dash') == './Makefile'
          and paths.key('./Makefile', '/work/dash')[0] == 'path:Makefile')
    check('edit_paths: once per key, empty dropped, order kept',
          paths.edit_paths(['/w/b.go', 'b.go', './/b.go', '', 'a.go'], '/w') == ['b.go', 'a.go'])
    check('paths.clean is Go path.Clean, not posixpath.normpath', paths.clean('//a//b/../c/') == '/a/c' and paths.clean('') == '.'
          and paths.clean('../../x') == '../../x' and paths.clean('/../x') == '/x')
    check('names follow Go: per-rune lower case, Go whitespace only', paths.key('ΟΔΟΣ')[0] == 'name:οδοσ'
          and paths.key('a\x1c')[0] == 'name:a\x1c' and paths.key('  Task　 01 ')[0] == 'name:task 01')


def test_edit_keys_across_harnesses(seeker):
    """The same file edited from Claude Code (absolute file_path) and from Codex (a patch path relative to cwd) is sent
    under one key, also for a file name with no extension."""
    import paths, rows_codex
    cwd = '/work/garden'
    claude = paths.edit_paths(rows_claude.edit_paths('Write', {'file_path': cwd + '/Makefile'}), cwd)
    codex = paths.edit_paths(rows_codex.input_paths({'command': '*** Begin Patch\n*** Update File: Makefile\n@@\n-a\n+b\n*** End Patch\n'}), cwd)
    check('Claude Code and Codex send one key for one file (Makefile)', claude == codex == ['./Makefile']
          and paths.key(claude[0], cwd)[0] == 'path:Makefile', f'{claude} {codex}')
    env = base_env(seeker.url, os.path.join(WORK, 'home-keys'))
    inp = {'session_id': SID, 'transcript_path': '', 'cwd': cwd, 'hook_event_name': 'PreToolUse', 'tool_name': 'Edit',
           'tool_input': {'file_path': cwd + '/./src//lib.rs', 'old_string': 'a', 'new_string': 'b'}}
    seeker.reset()
    seeker.state['check'] = 'allow'
    hook('pre-tool', inp, env)
    check('pre-tool sends the normalised path with cwd', seeker.calls and seeker.calls[0]['body'] == {'agent': SID, 'paths': ['src/lib.rs'], 'cwd': cwd},
          json.dumps(seeker.calls[0]['body'] if seeker.calls else {}))


def test_failure_safety(seeker):
    main, sub = setup_transcripts()
    I = inputs(main, sub)
    down = base_env(f'http://127.0.0.1:{closed_port()}', os.path.join(WORK, 'home-down'))
    p, dt = hook('flush', I['pre_compact'], down)
    check('CtrlB down: flush exits 0 within its limit', p.returncode == 0 and dt < 7, f'{dt:.2f}s')
    with open(os.path.join(WORK, 'home-down', 'hooks.log')) as fh:
        check('CtrlB down: error logged to hooks.log', 'upload' in fh.read())
    env = base_env(seeker.url, os.path.join(WORK, 'home-safe'))
    seeker.reset()
    p, dt = hook('upload', dict(I['stop'], transcript_path='/nonexistent.jsonl'), env)
    check('missing transcript: exits 0, sends nothing', p.returncode == 0 and not seeker.calls)
    p, _ = hook('session-start', I['session_start'], dict(env, CTRLB_OFF='1'))
    check('CTRLB_OFF=1: no output', p.returncode == 0 and not p.stdout.strip())
    nokey = dict(env)
    nokey.pop('CTRLB_KEY')
    p, _ = hook('session-start', I['session_start'], nokey)
    check('no key configured: off, no output', p.returncode == 0 and not p.stdout.strip())
    p, _ = hook('upload', 'not json', env)
    check('malformed hook input: exits 0', p.returncode == 0)
    p, _ = hook('pre-tool', 'not json', env)
    check('malformed pre-tool input: exits 0, no output', p.returncode == 0 and not p.stdout.strip())
    p, _ = hook('no-such-mode', I['stop'], env)
    check('unknown mode: exits 0, no output', p.returncode == 0 and not p.stdout.strip())


def test_config_and_status(seeker):
    home = os.path.join(WORK, 'home-cfg')
    env = {k: v for k, v in os.environ.items() if not k.startswith('CTRLB_')}
    env['CTRLB_HOME'] = home
    secret = 'cfgkey' + '-value-12345'
    p = subprocess.run([sys.executable, '-I', os.path.join(ROOT, 'bin', 'configure.py'), '--url', seeker.url + '/', '--dataset',
                        'agent_sessions', '--user', '', '--run', '', '--key-stdin'], input=secret + '\n', capture_output=True,
                       text=True, env=env, timeout=30)
    path = os.path.join(home, 'config.json')
    mode = stat.S_IMODE(os.stat(path).st_mode) if os.path.exists(path) else None
    with open(path) as fh:
        c = json.load(fh)
    check('configure.py writes ~/.ctrlb/config.json with mode 0600', p.returncode == 0 and mode == 0o600 and c.get('key') == secret
          and c.get('url') == seeker.url, f'mode {oct(mode or 0)} {p.stderr[-200:]}')
    check('configure.py never prints the key', secret not in p.stdout + p.stderr)
    check('configure.py prints the env lines with the key placeholder', 'export CTRLB_KEY="<your key>"' in p.stdout
          and f'export CTRLB_MCP_URL="{seeker.url}/mcp"' in p.stdout)
    check('configure.py: the CtrlB check fails with the fixture key, and says so', 'CtrlB check failed: HTTP 401' in p.stdout)
    p = subprocess.run([sys.executable, '-I', os.path.join(ROOT, 'bin', 'status.py')], capture_output=True, text=True,
                       env=dict(env, CTRLB_KEY=KEY), timeout=30)
    check('status.py: CtrlB answers with the latest runs; key masked', 'CtrlB answered: OK' in p.stdout and 'relay-demo-1' in p.stdout
          and KEY not in p.stdout and 'CTRLB_MCP_URL in the environment: NO' in p.stdout, p.stdout[-300:])
    check("status.py: says the key carries no user when it has no email claim", "key's user (written as user_name): (none in the key" in p.stdout)

    # the key's user: the email claim of the JWT payload, decoded without a signature check
    check('key_user: a fixture JWT with the email claim decodes to the user', common.key_user(JWT_KEY) == KEY_USER)
    check('key_user: no email claim, a non-JWT key, a broken payload or no key give no user',
          common.key_user(JWT_NOCLAIM) == '' and common.key_user(KEY) == '' and common.key_user('aaa.!!!.ccc') == ''
          and common.key_user('a.' + base64.urlsafe_b64encode(b'[1]').decode() + '.c') == '' and common.key_user('') == ''
          and common.key_user(fake_jwt(dict(email=7))) == '')
    check("key_user reads the claim CtrlB reads (email)", common.USER_CLAIM == 'email')
    home2 = os.path.join(WORK, 'home-cfg-jwt')
    env2 = dict(env, CTRLB_HOME=home2)
    p = subprocess.run([sys.executable, '-I', os.path.join(ROOT, 'bin', 'configure.py'), '--url', seeker.url, '--dataset',
                        'agent_sessions', '--user', 'some-label', '--run', '', '--key-stdin'], input=JWT_KEY + '\n',
                       capture_output=True, text=True, env=env2, timeout=30)
    with open(os.path.join(home2, 'config.json')) as fh:
        c = json.load(fh)
    check("configure.py stores the key's user as user, and ignores --user", p.returncode == 0 and c.get('user') == KEY_USER
          and '--user is ignored' in p.stdout, p.stdout[-300:] + p.stderr[-200:])
    check('configure.py with a JWT key: CtrlB answers, and the key is never printed', 'CtrlB answered: OK' in p.stdout
          and JWT_KEY not in p.stdout + p.stderr and JWT_KEY.split('.')[1] not in p.stdout + p.stderr)
    p = subprocess.run([sys.executable, '-I', os.path.join(ROOT, 'bin', 'status.py')], capture_output=True, text=True,
                       env=dict(env2, CTRLB_USER='seeker-t15-build'), timeout=30)
    check("status.py prints the key's user, CTRLB_USER as the label only, and never the key",
          f"key's user (written as user_name): {KEY_USER}" in p.stdout and 'is read as the agent label only' in p.stdout
          and '"agent": "seeker-t15-build"' in p.stdout and JWT_KEY not in p.stdout and JWT_KEY.split('.')[1] not in p.stdout,
          p.stdout[-400:])
    p = subprocess.run([sys.executable, '-I', os.path.join(ROOT, 'bin', 'configure.py'), '--url', seeker.url, '--dataset',
                        'agent_sessions', '--run', '', '--key-stdin'], input=JWT_NOCLAIM + '\n', capture_output=True, text=True,
                       env=env2, timeout=30)
    with open(os.path.join(home2, 'config.json')) as fh:
        c = json.load(fh)
    check('configure.py with a key that has no email claim stores no user', p.returncode == 0 and 'user' not in c
          and c.get('key') == JWT_NOCLAIM, p.stdout[-200:])
    p = subprocess.run([sys.executable, '-I', os.path.join(ROOT, 'bin', 'status.py')], capture_output=True, text=True,
                       env=dict(env2, CTRLB_KEY=JWT_KEY), timeout=30)
    check("status.py with CTRLB_KEY in the environment shows that key's user", f"key's user (written as user_name): {KEY_USER}" in p.stdout,
          p.stdout[-300:])
    p = subprocess.run([sys.executable, '-I', os.path.join(ROOT, 'bin', 'backfill.py'), '--count'], capture_output=True, text=True,
                       env=dict(env, CTRLB_KEY=KEY, CLAUDE_PROJECTS_DIR=os.path.join(WORK, 'projects')), timeout=30)
    check('backfill.py --count lists the fixture transcripts', p.returncode == 0 and 'transcript files' in p.stdout, p.stdout.strip()[:200])


# ---------------------------------------------------------------- Codex

COLS = {'_timestamp', 'host', 'harness', 'session_id', 'parent_session', 'agent_id', 'run_id', 'agent', 'seq', 'block_idx',
        'role', 'block_type', 'tool_name', 'tool_use_id', 'command', 'paths', 'body', 'is_error', 'is_summary', 'cwd',
        'git_branch', 'cost_usd', 'input_tokens', 'output_tokens', 'body_len', 'truncated'}


def crec(t, payload, ts):
    return json.dumps(dict(timestamp=ts, type=t, payload=payload)) + '\n'


def citem(payload, ts):
    return crec('response_item', payload, ts)


def setup_codex():
    """A Codex home in tests/work with the fixture rollout and a subagent rollout (synthetic, written here)."""
    home = os.path.join(WORK, 'codex-home')
    shutil.rmtree(home, ignore_errors=True)
    day = os.path.join(home, 'sessions', '2026', '09', '06')
    os.makedirs(day)
    main = os.path.join(day, f'rollout-2026-09-06T11-58-07-{CSID}.jsonl')
    sub = os.path.join(day, f'rollout-2026-09-06T12-02-00-{CSUB}.jsonl')
    shutil.copy(os.path.join(FIX, 'codex-rollout.jsonl'), main)
    with open(sub, 'w') as fh:
        fh.write(crec('session_meta', dict(id=CSUB, session_id=CSUB, cwd='/work/garden', parent_thread_id=CSID, cli_version='0.166.0',
                                           source={'subagent': {'other': 'worker'}}), '2026-09-06T12:02:00.000Z'))
        fh.write(citem(dict(type='message', role='user', content=[dict(type='input_text', text='Check the bucket math.')]), '2026-09-06T12:02:00.100Z'))
        fh.write(citem(dict(type='message', role='assistant', content=[dict(type='output_text', text='The bucket math is right.')]), '2026-09-06T12:02:09.000Z'))
    return home, main, sub


def cinputs(main, sub):
    raw = json.dumps(load('hooks/codex.json')).replace('{rollout}', main).replace('{sub_rollout}', sub)
    return json.loads(raw)


def test_codex_rows(seeker):
    chome, main, sub = setup_codex()
    I = cinputs(main, sub)
    env = base_env(seeker.url, os.path.join(WORK, 'home-codex'), CODEX_HOME=chome)
    seeker.reset()
    p, dt = chook('upload', I['stop'], env)
    rows = list(seeker.rows)
    check('codex Stop upload: the fixture rollout gives 11 rows', len(rows) == 11 and p.returncode == 0, f'{len(rows)} rows, {dt:.2f}s')
    check('codex records go to POST /ai/sessions/agent_sessions/records with the Bearer key',
          seeker.calls and all(c['path'] == '/ai/sessions/agent_sessions/records' and c['auth'] == 'Bearer ' + KEY for c in seeker.calls))
    check('codex rows: the section 5 columns, the same set as Claude Code rows', all(set(r) == COLS for r in rows),
          str(sorted(set(rows[0]) ^ COLS)) if rows else '')
    check('codex rows: harness codex, ids from session_meta, run and agent default to the thread id',
          all(r['harness'] == 'codex' and r['session_id'] == CSID and r['run_id'] == CSID and r['agent'] == CSID
              and r['parent_session'] == '' and r['agent_id'] == '' for r in rows))
    check('codex rows: cwd from session_meta and turn_context, branch from session_meta git',
          all(r['cwd'] == '/work/garden' and r['git_branch'] == 'feat/watering-schedule' for r in rows))
    check('codex rows: no user_name when the key carries no user', all('user_name' not in r for r in rows))
    seeker.reset()
    chook('upload', I['stop'], dict(env, CTRLB_HOME=os.path.join(WORK, 'home-codex-user'), CTRLB_KEY=JWT_KEY,
                                    CTRLB_USER='seeker-t15-build'))
    check("codex rows: user_name is the key's user, CTRLB_USER is the agent label",
          len(seeker.rows) == 11 and all(r.get('user_name') == KEY_USER and r['agent'] == 'seeker-t15-build' for r in seeker.rows)
          and not seeker.refused, f'{len(seeker.rows)} rows, {len(seeker.refused)} refused')
    by = {(r['seq'], r['block_idx']): r for r in rows}
    kinds = [(r['seq'], r['role'], r['block_type']) for r in rows]
    check('codex rows: developer message, encrypted reasoning, event_msg and turn_context give no rows',
          not any(s in (0, 1, 2, 3, 9, 10, 12, 16, 19) for s, _, _ in kinds), str(kinds))
    check('codex rows: user and assistant messages are text rows', by[(4, 0)]['role'] == 'user' and by[(4, 0)]['block_type'] == 'text'
          and by[(5, 0)]['role'] == 'assistant' and 'inspect the changes' in by[(5, 0)]['body'])
    t6 = by[(6, 0)]
    check('codex exec call: tool_use with the call id and its shell commands', t6['block_type'] == 'tool_use' and t6['tool_name'] == 'exec'
          and t6['tool_use_id'] == 'call_syntheticcall02' and t6['command'].split('\n')[:3] == [
              'pwd', "rg --files -g AGENTS.md -g '!.git/**' .",
              'git status --short --branch'], repr(t6['command'][:120]))
    check('codex exec output: tool_result with the tool name', by[(8, 0)]['block_type'] == 'tool_result' and by[(8, 0)]['tool_name'] == 'exec'
          and by[(8, 0)]['tool_use_id'] == 'call_syntheticcall02' and by[(8, 0)]['is_error'] is False)
    check('codex apply_patch inside exec: paths from the *** Add File line', by[(13, 0)]['paths'] == '["/tmp/garden-pr-body.md"]',
          by[(13, 0)]['paths'])
    check('codex paths empty on rows without a patch', all(r['paths'] == '' for r in rows if r['seq'] != 13))
    res = [r for r in rows if r['block_type'] == 'result']
    tin, tout = sum(r['input_tokens'] for r in rows), sum(r['output_tokens'] for r in rows)
    check('codex token_usage_record: one result row each, tokens from usage', len(res) == 3 and all(r['role'] == 'system' for r in res)
          and tin == 171125.0 and tout == 1135.0, f'{len(res)} rows, {tin} in, {tout} out')
    check('codex numbers are floats and flags booleans on every row',
          all(isinstance(r[k], float) for r in rows for k in ('cost_usd', 'input_tokens', 'output_tokens'))
          and all(isinstance(r[k], bool) for r in rows for k in ('is_error', 'is_summary', 'truncated')))
    check('codex _timestamp in microseconds from the record time', by[(4, 0)]['_timestamp'] == 1788695961565000)

    seeker.reset()
    chook('upload', I['stop'], env)
    check('codex second upload sends nothing', not seeker.rows, f'{len(seeker.rows)} rows')

    # shapes the fixture slice does not hold (synthetic lines), and a partial line
    s = planted()
    patch = '*** Begin Patch\n*** Update File: src/a.rs\n@@\n-x\n+y\n*** Update File: src/b.rs\n*** Move to: src/c.rs\n*** Delete File: src/d.rs\n*** End Patch\n'
    with open(main, 'a') as fh:
        fh.write(citem(dict(type='reasoning', summary=[dict(type='summary_text', text='**Checking the frost dates**')],
                            content=None, encrypted_content='x'), '2026-09-06T12:05:00.000Z'))
        fh.write(citem(dict(type='function_call', name='shell', call_id='call_f1',
                            arguments=json.dumps({'command': ['bash', '-lc', 'cargo test -p garden'], 'workdir': '/work/garden'})),
                       '2026-09-06T12:05:01.000Z'))
        fh.write(citem(dict(type='function_call_output', call_id='call_f1',
                            output=json.dumps({'output': 'test failed', 'metadata': {'exit_code': 101}})), '2026-09-06T12:05:09.000Z'))
        fh.write(citem(dict(type='custom_tool_call', name='apply_patch', call_id='call_f2', status='completed', input=patch),
                       '2026-09-06T12:05:10.000Z'))
        fh.write(citem(dict(type='custom_tool_call_output', call_id='call_f2', output='Script failed\nboom'), '2026-09-06T12:05:11.000Z'))
        fh.write(crec('token_usage_record', dict(response_id='resp_syntheticresp03',
                                                  usage=dict(input_tokens=1, output_tokens=1)), '2026-09-06T12:05:12.000Z'))
        fh.write(crec('compacted', dict(message='Summary: PR 12 drafted; two review comments posted.'), '2026-09-06T12:05:13.000Z'))
        fh.write(citem(dict(type='message', role='assistant', content=[dict(type='output_text', text=f'keys {s[1]} {s[2]} PASSWORD={s[4]}')]),
                       '2026-09-06T12:05:14.000Z'))
        fh.write(citem(dict(type='function_call', name='exec_command', call_id='call_f3',
                            arguments=json.dumps({'cmd': f'curl -H "Authorization: Bearer {s[7]}" localhost'})), '2026-09-06T12:05:15.000Z'))
        fh.write('{"type": "response_item", "partial')
    seeker.reset()
    chook('flush', I['pre_compact'], env)
    new = list(seeker.rows)
    nb = {r['seq']: r for r in new}
    check('codex appended lines upload alone; partial line waits; repeated response_id skipped', len(new) == 8
          and sorted(nb) == [20, 21, 22, 23, 24, 26, 27, 28], str(sorted((r['seq'], r['block_type']) for r in new)))
    check('codex reasoning summary is a thinking row', nb.get(20, {}).get('block_type') == 'thinking'
          and 'frost dates' in nb.get(20, {}).get('body', ''))
    check('codex shell function_call: command from argv, error from exit code', nb.get(21, {}).get('command') == 'cargo test -p garden'
          and nb.get(22, {}).get('is_error') is True and nb.get(22, {}).get('tool_name') == 'shell')
    check('codex apply_patch tool: Update, Move to and Delete paths in order', nb.get(23, {}).get('paths') ==
          '["src/a.rs", "src/b.rs", "src/c.rs", "src/d.rs"]' and nb.get(23, {}).get('tool_name') == 'apply_patch',
          nb.get(23, {}).get('paths'))
    check('codex failed exec script: is_error', nb.get(24, {}).get('is_error') is True)
    check('codex compacted message: is_summary text row', nb.get(26, {}).get('is_summary') is True and nb[26]['block_type'] == 'text')
    sent = json.dumps(new)
    leaked = [x for x in s if x in sent]
    check('codex: no planted secret reaches CtrlB (body and command)', not leaked and nb.get(28, {}).get('command', '').count('[REDACTED]') == 1,
          f'leaked {leaked}' if leaked else '')

    # subagent rollout: parent from session_meta, joins the parent's run
    seeker.reset()
    p, _ = chook('upload', I['subagent_stop'], env)
    subr = list(seeker.rows)
    check('codex SubagentStop uploads the subagent rollout: parent_session, agent_id, parent run, own agent id',
          len(subr) == 2 and all(r['session_id'] == CSUB and r['parent_session'] == CSID and r['agent_id'] == CSUB
                                 and r['run_id'] == CSID and r['agent'] == CSUB for r in subr), json.dumps(subr[:1])[:300])
    seeker.reset()
    chook('upload', I['subagent_stop'], dict(env, CTRLB_HOME=os.path.join(WORK, 'home-codex-label'), CTRLB_AGENT='builder-2',
                                             CTRLB_RUN='relay-run-8'))
    check('codex subagent with CTRLB_AGENT and CTRLB_RUN: label:thread id, the set run',
          seeker.rows and all(r['agent'] == f'builder-2:{CSUB}' and r['run_id'] == 'relay-run-8' for r in seeker.rows))
    p, _ = chook('flush', I['session_end'], env)
    check('codex SessionEnd flush exits 0', p.returncode == 0)


def test_codex_hooks(seeker):
    chome = os.path.join(WORK, 'codex-home')       # written by test_codex_rows
    day = os.path.join(chome, 'sessions', '2026', '09', '06')
    main = os.path.join(day, f'rollout-2026-09-06T11-58-07-{CSID}.jsonl')
    sub = os.path.join(day, f'rollout-2026-09-06T12-02-00-{CSUB}.jsonl')
    I = cinputs(main, sub)
    home = os.path.join(WORK, 'home-codex-pre')
    env = base_env(seeker.url, home, CODEX_HOME=chome)

    p, _ = chook('session-start', I['session_start'], dict(env, CTRLB_RUN=CSID))     # in a relay run: the whole rule
    rctx = out_of(p).get('additionalContext', '')
    seeker.reset()
    p, dt = chook('session-start', I['session_start'], env)
    o = out_of(p)
    ctx = o.get('additionalContext', '')
    check('codex session-start: rule names session id, run id and agent id', o.get('hookEventName') == 'SessionStart'
          and f'session_id {CSID}' in ctx and f'run id is {CSID}' in ctx and f'agent id is {CSID}' in ctx and p.returncode == 0, f'{dt:.2f}s')
    check('codex session-start with no run set: no task to find or stop rule',
          'no Relay tasks are waiting for you' in ctx and 'claim(next=true)' not in ctx and 'no task and stop' not in ctx, ctx[-300:])
    check('codex session-start: claims the registered task, its files come with it', registered_rule(rctx))
    check('codex session-start: the same recall rule as Claude Code', recall_rule(ctx, CSID))
    check('codex session-start: names the relay tools, ask and answer among them',
          'relay tools (peers, post, claim, release, handoff, ask, answer, brief, briefs, tasks, contract, contracts, done)' in rctx
          and 'Use the coordination tools before a prose note: handoff when' in rctx and '; ask when you are blocked' in rctx)
    check('codex session-start: slots', 'slot:<name>' in rctx)
    check('codex session-start: finish with evidence; reopen a finished task',
          'as the evidence, and next=true' in rctx and 'release it with finished=true' not in rctx
          and 'claim its name with reopen=true' in rctx)
    p, _ = chook('session-start', I['session_start'], dict(env, CTRLB_RUN='relay-run-8', CTRLB_AGENT='builder-1'))
    ctx = out_of(p).get('additionalContext', '')
    check('codex session-start: CTRLB_RUN and CTRLB_AGENT win', 'run id is relay-run-8' in ctx and 'agent id is builder-1' in ctx)
    p, _ = chook('session-start', I['session_start_subagent'], env)
    ctx = out_of(p).get('additionalContext', '')
    check('codex session-start in a subagent thread: parent session, parent run, own agent id', f'subagent of session {CSID}' in ctx
          and f'run id is {CSID}' in ctx and f'agent id is {CSUB}' in ctx, ctx[:160])
    seeker.reset()
    with open(main, 'a') as fh:
        fh.write('\n' + citem(dict(type='function_call', name='spawn_agent', call_id='call_s1', arguments='{"task": "check buckets"}'),
                              '2026-09-06T12:06:00.000Z'))
    p, _ = chook('subagent-start', I['subagent_start'], dict(env, CTRLB_HOME=os.path.join(WORK, 'home-codex')))
    o = out_of(p)
    ctx = o.get('additionalContext', '')
    check('codex subagent-start: parent session id, parent run id, own agent id', o.get('hookEventName') == 'SubagentStart'
          and f'subagent of session {CSID}' in ctx and f'run id is {CSID}' in ctx and f'agent id is {CSUB}' in ctx, ctx[:160])
    check('codex subagent-start flushed the parent first (only the new line)', [r['tool_name'] for r in seeker.rows] == ['spawn_agent'],
          str([r['tool_name'] for r in seeker.rows]))
    check('codex subagent-start in a session with no run and no relay call: the short rule',
          'no Relay tasks are waiting' in ctx and 'claim(next=true)' not in ctx, ctx[-300:])
    relay_rollout = os.path.join(WORK, 'codex-relay-rollout.jsonl')
    shutil.copyfile(main, relay_rollout)
    with open(relay_rollout, 'a') as fh:
        fh.write('\n' + json.dumps(dict(timestamp='2026-09-06T12:07:00.000Z', type='event_msg', payload=dict(
            type='item_completed', thread_id=CSID, item=dict(type='McpToolCall', id='exec-1', server='ctrlb', tool='task',
                                                             arguments=dict(run_id=CSID, agent=CSID, name='task 01'),
                                                             status='completed')))) + '\n')
    p, _ = chook('subagent-start', dict(I['subagent_start'], transcript_path=relay_rollout), dict(env, CTRLB_HOME=os.path.join(WORK, 'home-codex-relay')))
    ctx = out_of(p).get('additionalContext', '')
    sys.path.insert(0, os.path.join(ROOT, 'lib'))
    import rows_codex
    check("codex: a ctrlb relay call in the root's rollout (Codex has no hook after a tool call) gives the whole rule; "
          'relay_call reads its run and agent', 'claim(next=true)' in ctx and rows_codex.relay_call(relay_rollout) == (CSID, CSID)
          and rows_codex.relay_call(main) is None, ctx[-300:])
    mcp_line = lambda server, run: json.dumps(dict(timestamp='2026-09-06T12:08:00.000Z', type='event_msg', payload=dict(
        type='item_completed', thread_id=CSID, item=dict(type='McpToolCall', id='exec-2', server=server, tool='claim',
                                                         arguments=dict(run_id=run, agent='lead'), status='completed')))) + '\n'
    with open(relay_rollout, 'a') as fh:
        fh.write(mcp_line('other-ctrlb-service', 'not-this-run'))
    other = rows_codex.relay_call(relay_rollout)
    keep_dir = os.path.join(os.environ['CTRLB_HOME'], 'spawn', 'rollouts')
    kept = [json.load(open(os.path.join(keep_dir, f))) for f in os.listdir(keep_dir)]
    size_before = os.path.getsize(relay_rollout)
    with open(relay_rollout, 'a') as fh:
        fh.write(mcp_line('ctrlb', 'review-run') + '{"partial": ')
    named = rows_codex.relay_call(relay_rollout)
    kept2 = [json.load(open(os.path.join(keep_dir, f))) for f in os.listdir(keep_dir)]
    check('codex relay_call: another server is not ctrlb; the part read is kept and only new full lines are read',
          other == (CSID, CSID) and named == ('review-run', 'lead') and any(k['offset'] == size_before for k in kept)
          and any(k['offset'] == os.path.getsize(relay_rollout) - len('{"partial": ') for k in kept2), json.dumps([other, named, kept2])[:300])
    p, _ = chook('subagent-start', dict(I['subagent_start'], transcript_path=relay_rollout), dict(env, CTRLB_HOME=os.path.join(WORK, 'home-codex-relay')))
    ctx = out_of(p).get('additionalContext', '')
    check('codex: a session with no CTRLB_RUN that called a relay tool naming another run keeps its own run in the rule',
          'claim(next=true)' in ctx and f'run id is {CSID}' in ctx and 'review-run' not in ctx, ctx[-300:])
    seeker.reset()
    seeker.state['check'] = 'allow'
    chook('pre-tool', dict(I['pre_tool_patch'], transcript_path=relay_rollout), dict(env, CTRLB_HOME=os.path.join(WORK, 'home-codex-relay')))
    ccall = seeker.calls[0] if seeker.calls else {}
    seeker.reset()
    chook('session-end', dict(I['session_end'], transcript_path=relay_rollout), dict(env, CTRLB_HOME=os.path.join(WORK, 'home-codex-relay')))
    hand = [c for c in seeker.calls if c['path'].endswith('/handoffs')]
    seeker.reset()
    chook('session-end', I['session_end'], dict(env, CTRLB_HOME=os.path.join(WORK, 'home-codex-relay-none')))
    no_hand = [c for c in seeker.calls if c['path'].endswith('/handoffs')]
    check('codex session-end with no CTRLB_RUN: hands off its holds in its own run when its rollout shows a relay call; '
          'not when it shows none', len(hand) == 1 and hand[0]['path'] == f'/ai/relay/agent_sessions/runs/{CSID}/handoffs'
          and hand[0]['body'].get('agent') == CSID and not no_hand, json.dumps([hand, no_hand])[:300])
    check('codex: a later hook of that session uses its own run (the edit check)',
          ccall.get('path') == f'/ai/relay/agent_sessions/runs/{CSID}/check' and (ccall.get('body') or {}).get('agent') == CSID,
          json.dumps(ccall)[:300])
    parsed = []
    real_item = rows_codex._relay_item
    with mock.patch.object(rows_codex, '_relay_item', lambda line: parsed.append(1) or real_item(line)):
        with open(relay_rollout, 'a') as fh:
            fh.write('2}\n' + mcp_line('ctrlb', 'third-run'))
        third = rows_codex.relay_call(relay_rollout)
    check('codex relay_call: a second read parses only the lines added since the first', third == ('third-run', 'lead') and len(parsed) == 1,
          f'{third} {len(parsed)}')
    early = os.path.join(WORK, 'codex-early-bytes-rollout.jsonl')
    with open(early, 'w') as fh:
        fh.write(mcp_line('ctrlb', 'aaa-run') + '{"type": "event_msg", "payload": {"type": "note", "text": "' + 'x' * 200 + '"}}\n')
    first_read = rows_codex.relay_call(early)
    with open(early, 'r+') as fh:       # an earlier line changed in place, far before the kept offset's last 64 bytes
        text = fh.read()
        fh.seek(0)
        fh.write(text.replace('aaa-run', 'zzz-run'))
    with open(early, 'a') as fh:
        fh.write('{"type": "event_msg", "payload": {"type": "note"}}\n')
    second_read = rows_codex.relay_call(early)
    check('codex relay_call: the second read starts at the kept offset (an earlier line changed in place is not read again)',
          first_read == ('aaa-run', 'lead') and second_read == ('aaa-run', 'lead'), json.dumps([first_read, second_read]))
    with open(early, 'a') as fh:
        fh.write(mcp_line('ctrlb', 'late-run'))
    with mock.patch.object(rows_codex, '_relay_item', side_effect=TimeoutError('hook over its budget')):
        timed_out = rows_codex.relay_call(early)
    check("codex relay_call: when the hook's time runs out it answers what it had read before", timed_out == ('aaa-run', 'lead'),
          json.dumps(timed_out))
    rewritten = os.path.join(WORK, 'codex-rewritten-rollout.jsonl')
    with open(rewritten, 'w') as fh:
        fh.write(mcp_line('ctrlb', 'old-run'))
    first = rows_codex.relay_call(rewritten)
    with open(rewritten, 'r+') as fh:       # the same file (same inode), cut and written again, the same size
        fh.truncate(0)
        fh.write(mcp_line('ctrlb', 'new-run'))
    second = rows_codex.relay_call(rewritten)
    with mock.patch.object(rows_codex.common, 'private_dir', side_effect=OSError('read-only')):
        no_keep = rows_codex.relay_call(rewritten)
    check('codex relay_call: a file cut and written again is read again from the start; without a folder to keep the '
          'offset in, the whole file is read', first == ('old-run', 'lead') and second == ('new-run', 'lead')
          and no_keep == ('new-run', 'lead'), json.dumps([first, second, no_keep]))
    sdir = os.path.join(home, 'spawn', 'sessions')
    os.makedirs(sdir, mode=0o700, exist_ok=True)
    root_decision = os.path.join(sdir, CSID + '.json')
    with open(root_decision, 'w') as fh:
        json.dump(dict(parent='7a7a7a7a-0000-4000-8000-0000000000aa', parent_agent='lead', run='run-L', agent='lead.codex-root0000',
                       joined=True, renamed=False), fh)
    try:
        p, _ = chook('session-start', I['session_start_subagent'], dict(env, CTRLB_RUN='run-L'))
        tctx = out_of(p).get('additionalContext', '')
        seeker.reset()
        seeker.state['check'] = 'allow'
        chook('pre-tool', dict(I['pre_tool_patch'], session_id=CSUB, transcript_path=sub), dict(env, CTRLB_RUN='run-L'))
        tcall = seeker.calls[0] if seeker.calls else {}
        check("codex subagent thread with an inherited CTRLB_RUN other than its root's id: its start and a later hook use the "
              "root's run and the root's agent name with the thread id",
              'run id is run-L' in tctx and f'agent id is lead.codex-root0000:{CSUB}' in tctx
              and tcall.get('path') == '/ai/relay/agent_sessions/runs/run-L/check'
              and (tcall.get('body') or {}).get('agent') == f'lead.codex-root0000:{CSUB}', json.dumps([tctx[-200:], tcall.get('body')]))
    finally:
        for f in (root_decision, os.path.join(sdir, CSUB + '.json')):
            try:
                os.remove(f)
            except OSError:
                pass

    def run(state, name='pre_tool_patch', e=None):
        seeker.reset()
        seeker.state['check'] = state
        return chook('pre-tool', I[name], e or env)

    p, dt = run('allow')
    c = seeker.calls[0] if seeker.calls else {}
    check('codex pre-tool allow: exit 0, no output', p.returncode == 0 and not p.stdout.strip(), f'{dt:.2f}s')
    check('codex pre-tool posts the patch paths, cwd and agent to /ai/relay/{dataset}/runs/{run}/check',
          c.get('path') == f'/ai/relay/agent_sessions/runs/{CSID}/check'
          and c.get('body') == {'agent': CSID, 'paths': ['src/schedule/watering.py'], 'cwd': '/work/garden'}, json.dumps(c.get('body')))
    p, _ = run('advisory')
    o = out_of(p)
    ctx = o.get('additionalContext', '')
    check('codex pre-tool advisory: additionalContext naming the holder, no decision', o.get('hookEventName') == 'PreToolUse'
          and 'agent-b' in ctx and 'permissionDecision' not in o and p.returncode == 0, ctx[:120])
    p, _ = run('enforce')
    o = out_of(p)
    reason = o.get('permissionDecisionReason', '')
    check('codex pre-tool enforce: deny with a reason naming the holder and the claim call, exit 0',
          o.get('permissionDecision') == 'deny' and 'agent-b' in reason and p.returncode == 0
          and f'claim(resource="src/panels/Latency.tsx", run_id="{CSID}", agent="{CSID}")' in reason, reason[:160])
    check('codex pre-tool never answers ask', '"ask"' not in p.stdout)
    p, _ = run('enforce_lapsed')
    reason = out_of(p).get('permissionDecisionReason', '')
    check('codex pre-tool lapsed: deny naming the reason and the claim call', out_of(p).get('permissionDecision') == 'deny'
          and '(lapsed)' in reason and f'Call claim(resource="src/panels/Latency.tsx", run_id="{CSID}", agent="{CSID}")' in reason, reason[:160])
    p, _ = run('enforce_allow')
    check('codex pre-tool enforce with no conflict: no output', not p.stdout.strip())
    p, _ = run('self', e=dict(env, CTRLB_AGENT=SID))      # the 'self' answer names SID as the holder
    check('codex pre-tool: a hold of the caller itself is not a conflict', seeker.calls and not p.stdout.strip())
    p, dt = run('slow')
    check('codex pre-tool timeout: fails open within budget plus scheduling headroom', p.returncode == 0 and not p.stdout.strip() and dt < 4.0, f'{dt:.2f}s')
    with open(os.path.join(home, 'hooks.log')) as fh:
        check('codex pre-tool timeout is logged to hooks.log', 'codex pre-tool fail open' in fh.read())
    p, dt = chook('pre-tool', I['pre_tool_patch'], dict(env, CTRLB_URL=f'http://127.0.0.1:{closed_port()}'))
    check('codex pre-tool CtrlB down: fails open', p.returncode == 0 and not p.stdout.strip() and dt < 4.0, f'{dt:.2f}s')
    p, _ = run('error')
    check('codex pre-tool CtrlB 500: fails open', p.returncode == 0 and not p.stdout.strip())
    p, _ = run('enforce', e=dict(env, CTRLB_ENFORCE='off'))
    check('codex pre-tool CTRLB_ENFORCE=off: no call, no output', not seeker.calls and not p.stdout.strip())
    p, _ = run('enforce', 'pre_tool_patch_many')
    check('codex pre-tool: every path of a patch is checked, in order', seeker.calls and seeker.calls[0]['body']['paths'] ==
          ['docs/watering.md', 'src/old_name.rs', 'src/new_name.rs', 'src/unused.rs'] and out_of(p).get('permissionDecision') == 'deny',
          json.dumps(seeker.calls[0]['body'] if seeker.calls else {}))
    p, _ = run('enforce', 'pre_tool_bash_patch')
    check('codex pre-tool: a patch piped to apply_patch from a shell command is checked',
          seeker.calls and seeker.calls[0]['body']['paths'] == ['src/schedule/watering.py'] and out_of(p).get('permissionDecision') == 'deny')
    p, _ = run('enforce', 'pre_tool_bash')
    check('codex pre-tool: a shell command without a patch makes no call', not seeker.calls and not p.stdout.strip())
    p, _ = run('enforce', 'pre_tool_subagent_patch', dict(env, CTRLB_RUN='relay-run-8'))
    c = seeker.calls[0] if seeker.calls else {}
    check('codex pre-tool in a subagent: CTRLB_RUN in the route, the subagent thread id as agent',
          c.get('path') == '/ai/relay/agent_sessions/runs/relay-run-8/check' and c.get('body', {}).get('agent') == CSUB
          and f'agent="{CSUB}"' in out_of(p).get('permissionDecisionReason', ''))
    for mode, inp in (('pre-tool', 'not json'), ('upload', 'not json'), ('no-such-mode', I['stop'])):
        p, _ = chook(mode, inp, env)
        check(f'codex {mode} with {"malformed input" if inp == "not json" else "unknown mode"}: exits 0, no output',
              p.returncode == 0 and not p.stdout.strip())
    p, _ = chook('session-start', I['session_start'], dict(env, CTRLB_OFF='1'))
    check('codex CTRLB_OFF=1: no output', p.returncode == 0 and not p.stdout.strip())


def install(args, chome, extra_env=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith('CTRLB_') and k != 'CODEX_HOME'}
    env.update(CTRLB_HOME=os.path.join(WORK, 'home-install'), CODEX_HOME=chome)
    env.update(extra_env or {})
    return subprocess.run([sys.executable, '-I', INSTALL] + args, capture_output=True, text=True, env=env, timeout=30)


def read(path):
    with open(path, 'rb') as fh:
        return fh.read()


def can_validate_toml():
    """Python before 3.11 needs the optional Codex CLI for installer TOML validation."""
    return sys.version_info >= (3, 11) or shutil.which('codex') is not None


def test_root_install():
    """python3 -I install.py --codex (the command the connect page shows) is codex/install.py with the same options."""
    if not can_validate_toml():
        print('SKIP root Codex installer integration (needs Python 3.11+ or codex)')
        return
    chome = os.path.join(WORK, 'codex-install-root')
    shutil.rmtree(chome, ignore_errors=True)
    os.makedirs(chome)
    url = 'http://127.0.0.1:8095/mcp'
    env = {k: v for k, v in os.environ.items() if not k.startswith('CTRLB_') and k != 'CODEX_HOME'}
    env.update(CTRLB_HOME=os.path.join(WORK, 'home-install'), CODEX_HOME=chome)

    def run(args):
        return subprocess.run([sys.executable, '-I', os.path.join(ROOT, 'install.py')] + args, capture_output=True, text=True,
                              env=env, timeout=30)
    p = run(['--codex', '--mcp-url', url, '--dry-run'])
    check('install.py --codex --dry-run: the diff, nothing written', p.returncode == 0 and '+[mcp_servers.ctrlb]' in p.stdout
          and not os.listdir(chome), p.stderr[-200:])
    p = run(['--codex', '--mcp-url', url])
    first = read(os.path.join(chome, 'config.toml')) if p.returncode == 0 else b''
    p2 = run(['--codex', '--mcp-url', url])
    check('install.py --codex writes the block; a second run changes nothing', b'bearer_token_env_var = "CTRLB_KEY"' in first
          and p2.returncode == 0 and read(os.path.join(chome, 'config.toml')) == first and 'already up to date' in p2.stdout)
    p = run([])
    check('install.py without --codex: usage naming the Claude Code marketplace install, exit 2', p.returncode == 2
          and 'claude plugin marketplace add ctrlb-hq/ctrlb-agent' in p.stdout)


def test_init():
    """install.py init (bin/init.py): the CtrlB block in AGENTS.md and CLAUDE.md, never touching text outside it."""
    import project_rules
    import hooks_shared
    base = os.path.join(WORK, 'init')
    shutil.rmtree(base, ignore_errors=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith('CTRLB_')}
    env['CTRLB_HOME'] = os.path.join(WORK, 'home-init')

    def run(d, *args, script=os.path.join(ROOT, 'install.py')):
        pre = ['init'] if script.endswith('install.py') else []
        return subprocess.run([sys.executable, '-I', script] + pre + list(args) + ['--dir', d], capture_output=True,
                              text=True, env=env, timeout=30)

    def fresh(name, files=None):
        d = os.path.join(base, name)
        os.makedirs(d)
        for n, data in (files or {}).items():
            with open(os.path.join(d, n), 'wb') as fh:
                fh.write(data)
        return d

    def backups(d):
        return sorted(n for n in os.listdir(d) if '.ctrlb-backup-' in n)

    rule = hooks_shared.RECALL.format(sid='SID').replace('session_id SID', 'session_id')
    check('init: the block text is the session-start rule without the session id sentence',
          rule == project_rules.RULE + ' Pass this session_id to every subagent you start and tell it the same rule.')

    d = fresh('new')
    p = run(d, '--codex')
    text = read(os.path.join(d, 'AGENTS.md')).decode() if p.returncode == 0 else ''
    check('init --codex on a new project: AGENTS.md created with the block, no backup', p.returncode == 0
          and text.startswith('<!-- ctrlb:begin -->\n') and text.endswith('<!-- ctrlb:end -->\n') and '\n## CtrlB\n' in text
          and project_rules.RULE in text and not backups(d) and not os.path.exists(os.path.join(d, 'CLAUDE.md')), p.stderr[-200:])
    check('init: the block names the plugin version that wrote it',
          '<!-- written by ctrlb-agent ' + load('../../plugin/.claude-plugin/plugin.json')['version'] + ';' in text)

    mine = b'# Project rules\n\nUse tabs.\n'
    d = fresh('existing', {'AGENTS.md': mine, 'CLAUDE.md': mine})
    p = run(d, '--codex', '--claude')
    a, c = read(os.path.join(d, 'AGENTS.md')), read(os.path.join(d, 'CLAUDE.md'))
    check('init --codex --claude on existing files: block appended after one blank line, the old text kept byte for byte',
          p.returncode == 0 and a == c and a.startswith(mine + b'\n<!-- ctrlb:begin -->\n') and a.count(b'ctrlb:begin') == 1)
    bk = backups(d)
    check('init: one backup per changed file, holding the old text', len(bk) == 2
          and all(read(os.path.join(d, n)) == mine for n in bk), ', '.join(bk))
    p = run(d, '--codex', '--claude')
    check('init run twice: nothing written, no second backup', p.returncode == 0 and 'already up to date' in p.stdout
          and read(os.path.join(d, 'AGENTS.md')) == a and backups(d) == bk)
    p = run(d, '--codex', '--claude', script=os.path.join(ROOT, 'bin', 'init.py'))
    check('bin/init.py is the same command', p.returncode == 0 and 'already up to date' in p.stdout and backups(d) == bk)

    old = (b'# Rules\n\n<!-- ctrlb:begin -->\n## CtrlB\n\nan older text\n<!-- ctrlb:end -->\n\n## After\n\nkept\n')
    d = fresh('markers', {'AGENTS.md': old})
    p = run(d, '--codex')
    new = read(os.path.join(d, 'AGENTS.md'))
    check('init on a file with the markers: only the text between them is replaced', p.returncode == 0
          and new.startswith(b'# Rules\n\n<!-- ctrlb:begin -->\n') and new.endswith(b'<!-- ctrlb:end -->\n\n## After\n\nkept\n')
          and b'an older text' not in new and project_rules.RULE.encode() in new and len(backups(d)) == 1)

    for name, body, line in (('begin-only', b'# Rules\n\n<!-- ctrlb:begin -->\nhalf\n', 3),
                             ('end-only', b'# Rules\nx\ny\n<!-- ctrlb:end -->\n', 4),
                             ('two-blocks', b'<!-- ctrlb:begin -->\n<!-- ctrlb:end -->\n<!-- ctrlb:begin -->\n<!-- ctrlb:end -->\n', 1)):
        d = fresh(name, {'AGENTS.md': body})
        p = run(d, '--codex', '--claude')
        check(f'init refuses a broken block ({name}): exit 2, names line {line}, writes nothing in either file',
              p.returncode == 2 and f'line {line} ' in p.stderr and read(os.path.join(d, 'AGENTS.md')) == body
              and not backups(d) and not os.path.exists(os.path.join(d, 'CLAUDE.md')), p.stderr[-200:])

    d = fresh('remove', {'AGENTS.md': mine})
    run(d, '--codex')
    p = run(d, '--codex', '--remove')
    check('init --remove: the block and the blank line before it go, the old text comes back byte for byte',
          p.returncode == 0 and read(os.path.join(d, 'AGENTS.md')) == mine and len(backups(d)) == 2, p.stdout[-200:])
    p = run(d, '--codex', '--remove')
    check('init --remove with no block: nothing written', p.returncode == 0 and 'nothing to remove' in p.stdout and len(backups(d)) == 2)
    old = b'# Rules\n\n<!-- ctrlb:begin -->\n## CtrlB\n\nold\n<!-- ctrlb:end -->\n\n## After\n'
    d = fresh('remove-middle', {'CLAUDE.md': old})
    p = run(d, '--claude', '--remove')
    check('init --remove of a block in the middle: only the block goes; the blank lines around it, which init did not add, stay',
          p.returncode == 0 and read(os.path.join(d, 'CLAUDE.md')) == b'# Rules\n\n\n## After\n')
    for name, body in (('trailing-blank', b'# Rules\n\nUse tabs.\n\n'), ('crlf-trailing-blank', b'a\r\n\r\n')):
        d = fresh(name, {'AGENTS.md': body})
        run(d, '--codex')
        p = run(d, '--codex', '--remove')
        check(f'init then --remove on a file ending in a blank line ({name}): the same bytes come back',
              p.returncode == 0 and read(os.path.join(d, 'AGENTS.md')) == body)

    d = fresh('dry', {'AGENTS.md': mine})
    p = run(d, '--codex', '--claude', '--dry-run')
    check('init --dry-run: prints the diff, writes nothing, creates nothing', p.returncode == 0 and '+<!-- ctrlb:begin -->' in p.stdout
          and 'dry run, nothing written' in p.stdout and os.listdir(d) == ['AGENTS.md'] and read(os.path.join(d, 'AGENTS.md')) == mine)

    crlf = b'# Rules\r\n\r\nUse tabs.\r\n'
    d = fresh('crlf', {'AGENTS.md': crlf})
    p = run(d, '--codex')
    new = read(os.path.join(d, 'AGENTS.md'))
    check('init on a CRLF file: CRLF kept, no bare LF added', p.returncode == 0 and new.startswith(crlf + b'\r\n<!-- ctrlb:begin -->\r\n')
          and new.count(b'\n') == new.count(b'\r\n'))
    run(d, '--codex', '--remove')
    check('init --remove on a CRLF file: the old bytes come back', read(os.path.join(d, 'AGENTS.md')) == crlf)

    d = fresh('linked', {'AGENTS.md': mine})
    os.symlink('AGENTS.md', os.path.join(d, 'CLAUDE.md'))
    p = run(d, '--codex', '--claude')
    check('init with CLAUDE.md linked to AGENTS.md: the link stays a link, one block written once',
          p.returncode == 0 and os.path.islink(os.path.join(d, 'CLAUDE.md'))
          and read(os.path.join(d, 'AGENTS.md')).count(b'ctrlb:begin') == 1 and len(backups(d)) == 1, p.stdout[-200:])

    d = fresh('noflag')
    p = run(d)
    check('init without --codex or --claude: usage error, exit 2, nothing written', p.returncode == 2 and not os.listdir(d))

    check('init: the subagent rule carries the same after-failure sentence as the session rule',
          'After a command fails, call `find_command` with the command before changing it, then `next_actions` with a session_id and seq it returns to see what followed.' in hooks_shared.SUBAGENT)

    ex = b'# Rules\n\n```md\n<!-- ctrlb:begin -->\nan example\n<!-- ctrlb:end -->\n```\n\n~~~\n<!-- ctrlb:end -->\n~~~\n'
    d = fresh('fenced', {'AGENTS.md': ex})
    p = run(d, '--codex')
    new = read(os.path.join(d, 'AGENTS.md'))
    p2 = run(d, '--codex')
    check('init: markers inside ``` or ~~~ fences are examples; the block is appended after them, and a second run changes nothing',
          p.returncode == 0 and new.startswith(ex + b'\n<!-- ctrlb:begin -->\n') and new.count(b'<!-- ctrlb:begin -->') == 2
          and p2.returncode == 0 and read(os.path.join(d, 'AGENTS.md')) == new, p.stderr[-200:])
    real = ex + b'\n<!-- ctrlb:begin -->\n## CtrlB\n\nold text\n<!-- ctrlb:end -->\n'
    d = fresh('fenced-real', {'AGENTS.md': real})
    p = run(d, '--codex')
    new = read(os.path.join(d, 'AGENTS.md'))
    check('init: with a fenced example and a real block, only the real block is replaced',
          p.returncode == 0 and new.startswith(ex + b'\n<!-- ctrlb:begin -->\n') and b'an example' in new and b'old text' not in new
          and new.count(b'<!-- ctrlb:begin -->') == 2, p.stderr[-200:])

    mixed = b'a\r\nb\n\n<!-- ctrlb:begin -->\nold\n<!-- ctrlb:end -->\n'
    d = fresh('mixed', {'AGENTS.md': mixed})
    p = run(d, '--codex')
    new = read(os.path.join(d, 'AGENTS.md'))
    check('init on mixed line endings: the existing block is replaced, never a second one, in the file\'s main ending (LF)',
          p.returncode == 0 and new.startswith(b'a\r\nb\n\n<!-- ctrlb:begin -->\n') and new.count(b'ctrlb:begin') == 1
          and b'old' not in new and new.count(b'\r\n') == 1, repr(new[:80]))
    mixed_crlf = b'a\r\nb\r\nc\n<!-- ctrlb:begin -->\r\nold\r\n<!-- ctrlb:end -->\r\n'
    d = fresh('mixed-crlf', {'AGENTS.md': mixed_crlf})
    p = run(d, '--codex')
    new = read(os.path.join(d, 'AGENTS.md'))
    check('init on a mostly-CRLF file with one LF line: the block is replaced in CRLF', p.returncode == 0
          and new.count(b'ctrlb:begin') == 1 and b'old' not in new and new.count(b'\n') - new.count(b'\r\n') == 1, repr(new[:80]))

    notes = b'<!-- ctrlb:begin-notes -->\nmine\n<!-- ctrlb:end-notes -->\n'
    d = fresh('lookalike', {'CLAUDE.md': notes})
    p = run(d, '--claude')
    new = read(os.path.join(d, 'CLAUDE.md'))
    check('init: only the whole line <!-- ctrlb:begin --> is a marker; <!-- ctrlb:begin-notes --> is kept and the block appended',
          p.returncode == 0 and new.startswith(notes + b'\n<!-- ctrlb:begin -->\n'), p.stderr[-200:])

    bare = b'# Rules\n\nno newline at the end'
    d = fresh('no-eol', {'AGENTS.md': bare})
    p = run(d, '--codex', '--dry-run')
    check('init --dry-run on a file without a final newline: the diff says so', p.returncode == 0
          and '-no newline at the end\n\\ No newline at end of file\n+no newline at the end\n' in p.stdout
          and read(os.path.join(d, 'AGENTS.md')) == bare, p.stdout[-300:])
    run(d, '--codex')
    added = read(os.path.join(d, 'AGENTS.md'))
    p2 = run(d, '--codex')
    run(d, '--codex', '--remove')
    check('init on a file without a final newline: a second run changes nothing; --remove gives back the same bytes',
          added.startswith(bare + b'\n\n<!-- ctrlb:begin -->') and 'already up to date' in p2.stdout
          and read(os.path.join(d, 'AGENTS.md')) == bare)

    d = fresh('latin1', {'AGENTS.md': b'# R\xe8gles\n'})
    p = run(d, '--codex', '--claude')
    check('init on a file that is not UTF-8: one line naming the file, exit 1, no traceback, nothing written in either file',
          p.returncode == 1 and 'AGENTS.md: not UTF-8 text' in p.stderr and 'Traceback' not in p.stderr
          and len(p.stderr.strip().splitlines()) == 1 and os.listdir(d) == ['AGENTS.md'], p.stderr[-200:])
    d = fresh('unreadable')
    os.makedirs(os.path.join(d, 'AGENTS.md'))
    p = run(d, '--codex')
    check('init when AGENTS.md cannot be read: one line naming it, exit 1, no traceback', p.returncode == 1
          and 'AGENTS.md: cannot read it' in p.stderr and 'Traceback' not in p.stderr, p.stderr[-200:])
    check('init: next_actions is described with the session_id and seq that find_command returns, not with a command',
          all('`next_actions` with the command' not in t and 'then `next_actions` with a session_id and seq it returns' in t
              for t in (hooks_shared.RECALL, hooks_shared.SUBAGENT, project_rules.RULE)))

    for kind in ('```', '~~~'):
        body = b'# Rules\n\n' + kind.encode() + b'sh\nmake test\n'
        d = fresh('unclosed-' + ('backtick' if kind == '```' else 'tilde'), {'AGENTS.md': body})
        p = run(d, '--codex')
        p2 = run(d, '--codex')
        check(f'init refuses a file that ends inside an unclosed {kind} code block: exit 2, names line 3, writes nothing, '
              f'and a rerun does the same', p.returncode == 2 and 'line 3 opens a code block' in p.stderr
              and p2.returncode == 2 and read(os.path.join(d, 'AGENTS.md')) == body and not backups(d), p.stderr[-200:])

    d = fresh('tmp-file', {'AGENTS.md': mine, 'AGENTS.md.ctrlb-tmp': b'not ours\n'})
    p = run(d, '--codex')
    check('init: an existing AGENTS.md.ctrlb-tmp file is left untouched and no temporary file is left behind',
          p.returncode == 0 and read(os.path.join(d, 'AGENTS.md.ctrlb-tmp')) == b'not ours\n'
          and b'ctrlb:begin' in read(os.path.join(d, 'AGENTS.md'))
          and not [n for n in os.listdir(d) if n.endswith('.tmp')], ', '.join(os.listdir(d)))
    d = fresh('tmp-link', {'AGENTS.md': mine, 'target.txt': b'precious\n'})
    os.symlink('target.txt', os.path.join(d, 'AGENTS.md.ctrlb-tmp'))
    p = run(d, '--codex')
    check('init: a symlink named AGENTS.md.ctrlb-tmp is not followed; it and its target are left untouched',
          p.returncode == 0 and os.path.islink(os.path.join(d, 'AGENTS.md.ctrlb-tmp'))
          and read(os.path.join(d, 'target.txt')) == b'precious\n' and b'ctrlb:begin' in read(os.path.join(d, 'AGENTS.md')))

    d = fresh('second-broken', {'AGENTS.md': mine, 'CLAUDE.md': b'<!-- ctrlb:end -->\n'})
    p = run(d, '--codex', '--claude')
    check('init --codex --claude: a broken CLAUDE.md stops the run before AGENTS.md is written', p.returncode == 2
          and 'CLAUDE.md' in p.stderr and read(os.path.join(d, 'AGENTS.md')) == mine and not backups(d), p.stderr[-200:])


def test_recall_search_scope():
    import hooks_shared
    import project_rules
    recall = hooks_shared.RECALL.format(sid=SID)
    subagent = hooks_shared.SUBAGENT.format(sid=SID)
    check('recall search scope: earlier work searches the user sessions without a session filter',
          all("call `search_sessions` without session_id to search this user's sessions." in rule
              for rule in (recall, project_rules.RULE)))
    check('recall search scope: the current session filter is only for its own turns before compaction',
          f"Pass session_id {SID} only to search this session's own turns before compaction." in recall
          and "Pass session_id only to search this session's own turns before compaction." in project_rules.RULE)
    check('recall search scope: subagents filter parent work and omit the filter for earlier work in general',
          f'Call `search_sessions` with session_id {SID} to search your parent\'s work' in subagent
          and "For earlier work in general, omit session_id to search this user's sessions." in subagent)
    docs = [os.path.join(ROOT, 'docs', name) for name in
            ('session-store.md', 'relay-tools.md', 'connect-codex.md', 'connect-claude-code.md')]
    docs.append(os.path.join(ROOT, 'plugin', 'skills', 'recall', 'SKILL.md'))
    check('recall search scope: docs distinguish general recall, pre-compaction turns and parent work',
          all("this user's sessions" in (text := open(path).read()) and "this session's own turns before compaction" in text
              and "parent's" in text for path in docs))
    for harness, script in (('Claude Code', HOOK), ('Codex', CODEX_HOOK)):
        env = base_env('http://127.0.0.1:1', os.path.join(WORK, 'recall-scope-' + harness.replace(' ', '-')))
        inp = dict(session_id=SID, transcript_path='', cwd=WORK, hook_event_name='SessionStart')
        p, _ = hook('session-start', inp, env, script=script)
        check(f'recall search scope: {harness} session-start emits the general and current-session rules',
              p.returncode == 0 and recall_rule(out_of(p).get('additionalContext', ''), SID))
        inp.update(hook_event_name='SubagentStart', agent_id='agent-fixture')
        p, _ = hook('subagent-start', inp, env, script=script)
        check(f'recall search scope: {harness} subagent-start emits the parent-session rule',
              p.returncode == 0 and subagent in out_of(p).get('additionalContext', ''))


def test_project_backup_exclusive():
    import project_rules
    from unittest.mock import patch
    base = os.path.join(WORK, 'backup-exclusive')
    shutil.rmtree(base, ignore_errors=True)
    os.makedirs(base)
    source = os.path.join(base, 'AGENTS.md')
    old = b'# Keep these rules\n'
    stamp = '20260102-030405'
    candidate = source + '.ctrlb-backup-' + stamp
    with patch('project_rules.datetime.datetime') as date:
        date.now.return_value.strftime.return_value = stamp
        with open(source, 'wb') as fh:
            fh.write(old)
        os.chmod(source, 0o640)
        target = os.path.join(base, 'absent.txt')
        os.symlink(target, candidate)
        os.mkdir(candidate + '-1')
        with open(candidate + '-2', 'wb') as fh:
            fh.write(b'existing backup\n')
        backup = project_rules.write(source, 'updated\n')
        check('backup exclusive: dangling symlinks, directories and files are skipped without changing them',
              backup == candidate + '-3' and not os.path.lexists(target) and os.path.islink(candidate)
              and os.path.isdir(candidate + '-1') and read(candidate + '-2') == b'existing backup\n'
              and read(backup) == old and read(source) == b'updated\n'
              and stat.S_IMODE(os.stat(source).st_mode) == 0o640)

    # Insert a collision at the creation call, after any preliminary name check could have run.
    for collision in ('file', 'symlink'):
        source = os.path.join(base, collision + '.md')
        candidate = source + '.ctrlb-backup-' + stamp
        target = os.path.join(base, collision + '-target.txt')
        with open(source, 'wb') as fh:
            fh.write(old)
        with open(target, 'wb') as fh:
            fh.write(b'precious\n')
        real_open, attempts = os.open, []

        def racing_open(path, flags, mode=0o777, *args, **kwargs):
            if str(path).startswith(candidate):
                attempts.append((str(path), flags, mode))
                if str(path) == candidate:
                    if collision == 'symlink':
                        os.symlink(target, path)
                    else:
                        with open(path, 'wb') as fh:
                            fh.write(b'concurrent backup\n')
            return real_open(path, flags, mode, *args, **kwargs)

        with patch('project_rules.datetime.datetime') as date, patch('project_rules.os.open', side_effect=racing_open):
            date.now.return_value.strftime.return_value = stamp
            backup = project_rules.write(source, 'updated\n')
        required = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, 'O_NOFOLLOW', 0)
        check(f'backup exclusive: a racing {collision} is preserved and creation retries a fresh suffix',
              backup == candidate + '-1' and len(attempts) == 2
              and all(flags & required == required and mode in (0o600, 0o644) for _, flags, mode in attempts)
              and read(target) == b'precious\n' and read(backup) == old and read(source) == b'updated\n'
              and (os.path.islink(candidate) if collision == 'symlink' else read(candidate) == b'concurrent backup\n'))

    source = os.path.join(base, 'reserved.md')
    candidate = source + '.ctrlb-backup-' + stamp
    target = os.path.join(base, 'reserved-target.txt')
    saved = candidate + '.saved'
    with open(source, 'wb') as fh:
        fh.write(old)
    with open(target, 'wb') as fh:
        fh.write(b'precious\n')

    def swapped_open(path, flags, mode=0o777, *args, **kwargs):
        fd = real_open(path, flags, mode, *args, **kwargs)
        if str(path) == candidate:
            os.rename(path, saved)
            os.symlink(target, path)
        return fd

    with patch('project_rules.datetime.datetime') as date, patch('project_rules.os.open', side_effect=swapped_open):
        date.now.return_value.strftime.return_value = stamp
        project_rules.write(source, 'updated\n')
    check('backup exclusive: copying uses the reserved descriptor even if its path becomes a symlink',
          os.path.islink(candidate) and read(target) == b'precious\n' and read(saved) == old
          and stat.S_IMODE(os.stat(saved).st_mode) in (0o600, stat.S_IMODE(os.stat(source).st_mode)))


def test_project_list_fences():
    import project_rules
    examples = {
        'bullet': '- ```md\n  <!-- ctrlb:begin -->\n  example to preserve\n  <!-- ctrlb:end -->\n  ```\n',
        'ordered': '1. ~~~md\n   <!-- ctrlb:begin -->\n   example to preserve\n   <!-- ctrlb:end -->\n   ~~~\n',
        'continuation': '- item\n\n  ```md\n  <!-- ctrlb:begin -->\n  example to preserve\n  <!-- ctrlb:end -->\n  ```\n',
        'nested': '- outer\n  - inner\n\n    ```md\n    <!-- ctrlb:begin -->\n    example to preserve\n    <!-- ctrlb:end -->\n    ```\n',
        'nested-marker': '- outer\n    - ~~~md\n      <!-- ctrlb:begin -->\n      example to preserve\n      <!-- ctrlb:end -->\n      ~~~\n',
        'tabs': '- outer\n\t- ```md\n\t  <!-- ctrlb:begin -->\n\t  example to preserve\n\t  <!-- ctrlb:end -->\n\t  ```\n',
        'fence-content': '```md\n- ```\n<!-- ctrlb:begin -->\nexample to preserve\n<!-- ctrlb:end -->\n```\n',
    }
    for name, example in examples.items():
        for nl in ('\n', '\r\n'):
            old = example.replace('\n', nl)
            real = old + nl + project_rules.BEGIN + nl + 'old rule' + nl + project_rules.END + nl
            name_check = f'list fences: {name} ({repr(nl)}) keeps examples through append, update, rerun and removal'
            try:
                added = project_rules.render(old)
                replaced = project_rules.render(real)
            except project_rules.Refused as e:
                check(name_check, False, str(e))
                continue
            check(name_check,
                  added.startswith(old + nl + project_rules.BEGIN + nl)
                  and project_rules.render(added) == added and project_rules.render(added, remove=True) == old
                  and replaced.startswith(old + nl + project_rules.BEGIN + nl) and 'old rule' not in replaced
                  and project_rules.render(old, remove=True) == old)
    outdent = '- ```md\n  <!-- ctrlb:begin -->\n  <!-- ctrlb:end -->\n\n# Outside the list\n'
    check('list fences: an outdent ends the list fence before a real block',
          project_rules.render(outdent).startswith(outdent + '\n' + project_rules.BEGIN + '\n'))
    indented = '    ```md\n' + project_rules.BEGIN + '\nold rule\n' + project_rules.END + '\n    ```\n'
    check('list fences: four-space indentation without a list does not open a fence',
          project_rules._markers(project_rules.split_lines(indented)) == ([1], [3], None))


def test_model_call_guard(seeker):
    """guardModelCalls (docs/relay-hooks.md): with the setting on, a shell command that starts codex exec, codex review
    or claude -p is refused unless a time limit (timeout) runs it. Off by default; reads only the command; calls
    nothing. Joining the run is not its job: the started agent finds its parent (test_spawned_agents)."""
    sys.path.insert(0, os.path.join(ROOT, 'lib'))
    import model_call_guard as g
    on = {'guard_model_calls': True, 'run': ''}

    def reason(cmd, cfg=on):
        return g.check(cfg, cmd)

    refused = {
        'bare codex exec': 'codex exec "fix the parser"',
        'codex alias e': 'codex e "fix it"',
        'codex review': 'cat diff.txt | codex review -',
        'codex option before exec': 'codex -m gpt-6-astra -c x=1 exec "go"',
        'claude -p': 'claude -p "review this"',
        'claude --print with a full path': '/usr/local/bin/claude --model x --print hi',
        'timeout 0 is no limit': 'timeout 0 codex exec x',
        'run and agent but no time limit': 'CTRLB_RUN=r1 CTRLB_AGENT=worker-1 codex exec x',
        'after cd and &&': 'cd repo && codex exec x',
        'on a second line': 'ls\ncodex exec x',
        'inside bash -lc': "cd repo && bash -lc 'codex exec hi' 2>&1",
        'inside $( ) in quotes': 'echo "$(claude -p hi)"',
        'in a subshell': '(cd d; codex exec x) > out.txt',
        'in the background': 'nohup codex exec x > log 2>&1 &',
        'a timeout on another command': 'timeout 5 make build; codex exec x',
        'in $( ) under a timed command (it runs first)': 'timeout 5 echo "$(codex exec hi)"',
        'in backquotes': 'echo `claude -p hi`',
        'in an assignment substitution': 'OUT=$(codex exec hi) make',
        'after if, then': 'if true; then codex exec hi; fi',
        'in a for loop': 'for f in a b; do codex exec "$f"; done',
        'in braces': '{ codex exec x; }',
        'a quoted program name': "'codex' exec hi",
        'a function defined and called': 'review() { codex exec hi; }; review',
        'function keyword form, called on the next line': 'function r { claude -p x; }\nr',
        'nested substitutions with a quoted parenthesis': 'echo "$(echo "$(codex exec "a)b")")"',
        'a subshell-body function, called': 'f() ( codex exec hi ); f',
        'after a function whose body prints a quoted {': "f() { printf '%s' '{'; }; codex exec hi",
        'a call before the function is defined again': 'f() { codex exec hi; }; f; f() { echo ok; }',
        'the second definition is the one called': 'f() { echo ok; }; f() { codex exec hi; }; f',
        'a function defined when another one runs': 'setup() { review() { codex exec hi; }; }; setup; review',
        'a function defined after then, called': 'if true; then f() { codex exec hi; }; fi; f',
        'after --, --help is the prompt': 'codex exec -- --help',
        'a ( ) function body does not change a function outside it':
            'f() { codex exec hi; }; setup() ( f() { echo ok; }; ); setup; f',
    }
    bad = {n: c for n, c in refused.items() if not reason(c)}
    check('guard: refuses each model CLI call with no time limit', not bad, json.dumps(bad))
    allowed = {
        'timeout alone': 'timeout 1200 codex exec "fix it"',
        'assignments and timeout': 'CTRLB_RUN=r1 timeout 1200 codex exec "fix it"',
        'env and timeout options': 'env A=1 timeout -k 10 --signal=TERM 20m claude -p hi',
        'export earlier in the command': 'export CTRLB_RUN=r1 && timeout 600 codex e x',
        'outer timeout around bash -lc': "timeout 1200 bash -lc 'codex exec hi'",
        'timeout from a variable': 'timeout "$LIMIT" codex exec x',
        'codex --version': 'codex --version',
        'codex login status': 'codex login status',
        'codex resume (interactive)': 'codex resume --last',
        'claude mcp list': 'claude mcp list',
        'grep for the text': 'grep -rn "codex exec" docs/',
        'echo the text': 'echo claude -p hi',
        'commit message': 'git commit -m "run claude -p in CI"',
        'heredoc body': "cat > run.sh <<'EOF'\ncodex exec x\nclaude -p y\nEOF\nchmod +x run.sh",
        'unreadable command': 'codex exec "unclosed',
        '$( ) in single quotes is text': "printf %s '$(codex exec hi)'",
        'a quoted ; is text': "printf %s ';' codex exec hi",
        'a quoted && is text': 'echo "a && codex exec hi"',
        'a comment': '# codex exec hi\nls',
        'timed inside a substitution': 'echo "$(timeout 5 codex exec "a)b")"',
        'timeout around bash -c with a substitution inside': "timeout 5 bash -c 'echo $(codex exec hi)'",
        'a function defined and not called': 'review() { codex exec hi; }; echo ready',
        'a timed call inside a called function': 'g() {\n  timeout 60 codex exec x\n}\ng',
        'a function that calls itself': 'f() { f; }; f',
        'a subshell-body function, not called': 'f() ( codex exec hi ); echo ready',
        'a quoted } inside a function body that is not called': "f() { printf '%s' '}'; codex exec hi; }; echo ready",
        'echo } inside a function body that is not called': 'f() { echo }; codex exec hi; }; echo ready',
        'a function defined after then, not called': 'if true; then f() { codex exec hi; }; fi; echo ready',
        'codex exec help': 'codex exec --help',
        'codex review help': 'codex review -h',
        'codex help before the subcommand': 'codex --help exec x',
        'claude -p help': 'claude -p --help',
        'command -v looks a name up': 'command -v codex exec',
        'empty': '',
    }
    bad = {n: reason(c) for n, c in allowed.items() if reason(c)}
    check('guard: lets through timed calls, other CLI commands and text that only names a model CLI', not bad,
          json.dumps(bad)[:400])
    check('guard: off by default (no setting), even for a bare call', g.check({'run': ''}, 'codex exec x') is None)
    r = reason('codex exec x') or ''
    check('guard: the refusal names the call, the time limit and the command to run instead, and asks for no run or name',
          '`codex exec`' in r and 'without a time limit' in r and 'timeout 1200 codex exec' in r
          and 'CTRLB_RUN' not in r and 'CTRLB_AGENT' not in r, r)

    # the setting: environment first, then the config file
    home = os.path.join(WORK, 'home-guard')
    shutil.rmtree(home, ignore_errors=True)
    os.makedirs(home)
    env = base_env(seeker.url, home)
    cfg_of = lambda e: json.loads(subprocess.run(
        [sys.executable, '-I', '-c', 'import sys, json; sys.path.insert(0, ' + repr(os.path.join(ROOT, 'lib')) + '); '
         'import common; print(json.dumps(common.config()["guard_model_calls"]))'], capture_output=True, text=True, env=e).stdout)
    with open(os.path.join(home, 'config.json'), 'w') as fh:
        json.dump({'guardModelCalls': True}, fh)
    check('setting: guardModelCalls in config.json turns it on; CTRLB_GUARD_MODEL_CALLS=0 turns it off; 1 turns it on',
          cfg_of(env) is True and cfg_of(dict(env, CTRLB_GUARD_MODEL_CALLS='0')) is False
          and cfg_of(dict(base_env(seeker.url, os.path.join(WORK, 'home-guard-none')), CTRLB_GUARD_MODEL_CALLS='1')) is True
          and cfg_of(base_env(seeker.url, os.path.join(WORK, 'home-guard-none'))) is False)
    os.remove(os.path.join(home, 'config.json'))

    # the Claude Code hook
    inp = {'session_id': SID, 'transcript_path': '', 'cwd': '/work/dash', 'hook_event_name': 'PreToolUse',
           'tool_name': 'Bash', 'tool_input': {'command': 'codex exec "fix it"', 'description': 'run codex'}}
    seeker.reset()
    p, dt = hook('pre-bash', inp, dict(env, CTRLB_GUARD_MODEL_CALLS='1'))
    o = out_of(p)
    check('pre-bash: a bare codex exec is denied with the refusal, exit 0, no call to CtrlB',
          p.returncode == 0 and o.get('permissionDecision') == 'deny' and '`codex exec`' in o.get('permissionDecisionReason', '')
          and not seeker.calls, f'{json.dumps(o)[:300]} {dt:.2f}s')
    p, _ = hook('pre-bash', dict(inp, tool_input={'command': 'timeout 1200 codex exec x'}), dict(env, CTRLB_GUARD_MODEL_CALLS='1'))
    check('pre-bash: a timed call prints nothing', p.returncode == 0 and not p.stdout.strip(), p.stdout[:200])
    p, _ = hook('pre-bash', inp, env)
    check('pre-bash: setting off prints nothing, calls nothing', p.returncode == 0 and not p.stdout.strip() and not seeker.calls)
    with open(os.path.join(home, 'config.json'), 'w') as fh:
        json.dump({'guardModelCalls': True}, fh)
    p, _ = hook('pre-bash', inp, env)
    on_by_file = out_of(p).get('permissionDecision') == 'deny'
    p, _ = hook('pre-bash', inp, dict(env, CTRLB_GUARD_MODEL_CALLS='off'))
    check('pre-bash: the early exit reads the setting as config does (file on: denied; variable off: nothing)',
          on_by_file and p.returncode == 0 and not p.stdout.strip())
    os.remove(os.path.join(home, 'config.json'))
    p, _ = hook('pre-bash', '{not json', dict(env, CTRLB_GUARD_MODEL_CALLS='1'))
    check('pre-bash: unreadable input fails open', p.returncode == 0 and not p.stdout.strip())

    hk = load('../../plugin/hooks/hooks.json')['hooks']
    guard = [grp for grp in hk['PreToolUse'] if grp.get('matcher') == 'Bash']
    check('hooks.json: PreToolUse Bash runs pre-bash (timeout 5, not async) only for a command that names codex or claude',
          len(guard) == 1 and [h.get('if') for h in guard[0]['hooks']] == ['Bash(*codex*)', 'Bash(*claude*)']
          and all(h['command'].endswith('hook.py" pre-bash') and h['timeout'] == 5 and 'async' not in h for h in guard[0]['hooks']))

    # the Codex hook: Codex matches its shell tools as Bash, with tool_input.command
    cinp = {'session_id': CSID, 'transcript_path': '', 'cwd': '/work/dash', 'hook_event_name': 'PreToolUse',
            'tool_name': 'Bash', 'tool_input': {'command': 'claude -p "review"'}}
    p, _ = chook('pre-bash', cinp, dict(env, CTRLB_GUARD_MODEL_CALLS='1'))
    o = out_of(p)
    check('codex pre-bash: a bare claude -p is denied', p.returncode == 0 and o.get('permissionDecision') == 'deny'
          and '`claude -p`' in o.get('permissionDecisionReason', ''), json.dumps(o)[:200])
    p, _ = chook('pre-bash', cinp, env)
    check('codex pre-bash: setting off prints nothing', p.returncode == 0 and not p.stdout.strip())

    if can_validate_toml():
        chome = os.path.join(WORK, 'codex-guard')
        for extra, want in (({}, False), ({'CTRLB_GUARD_MODEL_CALLS': '1'}, True)):
            shutil.rmtree(chome, ignore_errors=True)
            os.makedirs(chome)
            p = install(['--mcp-url', 'http://127.0.0.1:8095/mcp'], chome, extra)
            text = read(os.path.join(chome, 'config.toml')).decode() if p.returncode == 0 else ''
            has = '[[hooks.PreToolUse]]\nmatcher = "Bash"\n[[hooks.PreToolUse.hooks]]\ntype = "command"\n' in text and ' pre-bash"' in text
            check(f'codex install: the Bash pre-bash hook is written {"only " if want else "not "}when guardModelCalls is '
                  f'{"on" if want else "off"}', p.returncode == 0 and has == want, p.stderr[-200:])

    doc = open(os.path.join(ROOT, 'docs', 'relay-hooks.md')).read()
    skill = open(os.path.join(ROOT, 'plugin', 'skills', 'relay', 'SKILL.md')).read()
    check('docs: relay-hooks.md documents guardModelCalls, its variable, pre-bash and what it does not see',
          'guardModelCalls' in doc and 'CTRLB_GUARD_MODEL_CALLS' in doc and '`pre-bash`' in doc and 'script' in doc)
    check('relay skill: a model CLI call runs under timeout, and the agent it starts joins the run on its own',
          'timeout' in skill and 'guardModelCalls' in skill and 'joinSpawned' in skill)


def _spawn_lib():
    sys.path.insert(0, os.path.join(ROOT, 'lib'))
    import spawn
    return spawn


def test_spawned_agents(seeker):
    """joinSpawned (docs/relay-hooks.md, lib/spawn.py): an agent CLI started from another agent's shell finds that agent
    when it starts, from a process record or the CTRLB_PARENT_* variables, joins its Relay run under its own name when
    that agent is in one, and says who started it. No command text is read."""
    spawn = _spawn_lib()
    main, sub = setup_transcripts()
    I = inputs(main, sub)
    home = os.path.join(WORK, 'home-spawn')
    shutil.rmtree(home, ignore_errors=True)
    env = base_env(seeker.url, home)
    PSID = '7a7a7a7a-0000-4000-8000-0000000000aa'

    # the decision, alone
    lead = dict(session=PSID, run='run-s1', agent='lead', relay=True, how='environment')
    d = spawn.decide({'run': '', 'agent': ''}, SID, lead, 'codex')
    check('decide: a child with no run of its own joins a parent in a run, named <parent>.<program>-<8 of its id>',
          d['joined'] and d['run'] == 'run-s1' and d['agent'] == 'lead.codex-' + SID[:8] and not d['renamed'], json.dumps(d))
    d = spawn.decide({'run': 'run-s1', 'agent': 'lead'}, SID, lead, 'claude')
    check("decide: a child that inherited the parent's run and agent name joins under a name of its own",
          d['joined'] and d['renamed'] and d['agent'] == 'lead.claude-' + SID[:8], json.dumps(d))
    d = spawn.decide({'run': 'bench-4', 'agent': 'worker-2'}, SID, lead, 'claude')
    check('decide: a child with its own run and agent name is left as it is',
          not d['joined'] and not d['renamed'] and d['run'] == 'bench-4' and d['agent'] == 'worker-2', json.dumps(d))
    d = spawn.decide({'run': 'bench-4', 'agent': 'lead'}, SID, lead, 'claude')
    check("decide: its own run but the parent's agent name: stays in its run, renamed",
          not d['joined'] and d['renamed'] and d['run'] == 'bench-4' and d['agent'] == 'lead.claude-' + SID[:8], json.dumps(d))
    d = spawn.decide({'run': '', 'agent': 'lead'}, SID, dict(lead, agent='lead.codex-bbbbbbbb', base_agent='lead'), 'claude')
    check("decide: a grandchild that inherited the first agent's CTRLB_AGENT is renamed below its own parent",
          d['joined'] and d['renamed'] and d['agent'] == 'lead.codex-bbbbbbbb.claude-' + SID[:8], json.dumps(d))
    with mock.patch.dict(os.environ, {'CTRLB_RUN': ''}):
        d = spawn.decide({'run': '', 'agent': '', 'mode': 'local'}, SID, lead, 'codex')
    check('decide: in local mode a child without CTRLB_RUN in its environment joins nothing (its relay tools need it)',
          not d['joined'] and d['run'] == '', json.dumps(d))
    d = spawn.decide({'run': '', 'agent': ''}, SID, dict(lead, relay=False, run=PSID, agent=PSID), 'codex')
    check('decide: a parent in no relay run is not joined, and a parent with no label gives no prefix',
          not d['joined'] and d['agent'] == '' and spawn.child_name(PSID, PSID, 'codex', SID) == 'codex-' + SID[:8], json.dumps(d))

    # the Claude Code parent: CTRLB_PARENT_* in CLAUDE_ENV_FILE, appended, once
    env_file = os.path.join(WORK, 'spawn-env-file')
    with open(env_file, 'w') as fh:
        fh.write('export OTHER_PLUGIN=1\n')
    pin = dict(I['session_start'], session_id=PSID)
    seeker.reset()
    p, _ = hook('session-start', pin, dict(env, CTRLB_RUN='run-s1', CTRLB_AGENT='lead', CLAUDE_ENV_FILE=env_file))
    hook('session-start', pin, dict(env, CTRLB_RUN='run-s1', CTRLB_AGENT='lead', CLAUDE_ENV_FILE=env_file))
    text = open(env_file).read()
    check('parent: session-start appends CTRLB_PARENT_SESSION, _RUN, _AGENT and _RELAY to CLAUDE_ENV_FILE once, keeping other lines',
          text.startswith('export OTHER_PLUGIN=1\n') and text.count(f'export CTRLB_PARENT_SESSION={PSID}\n') == 1
          and 'export CTRLB_PARENT_RUN=run-s1\n' in text and 'export CTRLB_PARENT_AGENT=lead\n' in text
          and 'export CTRLB_PARENT_RELAY=1\n' in text and p.returncode == 0, text)
    check('parent: its own context is unchanged and nothing is written to CtrlB', 'run id is run-s1' in out_of(p).get('additionalContext', '')
          and 'started you' not in out_of(p).get('additionalContext', '') and not [c for c in seeker.calls if c['method'] != 'GET'])

    # a Claude Code child that inherited the parent's variables (and its CTRLB_RUN and CTRLB_AGENT)
    parent_env = dict(CTRLB_PARENT_SESSION=PSID, CTRLB_PARENT_RUN='run-s1', CTRLB_PARENT_AGENT='lead', CTRLB_PARENT_RELAY='1')
    seeker.reset()
    p, _ = hook('session-start', I['session_start'], dict(env, CTRLB_RUN='run-s1', CTRLB_AGENT='lead', **parent_env))
    ctx = out_of(p).get('additionalContext', '')
    name = spawn.child_name('lead', PSID, '', SID)
    check('child: joins the run under its own name and is told who started it, with the parent session to search',
          'run id is run-s1' in ctx and f'agent id is {name}' in ctx and f'lead (session {PSID}) started you' in ctx
          and f'search_sessions` with session_id {PSID}' in ctx and 'Do the work you were asked to do' in ctx, ctx[-900:])
    check('child: is not told to take the next free task', 'claim(next=true)' not in ctx and 'no task and stop' not in ctx)
    posts = [c for c in seeker.calls if c['path'].endswith('/runs/run-s1/posts')]
    check('child: posts one note in the run saying who started it', len(posts) == 1 and posts[0]['body']['agent'] == name
          and f'Started by lead (session {PSID})' in posts[0]['body']['text'], json.dumps(seeker.calls)[:400])
    seeker.reset()
    p, _ = hook('session-start', I['session_start_compact'], dict(env, CTRLB_RUN='run-s1', CTRLB_AGENT='lead', **parent_env))
    check('child after compaction: the same name, no second note', f'agent id is {name}' in out_of(p).get('additionalContext', '')
          and not [c for c in seeker.calls if c['path'].endswith('/posts')])
    seeker.reset()
    hook('upload', I['stop'], dict(env, CTRLB_RUN='run-s1', CTRLB_AGENT='lead', **parent_env))
    rows = [r for r in seeker.rows if r.get('session_id') == SID]
    check("child: every later hook uses the decision: its rows carry the run and its own name, not the parent's",
          rows and all(r.get('run_id') == 'run-s1' and r.get('agent') == name for r in rows),
          json.dumps([(r.get('run_id'), r.get('agent')) for r in rows[:3]]))

    # a parent in no relay run: the child is told who started it and joins nothing
    shutil.rmtree(home, ignore_errors=True)
    seeker.reset()
    quiet = dict(parent_env, CTRLB_PARENT_RUN=PSID, CTRLB_PARENT_AGENT=PSID, CTRLB_PARENT_RELAY='0')
    p, _ = hook('session-start', I['session_start'], dict(env, **quiet))
    ctx = out_of(p).get('additionalContext', '')
    check('child of a parent in no run: told who started it, keeps its own run, nothing sent',
          f'(session {PSID}) started you' in ctx and f'run id is {SID}' in ctx and 'Do the work you were asked' not in ctx
          and not seeker.calls, ctx[-600:])
    # the same parent, once it called a relay tool: the next child joins
    shutil.rmtree(home, ignore_errors=True)
    sys.path.insert(0, os.path.join(ROOT, 'lib'))
    subprocess.run([sys.executable, '-I', '-c', 'import sys; sys.path.insert(0, ' + repr(os.path.join(ROOT, 'lib')) + '); '
                    f'import common; common.SessionRecord({PSID!r}, {PSID!r}).update({PSID!r}, lambda r: r.update(relay=True))'],
                   env=env, check=True)
    seeker.reset()
    p, _ = hook('session-start', I['session_start'], dict(env, **quiet))
    ctx = out_of(p).get('additionalContext', '')
    check('child of a parent that called a relay tool after it started: joins that run (the session id), no label prefix',
          f'run id is {PSID}' in ctx and f'agent id is agent-{SID[:8]}' in ctx, ctx[-600:])

    # joinSpawned off: nothing changes
    shutil.rmtree(home, ignore_errors=True)
    seeker.reset()
    p, _ = hook('session-start', I['session_start'], dict(env, CTRLB_JOIN_SPAWNED='0', CTRLB_RUN='run-s1', CTRLB_AGENT='lead', **parent_env))
    ctx = out_of(p).get('additionalContext', '')
    check('joinSpawned off: the child keeps the inherited name, is told nothing, writes nothing to CtrlB',
          'agent id is lead' in ctx and 'started you' not in ctx and not [c for c in seeker.calls if c['method'] != 'GET']
          and not os.path.exists(os.path.join(home, 'spawn')))

    # the process record: a Codex parent (no environment file) found by the child's process tree. Linux only: the fake
    # agents are links to Python, which on macOS starts again under its own name; real agent CLIs do not.
    if os.path.isdir('/proc/self'):
        shutil.rmtree(home, ignore_errors=True)
        bindir = os.path.join(WORK, 'spawn-bin')
        shutil.rmtree(bindir, ignore_errors=True)
        os.makedirs(bindir)
        for prog in ('codex', 'claude'):
            os.symlink(sys.executable, os.path.join(bindir, prog))
        cin = load('hooks/codex.json')['session_start']
        cin = dict(cin, session_id=PSID, transcript_path='')
        # driver.py runs as "codex" (the parent): its own session-start hook writes its process record, then it starts
        # child.py as "claude" without CTRLB_RUN or CTRLB_AGENT; child.py runs its session-start hook and prints its output
        driver, child = os.path.join(WORK, 'spawn-driver.py'), os.path.join(WORK, 'spawn-child.py')
        with open(os.path.join(WORK, 'spawn-in.json'), 'w') as fh:
            json.dump(dict(parent=cin, child=I['session_start'], codex_hook=CODEX_HOOK, hook=HOOK,
                           claude=os.path.join(bindir, 'claude'), child_py=child), fh)
        with open(driver, 'w') as fh:
            fh.write(textwrap.dedent("""
                import json, os, subprocess, sys
                d = json.load(open(sys.argv[1]))
                subprocess.run([sys.executable, '-I', d['codex_hook'], 'session-start'], input=json.dumps(d['parent']),
                               text=True, capture_output=True)
                env = {k: v for k, v in os.environ.items() if k not in ('CTRLB_RUN', 'CTRLB_AGENT')}
                out = subprocess.run([d['claude'], '-I', d['child_py'], sys.argv[1]], text=True, capture_output=True, env=env)
                sys.stdout.write(out.stdout)
                sys.stderr.write(out.stderr)
            """))
        with open(child, 'w') as fh:
            fh.write(textwrap.dedent("""
                import json, subprocess, sys
                d = json.load(open(sys.argv[1]))
                p = subprocess.run([sys.executable, '-I', d['hook'], 'session-start'], input=json.dumps(d['child']),
                                   text=True, capture_output=True)
                sys.stdout.write(p.stdout)
            """))
        seeker.reset()
        p = subprocess.run([os.path.join(bindir, 'codex'), '-I', driver, os.path.join(WORK, 'spawn-in.json')],
                           capture_output=True, text=True, timeout=60, env=dict(env, CTRLB_RUN='run-s2', CTRLB_AGENT='lead'))
        ctx = out_of(subprocess.CompletedProcess([], 0, p.stdout.strip(), '')).get('additionalContext', '')
        check('process record: a child of a Codex parent finds it with no variables, joins its run as lead.claude-<id>',
              'run id is run-s2' in ctx and f'agent id is lead.claude-{SID[:8]}' in ctx and f'lead (session {PSID}) started you' in ctx,
              (ctx or p.stdout + p.stderr)[-600:])
        j = json.load(open(os.path.join(home, 'spawn', 'sessions', SID + '.json')))
        check('process record: the decision is kept, found by process', j.get('how') == 'process' and j.get('parent') == PSID, json.dumps(j))
    ps_line = '  4242 Sun Nov  2 05:01:14 2026     /opt/homebrew/bin/codex exec "fix it"\n'
    with mock.patch.object(spawn.os.path, 'isdir', lambda d: False), \
            mock.patch('builtins.open', side_effect=FileNotFoundError), \
            mock.patch.object(spawn.subprocess, 'run', lambda *a, **k: subprocess.CompletedProcess(a, 0, ps_line, '')):
        got = spawn._proc(5000)
    check('process lookup without /proc (macOS): ps gives the parent, the program and the start time',
          got == (4242, 'codex', 'Sun Nov  2 05:01:14 2026'), repr(got))
    got = []
    for line in ('  4242 Sun Nov  2 05:01:14 2026     -zsh\n', '  4242 Sun Nov  2 05:01:14 2026     /bin/sh -c python3 -I hook.py\n'):
        with mock.patch.object(spawn.os.path, 'isdir', lambda d: False), \
                mock.patch('builtins.open', side_effect=FileNotFoundError), \
                mock.patch.object(spawn.subprocess, 'run', lambda *a, _l=line, **k: subprocess.CompletedProcess(a, 0, _l, '')):
            got.append(spawn._proc(5000))
    check('process lookup without /proc (macOS): a login shell and /bin/sh are named as shells',
          [g[1] for g in got] == ['zsh', 'sh'], repr(got))
    ptree = {10: (20, 'python3', 'H'), 20: (30, 'sh', 'SH'), 30: (40, 'codex', 'C'), 40: (1, 'zsh', 'Z')}
    with mock.patch.object(spawn, '_proc', lambda pid: ptree.get(pid)):
        ap = spawn.agent_process(10)
    check('own agent process: codex -> sh -> hook finds codex', ap == (30, 'codex', 'C'), repr(ap))
    codex_parent_rollout = os.path.join(WORK, 'spawn-codex-parent.jsonl')
    with open(codex_parent_rollout, 'w') as fh:
        fh.write(json.dumps(dict(type='event_msg', payload=dict(type='item_completed', item=dict(
            type='McpToolCall', server='ctrlb', tool='claim', arguments=dict(run_id='review', agent='lead'))))) + '\n')
    ctree = {100: (200, 'claude', 'S1'), 200: (300, 'codex', 'S2'), 300: (1, 'zsh', 'S3')}
    with mock.patch.object(spawn, '_proc', lambda pid: ctree.get(pid)):
        spawn._write_json(spawn._record_path(200), dict(session=PSID, run=PSID, agent='', relay=False, start='S2',
                                                        program='codex', transcript=codex_parent_rollout))
        fp = spawn.find_parent((100, 'claude', 'S1'), SID, env={})
    os.remove(spawn._record_path(200))
    check("a Codex parent that called a relay tool (seen in its rollout) is in a run; its children join the parent's own "
          'run, not a run the call named', fp and fp['relay'] and fp['run'] == PSID and fp['agent'] == '', json.dumps(fp))
    spawn._write_json(spawn._session_path('root-x'), dict(parent=PSID, parent_agent='lead', run='run-L', agent='lead.codex-1234',
                                                          joined=True, renamed=False))
    spawn.link('thread-y', 'root-x')
    lc = {'run': '', 'agent': '', 'join_spawned': True}
    spawn.apply(lc, 'thread-y')
    check("Codex subagent thread: every later hook reads its root session's run and agent name",
          lc['run'] == 'run-L' and lc['agent'] == 'lead.codex-1234' and spawn.joined('thread-y').get('root') == 'root-x', json.dumps(lc))
    tree = {100: (200, 'claude', 'S1'), 200: (300, 'codex', 'S2'), 300: (1, 'zsh', 'S3')}
    with open(spawn._record_path(200), 'w') as fh:
        json.dump(dict(session=PSID, run='run-s3', agent='lead', relay=True, start='OLD'), fh)
    with mock.patch.object(spawn, '_proc', lambda pid: tree.get(pid)):
        stale = spawn.find_parent((100, 'claude', 'S1'), SID, env={})
        with open(spawn._record_path(200), 'w') as fh:
            json.dump(dict(session=PSID, run='run-s3', agent='lead', relay=True, start='S2'), fh)
        found = spawn.find_parent((100, 'claude', 'S1'), SID, env={})
        own = spawn.find_parent((100, 'claude', 'S1'), PSID, env={})
    os.remove(spawn._record_path(200))
    check('process record: one whose start time does not match its process (a reused pid) is ignored; a match is found; '
          'a record of the session itself is not its parent', stale is None and found and found['run'] == 'run-s3'
          and found['how'] == 'process' and own is None, json.dumps([stale, found, own]))
    p = subprocess.run([sys.executable, '-I', '-c', 'import sys; sys.path.insert(0, ' + repr(os.path.join(ROOT, 'lib'))
                        + '); import spawn; print(spawn.agent_process())'], capture_output=True, text=True, env=env)
    check('own agent process: a hook run by another program (not an agent or a shell under one) has none',
          p.stdout.strip() == 'None', p.stdout + p.stderr[-200:])

    # a session with no run that registers a task is in a relay run from then on: its subagents get the whole rule
    TSID = '7a7a7a7a-0000-4000-8000-0000000000cc'
    p, _ = hook('subagent-start', dict(I['subagent_start'], session_id=TSID), env)
    before = out_of(p).get('additionalContext', '')
    hook('post-tool', dict(session_id=TSID, transcript_path=main, cwd='/work/dash', hook_event_name='PostToolUse',
                           tool_name='mcp__plugin_ctrlb-agent_ctrlb__task',
                           tool_input=dict(run_id=TSID, agent=TSID, name='task 01 parser', text='fix it', paths=['src/p.py']),
                           tool_response=[dict(type='text', text='{"ok": true}')]), env)
    p, _ = hook('subagent-start', dict(I['subagent_start'], session_id=TSID), env)
    after = out_of(p).get('additionalContext', '')
    seeker.reset()
    seeker.state['check'] = 'allow'
    hook('pre-tool', dict(I['pre_tool_edit'], session_id=TSID), env)
    edit_check = [c for c in seeker.calls if c['path'].endswith('/check')]
    check('a session with no run that registered a task: its later hooks check edits in its own run',
          len(edit_check) == 1 and edit_check[0]['path'].endswith(f'/runs/{TSID}/check')
          and edit_check[0]['body'].get('agent') == TSID, json.dumps(seeker.calls)[:300])
    check('a session with no run: its subagent gets the short rule; after a task call, the whole rule in its own run',
          'no Relay tasks are waiting' in before and 'claim(next=true)' not in before
          and 'claim(next=true)' in after and f'run id is {TSID}' in after, after[-300:])
    hooks_json = open(os.path.join(ROOT, 'plugin', 'hooks', 'hooks.json')).read()
    post_groups = [re.compile(g['matcher']) for g in json.loads(hooks_json)['hooks']['PostToolUse']]
    runs_after = lambda t: any(m.search(t) for m in post_groups)
    should = [p + n for p in ('mcp__ctrlb__', 'mcp__plugin_ctrlb-agent_ctrlb__')
              for n in ('post', 'claim', 'release', 'handoff', 'done', 'task', 'brief', 'ask', 'answer', 'contract')]
    should_not = [p + n for p in ('mcp__ctrlb__', 'mcp__plugin_ctrlb-agent_ctrlb__')
                  for n in ('tasks', 'briefs', 'contracts', 'peers', 'search_sessions')]
    check('hooks.json: the post-tool hook runs after every relay tool that writes to a run, and not after tasks, briefs, '
          'contracts or the read tools (matchers read as regular expressions)',
          all(runs_after(t) for t in should) and not any(runs_after(t) for t in should_not),
          json.dumps([t for t in should if not runs_after(t)] + [t for t in should_not if runs_after(t)]))

    # spawnIdleMinutes: the watchdog stops a process whose transcript does not change, and not one whose transcript does
    tr = os.path.join(WORK, 'spawn-transcript.jsonl')
    open(tr, 'w').close()
    victim = subprocess.Popen(['sleep', '60'])
    start = spawn._proc(victim.pid)[2]
    t0 = time.monotonic()
    stop_touch = time.monotonic() + 3
    th = threading.Thread(target=spawn._watcher, args=(dict(pid=victim.pid, start=start, transcript=tr, minutes=1.5 / 60), 0.2, 1))
    th.start()
    while time.monotonic() < stop_touch:
        os.utime(tr)
        time.sleep(0.3)
    alive_while_busy = victim.poll() is None
    try:
        victim.wait(timeout=10)
    except subprocess.TimeoutExpired:
        victim.kill()
    th.join(5)
    check('spawnIdleMinutes: alive while its transcript changes; stopped (SIGTERM) once it stops changing',
          alive_while_busy and victim.returncode == -signal.SIGTERM and time.monotonic() - t0 < 9, f'{victim.returncode}')
    other = subprocess.Popen(['sleep', '60'])
    try:
        ostart = spawn._proc(other.pid)[2]
        spawn._write_json(spawn._record_path(other.pid), dict(session='new-session-after-clear', start=ostart))
        th = threading.Thread(target=spawn._watcher, args=(dict(pid=other.pid, start=ostart, transcript=tr, minutes=0.5 / 60,
                                                                sid='watched-session'), 0.2, 1))
        th.start()
        th.join(8)
        cleared = other.poll() is None and not th.is_alive()
        try:
            spawn._signal(other.pid, 'NOT-ITS-START', None, signal.SIGTERM)
            refused = False
        except ProcessLookupError:
            refused = True
        time.sleep(0.2)
        check('spawnIdleMinutes: the watchdog ends without a signal when the process runs another session (/clear); '
              'without a pidfd a signal goes only to the process with the watched start time',
              cleared and refused and other.poll() is None, f'{cleared} {refused} {other.poll()}')
    finally:
        other.kill()
        other.wait()
        try:
            os.remove(spawn._record_path(other.pid))
        except OSError:
            pass
    check('setting: joinSpawned on by default; spawnIdleMinutes 0 by default and read as minutes',
          json.loads(subprocess.run([sys.executable, '-I', '-c', 'import sys, json; sys.path.insert(0, ' + repr(os.path.join(ROOT, 'lib'))
                                     + '); import common; c = common.config(); print(json.dumps([c["join_spawned"], c["spawn_idle_minutes"]]))'],
                                    capture_output=True, text=True, env=env).stdout) == [True, 0]
          and json.loads(subprocess.run([sys.executable, '-I', '-c', 'import sys, json; sys.path.insert(0, ' + repr(os.path.join(ROOT, 'lib'))
                                         + '); import common; c = common.config(); print(json.dumps([c["join_spawned"], c["spawn_idle_minutes"]]))'],
                                        capture_output=True, text=True,
                                        env=dict(env, CTRLB_JOIN_SPAWNED='off', CTRLB_SPAWN_IDLE_MINUTES='30')).stdout) == [False, 30])

    doc = open(os.path.join(ROOT, 'docs', 'relay-hooks.md')).read()
    check('docs: relay-hooks.md documents joinSpawned, spawnIdleMinutes and how the parent is found',
          'joinSpawned' in doc and 'CTRLB_JOIN_SPAWNED' in doc and 'spawnIdleMinutes' in doc and 'CLAUDE_ENV_FILE' in doc
          and 'process record' in doc)


def test_codex_install():
    if not can_validate_toml():
        print('SKIP Codex installer integration (needs Python 3.11+ or codex)')
        return
    real = os.path.expanduser('~/.codex/config.toml')
    real_before = read(real) if os.path.isfile(real) else None
    chome = os.path.join(WORK, 'codex-install')
    shutil.rmtree(chome, ignore_errors=True)
    os.makedirs(chome)
    cfgp = os.path.join(chome, 'config.toml')
    url = 'http://127.0.0.1:8095/mcp'
    original = 'model = "gpt-6-astra"\n\n[mcp_servers.docs]\ncommand = "docs-mcp"\n\n[[hooks.PreToolUse]]\nmatcher = "Bash"\n' \
               '[[hooks.PreToolUse.hooks]]\ntype = "command"\ncommand = "true"\n'

    p = install(['--mcp-url', url, '--dry-run'], chome)
    check('install --dry-run with no config.toml: prints the diff, writes nothing', p.returncode == 0 and not os.listdir(chome)
          and '+[mcp_servers.ctrlb]' in p.stdout and 'dry run: nothing written' in p.stdout, p.stderr[-200:])
    with open(cfgp, 'w') as fh:
        fh.write(original)
    p = install(['--mcp-url', url, '--dry-run'], chome)
    check('install --dry-run on an existing config.toml: diff only, file and directory unchanged',
          p.returncode == 0 and read(cfgp) == original.encode() and os.listdir(chome) == ['config.toml']
          and p.stdout.startswith('--- ') and '+bearer_token_env_var = "CTRLB_KEY"' in p.stdout)
    p = install(['--mcp-url', url], chome, dict(CTRLB_KEY=KEY))
    first = read(cfgp).decode()
    backups = [n for n in os.listdir(chome) if '.ctrlb-backup-' in n]
    check('install: backup of the previous file first', p.returncode == 0 and len(backups) == 1
          and read(os.path.join(chome, backups[0])) == original.encode(), p.stdout + p.stderr)
    check('install: keeps what was there and adds the block', first.startswith(original) and '[mcp_servers.ctrlb]' in first
          and f'url = "{url}"' in first)
    check('install: the key is never written; bearer_token_env_var names CTRLB_KEY', KEY not in first
          and 'bearer_token_env_var = "CTRLB_KEY"' in first and 'env_http_headers = { "X-CtrlB-Run" = "CTRLB_RUN", "X-CtrlB-Agent" = "CTRLB_AGENT" }' in first)
    hook_cmd = f'python3 -I \\"{os.path.realpath(CODEX_HOOK)}\\"'
    want = {'SessionStart': 'session-start', 'SubagentStart': 'subagent-start', 'PreCompact': 'flush', 'SessionEnd': 'session-end',
            'Stop': 'upload', 'SubagentStop': 'upload', 'PreToolUse': 'pre-tool'}
    check('install: [[hooks.<Event>]] for the seven events, each calling codex/hook.py with its mode',
          all(f'[[hooks.{e}]]\n' in first and f'[[hooks.{e}.hooks]]\ntype = "command"\ncommand = "{hook_cmd} {m}"' in first
              for e, m in want.items()))
    check('install: PreToolUse matcher apply_patch; Stop and SubagentStop async',
          '[[hooks.PreToolUse]]\nmatcher = "apply_patch"\n' in first and first.count('async = true') == 2)
    p = install(['--mcp-url', url], chome, dict(CTRLB_KEY=KEY))
    check('install run twice: config.toml byte-identical, no second backup', p.returncode == 0 and read(cfgp).decode() == first
          and 'already up to date' in p.stdout and len([n for n in os.listdir(chome) if '.ctrlb-backup-' in n]) == 1, p.stdout)
    p = install(['--mcp-url', url, '--dry-run'], chome)
    check('install --dry-run after install: nothing to change', p.returncode == 0 and 'already up to date' in p.stdout)
    p = install(['--mcp-url', 'http://127.0.0.1:9999/mcp', '--dry-run'], chome)
    check('install --dry-run with a new URL: shows the one changed line, writes nothing', read(cfgp).decode() == first
          and '-url = "http://127.0.0.1:8095/mcp"' in p.stdout and '+url = "http://127.0.0.1:9999/mcp"' in p.stdout)

    endpoint = 'http://127.0.0.1:5080/api/org/agent_sessions/_otel/v1/logs'
    p = install(['--mcp-url', url, '--otel'], chome)
    check('install --otel without --otel-endpoint: refused, nothing written', p.returncode == 2 and read(cfgp).decode() == first)
    p = install(['--mcp-url', url, '--otel', '--otel-endpoint', endpoint, '--otel-header', f'Authorization=Bearer {KEY}'], chome,
                dict(CTRLB_KEY=KEY))
    check('install: refuses to write the CtrlB key into an OTEL header', p.returncode == 2 and KEY not in read(cfgp).decode())
    p = install(['--mcp-url', url, '--otel', '--otel-endpoint', endpoint, '--otel-header', 'Authorization=Basic <ingest credentials>'], chome)
    otel = read(cfgp).decode()
    check('install --otel: [otel] and [otel.exporter.otlp-http] with endpoint, protocol json, headers map',
          p.returncode == 0 and '[otel]\nlog_user_prompt = true\nlog_agent_responses = true\n' in otel
          and f'[otel.exporter.otlp-http]\nendpoint = "{endpoint}"\nprotocol = "json"\nheaders = {{ "Authorization" = "Basic <ingest credentials>" }}' in otel)
    p = install(['--mcp-url', url, '--otel', '--otel-endpoint', endpoint, '--otel-header', 'Authorization=Basic <ingest credentials>'], chome)
    check('install --otel run twice: byte-identical', p.returncode == 0 and read(cfgp).decode() == otel and 'already up to date' in p.stdout)

    if shutil.which('codex'):
        env = {k: v for k, v in os.environ.items() if k != 'CODEX_HOME'}
        env['CODEX_HOME'] = chome
        v = subprocess.run(['codex', 'mcp', 'list'], capture_output=True, text=True, env=env, timeout=60)
        check('codex mcp list loads the written config.toml (hooks, mcp server, otel)', v.returncode == 0 and 'ctrlb' in v.stdout
              and 'CTRLB_KEY' in v.stdout, (v.stdout + v.stderr).strip()[-200:])
    else:
        print('SKIP codex mcp list (codex is not installed)')

    p = install(['--uninstall'], chome)
    check('install --uninstall: the original file comes back byte-identical', p.returncode == 0 and read(cfgp) == original.encode(),
          p.stdout + p.stderr)
    with open(cfgp, 'w') as fh:
        fh.write(original + '\n[mcp_servers.ctrlb]\nurl = "http://127.0.0.1:1/mcp"\n')
    before = read(cfgp)
    p = install(['--mcp-url', url], chome)
    check('install: refuses when config.toml already has an MCP server named ctrlb, writes nothing', p.returncode == 2
          and read(cfgp) == before and 'codex mcp remove ctrlb' in p.stderr, p.stderr[-200:])
    with open(cfgp, 'w') as fh:
        fh.write('[otel]\nlog_user_prompt = false\n')
    p = install(['--mcp-url', url, '--otel', '--otel-endpoint', endpoint], chome)
    check('install --otel: refuses when [otel] exists outside the block', p.returncode == 2 and read(cfgp) == b'[otel]\nlog_user_prompt = false\n')
    p = install(['--mcp-url', url, '--config', os.path.join(chome, 'other', 'config.toml')], chome)
    check('install --config: writes the named file', p.returncode == 0 and os.path.isfile(os.path.join(chome, 'other', 'config.toml')))
    p = install([], chome)
    check('install with no MCP URL: refused with the placeholder hint', p.returncode == 2 and '<your CtrlB URL>' in p.stderr)
    real_after = read(real) if os.path.isfile(real) else None
    check('the real ~/.codex/config.toml is untouched', real_before == real_after)


def test_codex_backfill(seeker):
    chome = os.path.join(WORK, 'codex-home')
    env = {k: v for k, v in os.environ.items() if not k.startswith('CTRLB_')}
    env.update(CTRLB_HOME=os.path.join(WORK, 'home-backfill'), CTRLB_URL=seeker.url, CTRLB_KEY=KEY, CODEX_HOME=chome)
    p = subprocess.run([sys.executable, '-I', os.path.join(ROOT, 'bin', 'backfill.py'), '--codex', '--count'], capture_output=True,
                       text=True, env=env, timeout=30)
    check('backfill.py --codex --count lists the rollouts', p.returncode == 0 and '2 Codex rollout files' in p.stdout, p.stdout.strip()[:200])
    seeker.reset()
    p = subprocess.run([sys.executable, '-I', os.path.join(ROOT, 'bin', 'backfill.py'), '--codex', '--project', 'garden', '--yes'],
                       capture_output=True, text=True, env=env, timeout=60)
    check('backfill.py --codex --yes uploads codex rows', p.returncode == 0 and seeker.rows
          and all(r['harness'] == 'codex' for r in seeker.rows) and {r['session_id'] for r in seeker.rows} == {CSID, CSUB},
          f'{len(seeker.rows)} rows')


def test_validate():
    if not shutil.which('claude'):
        print('SKIP claude plugin validate (claude is not installed)')
        return
    for target in (ROOT, os.path.join(ROOT, 'plugin')):
        v = subprocess.run(['claude', 'plugin', 'validate', target], capture_output=True, text=True, timeout=60)
        check(f'claude plugin validate {os.path.relpath(target, ROOT)}', v.returncode == 0 and 'Validation passed' in v.stdout,
              (v.stdout + v.stderr).strip().splitlines()[-1][:200] if (v.stdout + v.stderr).strip() else '')


def test_wait_wording():
    """Claims never wait (wait_s is gone from claim; a refused task claim lists free_tasks), so the session rule, the relay skill and
    the tool docs never offer claim(wait_s), timed_out or a place in line; peers keeps wait_s. They keep what
    belongs_to_task means, ask_about, and contracts (now: read it before building on it, and check a stale one)."""
    sys.path.insert(0, os.path.join(ROOT, 'lib'))
    import hooks_shared as H
    rule = H.RELAY
    check('relay rule: no wait_s, no waiting in line on a claim', 'wait_s' not in rule and 'wait in line' not in rule.replace('Never wait in line', ''))
    check('relay rule: belongs_to_task means claim the task it names', 'If reason is belongs_to_task' in rule and 'claim that task (task in the answer)' in rule)
    check('relay rule: read a contract before building on it; a stale one means read its file',
          'Before you build on a contract, read it with `contracts`. If it is stale, read the file it names and check your '
          'assumptions against it.' in rule)
    check('relay rule: ask about a task\'s ask_about', 'If the claim answers ask_about' in rule)
    skill = open(os.path.join(ROOT, 'plugin', 'skills', 'relay', 'SKILL.md')).read()
    check('relay skill: no claim wait_s, timed_out or position; free_tasks explained',
          'Claim with `wait_s=30`' not in skill and 'timed_out' not in skill and '`position`' not in skill
          and not [ln for ln in skill.splitlines() if 'wait_s' in ln and 'claim(' in ln] and '`free_tasks`' in skill
          and '(`free_tasks: []`), post a note saying you have no task and stop' in skill)
    check('relay skill: belongs_to_task, ask_about, changed_since_read, stale, peers wait_s',
          '`reason: belongs_to_task`' in skill and '`ask_about`' in skill and '`changed_since_read`' in skill
          and 'If it is `stale`' in skill and 'peers(kind="answer", since=<ts>, wait_s=30)' in skill)
    tools = open(os.path.join(ROOT, 'docs', 'relay-tools.md')).read()
    claim_doc = tools[tools.index('### claim'):tools.index('### release')]
    check('relay-tools: claim has no wait_s argument and no timed_out answer; a claim sending wait_s gets 400 naming peers',
          '| `wait_s` |' not in claim_doc and '"reason": "timed_out"' not in tools and '"position"' not in tools
          and 'A claim that sends `wait_s` is refused (HTTP 400)' in claim_doc and 'use peers with wait_s' in claim_doc)
    check('relay-tools: the refused task claim lists free_tasks with since; the empty list means stop',
          '"free_tasks": [{"name": "task 05 mode"' in claim_doc and '"since": 1793000100000000' in claim_doc
          and '`there is no free task in this run`' in claim_doc and '`idle_agents`' in claim_doc)
    check('relay-tools: belongs_to_task answer and ask_about',
          '"reason": "belongs_to_task"' in tools and '"task": "task 03 median"' in tools and '"ask_about"' in tools)
    check('relay-tools: peers since and wait_s, task ask_about, contracts since',
          '| `since` | Only events after this' in tools and '| `wait_s` | When no event matches yet' in tools
          and '| `ask_about` |' in tools and '| `since` | When you last read the contracts' in tools)


def test_takeover_wording():
    """The session rule and the relay skill tell the agent to take over a stalled
    task (or a file held by an agent that stopped) with takeover=true instead of waiting in line; the tool docs give
    takeover, not_stalled, other_user, taken_over_from and stall; a held refusal of an edit carries the takeover hint;
    a held takeover's token is kept like a claim's."""
    sys.path.insert(0, os.path.join(ROOT, 'lib'))
    import hooks_shared as H
    rule = H.RELAY
    v8 = ("If tasks shows a task you need as stalled, claim it with takeover=true, read its evidence and notes, and "
          "continue it. Never wait in line on a stalled task. If a file you need is held by an agent that has stopped, "
          "the hold shows as stalled; take it over the same way.")
    check('relay rule: take over a stalled task or file, never wait in line on it', v8 in rule)
    check('relay rule: nonblocking claims preserve ownership, contracts, questions and test instructions',
          'wait_s' not in rule and registered_rule(rule) and 'If reason is belongs_to_task' in rule
          and "Before you build on a contract, read it with `contracts`. If it is stale, read the file it names" in rule and 'If the claim answers ask_about' in rule
          and "Run the task's test command" in rule and rule.index('another agent has it: pick another') < rule.index(v8))
    skill = open(os.path.join(ROOT, 'plugin', 'skills', 'relay', 'SKILL.md')).read()
    check('relay skill: takeover=true on a stalled task or file, not_stalled, other_user, taken_over_from',
          'claim it with `takeover=true`' in skill and 'Never wait in line on a stalled task' in skill
          and '`reason: not_stalled`' in skill and '`reason: other_user`' in skill and '`taken_over_from`' in skill)
    tools = open(os.path.join(ROOT, 'docs', 'relay-tools.md')).read()
    check('relay-tools: takeover argument and its answers',
          '| `takeover` |' in tools and '"taken_over_from": "agent-b"' in tools and '"reason": "not_stalled"' in tools
          and '`reason: other_user`' in tools and '"stall_after_s": 300' in tools and '`stalled_holds`' in tools)
    check('pre-tool hint: a held refusal suggests takeover=true when the holder has stopped',
          'takeover=true' in H.STALLED_HINT and 'stalled' in H.STALLED_HINT)
    got = H.record_tokens('run-takeover', 'agent-a', 'mcp__ctrlb__claim',
                          {'held': True, 'key': 'name:task 03 median', 'paths': ['path:src/median.py'],
                           'token': 'tok-1', 'taken_over_from': 'agent-b'})
    check('post-tool: a held takeover keeps its token for the task and its files like a claim',
          sorted(got) == ['name:task 03 median', 'path:src/median.py']
          and common.TokenState('run-takeover', 'agent-a').get(['name:task 03 median']) == {'name:task 03 median': 'tok-1'})


def test_done_evidence(seeker):
    """The session rule tells the agent to run the task's test command and give it, its
    exit code and its last output line as the done evidence (the check it ran when the task has none); the
    PostToolUse hook remembers the agent's last test-looking Bash command, its exit code and last output line, and when
    done (or a finished release) is refused for evidence it quotes that command, without calling CtrlB or filling the
    evidence in, and prefers the task's test command when the refusal quotes it; a done drops the agent's tokens
    like a release."""
    sys.path.insert(0, os.path.join(ROOT, 'lib'))
    import hooks_shared as H
    rule = H.RELAY
    v7 = ("Run the task's test command (tasks and claim show it as test); when it passes, call done on the task name "
          "with that command, its exit code and its last output line as the evidence, and next=true. When the task has no test "
          "command, give the check you ran. A task is finished only when the record shows what was checked.")
    check('relay rule: run the task\'s test command and give it as the done evidence; the check you ran when there is none',
          v7 in rule)
    check('relay rule: no completion instruction without task test and evidence details', "call done on the task name with the command you ran and its result" not in rule)
    check('relay rule: task completion, ownership, contracts and questions agree', registered_rule(rule)
          and 'If reason is belongs_to_task' in rule and "Before you build on a contract, read it with `contracts`. If it is stale, read the file it names" in rule and 'If the claim answers ask_about' in rule
          and 'release it with finished=true' not in rule)
    tests = ['pytest -q tests/test_x.py', 'python -m pytest', 'python3 -m unittest discover', 'cd app && go test ./...',
             'npm test', 'npm run test -- --watch=false', 'cargo test --quiet', 'make test', 'make -C sub test', 'python -m unittest -v']
    not_tests = ['ls -la', 'pip install pytest', 'npm install', 'cat pytest.ini', 'go build ./...', 'make build', 'git status']
    check('test commands: pytest, unittest, go test, npm test, cargo test, make test are tests',
          all(H.is_test_command(c) for c in tests), ', '.join(c for c in tests if not H.is_test_command(c)))
    check('test commands: installs, reads and builds are not', not any(H.is_test_command(c) for c in not_tests),
          ', '.join(c for c in not_tests if H.is_test_command(c)))
    check('bash result: stdout object is exit 0 with the last line; an error text carries its exit code',
          H.bash_result({'stdout': 'a\n3 passed in 0.01s\n\n', 'stderr': ''}) == (0, '3 passed in 0.01s')
          and H.bash_result('Error: Exit code 2\nE   assert 1 == 2\nFAILED t.py') == (2, 'FAILED t.py')
          and H.bash_result('Exit code 1\nboom') == (1, 'boom'))

    home = os.path.join(WORK, 'home-done')
    shutil.rmtree(home, ignore_errors=True)
    env = base_env(seeker.url, home)
    base = {'session_id': SID, 'transcript_path': '', 'cwd': '/work/dash', 'hook_event_name': 'PostToolUse'}
    seeker.reset()
    p, _ = hook('post-tool', dict(base, tool_name='Bash', tool_use_id='b1', tool_input={'command': 'pytest -q tests/test_lat.py'},
                                  tool_response={'stdout': '...\n4 passed in 0.03s\n', 'stderr': '', 'interrupted': False}), env)
    check('post-tool Bash test: no output, no CtrlB call', p.returncode == 0 and not p.stdout.strip() and not seeker.calls, p.stdout + p.stderr)
    hook('post-tool', dict(base, tool_name='Bash', tool_use_id='b2', tool_input={'command': 'ls src'}, tool_response={'stdout': 'a.py'}), env)
    refusal = {'done': False, 'resource': 'task 07 latency', 'key': 'name:task 07 latency', 'reason': 'evidence_required',
               'message': 'say which command you ran and its exit code'}
    done_in = dict(base, tool_name='mcp__ctrlb__done', tool_use_id='d1', tool_input={'resource': 'task 07 latency'},
                   tool_response=[{'type': 'text', 'text': json.dumps(refusal)}])
    p, _ = hook('post-tool', done_in, env)
    ctx = out_of(p).get('additionalContext', '')
    check('post-tool done refused for evidence: quotes the last test command, its exit code and the call to make',
          out_of(p).get('hookEventName') == 'PostToolUse' and '`pytest -q tests/test_lat.py` (exit code 0)' in ctx
          and 'done(resource="task 07 latency"' in ctx and '"summary": "4 passed in 0.03s"' in ctx and 'ls src' not in ctx, ctx[:300])
    check('post-tool done refused: CtrlB is not called and nothing is filled in for the agent', not seeker.calls)
    # A failing run that only the transcript shows (no PostToolUse for it) is newer: it is the one quoted.
    tr = os.path.join(WORK, 'done-transcript.jsonl')
    with open(tr, 'w') as fh:
        fh.write(json.dumps({'type': 'assistant', 'timestamp': '2099-01-01T00:00:00.000Z', 'message': {'role': 'assistant', 'content': [
            {'type': 'tool_use', 'id': 'x1', 'name': 'Bash', 'input': {'command': 'go test ./...'}}]}}) + '\n')
        fh.write(json.dumps({'type': 'user', 'timestamp': '2099-01-01T00:00:01.000Z', 'message': {'role': 'user', 'content': [
            {'type': 'tool_result', 'tool_use_id': 'x1', 'is_error': True, 'content': 'Exit code 1\n--- FAIL: TestLat\nFAIL\tdash/lat'}]}}) + '\n')
    p, _ = hook('post-tool', dict(done_in, transcript_path=tr), env)
    ctx = out_of(p).get('additionalContext', '')
    check('post-tool done refused: a newer failing run in the transcript is quoted with its exit code',
          '`go test ./...` (exit code 1)' in ctx and 'otherwise run the task\'s test first' in ctx, ctx[:300])
    rel_in = dict(base, tool_name='mcp__ctrlb__release', tool_use_id='r1', tool_input={'resource': 'task 07 latency'},
                  tool_response=[{'type': 'text', 'text': json.dumps({'released': 0, 'finished': False, 'resource': 'task 07 latency',
                                                                      'key': 'name:task 07 latency', 'reason': 'evidence_required',
                                                                      'deprecated': 'finished is replaced by done(resource)'})}])
    p, _ = hook('post-tool', rel_in, env)
    check('post-tool release refused for evidence: the same hint', '`pytest -q tests/test_lat.py` (exit code 0)' in out_of(p).get('additionalContext', ''))
    fresh = base_env(seeker.url, os.path.join(WORK, 'home-done-empty'))
    p, _ = hook('post-tool', done_in, fresh)
    check('post-tool done refused with no test run seen: tells the agent to run the test first',
          "Run the task's test, then call done(" in out_of(p).get('additionalContext', ''))
    # the refusal quotes the task's test command. It wins over a last test command that is not it.
    task_test = 'python3 -m pytest tests/test_lat.py'
    quoted = dict(refusal, test=task_test, message=refusal['message'] + "; the task's test command is: " + task_test)
    p, _ = hook('post-tool', dict(done_in, transcript_path=tr, tool_response=[{'type': 'text', 'text': json.dumps(quoted)}]), env)
    ctx = out_of(p).get('additionalContext', '')
    check('post-tool done refused quoting the task\'s test: shows that test, not the last other test command',
          f"The task's test command is `{task_test}`." in ctx and 'The last test command you ran, `go test ./...`, is not it.' in ctx
          and f'"command": "{task_test}"' in ctx and '<its exit code>' in ctx, ctx[:400])
    only_msg = dict(quoted)
    del only_msg['test']
    p, _ = hook('post-tool', dict(done_in, tool_response=[{'type': 'text', 'text': json.dumps(only_msg)}]), fresh)
    ctx = out_of(p).get('additionalContext', '')
    check('post-tool done refused: the test is read from the message when the answer has no test field',
          f"The task's test command is `{task_test}`. Run it;" in ctx, ctx[:300])
    hook('post-tool', dict(base, tool_name='Bash', tool_use_id='b3', tool_input={'command': 'cd /work/dash && ' + task_test + ' -q'},
                           tool_response={'stdout': '...\n2 passed in 0.01s\n', 'stderr': ''}), fresh)
    p, _ = hook('post-tool', dict(done_in, tool_response=[{'type': 'text', 'text': json.dumps(quoted)}]), fresh)
    ctx = out_of(p).get('additionalContext', '')
    check('post-tool done refused: a last test command that runs the task\'s test is quoted with its result',
          f'`cd /work/dash && {task_test} -q` (exit code 0), the task\'s test' in ctx and '"summary": "2 passed in 0.01s"' in ctx, ctx[:400])
    check('names_test: white space collapsed, a cd before and a flag after still match',
          H.names_test('cd x &&  python3 -m pytest   tests/test_lat.py -q', task_test) and not H.names_test('python3 -I -c "x"', task_test)
          and not H.names_test('pytest', ''))
    # A done that finished the task: no output, and the task's tokens are dropped like a release's.
    hook('post-tool', dict(base, tool_name='mcp__ctrlb__claim', tool_use_id='c1', tool_input={'resource': 'task 07 latency'},
                           tool_response=[{'type': 'text', 'text': json.dumps({'held': True, 'key': 'name:task 07 latency',
                                                                               'paths': ['path:src/panels/Latency.tsx'], 'token': 'tok-9'})}]), env)
    tok_file = os.path.join(home, 'state', 'tokens-' + hashlib.sha1(f'{SID}\0{SID}'.encode()).hexdigest() + '.json')

    def tokens():
        try:
            return json.load(open(tok_file))
        except (OSError, ValueError):
            return {}
    had = tokens().get('path:src/panels/Latency.tsx')
    p, _ = hook('post-tool', dict(done_in, tool_response=[{'type': 'text', 'text': json.dumps(
        {'done': True, 'resource': 'task 07 latency', 'key': 'name:task 07 latency', 'paths': ['path:src/panels/Latency.tsx']})}]), env)
    gone = tokens().get('path:src/panels/Latency.tsx')
    check('post-tool done: no output, the task\'s tokens dropped', not p.stdout.strip() and had and not gone, f'{had} {gone}')
    # The same claim and release under the plugin install's tool names keep and drop the token the same way.
    hook('post-tool', dict(base, tool_name='mcp__plugin_ctrlb-agent_ctrlb__claim', tool_use_id='c2', tool_input={'resource': 'task 08 plugin'},
                           tool_response=[{'type': 'text', 'text': json.dumps({'held': True, 'key': 'name:task 08 plugin',
                                                                               'paths': ['path:src/panels/Plugin.tsx'], 'token': 'tok-10'})}]), env)
    had = tokens().get('path:src/panels/Plugin.tsx')
    hook('post-tool', dict(base, tool_name='mcp__plugin_ctrlb-agent_ctrlb__release', tool_use_id='r2', tool_input={'resource': 'task 08 plugin'},
                           tool_response=[{'type': 'text', 'text': json.dumps({'released': 1, 'key': 'name:task 08 plugin',
                                                                               'paths': ['path:src/panels/Plugin.tsx']})}]), env)
    gone = tokens().get('path:src/panels/Plugin.tsx')
    check('post-tool: claim and release under the plugin tool names keep and drop the token', had == 'tok-10' and not gone, f'{had} {gone}')
    hk = load('../../plugin/hooks/hooks.json')['hooks']['PostToolUse']
    check('hooks.json: a second PostToolUse group for Bash and the done tool', len(hk) == 3
          and hk[1].get('matcher') == 'Bash|mcp__ctrlb__done|mcp__plugin_ctrlb-agent_ctrlb__done'
          and hk[1]['hooks'][0]['command'].endswith('hook.py" post-tool'))
    skill = open(os.path.join(ROOT, 'plugin', 'skills', 'relay', 'SKILL.md')).read()
    check('relay skill: done with evidence, evidence_required', 'call `done`' in skill and '`evidence`' in skill
          and '`reason: evidence_required`' in skill)
    check('relay skill: the task\'s test command shown by tasks and claim is the done evidence',
          "Run the task's test command (`tasks` and `claim` show it as `test`)" in skill and 'give the check you ran' in skill
          and 'When the refusal carries `test`' in skill)
    tools = open(os.path.join(ROOT, 'docs', 'relay-tools.md')).read()
    check('relay-tools: done, its evidence and refusal, stalled tasks', '#### done (finish a task)' in tools and '| `evidence` |' in tools
          and '"reason": "evidence_required"' in tools and '`stalled`' in tools)
    check('relay-tools: task test, the claim and tasks answers, the refusal\'s test, evidence_matches_test',
          '| `test` | Optional. The command that tests the task' in tools and '"test": "python3 -m pytest tests/test_median.py"' in tools
          and "`test` when the task's registration names one" in tools and '`evidence_matches_test`' in tools and '`done_with_task_test`' in tools)


def test_round6_hooks(seeker):
    """The session-end hook hands off every hold of the agent in one call
    (handoff all=true, no receiver) with a text from the session's own record (edited files, last test and exit code,
    last note), then flushes; CtrlB down or slow means nothing is written and the hook ends inside the SessionEnd
    budget. Every Bash command is recorded with its exit code (PostToolUse and PostToolUseFailure). PreToolUse on done
    witnesses the evidence: a matching command and exit code of this session adds witnessed=true (and a missing exit
    code), a mismatch is denied naming what the hook saw, no evidence command gives no output, and a witnessed the
    agent set itself is dropped."""
    sys.path.insert(0, os.path.join(ROOT, 'lib'))
    import hooks_shared as H
    rule = H.RELAY
    check('relay rule: redirect to a free task; an empty list means note and stop',
          'If a claim is refused and the answer lists free tasks, claim one of them next. If the list is empty, post a note '
          'saying you have no task and stop.' in rule and 'wait_s' not in rule)
    # The hooks manage autoSlots files, so the session rule needs no file-specific slot instructions.
    check('relay rule: shared-file slots are handled by hooks without file-specific instructions',
          'Edit REGISTRY.md only between' not in rule and 'REGISTRY.md' not in rule)
    home = os.path.join(WORK, 'home-r6')
    shutil.rmtree(home, ignore_errors=True)
    run = 'run-r6'
    env = base_env(seeker.url, home, CTRLB_RUN=run, CTRLB_AGENT='agent-1')
    base = {'session_id': SID, 'transcript_path': '', 'cwd': '/work/dash'}
    post = dict(base, hook_event_name='PostToolUse')
    seeker.reset()
    hook('post-tool', dict(post, tool_name='Edit', tool_input={'file_path': '/work/dash/src/lat.py', 'old_string': 'a', 'new_string': 'b'},
                           tool_response={'filePath': '/work/dash/src/lat.py'}), env)
    hook('post-tool', dict(post, tool_name='Write', tool_input={'file_path': '/work/dash/tests/test_lat.py', 'content': 'x'},
                           tool_response={'filePath': '/work/dash/tests/test_lat.py', 'type': 'create'}), env)
    hook('post-tool', dict(post, tool_name='Bash', tool_input={'command': 'ls src'}, tool_response={'stdout': 'lat.py\n', 'stderr': ''}), env)
    p, _ = hook('post-tool-failure', dict(base, hook_event_name='PostToolUseFailure', tool_name='Bash',
                                          tool_input={'command': 'python3 -m pytest tests/test_lat.py'},
                                          error='Exit code 1\nE   assert 2 == 3\n1 failed in 0.02s', is_interrupt=False), env)
    check('post-tool-failure Bash: no output, exits 0, no CtrlB call', p.returncode == 0 and not p.stdout.strip() and not seeker.calls)
    hook('post-tool', dict(post, tool_name='mcp__ctrlb__post', tool_input={'text': 'lat: half done, p99 left', 'run_id': run},
                           tool_response=[{'type': 'text', 'text': '{"ok": true}'}]), env)
    saved, common.HOME = common.HOME, home        # read the record the hooks wrote under their CTRLB_HOME
    try:
        rec = common.SessionRecord(run, 'agent-1').get(SID)
    finally:
        common.HOME = saved
    check('session record: every Bash command with its exit code (a failure from PostToolUseFailure), edits relative, the note',
          [(b['command'], b['exit_code']) for b in rec['bash']] == [('ls src', 0), ('python3 -m pytest tests/test_lat.py', 1)]
          and rec['bash'][1]['summary'] == '1 failed in 0.02s' and rec['edited'] == ['src/lat.py', 'tests/test_lat.py']
          and rec['note']['text'] == 'lat: half done, p99 left', json.dumps(rec)[:300])
    p, dt = hook('session-end', dict(base, hook_event_name='SessionEnd', reason='other'), env)
    calls = [c for c in seeker.calls if c['path'].endswith('/handoffs')]
    body = calls[0]['body'] if calls else {}
    check('session-end: one handoff call, all=true, no receiver, the agent; the path names the run',
          len(calls) == 1 and body.get('all') is True and body.get('to') == '' and body.get('agent') == 'agent-1'
          and calls[0]['path'] == f'/ai/relay/{common.DEFAULT_DATASET}/runs/{run}/handoffs', json.dumps(calls)[:300])
    text = body.get('text') or ''
    check('session-end: the text names the edited files, the last test and its exit code, and the last note',
          'Edited: src/lat.py, tests/test_lat.py.' in text and '`python3 -m pytest tests/test_lat.py` exit 1' in text
          and '1 failed in 0.02s' in text and 'Last note: lat: half done, p99 left' in text and len(text) <= 1500, text)
    check('session-end: exits 0 with no output', p.returncode == 0 and not p.stdout.strip())
    order = [c['path'].rsplit('/', 1)[-1] for c in seeker.calls]
    check('session-end: the handoff goes before the transcript flush', order and order[0] == 'handoffs', ','.join(order))
    seeker.reset()
    seeker.state['handoff'] = 'slow'
    p, dt = hook('session-end', dict(base, hook_event_name='SessionEnd', reason='other'), env)
    seeker.state['handoff'] = 'ok'
    check('session-end, CtrlB slow: gives up within the SessionEnd budget plus scheduling headroom, exits 0 quietly',
          p.returncode == 0 and not p.stdout.strip() and not p.stderr.strip() and dt < 3.0, f'{dt:.2f}s')
    down = base_env(f'http://127.0.0.1:{closed_port()}', home, CTRLB_RUN=run, CTRLB_AGENT='agent-1')
    p, dt = hook('session-end', dict(base, hook_event_name='SessionEnd', reason='other'), down)
    check('session-end, CtrlB down: nothing written, exits 0 quietly, fast', p.returncode == 0 and not p.stdout.strip()
          and not p.stderr.strip() and dt < 3.0, f'{dt:.2f}s')
    seeker.reset()
    plain = base_env(seeker.url, os.path.join(WORK, 'home-r6-plain'))
    hook('session-end', dict(base, hook_event_name='SessionEnd', reason='other'), plain)
    check('session-end outside a relay run (no CTRLB_RUN, no relay tool called): no handoff call',
          not [c for c in seeker.calls if c['path'].endswith('/handoffs')])
    fresh = dict(rec, bash=[], edited=[], note=None)
    check('handoff text with nothing recorded', H.handoff_text(fresh) == 'Session ended. Edited: nothing. Last test: none run.')

    # The witness.
    pre = dict(base, hook_event_name='PreToolUse', tool_name='mcp__ctrlb__done')
    hook('post-tool', dict(post, tool_name='Bash', tool_input={'command': 'cd /work/dash && python3 -m pytest tests/test_lat.py -q'},
                           tool_response={'stdout': '..\n2 passed in 0.01s\n', 'stderr': ''}), env)
    ev_in = {'resource': 'task 07 latency', 'run_id': run, 'agent': 'agent-1',
             'evidence': {'command': 'cd /work/dash && python3 -m pytest tests/test_lat.py -q', 'exit_code': 0, 'summary': '2 passed in 0.01s'}}
    p, _ = hook('pre-tool', dict(pre, tool_input=ev_in), env)
    o = out_of(p)
    check('witness: the complete command of this session, same exit code: allow with witnessed=true, the rest kept',
          o.get('permissionDecision') == 'allow' and o.get('updatedInput', {}).get('witnessed') is True
          and o['updatedInput'].get('resource') == 'task 07 latency' and o['updatedInput']['evidence']['summary'] == '2 passed in 0.01s', json.dumps(o)[:300])
    no_code = dict(ev_in, evidence={'command': 'cd /work/dash && python3 -m pytest tests/test_lat.py -q'})
    o = out_of(hook('pre-tool', dict(pre, tool_input=no_code), env)[0])
    check('witness: no exit code given: filled from the record (the newest run, exit 0)',
          o.get('permissionDecision') == 'allow' and o['updatedInput']['evidence'].get('exit_code') == 0 and o['updatedInput']['witnessed'] is True,
          json.dumps(o)[:300])
    lie = dict(ev_in, witnessed=True, evidence={'command': 'python3 -m pytest tests/test_other.py', 'exit_code': 0})
    o = out_of(hook('pre-tool', dict(pre, tool_input=lie), env)[0])
    reason = o.get('permissionDecisionReason', '')
    check('witness: a command this session never ran is denied, naming the last test it did run, even with witnessed set',
          o.get('permissionDecision') == 'deny' and 'never ran it' in reason
          and '`cd /work/dash && python3 -m pytest tests/test_lat.py -q` (exit code 0)' in reason and 'updatedInput' not in o, reason)
    wrong = dict(ev_in, evidence={'command': 'python3 -m pytest tests/test_lat.py', 'exit_code': 3})
    hook('post-tool-failure', dict(base, hook_event_name='PostToolUseFailure', tool_name='Bash',
                                   tool_input={'command': 'python3 -m pytest tests/test_lat.py'}, error='Exit code 1\n1 failed'), env)
    o = out_of(hook('pre-tool', dict(pre, tool_input=wrong), env)[0])
    reason = o.get('permissionDecisionReason', '')
    check('witness: the command ran but with another exit code: denied, naming the exit code it saw',
          o.get('permissionDecision') == 'deny' and 'with exit code 3' in reason and 'this session ran it with exit code 1' in reason, reason)
    p, _ = hook('pre-tool', dict(pre, tool_input={'resource': 'task 07 latency', 'run_id': run}), env)
    check('witness: a done with no evidence command: no output (CtrlB answers evidence_required)', p.returncode == 0 and not p.stdout.strip())
    o = out_of(hook('pre-tool', dict(pre, tool_input={'resource': 'task 07 latency', 'witnessed': True, 'evidence': 'it works'}), env)[0])
    check('witness: a witnessed the agent set itself with no evidence command is dropped',
          o.get('permissionDecision') == 'allow' and 'witnessed' not in o.get('updatedInput', {'witnessed': 1}), json.dumps(o)[:200])
    other = base_env(seeker.url, home, CTRLB_RUN=run, CTRLB_AGENT='agent-1')
    o = out_of(hook('pre-tool', dict(pre, session_id='5f0c7a52-0000-4000-8000-0000000000ff', tool_input=ev_in), other)[0])
    check('witness: only this session counts (another session of the same agent saw nothing)',
          o.get('permissionDecision') == 'deny' and 'no Bash command in this session' in o.get('permissionDecisionReason', ''),
          json.dumps(o)[:200])
    hk = load('../../plugin/hooks/hooks.json')['hooks']
    check('hooks.json: PreToolUse on the done tool, PostToolUse on edits and post, PostToolUseFailure on Bash',
          len(hk['PreToolUse']) == 4 and hk['PreToolUse'][1]['matcher'] == 'mcp__ctrlb__done|mcp__plugin_ctrlb-agent_ctrlb__done'
          and hk['PostToolUse'][2]['matcher'].startswith('Edit|Write|MultiEdit|NotebookEdit|mcp__ctrlb__post')
          and hk['PostToolUseFailure'][0]['matcher'] == 'Bash' and hk['SessionEnd'][0]['hooks'][0]['command'].endswith('session-end'))
    # Codex: the same session-end handoff in a relay run, with what it can say.
    seeker.reset()
    shutil.rmtree(os.path.join(WORK, 'home-r6-codex'), ignore_errors=True)
    cenv = base_env(seeker.url, os.path.join(WORK, 'home-r6-codex'), CTRLB_RUN=run, CTRLB_AGENT='codex-1')
    p, dt = chook('session-end', {'session_id': CSID, 'transcript_path': '', 'cwd': '/work/dash', 'hook_event_name': 'SessionEnd'}, cenv)
    calls = [c for c in seeker.calls if c['path'].endswith('/handoffs')]
    check('codex session-end: one open handoff of all holds in a relay run, exits 0',
          p.returncode == 0 and len(calls) == 1 and calls[0]['body'].get('all') is True and calls[0]['body'].get('to') == ''
          and 'Codex' in calls[0]['body'].get('text', ''), json.dumps(calls)[:300])


def test_handoff_redaction(seeker):
    import hooks_shared as shared
    marker = 'synthetic-' + 'handoff-value-001'
    other = 'synthetic-' + 'handoff-value-002'
    command = 'pytest --api-key=' + marker
    summary = 'PASSWORD="prefix ' + marker + ' suffix"'
    note = 'TOKEN=' + marker
    run, agent = 'run-redacted-handoff', 'agent-1'
    home = os.path.join(WORK, 'home-redacted-handoff')
    shutil.rmtree(home, ignore_errors=True)
    env = base_env(seeker.url, home, CTRLB_RUN=run, CTRLB_AGENT=agent)
    base = dict(session_id=SID, cwd='/work/dash', transcript_path='')
    post = dict(base, hook_event_name='PostToolUse')
    hook('post-tool', dict(post, tool_name='Bash', tool_input={'command': command},
                           tool_response={'exit_code': 0, 'stdout': summary}), env)
    hook('post-tool', dict(post, tool_name='mcp__ctrlb__post', tool_input={'text': note},
                           tool_response={'ok': True}), env)
    with mock.patch.object(common, 'HOME', home):
        rec = common.SessionRecord(run, agent).get(SID)
        check('session record retains raw commands locally for witness comparison', rec['bash'][0]['command'] == command)
        text = shared.handoff_text(rec)
        check('handoff text redacts recorded commands, output and notes before shortening',
              marker not in text and text.count('[REDACTED]') == 3 and 'suffix' not in text)
        long = dict(rec, note={'text': 'PASSWORD="' + ('word ' * 60) + marker + '"'})
        check('handoff shortening does not expose part of a quoted credential',
              'word' not in shared.handoff_text(long) and marker not in shared.handoff_text(long))
    seeker.reset()
    p, _ = hook('session-end', dict(base, hook_event_name='SessionEnd'), env)
    calls = [c for c in seeker.calls if c['path'].endswith('/handoffs')]
    check('automatic session-end handoff sends only redacted recorded text',
          p.returncode == 0 and len(calls) == 1 and marker not in json.dumps(calls[0]['body'])
          and calls[0]['body']['text'].count('[REDACTED]') == 3)
    cfg = dict(url=seeker.url, key=KEY, dataset=common.DEFAULT_DATASET)
    seeker.reset()
    common.handoff_all(cfg, run, agent, command + '\n' + summary + '\n' + note)
    check('shared handoff sender redacts text for either harness',
          len(seeker.calls) == 1 and marker not in json.dumps(seeker.calls[0]['body']))
    pre = dict(base, hook_event_name='PreToolUse', tool_name='mcp__ctrlb__done')
    evidence = {'command': command, 'summary': summary}
    o = out_of(hook('pre-tool', dict(pre, tool_input={'resource': 'task', 'evidence': evidence}), env)[0])
    check('witness compares the raw command and returns redacted evidence with the observed exit code',
          o.get('permissionDecision') == 'allow' and o['updatedInput'].get('witnessed') is True
          and o['updatedInput']['evidence']['exit_code'] == 0 and marker not in json.dumps(o))
    for ev in ({'command': command.replace(marker, other), 'exit_code': 0},
               {'command': command, 'exit_code': 3}):
        o = out_of(hook('pre-tool', dict(pre, tool_input={'resource': 'task', 'evidence': ev}), env)[0])
        check('witness rejects a different raw credential or exit code and redacts its refusal',
              o.get('permissionDecision') == 'deny' and marker not in json.dumps(o) and other not in json.dumps(o))
    for test in ('', command, 'pytest --api-key=' + other):
        refusal = {'done': False, 'reason': 'evidence_required', 'resource': 'task', 'test': test}
        p, _ = hook('post-tool', dict(post, tool_name='mcp__ctrlb__done', tool_input={'resource': 'task'},
                                     tool_response=refusal), env)
        context = out_of(p).get('additionalContext', '')
        check('evidence hints redact the task test, recorded command and output',
              bool(context) and marker not in context and other not in context and '[REDACTED]' in context)
    # Follow the actual hint's suggested evidence into the witness, using both its task-test branches.
    command = 'API_TOKEN=' + marker + ' pytest -q tests/'
    with mock.patch.object(common, 'HOME', home):
        shared.record_test(run, agent, command, 0, summary)
        shared.record_bash(run, agent, SID, command, 0, summary)
        rec = common.SessionRecord(run, agent).get(SID)
        for test in ('', 'pytest -q tests/'):
            hint = shared.evidence_hint(run, agent, {'reason': 'evidence_required', 'resource': 'task', 'test': test})
            text = hint['hookSpecificOutput']['additionalContext']
            suggested = json.JSONDecoder().raw_decode(text.split('evidence=', 1)[1])[0]
            result = shared.witness(rec, {'resource': 'task', 'evidence': suggested})['hookSpecificOutput']
            check('redacted evidence hint is accepted by the witness: ' + (test or 'last test'),
                  marker not in text and suggested['command'] == 'API_TOKEN=[REDACTED] pytest -q tests/'
                  and result.get('permissionDecision') == 'allow' and result['updatedInput']['witnessed'] is True
                  and result['updatedInput']['evidence']['exit_code'] == 0 and marker not in json.dumps(result))
        for ev in ({'command': 'API_TOKEN=[REDACTED] pytest -q tests/', 'exit_code': 3},
                   {'command': 'API_TOKEN=[REDACTED] pytest -q other/', 'exit_code': 0}):
            result = shared.witness(rec, {'evidence': ev})['hookSpecificOutput']
            check('redacted witness still rejects a different command or exit code', result['permissionDecision'] == 'deny')


def _wait_for(pred, timeout=6.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(min(0.05, max(0, deadline - time.monotonic())))
    return pred()


def _log(home):
    try:
        with open(os.path.join(home, 'hooks.log')) as fh:
            return fh.read()
    except OSError:
        return ''


def test_round8_hooks(seeker):
    """The Relay hooks described in docs/relay-hooks.md. A: PreToolUse on the Agent tool registers a task named
    from the description, with the prompt as its text and the workspace paths it names, and posts an interface block
    as a contract; it prints nothing and returns inside its budget. B: PostToolUse on Write/Edit under the scratchpad
    or the scratch directory posts a small text file as an artifact note (detached), once per content; binaries, big
    files and secrets are skipped and logged. C: SessionStart in a relay run adds the record block and writes the
    artefacts back. D: the scratch directory. E: autoSlots claim before the edit, deny when held, release after."""
    sys.path.insert(0, os.path.join(ROOT, 'lib'))
    import hooks_shared as H
    import relay_hooks as RH
    rule = H.RELAY
    check('relay rule: claim(next=true) first, done with next=true, peers for waiting or context',
          'Start with `claim(next=true)`. When your test passes, call `done` with the evidence and `next=true`. Call `peers` '
          "only when you are waiting for an answer or a handoff, or need context from the run's record; do not poll before each task." in rule)
    skill = read(os.path.join(ROOT, 'plugin', 'skills', 'relay', 'SKILL.md')).decode()
    tools = read(os.path.join(ROOT, 'docs', 'relay-tools.md')).decode()
    polling = ("Call peers only when you are waiting for an answer or a handoff, or need context from the run's record; "
               "do not poll before each task.")
    check('relay instructions agree on polling and completion across hooks, skill and docs',
          all(polling in text.replace('`', '') and registered_rule(text)
              and 'Before starting a task, call peers' not in text.replace('`', '') for text in (rule, skill, tools)))
    check('relay rule: helper scripts and binaries go to the named scratch directory, not /tmp',
          'Put helper scripts and binaries in the scratch directory {scratch}, not in /tmp.' in rule)
    ws = os.path.join(WORK, 'ws-r8')
    shutil.rmtree(ws, ignore_errors=True)
    for f in ('src/parse.py', 'src/emit.py', 'docs/notes.md', 'REGISTRY.md'):
        os.makedirs(os.path.dirname(os.path.join(ws, f)), exist_ok=True)
        with open(os.path.join(ws, f), 'w') as fh:
            fh.write('x\n')
    run = 'run-r8'
    home = os.path.join(WORK, 'home-r8')
    shutil.rmtree(home, ignore_errors=True)
    env = base_env(seeker.url, home, CTRLB_RUN=run, CTRLB_AGENT='agent-1')
    base = {'session_id': SID, 'transcript_path': '', 'cwd': ws}

    # A: a spawn registers a task (and a contract).
    prompt = ('Fix the tokenizer in src/parse.py (see `src/parse.py:12`) and src/emit.py; notes in docs/. Not src/gone.py, '
              'not /etc/hostname, not https://example.test/a/b.py. Test: `python3 -m pytest tests/test_parse.py`.\n\n'
              '## API\n\n```json\n{"tokens": ["str"], "pos": "int"}\n```\n')
    agent_in = dict(base, hook_event_name='PreToolUse', tool_name='Agent',
                    tool_input={'prompt': prompt, 'description': 'Fix the tokenizer', 'subagent_type': 'general-purpose'})
    seeker.reset()
    p, dt = hook('pre-agent', agent_in, env)
    briefs = [c for c in seeker.calls if c['path'].endswith('/briefs')]
    b0 = briefs[0]['body'] if briefs else {}
    check('pre-agent: registers task 01 named from the description, the prompt as its text, workspace paths it names',
          len(briefs) == 2 and b0.get('resource') == 'task 01 Fix the tokenizer' and b0.get('text') == prompt
          and b0.get('paths') == ['src/parse.py', 'src/emit.py', 'docs'] and b0.get('agent') == 'agent-1' and b0.get('cwd') == ws
          and briefs[0]['path'] == f'/ai/relay/agent_sessions/runs/{run}/briefs', json.dumps(b0)[:400])
    check('pre-agent: the test command the prompt quotes is the task\'s test', b0.get('test') == 'python3 -m pytest tests/test_parse.py')
    b1 = briefs[1]['body'] if len(briefs) > 1 else {}
    check('pre-agent: the interface block is posted as a contract on the same paths',
          b1.get('resource') == 'contract:task 01 Fix the tokenizer API' and b1.get('paths') == b0.get('paths')
          and '"tokens": ["str"]' in b1.get('text', '') and b1['text'].startswith('```json'), json.dumps(b1)[:300])
    check('pre-agent: prints nothing (the spawn goes ahead unchanged), exits 0', p.returncode == 0 and not p.stdout.strip() and not p.stderr.strip())
    seeker.reset()
    secret = 'sk-' + 'ant-api03-abcdefghijklmnopqrstuvwx'
    hook('pre-agent', dict(agent_in, tool_input={'prompt': f'Update src/emit.py; the key is {secret}', 'description': 'emit/fix'}), env)
    b = seeker.calls[0]['body'] if seeker.calls else {}
    check('pre-agent: the second spawn is task 02; a "/" in the description is not a path; secrets are redacted; no contract',
          len(seeker.calls) == 1 and b.get('resource') == 'task 02 emit fix' and secret not in json.dumps(b) and '[REDACTED]' in b.get('text', ''),
          json.dumps(b)[:300])
    seeker.reset()
    hook('pre-agent', dict(agent_in, tool_input={'prompt': 'Look around and report.', 'description': 'explore'}), env)
    check('pre-agent: a prompt naming no workspace path registers nothing', not seeker.calls)
    plain = base_env(seeker.url, os.path.join(WORK, 'home-r8-plain'))
    hook('pre-agent', agent_in, plain)
    check('pre-agent outside a relay run (no CTRLB_RUN, no relay tool called): nothing written', not seeker.calls)
    seeker.state['briefs'] = 'slow'
    p, dt = hook('pre-agent', agent_in, env)
    seeker.state['briefs'] = 'ok'
    check(f'pre-agent budget: CtrlB slower ({SLOW_S}s) than the {RH.REGISTER_S}s registration: returns inside the 5 s hook timeout, '
          'no output, the spawn proceeds', p.returncode == 0 and not p.stdout.strip() and dt < RH.REGISTER_S + 2.0, f'{dt:.2f}s')
    down = base_env(f'http://127.0.0.1:{closed_port()}', home, CTRLB_RUN=run, CTRLB_AGENT='agent-1')
    p, dt = hook('pre-agent', agent_in, down)
    check('pre-agent, CtrlB down: no output, fast', p.returncode == 0 and not p.stdout.strip() and dt < 3.0, f'{dt:.2f}s')
    check('prompt_contract: a section under an "Interface:" label; none in plain prose',
          RH.prompt_contract('Do it.\n\nInterface:\nparse(s: str) -> list[str]\n\nNotes:\nnone') == ('Interface', 'parse(s: str) -> list[str]')
          and RH.prompt_contract('Fix the bug.\n```sh\nmake\n```\n') is None)

    # D: the scratch directory, created by session-start and named in the rule.
    seeker.reset()
    shutil.rmtree(os.path.join(WORK, 'scratch-r8'), ignore_errors=True)
    senv = dict(env, CTRLB_SCRATCH_DIR=os.path.join(WORK, 'scratch-r8', '{run}'))
    p, dt = hook('session-start', dict(base, hook_event_name='SessionStart', source='startup'), senv)
    ctx = out_of(p).get('additionalContext', '')
    scratch = os.path.join(WORK, 'scratch-r8', run)
    check('session-start: creates scratchDir ({run} replaced) and the rule names it',
          os.path.isdir(scratch) and f'Put helper scripts and binaries in the scratch directory {scratch}, not in /tmp.' in ctx, ctx[-300:])
    default = os.path.join(home, 'relay', run, 'scratch')
    seeker.reset()
    p, _ = hook('session-start', dict(base, hook_event_name='SessionStart', source='startup'), env)
    ctx = out_of(p).get('additionalContext', '')
    check('session-start: the default scratchDir is <CTRLB_HOME>/relay/<run id>/scratch', os.path.isdir(default) and default in ctx)

    # C: the record block.
    block = ctx[ctx.find('CtrlB Relay record of run'):] if 'CtrlB Relay record of run' in ctx else ''
    gets = sorted(c['path'].split('?')[0].rsplit('/', 1)[-1] for c in seeker.calls if c['method'] == 'GET')
    check('session-start (relay run): reads tasks, contracts, peers (notes, handoffs) and artifacts of this agent',
          gets == ['artifacts', 'contracts', 'peers', 'peers', 'tasks'] and any('agent=agent-1' in c['path'] for c in seeker.calls
                                                                                   if '/artifacts' in c['path']), ','.join(gets))
    check('session-start block: open tasks with holder, stalled flag and test; a finished task left out',
          'task 01 parse (held by agent-b, stalled; test: python3 -m pytest tests/test_parse.py)' in block
          and 'task 02 emit (free, handed off)' in block and 'task 00 setup' not in block, block)
    check('session-start block: contract names, the open handoff and the one addressed to this agent, not others',
          'Contracts: parse api.' in block and 'task 02 emit handed off openly by agent-c' in block
          and 'task 04 render handed to you by agent-c' in block and 'task 09 other' not in block, block)
    check('session-start block: this agent\'s last notes only (no artifact notes)',
          'Your last notes: parse: tokenizer done.' in block and 'someone else' not in block and 'artifact body' not in block, block)
    restored = os.path.join(default, 'plan.md')
    check('session-start: artefacts written back to scratchDir (a path escaping it is not), and the block says where',
          os.path.isfile(restored) and open(restored).read() == R8_ARTIFACT and not os.path.exists(os.path.join(home, 'relay', run, 'escape.md'))
          and f'Your artefacts from the previous session are in {default}.' in block, block)
    check('session-start block: under 2 KB, after the rule', 0 < len(block.encode()) < 2048 and ctx.index('CtrlB Relay: your run id') < ctx.index(block))
    seeker.reset()
    for r in ('tasks', 'contracts', 'peers', 'artifacts'):
        seeker.state[r] = 'slow'
    p, dt = hook('session-start', dict(base, hook_event_name='SessionStart', source='resume'), env)
    for r in ('tasks', 'contracts', 'peers', 'artifacts'):
        seeker.state[r] = 'ok'
    ctx = out_of(p).get('additionalContext', '')
    check(f'session-start budget: CtrlB slower ({SLOW_S}s) than the {RH.RECORD_S}s read: returns inside it with the rule and '
          'the block from the local cache, saying so', p.returncode == 0 and dt < RH.RECORD_S + 1.5 and 'your run id is run-r8' in ctx
          and 'from the local cache of' in ctx and 'task 01 parse (held by agent-b' in ctx, f'{dt:.2f}s')
    fresh = base_env(f'http://127.0.0.1:{closed_port()}', os.path.join(WORK, 'home-r8-fresh'), CTRLB_RUN=run, CTRLB_AGENT='agent-1')
    p, dt = hook('session-start', dict(base, hook_event_name='SessionStart', source='startup'), fresh)
    ctx = out_of(p).get('additionalContext', '')
    check('session-start, CtrlB down and no cache: the rule alone, fast', 'your run id is run-r8' in ctx and 'CtrlB Relay record' not in ctx
          and dt < 3.0, f'{dt:.2f}s')
    seeker.reset()
    hook('session-start', dict(base, hook_event_name='SessionStart', source='startup'), base_env(seeker.url, os.path.join(WORK, 'home-r8-plain')))
    check('session-start without CTRLB_RUN: no record read', not seeker.calls)

    # B: artefacts.
    pad = os.path.join(WORK, 'pad-r8')
    shutil.rmtree(pad, ignore_errors=True)
    os.makedirs(os.path.join(pad, 'notes'))
    bhome = os.path.join(WORK, 'home-r8b')
    shutil.rmtree(bhome, ignore_errors=True)
    benv = base_env(seeker.url, bhome, CTRLB_RUN=run, CTRLB_AGENT='agent-1')
    post = dict(base, hook_event_name='PostToolUse', scratchpad_dir=pad)

    def write(rel, data, root=pad, tool='Write'):
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'wb' if isinstance(data, bytes) else 'w') as fh:
            fh.write(data)
        return hook('post-tool', dict(post, tool_name=tool, tool_input={'file_path': path, 'content': 'x'},
                                      tool_response={'filePath': path}), benv)

    def posts():
        return [c for c in seeker.calls if c['path'].endswith('/posts')]

    seeker.reset()
    text = 'step 1: parse\nstep 2: emit\n'
    p, dt = write('notes/plan.md', text)
    _wait_for(lambda: posts())
    ps = posts()
    body = ps[0]['body'] if ps else {}
    check('post-tool Write under the scratchpad: one artifact NOTE keyed artifact:<relative path>, the text and its hash',
          len(ps) == 1 and body.get('key') == 'artifact:notes/plan.md' and body.get('text') == text and body.get('agent') == 'agent-1'
          and body.get('hash') == 'sha256:' + hashlib.sha256(text.encode()).hexdigest()
          and ps[0]['path'] == f'/ai/relay/agent_sessions/runs/{run}/posts', json.dumps(body)[:300])
    check('post-tool artifact: no output, exits 0', p.returncode == 0 and not p.stdout.strip())
    seeker.reset()
    write('notes/plan.md', text, tool='Edit')
    time.sleep(1.0)
    check('post-tool artifact: the same content again posts nothing', not posts())
    write('notes/plan.md', text + 'step 3: ship\n', tool='Edit')
    _wait_for(lambda: posts())
    check('post-tool artifact: changed content is posted again', len(posts()) == 1)
    seeker.reset()
    write('tool.bin', b'\x7fELF\x00\x01\x02')
    write('big.txt', 'y' * (40 * 1024))
    pw = 'hunter2' + 'pass'
    write('creds.txt', f'password = {pw}\n')
    write('.env', 'X=1\n')
    outside = os.path.join(WORK, 'ws-r8', 'src', 'parse.py')
    hook('post-tool', dict(post, tool_name='Write', tool_input={'file_path': outside, 'content': 'x'}), benv)
    time.sleep(1.0)
    log = _log(bhome)
    check('post-tool artifact: binary, over 32 KB, secret content and a .env file are skipped and logged; a file outside the '
          'scratch directories is not an artefact', not posts() and 'artifact skipped: tool.bin: binary' in log
          and 'artifact skipped: big.txt: 40960 bytes' in log and 'artifact skipped: creds.txt: matches a secret pattern' in log
          and 'artifact skipped: .env: secret-looking file name' in log and 'parse.py' not in log, log[-600:])
    check('post-tool artifact: the hook log never holds the secret', pw not in log)
    sdir = os.path.join(bhome, 'relay', run, 'scratch')
    seeker.reset()
    write('helpers/run.sh', '#!/bin/sh\necho hi\n', root=sdir)
    _wait_for(lambda: posts())
    check('post-tool artifact: a file under scratchDir is an artefact too', [c['body'].get('key') for c in posts()] == ['artifact:helpers/run.sh'])
    seeker.reset()
    seeker.state['posts'] = 'slow'
    p, dt = write('notes/slow.md', 'slow post\n')
    check(f'post-tool artifact budget: CtrlB slower ({SLOW_S}s) than the hook: returns at once (the post is detached)',
          p.returncode == 0 and not p.stdout.strip() and dt < 3.0, f'{dt:.2f}s')
    _wait_for(lambda: 'artifact posted: run-r8/agent-1 notes/slow.md' in _log(bhome), SLOW_S + 3)
    seeker.state['posts'] = 'error'
    write('notes/err.md', 'failed post\n')
    _wait_for(lambda: 'artifact post run-r8/agent-1 notes/err.md: SeekerError' in _log(bhome))
    seeker.state['posts'] = 'ok'
    seeker.reset()
    write('notes/err.md', 'failed post\n', tool='Edit')
    _wait_for(lambda: posts())
    check('post-tool artifact: a post that failed is tried again on the next write of the same content', len(posts()) == 1)
    seeker.reset()
    seeker.state['artifacts'] = 'missing'
    p, _ = hook('session-start', dict(base, hook_event_name='SessionStart', source='startup', scratchpad_dir=pad), benv)
    seeker.state['artifacts'] = 'ok'
    ctx = out_of(p).get('additionalContext', '')
    check('session-start, a CtrlB without the artifacts read: artefacts restored from the local copies of what this machine posted',
          os.path.isfile(os.path.join(sdir, 'notes', 'plan.md')) and open(os.path.join(sdir, 'notes', 'plan.md')).read() == text + 'step 3: ship\n'
          and f'Your artefacts from the previous session are in {sdir}.' in ctx, ctx[-300:])

    # E: autoSlots.
    ehome = os.path.join(WORK, 'home-r8e')
    shutil.rmtree(ehome, ignore_errors=True)
    eenv = base_env(seeker.url, ehome, CTRLB_RUN=run, CTRLB_AGENT='agent-1')
    reg = os.path.join(ws, 'REGISTRY.md')
    pre = dict(base, hook_event_name='PreToolUse', tool_name='Edit', tool_input={'file_path': reg, 'old_string': 'x', 'new_string': 'y'})
    epost = dict(pre, hook_event_name='PostToolUse', tool_response={'filePath': reg})
    seeker.reset()
    p, dt = hook('pre-tool', pre, eenv)
    claims = [c for c in seeker.calls if c['path'].endswith('/claims')]
    check('autoSlots pre-tool: claims slot:REGISTRY.md with the file, no edit check, edit allowed (no output)',
          len(claims) == 1 and claims[0]['method'] == 'POST' and claims[0]['body'].get('resource') == 'slot:REGISTRY.md'
          and claims[0]['body'].get('paths') == ['REGISTRY.md'] and not [c for c in seeker.calls if c['path'].endswith('/check')]
          and p.returncode == 0 and not p.stdout.strip(), json.dumps(claims)[:300])
    saved, common.HOME = common.HOME, ehome
    try:
        tok = common.TokenState(run, 'agent-1').get(['slot:registry.md', 'path:REGISTRY.md'])
    finally:
        common.HOME = saved
    check('autoSlots pre-tool: the slot\'s token is kept for the slot and the file', tok == {'slot:registry.md': 'tok-slot', 'path:REGISTRY.md': 'tok-slot'}, str(tok))
    seeker.reset()
    p, dt = hook('post-tool', epost, eenv)
    rel = [c for c in seeker.calls if c['path'].endswith('/claims')]
    check('autoSlots post-tool: releases slot:REGISTRY.md (the agent spends no turn on it)',
          len(rel) == 1 and rel[0]['method'] == 'DELETE' and rel[0]['body'].get('resource') == 'slot:REGISTRY.md' and not p.stdout.strip())
    seeker.reset()
    hook('post-tool', epost, eenv)
    check('autoSlots post-tool: nothing to release when the hook holds no slot', not seeker.calls)
    seeker.state['claims'] = 'held'
    p, _ = hook('pre-tool', pre, eenv)
    seeker.state['claims'] = 'ok'
    o = out_of(p)
    check('autoSlots pre-tool: held by another agent: the edit is denied with "held by <agent>, try again in a moment"',
          o.get('permissionDecision') == 'deny' and o.get('permissionDecisionReason') == 'CtrlB Relay: REGISTRY.md is held by agent-b, try again in a moment.',
          json.dumps(o))
    seeker.state['claims'] = 'slow'
    p, dt = hook('pre-tool', pre, eenv)
    seeker.state['claims'] = 'ok'
    check(f'autoSlots budget: CtrlB slower ({SLOW_S}s) than the {RH.SLOT_S}s claim: fails open inside the pre-tool budget (edit allowed)',
          p.returncode == 0 and not p.stdout.strip() and dt < H.CHECK_S + 2.0 and 'auto slot fail open' in _log(ehome), f'{dt:.2f}s')
    seeker.state['claims'] = 'error'
    p, _ = hook('pre-tool', pre, eenv)
    seeker.state['claims'] = 'ok'
    check('autoSlots: CtrlB error: fails open (edit allowed)', p.returncode == 0 and not p.stdout.strip())
    seeker.reset()
    hook('pre-tool', pre, eenv)
    seeker.state['release'] = 'slow'
    p, dt = hook('post-tool', epost, eenv)
    seeker.state['release'] = 'ok'
    check(f'autoSlots release budget: CtrlB slower ({SLOW_S}s) than the {RH.RELEASE_S}s release: returns inside it (the slot lapses)',
          p.returncode == 0 and not p.stdout.strip() and dt < RH.RELEASE_S + 2.0, f'{dt:.2f}s')
    seeker.reset()
    hook('pre-tool', pre, eenv)
    p, _ = hook('post-tool-failure', dict(pre, hook_event_name='PostToolUseFailure', error='old_string not found'), eenv)
    check('autoSlots post-tool-failure: a failed edit releases the slot too',
          [c['method'] for c in seeker.calls if c['path'].endswith('/claims')] == ['POST', 'DELETE'] and not p.stdout.strip())
    seeker.reset()
    hook('pre-tool', dict(pre, tool_input={'file_path': os.path.join(ws, 'src', 'parse.py'), 'old_string': 'x', 'new_string': 'y'}), eenv)
    check('autoSlots: a file not in the list is not claimed (the edit check runs as before)',
          not [c for c in seeker.calls if c['path'].endswith('/claims')] and [c for c in seeker.calls if c['path'].endswith('/check')])
    seeker.reset()
    hook('pre-tool', pre, dict(eenv, CTRLB_AUTO_SLOTS=''))
    check('autoSlots off (CTRLB_AUTO_SLOTS empty): no claim', not [c for c in seeker.calls if c['path'].endswith('/claims')])
    seeker.reset()
    hook('pre-tool', pre, base_env(seeker.url, os.path.join(WORK, 'home-r8-plain')))
    check('autoSlots outside a relay run: no claim', not [c for c in seeker.calls if c['path'].endswith('/claims')])

    # hooks.json, Codex and the docs.
    hk = load('../../plugin/hooks/hooks.json')['hooks']
    agent_hook = [g for g in hk['PreToolUse'] if g.get('matcher') == 'Agent|Task']
    fail_edit = [g for g in hk['PostToolUseFailure'] if g.get('matcher') == 'Edit|Write|MultiEdit|NotebookEdit']
    check('hooks.json: PreToolUse Agent|Task runs pre-agent (timeout 5); PostToolUseFailure on edit tools; SessionStart timeout 10',
          len(agent_hook) == 1 and agent_hook[0]['hooks'][0]['command'].endswith('hook.py" pre-agent') and agent_hook[0]['hooks'][0]['timeout'] == 5
          and 'async' not in agent_hook[0]['hooks'][0] and len(fail_edit) == 1 and fail_edit[0]['hooks'][0]['command'].endswith('post-tool-failure')
          and hk['SessionStart'][0]['hooks'][0]['timeout'] == 10)
    seeker.reset()
    cenv = base_env(seeker.url, os.path.join(WORK, 'home-r8-codex'), CTRLB_RUN=run, CTRLB_AGENT='agent-1')
    p, dt = chook('session-start', {'session_id': CSID, 'transcript_path': '', 'cwd': ws, 'hook_event_name': 'SessionStart',
                                    'source': 'startup'}, cenv)
    ctx = out_of(p).get('additionalContext', '')
    check('codex session-start (relay run): the same record block and scratch directory',
          'CtrlB Relay record of run run-r8' in ctx and 'task 01 parse (held by agent-b' in ctx
          and os.path.join(WORK, 'home-r8-codex', 'relay', run, 'scratch') in ctx, ctx[-300:])
    doc = open(os.path.join(ROOT, 'docs', 'relay-hooks.md')).read()
    check('docs/relay-hooks.md: every Relay hook with its event, fields, output and budget, citing the hooks reference',
          all(w in doc for w in ('PreToolUse', 'PostToolUse', 'PostToolUseFailure', 'SessionStart', 'SessionEnd', 'scratchpad_dir',
                                 'tool_input.prompt', 'tool_input.description', 'permissionDecision', 'additionalContext',
                                 'autoSlots', 'scratchDir', 'fail open', 'code.claude.com/docs/en/hooks')))
    tools = open(os.path.join(ROOT, 'docs', 'relay-tools.md')).read()
    claim_doc = tools[tools.index('### claim'):tools.index('### release')]
    check('relay-tools: claim next and done next documented',
          '| `next` |' in claim_doc and '`free_tasks: []`' in claim_doc and 'done(resource="task 03 median", evidence=' in tools
          and 'next=true' in tools[tools.index('#### done'):tools.index('### handoff')])
    skill = open(os.path.join(ROOT, 'plugin', 'skills', 'relay', 'SKILL.md')).read()
    check('relay skill: claim(next=true), done with next=true, autoSlots instead of the REGISTRY.md claim',
          '`claim(next=true)`' in skill and '`next=true`' in skill and 'claim(resource="slot:REGISTRY.md"' not in skill)


def test_local_mode(seeker):
    main, sub = setup_transcripts()
    home = os.path.join(WORK, 'home-local')
    os.makedirs(home, exist_ok=True)
    config_path = os.path.join(home, 'config.json')
    cfg = dict(url=seeker.url, key=KEY, dataset=common.DEFAULT_DATASET, mcp_url=seeker.url + '/mcp', mode='local')
    with open(config_path, 'w') as fh:
        json.dump(cfg, fh)
    with mock.patch.object(common, 'HOME', home), mock.patch.dict(os.environ, {}, clear=True):
        check('local mode: config file enables local mode', common.local_mode(common.config()))
        with mock.patch.dict(os.environ, CTRLB_MODE='hosted'):
            cfg = common.config()
            check('local mode: environment hosted overrides file local and keeps Relay enabled',
                  cfg['mode'] == 'hosted' and not common.local_mode(cfg) and not common.relay_off(cfg))
        with mock.patch.object(common, '_file_config', return_value={}):
            check('local mode: defaults to hosted', not common.local_mode(common.config()))
            with mock.patch.dict(os.environ, CTRLB_MODE='local'):
                check('local mode: environment enables local mode', common.local_mode(common.config()))
                check('local mode: resolved config controls mode without re-reading the environment',
                      not common.local_mode(dict(mode='hosted')))

    ci = inputs(main, sub)
    _, cm, cs = setup_codex()
    xi = cinputs(cm, cs)
    for call, fixtures, label in ((hook, ci, 'Claude'), (chook, xi, 'Codex')):
        for source in ('file', 'environment'):
            h = home if source == 'file' else os.path.join(WORK, 'home-local-' + label)
            env = base_env(seeker.url, h)
            if source == 'environment':
                env['CTRLB_MODE'] = 'local'
            seeker.reset()
            for mode, fixture in (('session-start', 'session_start'), ('subagent-start', 'subagent_start')):
                p, _ = call(mode, fixtures[fixture], env)
                ctx = out_of(p).get('additionalContext', '')
                check(f'{label} local {source} {mode}: recall context without Relay', p.returncode == 0
                      and 'search_sessions' in ctx and 'Relay' not in ctx and 'claim(' not in ctx)
            # REGISTRY.md exercises autoSlots; the Agent prompt would register a task and contract in hosted mode.
            edits = dict(fixtures['session_start'], tool_name='Write', tool_input={'file_path': 'REGISTRY.md', 'content': 'test'})
            if label == 'Codex':
                edits.update(tool_name='apply_patch', tool_input='*** Begin Patch\n*** Update File: REGISTRY.md\n@@\n-a\n+b\n*** End Patch')
            events = [('pre-tool', edits), ('session-end', dict(fixtures['session_start'], hook_event_name='SessionEnd'))]
            if label == 'Claude':
                events += [('pre-agent', dict(edits, tool_name='Agent', tool_input={'prompt':
                            'Task: update registry. Files: `REGISTRY.md`. Test: `python3 -m unittest`.'})),
                           ('post-tool', edits), ('post-tool-failure', edits),
                           ('pre-tool', dict(edits, tool_name='mcp__ctrlb__done', tool_input={})),
                           ('post-tool', dict(edits, tool_name='mcp__ctrlb__claim', tool_input={},
                                              tool_response={'held': True})),
                           ('post-tool', dict(edits, tool_name='Bash', tool_input={'command': 'python3 -m unittest'},
                                              tool_response={'exit_code': 0}))]
            for mode, event in events:
                p, _ = call(mode, event, env)
                check(f'{label} local {source} {mode} {event.get("tool_name", "")}: silent success',
                      p.returncode == 0 and not p.stdout.strip())
            check(f'{label} local {source}: no Relay HTTP calls', not any('/ai/relay/' in c['path'] for c in seeker.calls))
            check(f'{label} local {source}: session upload still runs', bool(seeker.rows))
        with open(config_path) as fh:
            hosted_override = dict(json.load(fh), run='saved-hosted-run')
        with open(config_path, 'w') as fh:
            json.dump(hosted_override, fh)
        env = base_env(seeker.url, home, CTRLB_MODE='hosted')
        seeker.reset()
        p, _ = call('session-start', fixtures['session_start'], env)
        check(f'{label} hosted override: local config retains hosted Relay session rules',
              p.returncode == 0 and registered_rule(out_of(p).get('additionalContext', '')))
        edit = dict(fixtures['session_start'], tool_name='Write',
                    tool_input={'file_path': 'src/parse.py', 'content': 'test'})
        if label == 'Codex':
            edit.update(tool_name='apply_patch', tool_input='*** Begin Patch\n*** Update File: src/parse.py\n@@\n-a\n+b\n*** End Patch')
        seeker.state['check'] = 'enforce'
        try:
            p, _ = call('pre-tool', edit, env)
        finally:
            seeker.state['check'] = 'allow'
        check(f'{label} hosted override: local config retains hosted edit enforcement',
              any(c['path'].endswith('/check') for c in seeker.calls)
              and out_of(p).get('permissionDecision') == 'deny')
        if label == 'Claude':
            # Spawn registration requires a prompt path that exists under the session's cwd.
            workspace = os.path.join(home, 'workspace')
            os.makedirs(os.path.join(workspace, 'src'), exist_ok=True)
            with open(os.path.join(workspace, 'src', 'parse.py'), 'w') as fh:
                fh.write('test\n')
            spawn = dict(edit, cwd=workspace, tool_name='Agent', tool_input={'description': 'Fix parser',
                         'prompt': 'Fix src/parse.py. Test: `python3 -m unittest`.'})
            seeker.reset()
            p, _ = call('pre-agent', spawn, env)
            briefs = [c for c in seeker.calls
                      if c['path'] == f'/ai/relay/{common.DEFAULT_DATASET}/runs/saved-hosted-run/briefs']
            check('Claude hosted override: local config retains hosted spawn registration',
                  p.returncode == 0 and len(briefs) == 1 and briefs[0]['method'] == 'POST'
                  and briefs[0]['body'].get('paths') == ['src/parse.py']
                  and briefs[0]['body'].get('cwd') == workspace)
        p, _ = call('session-end', dict(fixtures['session_start'], hook_event_name='SessionEnd'), env)
        check(f'{label} hosted override: local config retains hosted session-end handoff',
              p.returncode == 0 and any(c['path'].endswith('/handoffs') for c in seeker.calls))
    seeker.reset()
    env = base_env(seeker.url, home, CTRLB_MODE='local')
    p = subprocess.run([sys.executable, '-I', os.path.join(ROOT, 'lib', 'relay_hooks.py'), 'post-artifact'],
                       input=json.dumps(dict(run='local-run', agent='test-agent', rel='test.txt', text='test', hash='test')),
                       capture_output=True, text=True, env=env, timeout=5)
    check('local mode: pending detached artifact poster sends no Relay request', p.returncode == 0 and not seeker.calls)


def test_local_session_context(seeker):
    notice = 'CtrlB is local on this machine; the run is fixed when the session starts.'
    main, sub = setup_transcripts()
    ci = inputs(main, sub)
    _, cm, cs = setup_codex()
    xi = cinputs(cm, cs)
    for call, fixtures, label in ((hook, ci, 'Claude'), (chook, xi, 'Codex')):
        events = [('session-start', 'session_start'), ('subagent-start', 'subagent_start')]
        if label == 'Codex':
            events.append(('session-start', 'session_start_subagent'))
        for mode, run in (('local', ''), ('local', 'context-run'), ('hosted', ''), ('hosted', 'context-run')):
            for source in ('file', 'environment'):
                home = os.path.join(WORK, f'home-context-{label}-{mode}-{run}-{source}')
                os.makedirs(home, exist_ok=True)
                env = base_env(seeker.url, home, CTRLB_RUN=run, CTRLB_AGENT='context-agent')
                if source == 'file':
                    with open(os.path.join(home, 'config.json'), 'w') as fh:
                        json.dump(dict(mode=mode), fh)
                else:
                    env['CTRLB_MODE'] = mode
                for event, fixture in events:
                    seeker.reset()
                    p, _ = call(event, fixtures[fixture], env)
                    ctx = out_of(p).get('additionalContext', '')
                    tag = f'{label} {mode} {source} {fixture} run={run!r}'
                    check(tag + ': local sentence appears only in local context', p.returncode == 0
                          and ctx.count(notice) == (1 if mode == 'local' else 0))
                    check(tag + ': context keeps recall and the session run', 'search_sessions' in ctx
                          and (f'run id is {run}' in ctx if run else
                               ('CtrlB Relay:' not in ctx if mode == 'local' else 'CtrlB Relay:' in ctx)))


def test_local_relay(seeker):
    with mock.patch.dict(os.environ, {}, clear=True):
        local = dict(mode='local', run='saved-run')
        for value in (None, '', '   ', '${CTRLB_RUN}', ' ${CTRLB_RUN} '):
            env = {} if value is None else {'CTRLB_RUN': value}
            with mock.patch.dict(os.environ, env, clear=True):
                check(f'local Relay: unusable environment run {value!r} keeps Relay off despite saved run',
                      common.relay_off(local))
                check(f'hosted Relay: environment run {value!r} does not disable Relay',
                      not common.relay_off(dict(mode='hosted')))
        with mock.patch.dict(os.environ, CTRLB_RUN=' shared-run '):
            check('local Relay: trimmed environment run enables Relay', not common.relay_off(local))

    main, sub = setup_transcripts()
    ci = inputs(main, sub)
    _, cm, cs = setup_codex()
    xi = cinputs(cm, cs)
    run, agent = 'shared-run', 'builder-1'
    prefix = f'/ai/relay/{common.DEFAULT_DATASET}/runs/{run}/'
    for call, fixtures, label in ((hook, ci, 'Claude'), (chook, xi, 'Codex')):
        for source in ('file', 'environment'):
            home = os.path.join(WORK, 'home-relay-' + label + '-' + source)
            os.makedirs(home, exist_ok=True)
            if source == 'file':
                with open(os.path.join(home, 'config.json'), 'w') as fh:
                    json.dump(dict(mode='local', run='saved-run'), fh)
            env = base_env(seeker.url, home, CTRLB_RUN=run, CTRLB_AGENT=agent)
            if source == 'environment':
                env['CTRLB_MODE'] = 'local'
            tag = f'{label} local Relay {source}'
            seeker.reset()
            p, _ = call('session-start', fixtures['session_start'], env)
            ctx = out_of(p).get('additionalContext', '')
            check(tag + ': session rule names environment run and agent with recall', p.returncode == 0
                  and f'run id is {run}' in ctx and f'agent id is {agent}' in ctx
                  and 'search_sessions' in ctx and registered_rule(ctx))
            reads = sorted(c['path'].split('?')[0].rsplit('/', 1)[-1] for c in seeker.calls
                           if c['path'].startswith(prefix) and c['method'] == 'GET')
            check(tag + ': starts local server probe and reads Relay record',
                  any(c['path'] == '/healthz' for c in seeker.calls)
                  and reads == ['artifacts', 'contracts', 'peers', 'peers', 'tasks']
                  and f'CtrlB Relay record of run {run}' in ctx)
            p, _ = call('subagent-start', fixtures['subagent_start'], env)
            ctx = out_of(p).get('additionalContext', '')
            subagent = agent + (':agent-' + fixtures['subagent_start']['agent_id'] if label == 'Claude' else ':' + CSUB)
            check(tag + ': subagent gets Relay rule and own agent id', p.returncode == 0
                  and f'run id is {run}' in ctx and f'agent id is {subagent}' in ctx)

            workspace = os.path.join(home, 'workspace')
            os.makedirs(os.path.join(workspace, 'src'), exist_ok=True)
            with open(os.path.join(workspace, 'src', 'parse.py'), 'w') as fh:
                fh.write('test\n')
            base = dict(fixtures['session_start'], cwd=workspace)
            edit = dict(base, tool_name='Write', tool_input={'file_path': 'src/parse.py', 'content': 'test'})
            if label == 'Codex':
                edit.update(tool_name='apply_patch', tool_input='*** Begin Patch\n*** Update File: src/parse.py\n@@\n-a\n+b\n*** End Patch')
            seeker.reset()
            seeker.state['check'] = 'enforce'
            try:
                p, _ = call('pre-tool', edit, env)
            finally:
                seeker.state['check'] = 'allow'
            checks = [c for c in seeker.calls if c['path'] == prefix + 'check']
            check(tag + ': edit checks local Relay endpoint with run, agent and path', len(checks) == 1
                  and checks[0]['body'].get('agent') == agent and checks[0]['body'].get('paths') == ['src/parse.py'])
            check(tag + ': edit obeys enforcement answer', p.returncode == 0
                  and out_of(p).get('permissionDecision') == 'deny')

            if label == 'Claude':
                seeker.reset()
                spawn = dict(base, tool_name='Agent', tool_input={'description': 'Fix parser',
                             'prompt': 'Fix src/parse.py. Test: `python3 -m unittest`.\n\nInterface:\nparse(text) -> list[str]'})
                p, _ = call('pre-agent', spawn, env)
                briefs = [c for c in seeker.calls if c['path'] == prefix + 'briefs']
                check(tag + ': pre-agent registers task and contract', p.returncode == 0 and not p.stdout.strip()
                      and len(briefs) == 2 and briefs[0]['body'].get('paths') == ['src/parse.py']
                      and briefs[1]['body'].get('resource', '').startswith('contract:'))
                p, _ = call('post-tool', dict(base, tool_name='Bash', tool_input={'command': 'python3 -m unittest'},
                                            tool_response={'exit_code': 0, 'stdout': 'OK'}), env)
                with mock.patch.object(common, 'HOME', home):
                    last = common.LastTest(run, agent).get()
                check(tag + ': post-tool keeps test evidence', p.returncode == 0 and last
                      and last.get('command') == 'python3 -m unittest' and last.get('exit_code') == 0)
                call('post-tool', edit, env)

            with open(base['transcript_path'], 'a') as fh:
                if label == 'Claude':
                    fh.write(line('assistant', 'Finished this turn.', '2026-09-06T12:10:00.000Z'))
                else:
                    fh.write(citem(dict(type='message', role='assistant', content=[
                        dict(type='output_text', text='Finished this turn.')]), '2026-09-06T12:10:00.000Z'))
            seeker.reset()
            p, _ = call('session-end', dict(base, hook_event_name='SessionEnd'), env)
            handoffs = [c for c in seeker.calls if c['path'] == prefix + 'handoffs']
            check(tag + ': session end hands off all holds and still uploads', p.returncode == 0 and len(handoffs) == 1
                  and handoffs[0]['body'].get('agent') == agent and handoffs[0]['body'].get('all') is True
                  and handoffs[0]['body'].get('to') == '' and bool(seeker.rows))
            if label == 'Claude':
                text = handoffs[0]['body'].get('text', '') if handoffs else ''
                check(tag + ': handoff includes edit and test evidence',
                      'src/parse.py' in text and 'python3 -m unittest' in text)

            # An environment run is required even when the config has a saved run.
            for value in (None, '', '   ', '${CTRLB_RUN}'):
                offenv = dict(env)
                offenv.pop('CTRLB_RUN')
                if value is not None:
                    offenv['CTRLB_RUN'] = value
                seeker.reset()
                p, _ = call('session-start', base, offenv)
                ctx = out_of(p).get('additionalContext', '')
                call('pre-tool', edit, offenv)
                call('session-end', dict(base, hook_event_name='SessionEnd'), offenv)
                check(tag + f': unusable run {value!r} preserves recall and zero Relay calls', p.returncode == 0
                      and 'search_sessions' in ctx and 'Relay' not in ctx
                      and not any('/ai/relay/' in c['path'] for c in seeker.calls))

    seeker.reset()
    env = base_env(seeker.url, os.path.join(WORK, 'home-local-poster'), CTRLB_MODE='local', CTRLB_RUN=run)
    p = subprocess.run([sys.executable, '-I', os.path.join(ROOT, 'lib', 'relay_hooks.py'), 'post-artifact'],
                       input=json.dumps(dict(run=run, agent=agent, rel='test.txt', text='test', hash='test')),
                       capture_output=True, text=True, env=env, timeout=5)
    check('local Relay: detached artifact poster sends to local run', p.returncode == 0 and len(seeker.calls) == 1
          and seeker.calls[0]['path'] == prefix + 'posts' and seeker.calls[0]['body'].get('agent') == agent)


def local_stub(home):
    """Synthetic ctrlb executable; writes only test state and serves health until the test stops it."""
    path = os.path.join(home, 'bin', 'ctrlb')
    os.makedirs(os.path.dirname(path), exist_ok=True)
    source = '''import json, os, sys, threading, time
home = os.environ['CTRLB_HOME']
log_mutex = threading.RLock()
def log(event, **fields):
    entry = dict(event=event, pid=os.getpid(), time=round(time.monotonic(), 3))
    entry.update(fields)
    with log_mutex, open(os.path.join(home, 'startup.jsonl'), 'a') as fh:
        fh.write(json.dumps(entry) + '\\n')
def exception(exc_type, exc, tb):
    log('exception', type=exc_type.__name__, errno=getattr(exc, 'errno', None))
sys.excepthook = exception
log('starting')
if sys.argv[1] == 'serve':
    with open(os.path.join(home, 'serve-invocations.jsonl'), 'a') as fh:
        fh.write(json.dumps(dict(pid=os.getpid(), cwd=os.getcwd())) + '\\n')
with open(os.path.join(home, 'invoked.json'), 'w') as fh:
    json.dump(dict(argv=sys.argv[1:], pid=os.getpid(), detached=os.getsid(0) == os.getpid(),
                   stdin=os.read(0, 1).decode(), cwd=os.getcwd()), fh)
if sys.argv[1] == 'init':
    print('test-key-not-a-real-key')
    print('test-key-not-a-real-key', file=sys.stderr)
    if os.environ.get('STUB_FAIL'):
        sys.exit(1)
    port = sys.argv[sys.argv.index('--port') + 1] if '--port' in sys.argv else '7531'
    url = 'http://127.0.0.1:' + port
    with open(os.path.join(home, 'config.json'), 'w') as fh:
        json.dump(dict(url=url, mcp_url=url + '/mcp', key='test-key-not-a-real-key',
                       dataset='agent_sessions', mode='local'), fh)
    os.chmod(os.path.join(home, 'config.json'), 0o600)
elif sys.argv[1] == 'serve' and not os.environ.get('STUB_NO_HEALTH'):
    import http.server, socket, socketserver
    if os.environ.get('STUB_GETFQDN_DELAY'):
        def slow_getfqdn(host):
            log('getfqdn')
            time.sleep(float(os.environ['STUB_GETFQDN_DELAY']))
            return host
        socket.getfqdn = slow_getfqdn
    time.sleep(float(os.environ.get('STUB_START_DELAY', '0')))
    class H(http.server.BaseHTTPRequestHandler):
        timeout = 2
        def log_message(self, *args): pass
        def log_request(self, code='-', size='-'):
            log('request', method=self.command if self.command in ('GET', 'POST', 'DELETE') else 'other',
                route='healthz' if self.path == '/healthz' else 'other', status=code)
        def log_error(self, fmt, *args):
            exc = next((arg for arg in args if isinstance(arg, BaseException)), None)
            log('handler_error', type=type(exc).__name__ if exc else 'HTTPError')
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
    class Server(http.server.ThreadingHTTPServer):
        daemon_threads = True
        request_queue_size = 64
        def server_bind(self):
            socketserver.TCPServer.server_bind(self)
            self.server_name, self.server_port = self.server_address[:2]
            log('bound', host=self.server_address[0], port=self.server_address[1])
        def server_activate(self):
            super().server_activate()
            log('listening', backlog=self.request_queue_size)
        def get_request(self):
            request, address = super().get_request()
            log('accepted', port=address[1])
            return request, address
        def handle_error(self, request, address):
            exception(*sys.exc_info())
    with Server(('127.0.0.1', int(os.environ['STUB_PORT'])), H) as server:
        server.serve_forever()
'''
    with open(path, 'w') as fh:
        fh.write('#!' + sys.executable + '\n' + source)
    os.chmod(path, 0o700)
    return path


def wait_local_health(url, timeout=8.0):
    """Allow cold startup and transient loopback timeouts without extending the hook's budget."""
    deadline = time.monotonic() + timeout
    detail = 'health did not answer'
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False, detail
        try:
            status = common.local_health(dict(url=url, key=KEY), timeout=min(1.0, remaining))
            if status == 200:
                return True, 'health HTTP 200'
            detail = f'health HTTP {status}'
        except (OSError, common.urllib.error.URLError) as exc:
            detail = f'{type(exc).__name__}: {exc}'
        time.sleep(min(0.1, max(0, deadline - time.monotonic())))


def local_startup_diagnostics(home, url):
    """Only synthetic state: never dump config, request headers, bodies or exception messages."""
    print('Startup diagnostics:')
    marker = os.path.join(home, 'serve-invocations.jsonl')
    try:
        invocations = [json.loads(line) for line in read(marker).decode().splitlines()]
        for info in invocations:
            pid = info['pid']
            try:
                os.kill(pid, 0)
                alive = True
            except ProcessLookupError:
                alive = False
            print(f'  stub pid={pid} alive={alive}')
        if not invocations:
            print('  stub pid=unknown (no invocation recorded)')
    except (OSError, ValueError, KeyError) as exc:
        print(f'  stub pid=unknown ({type(exc).__name__})')
    try:
        if common.loopback_url(dict(url=url)):
            target = common.urllib.parse.urlsplit(url)
            with socket.create_connection((target.hostname, target.port or 80), timeout=0.5):
                outcome = 'connected'
        else:
            outcome = 'skipped (non-loopback URL)'
    except ConnectionRefusedError:
        outcome = 'refused'
    except socket.timeout:
        outcome = 'timeout'
    except (OSError, ValueError) as exc:
        outcome = type(exc).__name__
    print(f'  raw socket connect: {outcome}')
    try:
        print('  startup.jsonl:\n' + read(os.path.join(home, 'startup.jsonl')).decode())
    except OSError as exc:
        print(f'  startup.jsonl unavailable ({type(exc).__name__})')


def check_local_startup(name, ok, home, url, detail=''):
    check(name, ok, detail)
    if not ok:
        local_startup_diagnostics(home, url)


def stop_local_stub(home):
    marker = os.path.join(home, 'serve-invocations.jsonl')
    if os.path.exists(marker):
        for line in read(marker).decode().splitlines():
            try:
                os.kill(json.loads(line)['pid'], signal.SIGTERM)
            except ProcessLookupError:
                pass


def test_local_health_retry():
    with mock.patch.object(common, 'local_health', side_effect=[ConnectionRefusedError(), TimeoutError(),
                           common.urllib.error.URLError('timed out'), 200]) as probe, \
            mock.patch.object(time, 'sleep'):
        healthy, detail = wait_local_health('http://127.0.0.1')
    check('health retry: connection refusal, socket timeout and URL timeout are retried',
          healthy and probe.call_count == 4, detail)
    with mock.patch.object(common, 'local_health', side_effect=common.urllib.error.URLError('timed out')), \
            mock.patch.object(time, 'monotonic', side_effect=[0, 0, 0.1, 0.1]), mock.patch.object(time, 'sleep'):
        healthy, detail = wait_local_health('http://127.0.0.1', timeout=0.1)
    check('health retry: deadline exhaustion returns a failed check with the last error',
          not healthy and 'URLError' in detail and 'timed out' in detail)


def test_fixture_bind_without_dns():
    with mock.patch.object(socket, 'getfqdn', side_effect=AssertionError('unexpected reverse DNS')) as lookup:
        seeker = Seeker()
        try:
            healthy, detail = wait_local_health(seeker.url)
            check('HTTP fixtures: bind and serve health without reverse DNS', healthy and lookup.call_count == 0, detail)
            check('HTTP fixtures: server name and port come from the bound address',
                  (seeker.httpd.server_name, seeker.httpd.server_port) == seeker.httpd.server_address
                  and seeker.httpd.server_name == '127.0.0.1' and seeker.httpd.server_port > 0)
        finally:
            seeker.httpd.shutdown()
            seeker.httpd.server_close()


def test_local_stub_slow_dns():
    home = os.path.join(WORK, 'home-start-slow-dns')
    binary = local_stub(home)
    port = closed_port()
    url = f'http://127.0.0.1:{port}'
    env = base_env(url, home, STUB_PORT=str(port), STUB_GETFQDN_DELAY='10')
    process = subprocess.Popen([binary, 'serve'], cwd=home, env=env, stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        # The ordinary eight-second readiness deadline is shorter than the injected lookup delay.
        healthy, detail = wait_local_health(url)
        check_local_startup('local stub: health answers despite a ten-second reverse DNS delay', healthy, home, url, detail)
        events = [json.loads(line) for line in read(os.path.join(home, 'startup.jsonl')).decode().splitlines()]
        names = [event['event'] for event in events]
        check('local stub: bind and listen complete without calling getfqdn',
              'bound' in names and 'listening' in names and 'getfqdn' not in names
              and names.index('bound') < names.index('listening'))
    finally:
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=5)


def test_local_stub_idle_connections():
    import contextlib
    import io
    home = os.path.join(WORK, 'home-start-idle')
    binary = local_stub(home)
    port = closed_port()
    url = f'http://127.0.0.1:{port}'
    env = base_env(url, home, STUB_PORT=str(port))
    process = subprocess.Popen([binary, 'serve'], cwd=home, env=env, stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    idle = []
    try:
        healthy, detail = wait_local_health(url)
        check_local_startup('local stub: initial health answers', healthy, home, url, detail)
        if not healthy:
            return
        try:
            # More than the old backlog of five: none of these clients sends a request line.
            for _ in range(12):
                idle.append(socket.create_connection(('127.0.0.1', port), timeout=1))
            healthy, detail = wait_local_health(url, timeout=1.5)
        except OSError as exc:
            healthy, detail = False, type(exc).__name__
        check_local_startup('local stub: health answers while twelve idle clients remain connected',
                            healthy, home, url, detail)
        output = io.StringIO()
        with contextlib.redirect_stdout(output), mock.patch.dict(check.__globals__, results=[]):
            check_local_startup('synthetic startup failure', False, home, url)
        diagnostics = output.getvalue()
        check('startup diagnostics: failed checks show live pid, connected socket and startup/request events',
              'FAIL synthetic startup failure' in diagnostics and f'pid={process.pid} alive=True' in diagnostics
              and 'raw socket connect: connected' in diagnostics
              and all('"event": "' + event + '"' in diagnostics for event in ('bound', 'listening', 'request'))
              and KEY not in diagnostics and 'Authorization' not in diagnostics)
        expired = False
        if idle:
            idle[0].settimeout(4)
            try:
                expired = idle[0].recv(1) == b''
            except OSError:
                pass
        check_local_startup('local stub: idle request lines expire without a client close', expired, home, url)
    finally:
        for client in idle:
            client.close()
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=5)
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        local_startup_diagnostics(home, url)
    check('startup diagnostics: stopped stub shows dead pid and refused socket',
          f'pid={process.pid} alive=False' in output.getvalue() and 'raw socket connect: refused' in output.getvalue())
    output = io.StringIO()
    with contextlib.redirect_stdout(output), mock.patch.object(socket, 'create_connection', side_effect=socket.timeout):
        local_startup_diagnostics(home + '-missing', url)
    check('startup diagnostics: socket timeout and missing startup state are reported without raising',
          'raw socket connect: timeout' in output.getvalue() and 'stub pid=unknown' in output.getvalue()
          and 'startup.jsonl unavailable' in output.getvalue())


def test_local_startup(seeker):
    for call, label, sid in ((hook, 'Claude', SID), (chook, 'Codex', CSID)):
        home = os.path.join(WORK, 'home-start-' + label)
        binary = local_stub(home)
        marker = os.path.join(home, 'invoked.json')
        inp = dict(session_id=sid, hook_event_name='SessionStart')
        env = base_env(seeker.url, home, CTRLB_MODE='local', CTRLB_BIN=binary)
        os.chmod(home, 0o755)
        p, dt = call('session-start', inp, env)
        check_local_startup(f'{label} startup: healthy server is not started',
                            p.returncode == 0 and not os.path.exists(marker) and dt < 3.0, home, env['CTRLB_URL'])
        check_local_startup(f'{label} startup: existing CTRLB_HOME is private even with a healthy server',
                            stat.S_IMODE(os.stat(home).st_mode) == 0o700, home, env['CTRLB_URL'])
        port = closed_port()
        env.update(CTRLB_URL=f'http://127.0.0.1:{port}', STUB_PORT=str(port))
        try:
            p, dt = call('session-start', inp, env)
            healthy, detail = wait_local_health(env['CTRLB_URL'])
            info = json.loads(read(marker)) if os.path.exists(marker) else {}
            check_local_startup(f'{label} startup: detached serve retains CTRLB_HOME, null stdin, becomes healthy',
                  p.returncode == 0 and info.get('argv') == ['serve'] and info.get('detached') and info.get('stdin') == ''
                  and dt < 5.5 and healthy, home, env['CTRLB_URL'], f'{dt:.2f}s; {detail}')
            check_local_startup(f'{label} startup: detached serve uses CtrlB home as its working directory',
                  os.path.realpath(info.get('cwd', '')) == os.path.realpath(home), home, env['CTRLB_URL'])
        finally:
            stop_local_stub(home)
            if os.path.exists(marker):
                os.unlink(marker)
        env.update(CTRLB_URL=seeker.url, STUB_NO_HEALTH='1')
        seeker.state['health'] = 'slow'
        try:
            p, dt = call('session-start', inp, env, timeout=8)
            check_local_startup(f'{label} startup: slow health gives up within hook timeout, keeps recall context',
                  p.returncode == 0 and 3 <= dt < 5.5 and 'search_sessions' in out_of(p).get('additionalContext', '')
                  and 'health check did not answer in time' in _log(home), home, env['CTRLB_URL'], f'{dt:.2f}s')
        finally:
            seeker.state.pop('health', None)
        if os.path.exists(marker):
            os.unlink(marker)
        env['CTRLB_URL'] = 'http://192.0.2.1:7531'
        p, dt = call('session-start', inp, env)
        check_local_startup(f'{label} startup: non-loopback URL is skipped',
                            p.returncode == 0 and not os.path.exists(marker) and dt < 3.0, home, env['CTRLB_URL'])
        # Isolate PATH so no installed ctrlb can interfere with the missing-binary case.
        os.unlink(binary)
        env.update(CTRLB_URL=f'http://localhost:{closed_port()}', PATH=home)
        p, dt = call('session-start', inp, env)
        check_local_startup(f'{label} startup: missing binary logs one line and continues', p.returncode == 0 and dt < 3.0
              and _log(home).count('ctrlb executable not found') == 1 and 'search_sessions' in p.stdout, home, env['CTRLB_URL'])

    home = os.path.join(WORK, 'home-binary-lookup')
    binary = local_stub(home)
    with mock.patch.object(common, 'HOME', home), mock.patch.dict(os.environ, {}, clear=True):
        check('binary lookup: CTRLB_HOME/bin/ctrlb fallback', common.ctrlb_binary() == binary)
        with mock.patch.dict(os.environ, CTRLB_BIN=sys.executable):
            check('binary lookup: CTRLB_BIN has precedence', common.ctrlb_binary() == os.path.abspath(sys.executable))
        with mock.patch.object(common, 'HOME', home + '-other'), mock.patch.dict(os.environ, PATH=os.path.dirname(binary)):
            check('binary lookup: PATH fallback', common.ctrlb_binary() == binary)
    check('startup URL check: only exact loopback hostnames',
          all(common.loopback_url({'url': u}) for u in ('http://127.0.0.1:7531', 'http://localhost:7531'))
          and not any(common.loopback_url({'url': u}) for u in ('http://localhost.example.test', 'http://127.0.0.2',
                                                               'file://localhost', 'http://[bad')))


def test_local_startup_delayed():
    # The detached server can become ready after the hook's best-effort three-second poll ends.
    for call, label, sid in ((hook, 'Claude', SID), (chook, 'Codex', CSID)):
        home = os.path.join(WORK, 'home-start-delayed-' + label)
        binary = local_stub(home)
        port = closed_port()
        env = base_env(f'http://127.0.0.1:{port}', home, CTRLB_MODE='local', CTRLB_BIN=binary,
                       STUB_PORT=str(port), STUB_START_DELAY='4')
        try:
            p, dt = call('session-start', dict(session_id=sid, hook_event_name='SessionStart'), env, timeout=8)
            check_local_startup(f'{label} startup: cold server outlives poll budget while recall context is returned',
                  p.returncode == 0 and 3 <= dt < 5.5 and 'search_sessions' in out_of(p).get('additionalContext', '')
                  and 'health check did not answer in time' in _log(home), home, env['CTRLB_URL'], f'{dt:.2f}s')
            healthy, detail = wait_local_health(env['CTRLB_URL'])
            check_local_startup(f'{label} startup: cold server becomes healthy after the hook returns',
                                healthy, home, env['CTRLB_URL'], detail)
        finally:
            stop_local_stub(home)


def test_local_startup_concurrent():
    home = os.path.join(WORK, 'home-start-concurrent')
    binary = local_stub(home)
    port = closed_port()
    env = base_env(f'http://127.0.0.1:{port}', home, CTRLB_MODE='local', CTRLB_BIN=binary,
                   STUB_PORT=str(port), STUB_START_DELAY='0.4')
    project = os.path.join(WORK, 'startup-project')
    os.makedirs(project, exist_ok=True)
    launcher = ('import sys; sys.path.insert(0, ' + repr(os.path.join(ROOT, 'lib')) + '); '
                'import common; common.start_local_server(common.config())')
    processes = []
    marker = os.path.join(home, 'serve-invocations.jsonl')
    try:
        for _ in range(2):
            processes.append(subprocess.Popen([sys.executable, '-I', '-c', launcher], cwd=project, env=env,
                                              stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE))
        answers = [p.communicate(timeout=8) for p in processes]
        healthy, detail = wait_local_health(env['CTRLB_URL'])
        invocations = [json.loads(line) for line in read(marker).decode().splitlines()] if os.path.exists(marker) else []
        check_local_startup('local startup concurrent: two processes spawn one healthy server from CtrlB home',
              all(p.returncode == 0 for p in processes) and all(a == (b'', b'') for a in answers)
              and len(invocations) == 1 and os.path.realpath(invocations[0]['cwd']) == os.path.realpath(home)
              and healthy, home, env['CTRLB_URL'], detail)
        mutex = os.path.join(home, 'serve.lo' + 'ck')
        check_local_startup('local startup concurrent: spawn mutex is private', os.path.isfile(mutex)
              and stat.S_IMODE(os.stat(mutex).st_mode) == 0o600, home, env['CTRLB_URL'])
    finally:
        for p in processes:
            if p.poll() is None:
                p.kill()
                p.communicate()
        stop_local_stub(home)


def test_local_request_proxy():
    def server(calls):
        class H(StubRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.rfile.read(int(self.headers.get('Content-Length', 0)))
                calls.append((self.command, self.path, self.headers.get('Authorization')))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"ok": true}')

            do_POST = do_DELETE = do_GET

        httpd = StubHTTPServer(('127.0.0.1', 0), H)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        return httpd

    direct, proxied = [], []
    target, proxy = server(direct), server(proxied)
    proxy_url = f'http://127.0.0.1:{proxy.server_address[1]}'
    env = dict(http_proxy=proxy_url, HTTP_PROXY=proxy_url, https_proxy=proxy_url, HTTPS_PROXY=proxy_url,
               no_proxy='', NO_PROXY='')
    try:
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(common.urllib.request, '_opener', None):
            answers = []
            for host in ('127.0.0.1', 'localhost'):
                cfg = dict(mode='local', url=f'http://{host}:{target.server_address[1]}', key=KEY)
                for method, path in (('POST', '/upload'), ('POST', '/check'), ('POST', '/relay'),
                                     ('DELETE', '/handoff'), ('GET', '/healthz')):
                    answers.append(common.request(cfg, method, path, body={} if method != 'GET' else None))
            check('local request proxy: all loopback requests reach the server and the proxy receives nothing',
                  len(direct) == 10 and not proxied and all(a == {'ok': True} for a in answers)
                  and all(c[2] == 'Bearer ' + KEY for c in direct))
            common.request(dict(mode='hosted', url=f'http://127.0.0.1:{target.server_address[1]}', key=KEY),
                           'GET', '/hosted')
            with mock.patch.dict(os.environ, CTRLB_ALLOW_HTTP='1'):
                common.request(dict(mode='local', url='http://example.test', key=KEY), 'GET', '/remote')
            check('local request proxy: hosted and non-loopback requests retain proxy behavior',
                  len(proxied) == 2 and proxied[0][1].endswith('/hosted') and proxied[1][1].endswith('/remote'))
    finally:
        for httpd in (target, proxy):
            httpd.shutdown()
            httpd.server_close()


def test_request_transport():
    import contextlib
    import io
    import runpy
    state, source_calls, target_calls = {}, [], []

    def server(calls, redirect=False):
        class Handler(StubRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                calls.append((self.command, self.path))
                self.rfile.read(int(self.headers.get('Content-Length', 0)))
                self.send_response(state['status'] if redirect else 200)
                if redirect:
                    self.send_header('Location', state['location'])
                self.end_headers()
                self.wfile.write(b'{}')

            do_POST = do_GET

        httpd = StubHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        return httpd

    source, target = server(source_calls, True), server(target_calls)
    source_url = f'http://127.0.0.1:{source.server_address[1]}'
    target_url = f'http://127.0.0.1:{target.server_address[1]}'
    try:
        for mode in ('hosted', 'local'):
            rejected = []
            for status in (301, 302, 303, 307, 308):
                for location in (target_url + '/target', source_url + '/same-origin'):
                    for method in ('GET', 'POST'):
                        state.update(status=status, location=location)
                        before = len(source_calls)
                        try:
                            common.request(dict(url=source_url, key=KEY, mode=mode), method, '/records',
                                           body={'text': 'synthetic'} if method == 'POST' else None)
                            rejected.append(False)
                        except common.SeekerError as e:
                            rejected.append(e.status == status and 'redirects are not allowed' in str(e)
                                            and len(source_calls) == before + 1 and source_calls[-1] == (method, '/records'))
            check(mode + ' API calls reject same-origin and cross-origin redirects without replaying or changing methods',
                  all(rejected) and not target_calls and len(rejected) == 20)
    finally:
        for httpd in (source, target):
            httpd.shutdown()
            httpd.server_close()
    with mock.patch.dict(os.environ, {}, clear=True):
        allowed = ['https://example.test', 'http://localhost:7531', 'http://127.0.0.1', 'http://127.0.0.2',
                   'http://127.255.255.254', 'http://[::1]:7531']
        for url in allowed:
            common.validate_url(url)
        check('HTTPS and HTTP on localhost, the IPv4 loopback range and IPv6 loopback are allowed', True)
        unsafe = ['http://example.test', 'http://192.0.2.1', 'http://localhost.example.test',
                  'http://128.0.0.1', 'http://[::2]', 'http://127.0.0.1@example.test']
        rejected = []
        with mock.patch.object(common.urllib.request, 'build_opener') as build:
            for mode in ('hosted', 'local'):
                for url in unsafe:
                    try:
                        common.request(dict(url=url, key=KEY, mode=mode), 'POST', '/records', body={})
                        rejected.append(False)
                    except ValueError as e:
                        rejected.append('CTRLB_ALLOW_HTTP=1' in str(e))
            check('remote HTTP is rejected before network access in both modes', all(rejected) and not build.called)
        for opt in ('', '0', 'true', '1'):
            env = dict(CTRLB_URL='http://192.0.2.1', CTRLB_KEY=KEY, CTRLB_ALLOW_HTTP=opt)
            with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(common, '_file_config', return_value={}), \
                    mock.patch.object(common.urllib.request, 'build_opener') as build:
                build.return_value.open.return_value.__enter__.return_value.read.return_value = b'{}'
                try:
                    common.request(common.config(), 'POST', '/records', body={})
                    accepted = True
                except ValueError:
                    accepted = False
                check('environment HTTP configuration requires the exact opt-in value ' + repr(opt),
                      accepted == (opt == '1') and build.called == accepted)
        configure = runpy.run_path(os.path.join(ROOT, 'bin', 'configure.py'))['main']
        for opt in ('', '1'):
            home = os.path.join(WORK, 'home-http-configure-' + (opt or 'default'))
            shutil.rmtree(home, ignore_errors=True)
            argv = ['configure.py', '--url', 'http://192.0.2.1:7531', '--dataset', 'agent_sessions',
                    '--run', '', '--key-stdin']
            with mock.patch.dict(os.environ, {'CTRLB_ALLOW_HTTP': opt}, clear=True), \
                    mock.patch.object(common, 'HOME', home), mock.patch.object(common, 'runs', return_value={}) as runs, \
                    mock.patch.object(sys, 'argv', argv), mock.patch.object(sys, 'stdin', io.StringIO(KEY + '\n')), \
                    contextlib.redirect_stdout(io.StringIO()):
                try:
                    configure()
                    error = ''
                except SystemExit as e:
                    error = str(e)
            saved = os.path.isfile(os.path.join(home, 'config.json'))
            check('configure remote HTTP ' + ('with opt-in saves the URL' if opt else 'requires HTTPS or the opt-in'),
                  (saved and runs.called and not error) if opt else
                  (not saved and not runs.called and 'HTTPS' in error and 'CTRLB_ALLOW_HTTP=1' in error))


def test_mcp_url_transport():
    import contextlib
    import io
    import runpy
    urls = ['https://example.test/mcp', 'http://localhost:7531/mcp', 'http://127.0.0.2/mcp',
            'http://[::1]:7531/mcp', 'http://192.0.2.1/mcp', 'http://localhost.example.test/mcp']
    home = os.path.join(WORK, 'mcp-transport')
    shutil.rmtree(home, ignore_errors=True)
    os.makedirs(home)
    path = os.path.join(home, 'config.toml')
    original = 'model = "example"\n'
    with open(path, 'w') as fh:
        fh.write(original)
    for source in ('flag', 'environment', 'file'):
        for opt in ('', 'true', '1'):
            for url in urls:
                accepted = url in urls[:4] or opt == '1'
                env = {'CTRLB_ALLOW_HTTP': opt, 'PATH': os.environ.get('PATH', os.defpath)}
                args = ['--mcp-url', url] if source == 'flag' else []
                if source == 'environment':
                    env['CTRLB_MCP_URL'] = url
                stored = {'mcp_url': url} if source == 'file' else {}
                with mock.patch.object(common, '_file_config', return_value=stored):
                    # Run main in process so file configuration is isolated too; it performs no network I/O.
                    main = runpy.run_path(INSTALL)['main']
                    with mock.patch.dict(os.environ, env, clear=True), contextlib.redirect_stdout(io.StringIO()), \
                            contextlib.redirect_stderr(io.StringIO()) as err:
                        code = main(args + ['--config', path])
                new = read(path).decode()
                if accepted and not can_validate_toml():
                    check('safe MCP URL still requires a TOML validator: ' + source + ' ' + url,
                          code == 2 and new == original and 'TOML validation requires' in err.getvalue())
                    continue
                check('MCP installer transport: ' + source + ' opt=' + repr(opt) + ' ' + url,
                      (code == 0 and url in new) if accepted else
                      (code == 2 and new == original and 'HTTPS' in err.getvalue() and 'CTRLB_ALLOW_HTTP=1' in err.getvalue()))
                with open(path, 'w') as fh:
                    fh.write(original)
    status = runpy.run_path(os.path.join(ROOT, 'bin', 'status.py'))['main']
    for field in ('url', 'mcp_url'):
        for mode in ('hosted', 'local'):
            for opt in ('', 'true', '1'):
                cfg = dict(url='https://example.test', mcp_url='https://example.test/mcp', key=KEY,
                           dataset=common.DEFAULT_DATASET, user='', run='', agent='', enforce='', mode=mode, off=False)
                cfg[field] = 'http://192.0.2.1' + ('/mcp' if field == 'mcp_url' else '')
                with mock.patch.dict(os.environ, {'CTRLB_ALLOW_HTTP': opt}, clear=True), \
                        mock.patch.object(common, 'HOME', home), mock.patch.object(common, 'config', return_value=cfg), \
                        mock.patch.object(common, 'runs', return_value={}) as runs, \
                        mock.patch.object(common, 'local_health', return_value=200) as health, \
                        mock.patch.object(common, 'ctrlb_binary', return_value=None), contextlib.redirect_stdout(io.StringIO()) as out:
                    status()
                check('status checks ' + field + ' transport in ' + mode + ' opt=' + repr(opt),
                      (runs.called or health.called) == (opt == '1')
                      and (opt == '1' or ('Unsafe ' + field in out.getvalue() and 'CTRLB_ALLOW_HTTP=1' in out.getvalue())))


def test_local_health_answers(seeker):
    for call, label, sid in ((hook, 'Claude', SID), (chook, 'Codex', CSID)):
        for status in (401, 503):
            home = os.path.join(WORK, f'home-health-{label}-{status}')
            binary = local_stub(home)
            env = base_env(seeker.url, home, CTRLB_MODE='local', CTRLB_BIN=binary)
            if status == 401:
                env['CTRLB_KEY'] = 'invalid-test-key'
            seeker.state['health'] = 'down'
            try:
                p, dt = call('session-start', dict(session_id=sid, hook_event_name='SessionStart'), env)
                check(f'{label} startup: HTTP {status} does not spawn and returns quickly',
                      p.returncode == 0 and dt < 3.0 and not os.path.exists(os.path.join(home, 'invoked.json')))
                lines = _log(home).splitlines()
                check(f'{label} startup: HTTP {status} logs only the status once',
                      len(lines) == 1 and lines[0].endswith(f'local server: health HTTP {status}'))
            finally:
                seeker.state.pop('health', None)

    for status in (401, 503):
        # A connection refusal followed by an HTTP error exercises the post-spawn poll without a process.
        import io
        response = io.BytesIO(b'response body must not be read or logged')
        error = common.urllib.error.HTTPError('http://localhost/healthz', status, 'unused reason', {}, response)
        opener = mock.Mock()
        opener.open.side_effect = [ConnectionRefusedError(), error]
        with mock.patch.object(common.urllib.request, 'build_opener', return_value=opener), \
                mock.patch.object(common, 'ctrlb_binary', return_value='/synthetic/ctrlb'), \
                mock.patch.object(common.subprocess, 'Popen') as spawn, \
                mock.patch.object(common.time, 'sleep') as sleep, mock.patch.object(common, 'log') as log:
            common.start_local_server(dict(mode='local', url='http://localhost', key=KEY))
        check(f'startup poll: HTTP {status} ends polling after one spawn',
              spawn.call_count == 1 and opener.open.call_count == 2 and not sleep.called and response.closed
              and log.call_args_list == [mock.call(f'local server: health HTTP {status}')])


def test_local_binary_path():
    project = os.path.join(WORK, 'binary-project')
    relative_binary = local_stub(project)
    shutil.copy(relative_binary, os.path.join(project, 'ctrlb'))
    absolute_binary = local_stub(os.path.join(WORK, 'binary-absolute'))
    previous = os.getcwd()
    try:
        os.chdir(project)
        with mock.patch.object(common, 'HOME', os.path.join(WORK, 'binary-no-home')), \
                mock.patch.dict(os.environ, {}, clear=True):
            for relative in ('', '.', 'bin', os.pathsep.join(('', '.', 'bin'))):
                with mock.patch.dict(os.environ, PATH=relative + os.pathsep + os.path.dirname(absolute_binary)):
                    check(f'binary lookup: skips relative PATH {relative!r} for absolute entry',
                          common.ctrlb_binary() == absolute_binary)
                with mock.patch.dict(os.environ, PATH=relative):
                    check(f'binary lookup: relative PATH {relative!r} alone finds nothing', common.ctrlb_binary() is None)
    finally:
        os.chdir(previous)


def test_local_configure_status(seeker):
    home = os.path.join(WORK, "home-configure local's")
    binary = local_stub(home)
    env = base_env(seeker.url, home, CTRLB_BIN=binary)
    env.pop('CTRLB_URL')
    env.pop('CTRLB_KEY')
    configure = os.path.join(ROOT, 'bin', 'configure.py')
    status = os.path.join(ROOT, 'bin', 'status.py')
    for flags in ([], ['--port', '8753']):
        os.chmod(home, 0o755)
        p = subprocess.run([sys.executable, '-I', configure, '--local'] + flags, input='', capture_output=True,
                           text=True, env=env, timeout=5)
        invoked = os.path.join(home, 'invoked.json')
        args = json.loads(read(invoked)).get('argv') if os.path.exists(invoked) else []
        check('configure --local ' + ' '.join(flags) + ': invokes init without prompting or showing key',
              p.returncode == 0 and args == ['init'] + flags and KEY not in p.stdout + p.stderr)
        check('configure --local: existing CTRLB_HOME is private', stat.S_IMODE(os.stat(home).st_mode) == 0o700)
        path = os.path.join(home, 'config.json')
        cfg = json.loads(read(path)) if os.path.isfile(path) else {}
        check('configure --local: both exports read config when loaded',
              cfg.get('mcp_url') and 'export CTRLB_MCP_URL="$(' in p.stdout and 'export CTRLB_KEY="$(' in p.stdout
              and 'python3 -I -c' in p.stdout and 'config.json' in p.stdout)
        exports = '\n'.join(s.strip() for s in p.stdout.splitlines() if s.strip().startswith('export '))
        if exports:
            # Check the exported value without letting it reach the test output.
            script = exports + '\n' + 'test "$CTRLB_KEY" = ' + KEY
            q = subprocess.run(['/bin/sh', '-c', script], capture_output=True, text=True, env=env, timeout=5)
            check('configure --local: printed export is executable', q.returncode == 0 and not q.stdout and not q.stderr)
            hosted = dict(mode='hosted', mcp_url='https://example.test/mcp', key='hosted-test-key')
            with open(path, 'w') as fh:
                json.dump(hosted, fh)
            script = exports + '\ntest "$CTRLB_MCP_URL" = https://example.test/mcp && test "$CTRLB_KEY" = hosted-test-key'
            q = subprocess.run(['/bin/sh', '-c', script], capture_output=True, text=True, env=env, timeout=5)
            check('configure --local: saved exports reload hosted URL and key after config switches',
                  q.returncode == 0 and not q.stdout and not q.stderr)
            with open(path, 'w') as fh:
                json.dump(cfg, fh)
    p = subprocess.run([sys.executable, '-I', configure, '--local'], input='', capture_output=True, text=True,
                       env=dict(env, STUB_FAIL='1'), timeout=5)
    check('configure --local: failed init does not reveal output or key', p.returncode != 0 and KEY not in p.stdout + p.stderr)
    env['CTRLB_URL'] = seeker.url
    env['CTRLB_KEY'] = KEY
    seeker.reset()
    p = subprocess.run([sys.executable, '-I', status], capture_output=True, text=True, env=env, timeout=5)
    check('status local: mode, health answer, binary path; no key', p.returncode == 0 and '"mode": "local"' in p.stdout
          and 'HTTP 200' in p.stdout and binary in p.stdout and KEY not in p.stdout + p.stderr)
    check('status local: no Relay request', not any('/ai/relay/' in c['path'] for c in seeker.calls))
    p = subprocess.run([sys.executable, '-I', status], capture_output=True, text=True,
                       env=dict(env, CTRLB_URL=f'http://127.0.0.1:{closed_port()}'), timeout=5)
    check('status local: unreachable health is reported without failure', p.returncode == 0
          and 'health check failed' in p.stdout.lower() and KEY not in p.stdout + p.stderr)


def test_local_install():
    """Only local fixtures: the root installer verifies before replacing a binary."""
    import io
    import pathlib
    import tempfile
    from contextlib import redirect_stderr, redirect_stdout
    from unittest import mock
    import local_install

    with tempfile.TemporaryDirectory(prefix='local install ', dir=WORK) as temporary:
        release = pathlib.Path(temporary, 'release files')
        release.mkdir()
        stub = (b'#!/bin/sh\n'
                b'printf "%s\\n" "$@" > "$CTRLB_HOME/init-args"\n'
                b'echo "stub init output"\n'
                b'echo "stub init error output" >&2\n'
                b'if [ "${STUB_INIT_FAIL:-}" = 1 ]; then exit 1; fi\n')
        filename = 'ctrlb-linux-amd64'
        (release / filename).write_bytes(stub)
        manifest = release / 'SHA256SUMS'
        checksum = hashlib.sha256(stub).hexdigest()
        manifest.write_text(checksum + '  ' + filename + '\n')
        base = release.as_uri()

        def run(name, args, system='Linux', machine='x86_64', **extra):
            home = os.path.join(temporary, name)
            env = {k: v for k, v in os.environ.items() if not k.startswith('CTRLB_')}
            env.update(CTRLB_HOME=home)
            env.update(extra)
            launcher = ('import platform, runpy; platform.system = lambda: ' + repr(system) +
                        '; platform.machine = lambda: ' + repr(machine) + '; runpy.run_path(' +
                        repr(os.path.join(ROOT, 'install.py')) + ', run_name="__main__")')
            process = subprocess.run([sys.executable, '-I', '-c', launcher, '--local'] + args,
                                     capture_output=True, text=True, env=env, timeout=15)
            return process, pathlib.Path(home)

        p, home = run('success', ['--version', 'v1.2.3', '--download-url', base, '--port', '7654'])
        installed = home / 'bin' / 'ctrlb'
        check('local install: file base with spaces installs the verified bytes with mode 0755',
              p.returncode == 0 and installed.is_file() and installed.read_bytes() == stub
              and stat.S_IMODE(installed.stat().st_mode) == 0o755, p.stderr[-200:])
        check('local install: new CTRLB_HOME and bin are private',
              all(stat.S_IMODE(d.stat().st_mode) == 0o700 for d in (home, home / 'bin')))
        check('local install: the installed binary initializes the requested port',
              (home / 'init-args').is_file() and (home / 'init-args').read_text() == 'init\n--port\n7654\n')
        check('local install: next steps include both agents and two config-reading exports, init output suppressed',
              'claude plugin install ctrlb-agent@ctrlb-agent' in p.stdout and 'install.py --codex' in p.stdout
              and p.stdout.count('  export ') == 2 and 'export CTRLB_MCP_URL=' in p.stdout
              and 'export CTRLB_KEY=' in p.stdout and 'config.json' in p.stdout
              and 'stub init' not in p.stdout + p.stderr)
        exports = '\n'.join(line.strip() for line in p.stdout.splitlines() if line.lstrip().startswith('export '))
        syntax = subprocess.run(['/bin/sh', '-n'], input=exports, capture_output=True, text=True, timeout=5)
        check('local install: printed exports have valid shell syntax', syntax.returncode == 0)
        victim = pathlib.Path(temporary, 'untouched.txt')
        victim.write_text('keep me\n')
        stale = home / 'bin' / '.ctrlb-download-stale'
        stale.symlink_to(victim)
        os.chmod(home, 0o755)
        os.chmod(home / 'bin', 0o755)
        p, home = run('success', ['--download-url', base])
        check('local install: existing CTRLB_HOME and bin are tightened', p.returncode == 0
              and all(stat.S_IMODE(d.stat().st_mode) == 0o700 for d in (home, home / 'bin')))
        check('local install: a stale download link is untouched and fresh temporary files are removed',
              p.returncode == 0 and victim.read_text() == 'keep me\n' and stale.is_symlink()
              and list((home / 'bin').glob('.ctrlb-download-*')) == [stale] and installed.read_bytes() == stub)
        p, home = run('env-base', [], CTRLB_DOWNLOAD_URL=base)
        check('local install: CTRLB_DOWNLOAD_URL is a full base and init receives no default port flag',
              p.returncode == 0 and (home / 'init-args').read_text() == 'init\n', p.stderr[-200:])
        p, _ = run('flag-base', ['--download-url', base], CTRLB_DOWNLOAD_URL='http://192.0.2.1/releases')
        check('local install: --download-url wins over the environment', p.returncode == 0, p.stderr[-200:])

        manifest.write_text('0' * 64 + '  ' + filename + '\n')
        p, home = run('wrong-checksum', ['--download-url', base])
        check('local install: wrong checksum refuses, deletes the download and never runs init',
              p.returncode != 0 and 'checksum mismatch' in p.stderr and not (home / 'bin' / 'ctrlb').exists()
              and not list(home.glob('bin/.ctrlb-download-*')) and not (home / 'init-args').exists())
        previous = home / 'bin' / 'ctrlb'
        previous.write_bytes(b'previous installation')
        p, _ = run('wrong-checksum', ['--download-url', base])
        check('local install: failed verification preserves an existing installation',
              p.returncode != 0 and previous.read_bytes() == b'previous installation'
              and not list(home.glob('bin/.ctrlb-download-*')))
        manifest.write_text(checksum + '  ctrlb-darwin-arm64\n')
        p, home = run('missing-checksum', ['--download-url', base])
        check('local install: missing target checksum refuses and leaves nothing installed',
              p.returncode != 0 and 'missing checksum' in p.stderr and not (home / 'bin' / 'ctrlb').exists()
              and not list(home.glob('bin/.ctrlb-download-*')) and not (home / 'init-args').exists())
        for system, machine in [('Windows', 'AMD64'), ('Linux', 'riscv64')]:
            p, home = run('unsupported-' + machine, ['--download-url', base], system=system, machine=machine)
            check(f'local install: unsupported {system}/{machine} refuses before downloading',
                  p.returncode != 0 and 'unsupported platform' in p.stderr and not home.exists())
        p, home = run('unsafe-http', ['--download-url', 'http://192.0.2.1/releases'])
        check('local install: HTTP on a non-loopback host refuses before downloading',
              p.returncode != 0 and 'must use HTTPS' in p.stderr and not home.exists())

        manifest.write_text(checksum + '  ' + filename + '\n')
        p, home = run('init-failure', ['--download-url', base], STUB_INIT_FAIL='1')
        check('local install: init failure returns non-zero without relaying binary output',
              p.returncode != 0 and 'ctrlb init failed' in p.stderr and 'stub init' not in p.stdout + p.stderr)
        targets = []
        for system in ('Darwin', 'Linux'):
            for machine in ('arm64', 'aarch64', 'amd64', 'x86_64'):
                with mock.patch.object(local_install.platform, 'system', return_value=system), \
                        mock.patch.object(local_install.platform, 'machine', return_value=machine):
                    arch = 'arm64' if machine in ('arm64', 'aarch64') else 'amd64'
                    targets.append(local_install.target_name() == f'ctrlb-{system.lower()}-{arch}')
        check('local install: all four release targets and architecture aliases map correctly', all(targets))
        rejected = False
        try:
            local_install._SafeRedirect().redirect_request(None, None, 302, 'Found', {}, 'http://192.0.2.1/binary')
        except local_install.InstallError:
            rejected = True
        check('local install: redirects cannot bypass the HTTPS policy', rejected)
        redirects = []
        for target, allowed in [('https://files.example.test/ctrlb?sp=r&sig=abc', True),
                                ('https://user@files.example.test/ctrlb?sig=abc', False),
                                ('file:///etc/hostname', False)]:
            try:
                local_install.validate_url(target, redirect=True)
                redirects.append(allowed)
            except local_install.InstallError:
                redirects.append(not allowed)
        check('local install: a redirect may carry a query, but no user name and no file address', all(redirects))
        versions = []
        for args, version in [([], 'latest'), (['--version', 'v1.2.3'], 'v1.2.3')]:
            with mock.patch.object(local_install, 'install_binary') as download, \
                    mock.patch.dict(os.environ, {'CTRLB_DOWNLOAD_URL': base + '/<version>'}), \
                    redirect_stdout(io.StringIO()):
                code = local_install.main(args)
            versions.append(code == 0 and download.call_args.args[0] == base + '/' + version)
        check('local install: URL templates substitute the requested version or latest', all(versions))
        releases = 'https://github.com/ctrlb-hq/ctrlb-agent/releases'
        defaults = []
        environment = {k: v for k, v in os.environ.items() if k != 'CTRLB_DOWNLOAD_URL'}
        for args, expected in [([], releases + '/latest/download'),
                               (['--version', ''], releases + '/latest/download'),
                               (['--version', 'v1.2.3'], releases + '/download/v1.2.3')]:
            with mock.patch.object(local_install, 'install_binary') as download, \
                    mock.patch.dict(os.environ, environment, clear=True), \
                    redirect_stdout(io.StringIO()):
                code = local_install.main(args)
            defaults.append(code == 0 and download.call_args.args[0] == expected)
        check('local install: no download address uses the GitHub release, newest or named', all(defaults))
        refused = []
        for version in ('../v1', '..', '.', 'v1 beta', 'v1/x', 'v1?x'):
            stderr = io.StringIO()
            with mock.patch.object(local_install, 'install_binary') as download, \
                    mock.patch.dict(os.environ, environment, clear=True), redirect_stderr(stderr):
                code = local_install.main(['--version', version])
            refused.append(code == 1 and not download.called and '--version must be a release tag' in stderr.getvalue())
        check('local install: a version that is not one release tag is refused before any download', all(refused))


def test_local_install_streams():
    import http.client
    import io
    import pathlib
    import tempfile
    from contextlib import redirect_stderr
    import local_install

    filename = 'ctrlb-linux-amd64'
    base = 'https://example.test/release'
    with tempfile.TemporaryDirectory(prefix='install-streams-', dir=WORK) as temporary:
        home = pathlib.Path(temporary)
        (home / 'bin').mkdir()
        destination = home / 'bin' / 'ctrlb'
        destination.write_bytes(b'previous installation')
        for name, error in (('incomplete', http.client.IncompleteRead(b'partial')),
                            ('bad-status', http.client.BadStatusLine('invalid status'))):
            for stage in ('manifest', 'binary'):
                response = mock.MagicMock()
                response.__enter__.return_value = response
                response.read.side_effect = error
                manifest = io.BytesIO(('0' * 64 + '  ' + filename + '\n').encode())
                responses = [response] if stage == 'manifest' else [manifest, response]
                stderr = io.StringIO()
                with mock.patch.object(local_install, 'target_name', return_value=filename), \
                        mock.patch.object(local_install, '_open', side_effect=responses), \
                        mock.patch.dict(os.environ, CTRLB_HOME=str(home)), redirect_stderr(stderr):
                    try:
                        code = local_install.main(['--download-url', base])
                    except http.client.HTTPException:
                        code = None
                check(f'local install stream {stage} {name}: protocol errors become install errors and preserve binary',
                      code == 1 and 'could not download' in stderr.getvalue()
                      and 'nothing installed' in stderr.getvalue()
                      and destination.read_bytes() == b'previous installation'
                      and not list((home / 'bin').glob('.ctrlb-download-*')))

        chunk = b'x' * 8
        stream = mock.MagicMock()
        stream.__enter__.return_value = stream
        stream.read.side_effect = [chunk, chunk, chunk, AssertionError('read beyond size cap')]
        stderr = io.StringIO()
        manifest = io.BytesIO(('0' * 64 + '  ' + filename + '\n').encode())
        with mock.patch.object(local_install, 'target_name', return_value=filename), \
                mock.patch.object(local_install, 'MAX_BINARY_BYTES', 16, create=True), \
                mock.patch.object(local_install, '_open', side_effect=[manifest, stream]), \
                mock.patch.dict(os.environ, CTRLB_HOME=str(home)), redirect_stderr(stderr):
            try:
                code = local_install.main(['--download-url', base])
            except AssertionError:
                code = None
        check('local install stream size: stops reading at cap, removes temporary file and preserves binary',
              code == 1 and stream.read.call_count == 3 and 'download too large; nothing installed' in stderr.getvalue()
              and destination.read_bytes() == b'previous installation'
              and not list((home / 'bin').glob('.ctrlb-download-*')))

        manifest = io.BytesIO((hashlib.sha256(chunk).hexdigest() + '  ' + filename + '\n').encode())
        with mock.patch.object(local_install, 'target_name', return_value=filename), \
                mock.patch.object(local_install, 'MAX_BINARY_BYTES', len(chunk), create=True), \
                mock.patch.object(local_install, '_open', side_effect=[manifest, io.BytesIO(chunk)]), \
                mock.patch.object(local_install.subprocess, 'run', return_value=mock.Mock(returncode=0)):
            local_install.install_binary(base, str(home))
        check('local install stream size: binary exactly at the cap is accepted', destination.read_bytes() == chunk)


def test_local_install_download_errors():
    import pathlib
    import tempfile

    filename = 'ctrlb-linux-amd64'
    body = b'x' * (64 * 1024 + 1)
    manifest = (hashlib.sha256(body).hexdigest() + '  ' + filename + '\n').encode()

    class H(StubRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            path, _, query = self.path.partition('?')
            case, name = path.strip('/').split('/')
            is_manifest = name == 'SHA256SUMS'
            if case in ('signed-redirect', 'plain-http-redirect'):
                # A release host answers with a redirect to a signed file address.
                host = '127.0.0.1' if case == 'signed-redirect' else '127.0.0.2'
                self.send_response(302)
                self.send_header('Location', f'http://{host}:{self.server.server_address[1]}/signed/{name}?sp=r&sig=abc')
                self.end_headers()
            elif case == 'signed' and query != 'sp=r&sig=abc':
                self.send_response(403)
                self.end_headers()
            elif case == ('manifest-status' if is_manifest else 'binary-status'):
                self.connection.sendall(b'garbage status line\r\n\r\n')
            elif case == ('manifest-chunk' if is_manifest else 'binary-chunk'):
                self.connection.sendall(b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n10\r\ncut')
            else:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(manifest if is_manifest else body)
            self.close_connection = True

    httpd = StubHTTPServer(('127.0.0.1', 0), H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        with tempfile.TemporaryDirectory(prefix='download-errors-', dir=WORK) as temporary:
            for case in ('manifest-status', 'manifest-chunk', 'binary-status', 'binary-chunk', 'oversized'):
                home = pathlib.Path(temporary, case)
                (home / 'bin').mkdir(parents=True)
                destination = home / 'bin' / 'ctrlb'
                destination.write_bytes(b'previous installation')
                env = base_env('', str(home))
                # A smaller cap exercises the boundary without a large fixture.
                launcher = ('import sys, platform, runpy; sys.path.insert(0, ' + repr(os.path.join(ROOT, 'lib')) + '); '
                            'import local_install; local_install.MAX_BINARY_BYTES = 64 * 1024; '
                            'platform.system = lambda: "Linux"; platform.machine = lambda: "x86_64"; '
                            'runpy.run_path(' + repr(os.path.join(ROOT, 'install.py')) + ', run_name="__main__")')
                base = f'http://127.0.0.1:{httpd.server_address[1]}/{case}'
                p = subprocess.run([sys.executable, '-I', '-c', launcher, '--local', '--download-url', base],
                                   capture_output=True, text=True, env=env, timeout=10)
                message = ('download too large' if case == 'oversized' else
                           'could not download or read SHA256SUMS' if case.startswith('manifest') else
                           'could not download the CtrlB binary')
                check(f'local install download {case}: clear error, no traceback or partial replacement',
                      p.returncode != 0 and message in p.stderr and 'nothing installed' in p.stderr
                      and 'Traceback' not in p.stderr and destination.read_bytes() == b'previous installation'
                      and not list((home / 'bin').glob('.ctrlb-download-*')))
            for case in ('signed-redirect', 'plain-http-redirect'):
                home = pathlib.Path(temporary, case)
                (home / 'bin').mkdir(parents=True)
                destination = home / 'bin' / 'ctrlb'
                destination.write_bytes(b'previous installation')
                launcher = ('import platform, runpy; platform.system = lambda: "Linux"; '
                            'platform.machine = lambda: "x86_64"; '
                            'runpy.run_path(' + repr(os.path.join(ROOT, 'install.py')) + ', run_name="__main__")')
                base = f'http://127.0.0.1:{httpd.server_address[1]}/{case}'
                p = subprocess.run([sys.executable, '-I', '-c', launcher, '--local', '--download-url', base],
                                   capture_output=True, text=True, env=base_env('', str(home)), timeout=10)
                if case == 'signed-redirect':
                    # The fixture is not a program, so init fails after the verified replacement.
                    check('local install download: a redirect to a signed address with a query is followed',
                          destination.read_bytes() == body and 'ctrlb init' in p.stderr
                          and 'Traceback' not in p.stderr, p.stderr[-200:])
                else:
                    check('local install download: a redirect to plain HTTP on another host is refused',
                          p.returncode != 0 and 'redirected to an address that is not allowed' in p.stderr
                          and 'Traceback' not in p.stderr and destination.read_bytes() == b'previous installation'
                          and not list((home / 'bin').glob('.ctrlb-download-*')), p.stderr[-200:])
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_local_documentation():
    doc = read(os.path.join(ROOT, 'docs', 'local-mode.md')).decode()
    recall_tools = ['search_sessions', 'find_command', 'next_actions', 'fetch_records', 'session_index']
    check('local docs: five recall tools with descriptions',
          'five recall tools' in doc
          and re.findall(r'^- `(\w+)`: .+\.$', doc, re.M) == recall_tools)
    paths = ['README.md', 'docs/local-mode.md', 'docs/limits.md', 'docs/session-store.md',
             'docs/relay-tools.md',
             'plugin/skills/recall/SKILL.md', 'plugin/skills/relay/SKILL.md']
    for path in paths:
        text = read(os.path.join(ROOT, path)).decode()
        check(f'local recall wording: {path} names all five tools and session index scope',
              all(f'`{tool}`' in text for tool in recall_tools)
              and ("lists the caller's recent sessions" in text
                   or "list the caller's recent sessions" in text))
        check(f'local recall wording: {path} documents stored reads, run independence and no model writes',
              'read only stored records and make no model call' in text
              and 'with or without `CTRLB_RUN`' in text
              and 'Local mode writes no running notes or memories; those need a model.' in text)
    for name in ('claude-code', 'codex', 'http', 'otel'):
        path = f'docs/connect-{name}.md'
        check(f'local docs: {path} links to local setup',
              '(local-mode.md)' in read(os.path.join(ROOT, path)).decode())
    commands = [line for line in doc.splitlines() if line.startswith('ctrlb push ')]
    check('local docs: push uses hosted environment key or private key file without a key argument',
          commands and not any(re.search(r'--key(?:\s|=)', c) for c in commands)
          and 'CTRLB_HOSTED_KEY' in doc and '--key-file' in doc and 'group/world readable' in doc
          and 'getpass' in doc)
    check('local docs: existing binary setup documents configure local, port and binary override',
          'python3 -I bin/configure.py --local' in doc and 'CTRLB_BIN' in doc and '--port N' in doc)


def test_codex_install_hardening():
    """Installer shell quoting, header masking and exclusive temporary files."""
    if not can_validate_toml():
        print('SKIP Codex installer hardening integration (needs Python 3.11+ or codex)')
        return
    base = os.path.join(WORK, 'hardening')
    shutil.rmtree(base, ignore_errors=True)
    os.makedirs(base)

    # a checkout path with shell syntax in it: the hook command passes it as written and runs nothing from it
    odd = os.path.join(base, 'repo $(touch PWNED) `touch TICK` "q" \\b')
    for d in ('codex', 'lib'):
        shutil.copytree(os.path.join(ROOT, d), os.path.join(odd, d), ignore=shutil.ignore_patterns('__pycache__'))
    chome = os.path.join(base, 'codex-odd')
    os.makedirs(chome)
    env = {k: v for k, v in os.environ.items() if not k.startswith('CTRLB_') and k != 'CODEX_HOME'}
    env.update(CTRLB_HOME=os.path.join(base, 'home-install'), CODEX_HOME=chome)
    p = subprocess.run([sys.executable, '-I', os.path.join(odd, 'codex', 'install.py'), '--mcp-url', 'http://127.0.0.1:8095/mcp'],
                       capture_output=True, text=True, env=env, timeout=30)
    cmds = [json.loads(l.split('=', 1)[1]) for l in read(os.path.join(chome, 'config.toml')).decode().splitlines()
            if l.startswith('command = ')]
    run_dir = os.path.join(base, 'run')
    os.makedirs(run_dir)
    hook_env = dict(env, CTRLB_OFF='1')
    for c in cmds[:1]:
        subprocess.run(['sh', '-c', c], cwd=run_dir, input='', capture_output=True, text=True, env=hook_env, timeout=30)
    # what sh makes of the path word: printf it in place of python3 -I
    words = [subprocess.run(['sh', '-c', "printf '%s' " + c[len('python3 -I '):].rsplit(' ', 1)[0]], cwd=run_dir,
                            capture_output=True, text=True, timeout=10).stdout for c in cmds]
    check('codex install: a checkout path with $(...), backquotes, quotes and a backslash is one shell word; nothing in it runs',
          p.returncode == 0 and len(cmds) == 7 and not os.listdir(run_dir)
          and all(c.startswith('python3 -I ') for c in cmds)
          and all(w == os.path.realpath(os.path.join(odd, 'codex', 'hook.py')) for w in words),
          f'{os.listdir(run_dir)} {cmds[:1]} {p.stderr[-200:]}')

    # --dry-run never prints an OTEL header value; the file written without --dry-run holds it, as documented
    chome = os.path.join(base, 'codex-otel')
    os.makedirs(chome)
    hv = 'otelhdr' + 'value0001'
    args = ['--mcp-url', 'http://127.0.0.1:8095/mcp', '--otel', '--otel-endpoint', 'http://127.0.0.1:8095/api/o/s/_otel/v1/logs',
            '--otel-header', f'Authorization=Bearer {hv}', '--otel-header', 'X-Org=org1']
    p = install(args + ['--dry-run'], chome)
    check('codex install --dry-run: OTEL header values are shown as "<hidden>", never printed',
          p.returncode == 0 and hv not in p.stdout + p.stderr and 'org1' not in p.stdout
          and '+headers = { "Authorization" = "<hidden>", "X-Org" = "<hidden>" }' in p.stdout, p.stdout[-300:])
    p = install(args, chome)
    p2 = install(['--mcp-url', 'http://127.0.0.1:8095/mcp', '--otel', '--otel-endpoint', 'http://127.0.0.1:8095/api/o/s/_otel/v1/logs',
                  '--dry-run'], chome)
    check('codex install --dry-run: a header value already in config.toml is hidden on the removed line too',
          hv in read(os.path.join(chome, 'config.toml')).decode() and hv not in p2.stdout and '-headers = {' in p2.stdout,
          p2.stdout[-300:])

    # a file or link named like the old fixed temporary file is never written through
    chome = os.path.join(base, 'codex-tmp')
    os.makedirs(chome)
    victim = os.path.join(base, 'victim.txt')
    with open(victim, 'w') as fh:
        fh.write('keep me\n')
    os.symlink(victim, os.path.join(chome, 'config.toml.ctrlb-tmp'))
    p = install(['--mcp-url', 'http://127.0.0.1:8095/mcp'], chome)
    cfgp = os.path.join(chome, 'config.toml')
    check('codex install: a link named config.toml.ctrlb-tmp is not followed; config.toml is a new 0600 file; no temporary '
          'file is left', p.returncode == 0 and read(victim) == b'keep me\n' and not os.path.islink(cfgp)
          and stat.S_IMODE(os.stat(cfgp).st_mode) == 0o600
          and sorted(os.listdir(chome)) == ['config.toml', 'config.toml.ctrlb-tmp'], str(sorted(os.listdir(chome))))

def test_hardening(seeker):
    """Atomic config writes, private local state and confined artefact restoration."""
    import relay_hooks
    base = os.path.join(WORK, 'hardening-config')
    os.makedirs(base, exist_ok=True)
    # configure.py: atomic write, a 0600 backup of the old file, and a link kept and said
    home = os.path.join(base, 'home-cfg')
    cenv = {k: v for k, v in os.environ.items() if not k.startswith('CTRLB_')}
    cenv['CTRLB_HOME'] = home
    k1, k2 = 'cfgkey' + '-first-0001', 'cfgkey' + '-second-0002'

    def configure(key, url):
        return subprocess.run([sys.executable, '-I', os.path.join(ROOT, 'bin', 'configure.py'), '--url', url, '--dataset',
                               'agent_sessions', '--run', '', '--key-stdin'], input=key + '\n', capture_output=True, text=True,
                              env=cenv, timeout=30)
    configure(k1, seeker.url)
    p = configure(k2, seeker.url)
    names = sorted(os.listdir(home))
    bk = [n for n in names if n.startswith('config.json.ctrlb-backup-')]
    cfg_path = os.path.join(home, 'config.json')
    check('configure.py: a second run backs up the old config.json (0600) and replaces it whole (0600); no temporary file',
          p.returncode == 0 and len(bk) == 1 and k1 in read(os.path.join(home, bk[0])).decode()
          and stat.S_IMODE(os.stat(os.path.join(home, bk[0])).st_mode) == 0o600
          and k2 in read(cfg_path).decode() and stat.S_IMODE(os.stat(cfg_path).st_mode) == 0o600
          and not [n for n in names if 'ctrlb-tmp' in n] and k1 not in p.stdout + p.stderr and k2 not in p.stdout + p.stderr,
          str(names))
    real_cfg = os.path.join(base, 'dotfiles-config.json')
    shutil.copy(cfg_path, real_cfg)
    os.chmod(real_cfg, 0o600)
    os.unlink(cfg_path)
    os.symlink(real_cfg, cfg_path)
    p = configure(k1, seeker.url)
    check('configure.py: when config.json is a link, the file it points to is written, the link is kept, and it says so',
          p.returncode == 0 and os.path.islink(cfg_path) and k1 in read(real_cfg).decode()
          and 'is a link to' in p.stdout, p.stdout[-300:])

    # CTRLB_HOME, its state directory and every file the hooks write are private, also under umask 022, and an
    # existing world-readable log is tightened
    home = os.path.join(base, 'home-hooks')
    os.makedirs(home, mode=0o755)
    os.chmod(home, 0o755)
    with open(os.path.join(home, 'hooks.log'), 'w') as fh:
        fh.write('old line\n')
    os.chmod(os.path.join(home, 'hooks.log'), 0o644)
    tr = os.path.join(base, 'session.jsonl')
    shutil.copy(os.path.join(FIX, 'claude', 'session.jsonl'), tr)
    henv = base_env('http://127.0.0.1:9', home)
    old_umask = os.umask(0o022)
    try:
        for mode, ev in (('upload', 'Stop'), ('post-tool', 'PostToolUse')):
            hook(mode, {'session_id': SID, 'transcript_path': tr, 'cwd': '/work/dash', 'hook_event_name': ev, 'tool_name': 'Bash',
                        'tool_input': {'command': 'pytest -q'}, 'tool_response': {'stdout': 'ok', 'exit_code': 0}}, henv)
    finally:
        os.umask(old_umask)
    loose = []
    for d, ds, fs in os.walk(home):
        if os.path.relpath(d, home).split(os.sep)[0] == 'relay':
            continue
        for n in fs:
            m = stat.S_IMODE(os.stat(os.path.join(d, n)).st_mode)
            if m & 0o077:
                loose.append(f'{os.path.relpath(os.path.join(d, n), home)} {oct(m)}')
        m = stat.S_IMODE(os.stat(d).st_mode)
        if m & 0o077:
            loose.append(f'{os.path.relpath(d, home)}/ {oct(m)}')
    state = os.path.join(home, 'state')
    check('hooks: CTRLB_HOME and state are 0700 and every state, mutex and log file is 0600 (umask 022; an old 0644 log '
          'is tightened)', not loose and os.path.isdir(state) and os.listdir(state), ', '.join(loose))

    # artefact restore never writes through a link in the scratch directory, or outside it
    scratch = os.path.join(base, 'scratch')
    outside = os.path.join(base, 'outside')
    os.makedirs(scratch)
    os.makedirs(outside)
    os.symlink(outside, os.path.join(scratch, 'link'))
    arts = {'artifacts': {'artifacts': [{'path': 'link/settings.json', 'agent': 'a1', 'text': '{"x": 1}'},
                                        {'path': 'notes/plan.md', 'agent': 'a1', 'text': 'plan'}]}}
    old_home = common.HOME
    common.HOME = os.path.join(base, 'home-restore')
    try:
        have = relay_hooks.restore_artifacts(arts, 'run-h', 'a1', scratch)
        logtext = read(os.path.join(common.HOME, 'hooks.log')).decode() if os.path.exists(os.path.join(common.HOME, 'hooks.log')) else ''
    finally:
        common.HOME = old_home
    check('artefact restore: a path through a link in the scratch directory is refused and logged; a plain path is restored',
          have == ['notes/plan.md'] and not os.listdir(outside) and read(os.path.join(scratch, 'notes', 'plan.md')) == b'plan'
          and 'artifact not restored: link/settings.json' in logtext
          and not [n for n in os.listdir(os.path.join(scratch, 'notes')) if 'ctrlb-tmp' in n], f'{have} {os.listdir(outside)} {logtext[-200:]}')


def test_upload_acceptance(seeker):
    home = os.path.join(WORK, 'home-upload-acceptance')
    path = os.path.join(WORK, 'acceptance.jsonl')
    with open(path, 'w') as fh:
        fh.write(line('user', 'acknowledged text', '2026-11-03T10:00:00Z'))
    cfg = dict(url=seeker.url, key=KEY, dataset=common.DEFAULT_DATASET, user='', run='', agent='')
    responses = [(200, b'<html>login</html>'), (200, {'accepted': 1}), (201, {'accepted': 1}),
                 (204, b''), (202, b'not JSON'), (202, {}), (202, []), (202, {'accepted': 0}),
                 (202, {'accepted': 2}), (202, {'accepted': True}), (202, {'accepted': '1'}),
                 (202, {'accepted': 1.0})]
    try:
        with mock.patch.object(common, 'HOME', home):
            for status, answer in responses:
                seeker.reset()
                seeker.state['records_answer'] = lambda body, s=status, a=answer: (s, a)
                rejected = 0
                for _ in range(2):
                    try:
                        rows_claude.upload_file(cfg, path, time.time() + 5)
                    except common.SeekerError:
                        rejected += 1
                state = common.FileState(path, time.time() + 1)
                offset = json.loads(read(state.file)).get('offset') if os.path.exists(state.file) else 0
                check('upload requires HTTP 202 and an integer count for every row: ' + repr((status, answer)),
                      rejected == 2 and offset == 0 and len(seeker.calls) == 2)
            seeker.state.pop('records_answer')
            c = rows_claude.upload_file(cfg, path, time.time() + 5)
            check('full acceptance advances the upload checkpoint exactly once', c['rows'] == 1
                  and rows_claude.upload_file(cfg, path, time.time() + 5)['rows'] == 0)
    finally:
        seeker.state.pop('records_answer', None)


def test_upload_checkpoint_retry(seeker):
    import rows_codex
    cfg = dict(url=seeker.url, key=KEY, dataset=common.DEFAULT_DATASET, user='', run='', agent='')
    for harness in ('claude', 'codex'):
        home = os.path.join(WORK, 'home-checkpoint-' + harness)
        path = os.path.join(WORK, 'checkpoint-' + harness + '.jsonl')
        if harness == 'claude':
            filler = json.loads(line('user', 'filler', '2026-11-03T10:00:00Z'))
            usage = dict(type='assistant', message=dict(id='usage-at-boundary', role='assistant',
                         usage={'input_tokens': 73, 'output_tokens': 11}, content=[{'type': 'text', 'text': 'usage'}]))
            records = [filler] * 500 + [usage, dict(type='cost-state', totalCostUSD=0.75)]
            uploader = rows_claude.upload_file
        else:
            filler = dict(type='response_item', payload=dict(type='message', role='user',
                          content=[{'type': 'input_text', 'text': 'filler'}]))
            records = [dict(type='session_meta', payload={'id': 'thread-checkpoint'})] + [filler] * 500
            records += [dict(type='token_usage_record', payload=dict(response_id='usage-at-boundary',
                            usage={'input_tokens': 73, 'output_tokens': 11}))]
            uploader = rows_codex.upload_file
        with open(path, 'w') as fh:
            fh.writelines(json.dumps(r) + '\n' for r in records)
        requests = []

        def answer(body):
            requests.append(body)
            return (500, {}) if len(requests) == 2 else (202, {'accepted': len(body)})

        seeker.reset()
        seeker.state['records_answer'] = answer
        try:
            with mock.patch.object(common, 'HOME', home):
                try:
                    uploader(cfg, path, time.time() + 5)
                except common.SeekerError:
                    pass
                state = json.loads(read(common.FileState(path, time.time() + 1).file))
                check(harness + ' checkpoint excludes the unaccepted boundary usage state',
                      'usage-at-boundary' not in state['mids'] and state['cost'] == 0)
                uploader(cfg, path, time.time() + 5)
                accepted = [r for r in seeker.rows if r['input_tokens'] == 73]
                check(harness + ' retry retains the usage row and its full token count',
                      len(requests) == 3 and len(accepted) == 1 and accepted[0]['output_tokens'] == 11)
                if harness == 'claude':
                    check('retry retains cumulative cost accounting', sum(r['cost_usd'] for r in seeker.rows) == 0.75)
        finally:
            seeker.state.pop('records_answer', None)


def test_upload_large_line(seeker):
    cfg = dict(url=seeker.url, key=KEY, dataset=common.DEFAULT_DATASET, user='', run='', agent='')
    path = os.path.join(WORK, 'large-line.jsonl')
    blocks = [{'type': 'text', 'text': 'z' * 65_000} for _ in range(70)]
    with open(path, 'w') as fh:
        fh.write(json.dumps(dict(type='assistant', message={'role': 'assistant', 'content': blocks})) + '\n')
        fh.write(line('user', 'tail after large line', '2026-11-03T10:00:00Z'))
    for fail_second in (False, True):
        home = os.path.join(WORK, 'home-large-line-' + str(fail_second))
        calls = []

        def answer(body):
            calls.append(body)
            return (500, {}) if fail_second and len(calls) == 2 else (202, {'accepted': len(body)})

        seeker.reset()
        seeker.state['records_answer'] = answer
        try:
            with mock.patch.object(common, 'HOME', home):
                try:
                    # This checks batching and retry cursors, not parsing speed on a busy runner.
                    rows_claude.upload_file(cfg, path, time.time() + 60)
                except common.SeekerError:
                    state = json.loads(read(common.FileState(path, time.time() + 1).file))
                    check('partial large-line checkpoint keeps a row cursor and pre-line parser state',
                          state['offset'] == 0 and state['line'] == 0 and 0 < state['row'] < 70)
                    rows_claude.upload_file(cfg, path, time.time() + 60)
                check('large line fits byte limits and reaches the tail after retry=' + str(fail_second),
                      len(seeker.rows) == 71 and seeker.rows[-1]['body'] == 'tail after large line'
                      and all(c['size'] <= rows_claude.BATCH_BYTES for c in seeker.calls)
                      and len({(r['seq'], r['block_idx']) for r in seeker.rows}) == 71)
        finally:
            seeker.state.pop('records_answer', None)


def test_upload_individual_row(seeker):
    path = os.path.join(WORK, 'large-metadata.jsonl')
    with open(path, 'w') as fh:
        fh.write(line('user', 'large metadata', '2026-11-03T10:00:00Z'))
        fh.write(line('user', 'tail', '2026-11-03T10:00:00Z'))

    def rows(rec, seq, st, ids, cfg, hostname):
        result = rows_claude.rows_from(rec, seq, st, ids, cfg, hostname)
        if seq == 0:
            for key, value in result[0].items():
                if isinstance(value, str) and key != 'user_name':
                    result[0][key] = '\x01' * 100000
        return result

    seeker.reset()
    cfg = dict(url=seeker.url, key=KEY, dataset=common.DEFAULT_DATASET, user='', run='', agent='')
    with mock.patch.object(common, 'HOME', os.path.join(WORK, 'home-large-metadata')):
        c = rows_claude.upload_file(cfg, path, time.time() + 10, rows_fn=rows)
    check('an individually oversized row is truncated to fit and does not block the following row',
          c['rows'] == 2 and seeker.rows[0]['truncated'] and seeker.rows[-1]['body'] == 'tail'
          and all(call['size'] <= rows_claude.BATCH_BYTES for call in seeker.calls))


def test_session_end_shared_budget():
    import contextlib
    import io
    import runpy
    cfg = dict(off=False)
    for script, sid in ((HOOK, SID), (CODEX_HOOK, CSID)):
        main = runpy.run_path(script)['main']
        clock, budgets = [0.0], []

        def handoff(cfg, inp, deadline):
            clock[0] = 1.0

        def upload(*args):
            budgets.append(args[2])

        with mock.patch.dict(main.__globals__, session_end=handoff, upload=upload), \
                mock.patch.object(common, 'config', return_value=cfg), \
                mock.patch.object(time, 'monotonic', side_effect=lambda: clock[0]), \
                mock.patch.object(sys, 'argv', [script, 'session-end']), \
                mock.patch.object(sys, 'stdin', io.StringIO(json.dumps({'session_id': sid}))):
            main()
        limit = 0.4 if script == HOOK else 4.0
        check('session-end handoff and flush share one total budget: ' + os.path.relpath(script, ROOT),
              len(budgets) == 1 and abs(budgets[0] - limit) < 0.001)


def test_upload_deadline(seeker):
    path = os.path.join(WORK, 'deadline.jsonl')
    with open(path, 'w') as fh:
        fh.writelines(line('user', 'budget', '2026-11-03T10:00:00Z') for _ in range(2000))
    cfg = dict(url=seeker.url, key=KEY, dataset=common.DEFAULT_DATASET, user='', run='', agent='')
    with mock.patch.object(common, 'HOME', os.path.join(WORK, 'home-deadline')):
        clock, timeouts = [0.0], []

        def send(cfg, rows, timeout):
            timeouts.append(timeout)
            clock[0] += 1
            return {'accepted': len(rows)}

        with mock.patch.object(rows_claude.time, 'monotonic', side_effect=lambda: clock[0]), \
                mock.patch.object(common, 'ingest_records', side_effect=send):
            result = rows_claude.upload_file(cfg, path, time.time() + 0.25)
        state = json.loads(read(common.FileState(path, time.time() + 1).file))
        check('uploader stops sending after its deadline and preserves only accepted progress',
              len(timeouts) == 1 and 0 < timeouts[0] <= 0.25 and result['rows'] == 500 and state['line'] == 500)
        with mock.patch.object(common, 'ingest_records') as send_mock:
            rows_claude.upload_file(cfg, path, time.time() - 1)
            check('expired upload does not parse or send new rows', not send_mock.called)
    # Exercise the socket timeout as well as the deterministic scheduling check.
    seeker.reset()

    def slow(body):
        time.sleep(0.3)
        return 202, {'accepted': len(body)}

    seeker.state['records_answer'] = slow
    try:
        with mock.patch.object(common, 'HOME', os.path.join(WORK, 'home-deadline-socket')):
            start = time.monotonic()
            try:
                rows_claude.upload_file(cfg, path, time.time() + 0.1)
            except (TimeoutError, OSError):
                pass
            check('upload socket timeout uses the remaining budget with no two-second minimum',
                  len(seeker.calls) <= 1 and time.monotonic() - start < 1.5)
    finally:
        seeker.state.pop('records_answer', None)


def test_installer_validation_and_credentials():
    import contextlib
    import io
    import runpy
    installer = runpy.run_path(INSTALL)
    main, validate = installer['main'], installer['validate']
    home = os.path.join(WORK, 'installer-validation')
    os.makedirs(home, exist_ok=True)
    path = os.path.join(home, 'config.toml')
    cfg = dict(mcp_url='http://127.0.0.1:8095/mcp', key='')
    original = 'mcp_servers = { docs = { command = "docs-mcp" } }\n'
    with open(path, 'w') as fh:
        fh.write(original)
    with mock.patch.object(common, 'config', return_value=cfg), mock.patch.dict(sys.modules, {'tomllib': None}), \
            contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err:
        code = main(['--config', path])
    check('installer without tomllib refuses extending an inline table and leaves the file intact',
          code == 2 and read(path).decode() == original and 'nothing written' in err.getvalue()
          and not glob.glob(path + '.ctrlb-backup-*'))
    with mock.patch.dict(sys.modules, {'tomllib': None}), mock.patch.object(shutil, 'which', return_value=None):
        try:
            validate('model = "test"\n')
            refused = False
        except installer['InstallError'] as e:
            refused = 'Python 3.11+' in str(e) and 'nothing written' in str(e)
    check('installer refuses to guess when neither TOML parser is available', refused)
    diagnostic = ('invalid inline table mcp_servers\npassword=synthetic-validator-secret\n' + 'detail ' * 200).encode()
    with mock.patch.object(common, 'config', return_value=cfg), mock.patch.dict(sys.modules, {'tomllib': None}), \
            mock.patch.object(shutil, 'which', return_value='/fake/codex'), \
            mock.patch.object(subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, b'', diagnostic)), \
            contextlib.redirect_stderr(io.StringIO()) as err:
        code = main(['--config', path])
    check('Codex validation refusal includes bounded redacted stderr and leaves the config intact',
          code == 2 and 'invalid inline table mcp_servers' in err.getvalue()
          and 'synthetic-validator-secret' not in err.getvalue() and '[REDACTED]' in err.getvalue()
          and len(err.getvalue()) < 650 and read(path).decode() == original
          and not glob.glob(path + '.ctrlb-backup-*'))
    if not can_validate_toml():
        print('SKIP successful TOML installation checks (needs Python 3.11+ or codex); refusal checks ran')
        return
    odd = os.path.join(home, 'checkout-\U0001f680', 'hook.py')
    with mock.patch.dict(installer['block'].__globals__, HOOK=odd):
        text = installer['block'](cfg['mcp_url'])
    validate(text)
    check('TOML basic strings retain non-BMP Unicode scalar values', '\\ud83d' not in text and odd in text)
    marker = 'synthetic-' + 'otel-header-001'
    for quoted in ("'Basic " + marker + "'", '"Basic ' + marker + '"'):
        text = installer['block'](cfg['mcp_url'], 'http://127.0.0.1:1/logs', [('Authorization', 'Basic ' + marker)])
        text = text.replace('"Basic ' + marker + '"', quoted)
        with open(path, 'w') as fh:
            fh.write(text)
        with mock.patch.object(common, 'config', return_value=cfg), contextlib.redirect_stdout(io.StringIO()) as out:
            code = main(['--config', path, '--uninstall', '--dry-run'])
        check('dry-run hides literal and basic TOML header strings: ' + quoted[0],
              code == 0 and marker not in out.getvalue() and '<hidden>' in out.getvalue() and read(path).decode() == text)
    multiline = '[otel.exporter.otlp-http]\nheaders = { Authorization = """Basic\n' + marker + '\n""" }\n'
    check('dry-run sanitization also hides multiline headers', marker not in installer['hide_header_values'](multiline))
    multiline = multiline.replace(marker, '[looks_like_a_table]\n' + marker)
    check('table-like lines inside a multiline header cannot reset dry-run masking',
          marker not in installer['hide_header_values'](multiline))
    for key in ('headers.Authorization', 'otlp-http.headers.Authorization', '"headers" . "Authorization"'):
        text = installer['block'](cfg['mcp_url'])
        text = text.replace(installer['END'], '[otel.exporter]\n' + key + " = 'Basic " + marker + "'\n" + installer['END'])
        with open(path, 'w') as fh:
            fh.write(text)
        with mock.patch.object(common, 'config', return_value=cfg), contextlib.redirect_stdout(io.StringIO()) as out:
            code = main(['--config', path, '--uninstall', '--dry-run'])
        check('uninstall dry-run masks dotted header values: ' + key,
              code == 0 and marker not in out.getvalue() and '<hidden>' in out.getvalue()
              and installer['has_credentials'](text) and read(path).decode() == text)
    for key in ('headers', 'http_headers', 'bearer_token'):
        check('credential detection matches complete names: ' + key,
              installer['has_credentials'](key + ' = "synthetic"')
              and not installer['has_credentials']('env_' + key + ' = "VARIABLE"'))
    with open(path, 'w') as fh:
        fh.write('model = "test"\n')
    os.chmod(path, 0o644)
    for repeat in (False, True):
        with mock.patch.object(common, 'config', return_value=cfg), contextlib.redirect_stdout(io.StringIO()) as out:
            code = main(['--config', path])
        check('an install without header credentials keeps mode 0644 and reports no credentials: repeat=' + str(repeat),
              code == 0 and stat.S_IMODE(os.stat(path).st_mode) == 0o644
              and 'credentials' not in out.getvalue())
    with open(path, 'w') as fh:
        fh.write('model = "test"\n')
    os.chmod(path, 0o644)
    args = ['--config', path, '--otel', '--otel-endpoint', 'http://127.0.0.1:1/logs', '--otel-header', 'Authorization=Basic ' + marker]
    with mock.patch.object(common, 'config', return_value=cfg), contextlib.redirect_stdout(io.StringIO()) as out:
        code = main(args)
    check('installer restricts newly added credentials to mode 0600 and reports the change',
          code == 0 and stat.S_IMODE(os.stat(path).st_mode) == 0o600 and '0600' in out.getvalue())
    os.chmod(path, 0o644)
    with mock.patch.object(common, 'config', return_value=cfg), contextlib.redirect_stdout(io.StringIO()):
        main(['--config', path, '--uninstall'])
    backups = glob.glob(path + '.ctrlb-backup-*')
    secret_backups = [b for b in backups if marker in read(b).decode()]
    check('credential-bearing installer backups are private', bool(secret_backups)
          and all(stat.S_IMODE(os.stat(b).st_mode) == 0o600 for b in secret_backups))


def test_witness_invocations():
    import hooks_shared as shared
    tests = ('echo pytest', "printf '%s\\n' 'pytest tests/missing.py'", '# pytest tests/missing.py',
             'false && pytest tests/missing.py', 'pytest tests/missing.py || true', 'pytest tests/missing.py; true')
    for command in tests:
        rec = {'bash': [{'command': command, 'exit_code': 0}]}
        out = shared.witness(rec, {'evidence': {'command': 'pytest tests/missing.py', 'exit_code': 0}})['hookSpecificOutput']
        check('witness rejects printed, skipped or masked test substrings: ' + command,
              out.get('permissionDecision') == 'deny' and not shared.is_test_command(command))
    rec = {'bash': [{'command': 'pytest -k "two  spaces"', 'exit_code': 0}]}
    out = shared.witness(rec, {'evidence': {'command': 'pytest -k "two spaces"', 'exit_code': 0}})['hookSpecificOutput']
    check('witness preserves whitespace within quoted arguments', out['permissionDecision'] == 'deny')
    rec = {'bash': [{'command': 'pytest -k "$TEST"', 'exit_code': 0}]}
    out = shared.witness(rec, {'evidence': {'command': "pytest -k '$TEST'", 'exit_code': 0}})['hookSpecificOutput']
    check('witness preserves shell quoting and expansion semantics', out['permissionDecision'] == 'deny')
    rec = {'bash': [{'command': 'pytest', 'exit_code': 0, 'truncated': True}]}
    out = shared.witness(rec, {'evidence': {'command': 'pytest', 'exit_code': 0}})['hookSpecificOutput']
    check('witness refuses a record whose command was truncated', out['permissionDecision'] == 'deny')
    rec = {'bash': [{'command': 'python3 -I tests/run_tests.py', 'exit_code': 0}]}
    out = shared.witness(rec, {'evidence': {'command': 'python3  -I tests/run_tests.py', 'exit_code': 0}})['hookSpecificOutput']
    check('witness accepts a complete direct test invocation with ordinary whitespace differences',
          out['permissionDecision'] == 'allow' and out['updatedInput']['witnessed'] is True)
    command = "printf '%s\\n' 'pytest tests/missing.py'"
    p = subprocess.run(['/bin/sh', '-c', command], capture_output=True, text=True, timeout=5)
    rec = {'bash': [{'command': command, 'exit_code': p.returncode, 'summary': p.stdout}]}
    out = shared.witness(rec, {'evidence': {'command': 'pytest tests/missing.py', 'exit_code': 0}})['hookSpecificOutput']
    check('an actual shell invocation that only prints a test is never its witness',
          p.returncode == 0 and 'pytest' in p.stdout and out['permissionDecision'] == 'deny')
    for command, allowed in (('pytest |& tail -1', False), ('pytest &> log', True), ('pytest >& log', True),
                             ('pytest > out.txt', True), ('pytest >> out.txt', True), ('pytest >| out.txt', True),
                             ('pytest 2>&1', True), ('pytest > out.txt 2>&1', True), ('pytest &>> log', True),
                             ('pytest > out.txt | tail -1', False), ('pytest 2>&1 | tail -1', False),
                             ('pytest &> log; true', False), ('pytest >& log && true', False),
                             ('pytest >', False), ('pytest &> | tail', False), ('pytest &', False)):
        rec = {'bash': [{'command': command, 'exit_code': 0}]}
        out = shared.witness(rec, {'evidence': {'command': command, 'exit_code': 0}})['hookSpecificOutput']
        check('witness distinguishes output redirection from masked exit status: ' + command,
              bool(shared.is_test_command(command)) == allowed
              and (out.get('updatedInput', {}).get('witnessed') is True) == allowed
              and out['permissionDecision'] == ('allow' if allowed else 'deny'))
    root = os.path.join(WORK, 'redirected-test')
    os.makedirs(root, exist_ok=True)
    script = os.path.join(root, 'test_status.py')
    with open(script, 'w') as fh:
        fh.write('import sys\nprint("test result")\nsys.exit(7)\n')
    command = sys.executable + ' ' + script + ' > ' + os.path.join(root, 'out.txt')
    result = subprocess.run(['/bin/sh', '-c', command], capture_output=True, timeout=5)
    out = shared.witness({'bash': [{'command': command, 'exit_code': result.returncode}]},
                         {'evidence': {'command': command, 'exit_code': 7}})['hookSpecificOutput']
    check('a real redirected test preserves its failing exit status in the witness',
          result.returncode == 7 and out['updatedInput']['witnessed'] is True)


def test_restore_concurrency():
    import relay_hooks
    root = os.path.join(WORK, 'restore-concurrency')
    scratch, outside = os.path.join(root, 'scratch'), os.path.join(root, 'outside')
    os.makedirs(scratch, exist_ok=True)
    os.makedirs(outside, exist_ok=True)
    parts = {'artifacts': {'artifacts': [{'path': 'plan.txt', 'agent': 'agent', 'text': 'old server copy'}]}}
    real_open = os.open
    real_link = os.link
    raced = [False]

    def create_first(source, path, **kwargs):
        if path == 'plan.txt' and not raced[0]:
            raced[0] = True
            with open(os.path.join(scratch, 'plan.txt'), 'w') as fh:
                fh.write('new user work')
        return real_link(source, path, **kwargs)

    with mock.patch.object(os, 'link', side_effect=create_first):
        relay_hooks.restore_artifacts(parts, 'run', 'agent', scratch)
    check('restoring an artifact cannot replace work created immediately before exclusive publication',
          raced[0] and read(os.path.join(scratch, 'plan.txt')) == b'new user work')
    os.mkdir(os.path.join(scratch, 'notes'))
    parts['artifacts']['artifacts'][0]['path'] = 'notes/other.txt'
    raced[0] = False

    def swap_parent(path, flags, mode=0o777, **kwargs):
        if path == 'notes' and not raced[0]:
            raced[0] = True
            os.rmdir(os.path.join(scratch, 'notes'))
            os.symlink(outside, os.path.join(scratch, 'notes'))
        return real_open(path, flags, mode, **kwargs)

    with mock.patch.object(os, 'open', side_effect=swap_parent):
        have = relay_hooks.restore_artifacts(parts, 'run', 'agent', scratch)
    check('restoring an artifact refuses a parent swapped to a symlink during directory traversal',
          raced[0] and not have and not os.listdir(outside))
    parts['artifacts']['artifacts'][0]['path'] = 'link.txt'
    os.symlink(os.path.join(outside, 'victim'), os.path.join(scratch, 'link.txt'))
    have = relay_hooks.restore_artifacts(parts, 'run', 'agent', scratch)
    check('artifact restoration never follows an existing destination symlink', not have and not os.listdir(outside))
    linked = os.path.join(root, 'linked')
    os.symlink(outside, linked)
    linked_scratch = os.path.join(linked, 'run', 'scratch')
    parts['artifacts']['artifacts'][0]['path'] = 'notes/plan.txt'
    have = relay_hooks.restore_artifacts(parts, 'run-linked', 'agent', linked_scratch)
    check('artifact restoration follows symlinked ancestors of the scratch root',
          have == ['notes/plan.txt'] and read(os.path.join(linked_scratch, 'notes', 'plan.txt')) == b'old server copy')
    parent = os.path.join(root, 'traversable')
    os.makedirs(os.path.join(parent, 'scratch'))
    os.chmod(parent, 0o111)
    try:
        have = relay_hooks.restore_artifacts(parts, 'run-traversable', 'agent', os.path.join(parent, 'scratch'))
        check('artifact restoration needs only traversal access above the scratch root', have == ['notes/plan.txt'])
    finally:
        os.chmod(parent, 0o700)


def test_relay_off_and_next(seeker):
    import hooks_shared as shared
    import runpy
    claude_hook = runpy.run_path(HOOK)
    home = os.path.join(WORK, 'home-off-next')
    cfg = dict(url=seeker.url, key=KEY, dataset=common.DEFAULT_DATASET, run='run-off-next', agent='agent',
               enforce='off', auto_slots=['REGISTRY.md'], mode='hosted', off=False)
    inp = dict(session_id=SID, cwd=WORK, tool_name='Edit', tool_input={'file_path': os.path.join(WORK, 'REGISTRY.md')})
    seeker.reset()
    with mock.patch.object(common, 'HOME', home):
        out = claude_hook['pre_tool'](cfg, inp)
        check('enforcement off bypasses automatic slot claims and all edit denial paths', out is None and not seeker.calls)
        inp.update(tool_name='mcp__ctrlb__done', tool_input={'resource': 'old', 'evidence': {'command': 'pytest'}})
        off = claude_hook['pre_tool'](cfg, inp)['hookSpecificOutput']
        check('enforcement off still refuses done evidence that was never witnessed', off['permissionDecision'] == 'deny')
        shared.record_bash(cfg['run'], cfg['agent'], SID, 'pytest', 0, 'passed')
        off = claude_hook['pre_tool'](cfg, inp)['hookSpecificOutput']
        check('enforcement off still certifies matching done evidence for witness-required workspaces',
              off['updatedInput']['witnessed'] is True and off['updatedInput']['evidence']['exit_code'] == 0)
        bare = dict(inp, tool_input={'resource': 'old'})
        check('enforcement off adds no automatic allow decision to done without evidence',
              claude_hook['pre_tool'](cfg, bare) is None)
        nonce = 'synthetic-' + 'next-hold-nonce'
        state = common.TokenState(cfg['run'], cfg['agent'])
        state.put(['name:old', 'path:old.py'], 'synthetic-old-hold')
        answer = dict(done=True, key='name:old', paths=['path:old.py'],
                      next=dict(held=True, key='name:new', paths=['path:new.py'], token=nonce))
        claude_hook['post_tool'](cfg, dict(inp, tool_response=answer))
        check('done with next saves the next task and file nonces after dropping the completed task',
              state.get(['name:old', 'path:old.py', 'name:new', 'path:new.py']) == {'name:new': nonce, 'path:new.py': nonce})
        shared.record_tokens(cfg['run'], cfg['agent'], 'done', {'done': True, 'next': {'held': False}})
        check('done with no free next task does not erase unrelated held tokens',
              state.get(['name:new', 'path:new.py']) == {'name:new': nonce, 'path:new.py': nonce})


def test_nested_codex_run():
    import rows_codex
    import runpy
    root = os.path.join(WORK, 'nested-codex')
    os.makedirs(root, exist_ok=True)
    paths = []
    for sid, parent in (('root-thread', ''), ('child-thread', 'root-thread'), ('grandchild-thread', 'child-thread')):
        path = os.path.join(root, sid + '.jsonl')
        with open(path, 'w') as fh:
            fh.write(json.dumps(dict(type='session_meta', payload=dict(id=sid, parent_thread_id=parent))) + '\n')
        paths.append(path)
    cfg = dict(run='', agent='')
    hook_run = runpy.run_path(CODEX_HOOK)['session_run']
    ids = [rows_codex.ids_for_file(cfg, p) for p in paths]
    check('nested Codex uploads share the root run while preserving immediate parent identity',
          [i['run_id'] for i in ids] == ['root-thread'] * 3 and ids[-1]['parent'] == 'child-thread'
          and ids[-1]['agent'] == 'grandchild-thread')
    check('nested Codex edit hooks resolve the same root run as uploads',
          hook_run(cfg, 'grandchild-thread', paths[-1]) == ('root-thread', 'child-thread'))
    check('explicit configured run overrides nested Codex ancestry',
          rows_codex.ids_for_file(dict(cfg, run='explicit-run'), paths[-1])['run_id'] == 'explicit-run')
    with open(paths[0], 'w') as fh:
        fh.write(json.dumps(dict(type='session_meta', payload=dict(id='root-thread', parent_thread_id='grandchild-thread'))) + '\n')
    check('cyclic Codex ancestry terminates', bool(rows_codex.ids_for_file(cfg, paths[-1])['run_id']))
    home = os.path.join(root, 'codex-home')
    sessions = os.path.join(home, 'sessions')
    ancestor = os.path.join(sessions, '2026', '01', '01')
    child = os.path.join(sessions, '2026', '02', '02')
    os.makedirs(ancestor)
    os.makedirs(child)
    ids = ['0190f0a0-1111-7000-8000-%012d' % i for i in range(4)]
    files = []
    for i in range(3):
        file = os.path.join(ancestor if i < 2 else child, 'rollout-2026-01-01T00-00-00-' + ids[i] + '.jsonl')
        with open(file, 'w') as fh:
            fh.write(json.dumps(dict(type='session_meta', payload=dict(id=ids[i], parent_thread_id=ids[i - 1] if i else '',
                                                                      base_instructions={'text': 'x' * 30000}))) + '\n')
        files.append(file)
    for i in range(300):
        with open(os.path.join(ancestor, 'rollout-unrelated-%08d.jsonl' % i), 'w') as fh:
            fh.write('unrelated rollout must not be read\n')
    with mock.patch.dict(os.environ, CODEX_HOME=home), mock.patch.object(rows_codex, 'read_meta', wraps=rows_codex.read_meta) as meta, \
            mock.patch.object(rows_codex, 'rollouts', wraps=rows_codex.rollouts) as rollouts:
        runs = [rows_codex.ids_for_file(cfg, files[-1])['run_id'] for _ in range(5)]
        check('nested Codex lookup reads only the current rollout and its ancestors, indexing filenames once',
              runs == [ids[0]] * 5 and meta.call_count == 15 and rollouts.call_count == 1
              and all(call.args[0] in files for call in meta.call_args_list))


def test_backfill_failure_status(seeker):
    path = os.path.join(WORK, 'backfill-failure-projects', 'example')
    os.makedirs(path, exist_ok=True)
    for name in ('one', 'two'):
        with open(os.path.join(path, name + '.jsonl'), 'w') as fh:
            fh.write(line('user', 'backfill', '2026-11-03T10:00:00Z'))
    for partial in (False, True):
        seeker.reset()
        requests = []

        def answer(body):
            requests.append(body)
            return (202, {'accepted': len(body)}) if partial and len(requests) == 1 else (500, {})

        seeker.state['records_answer'] = answer
        try:
            env = base_env(seeker.url, os.path.join(WORK, 'home-backfill-failed-' + str(partial)),
                           CLAUDE_PROJECTS_DIR=os.path.dirname(path))
            p = subprocess.run([sys.executable, '-I', os.path.join(ROOT, 'bin', 'backfill.py'), '--yes'],
                               env=env, capture_output=True, text=True, timeout=10)
            check('backfill reports a failing exit status and count after trying every file: partial=' + str(partial),
                  p.returncode != 0 and len(requests) == 2 and ('1/2' if partial else '2/2') + ' files failed' in p.stdout)
        finally:
            seeker.state.pop('records_answer', None)


def test_redaction_linear_budget():
    # Bound wall time in a subprocess, and measure matching CPU time independently of runner contention.
    source = '''import json, sys, time
sys.path.insert(0, sys.argv[1])
import redact
inputs = ['a-' * 100000 + 'z', 'x-' * 100000 + 'token=1', 'A_SECRET_' * 22000 + 'Z',
          'token = tokens[i]; ' * 12000, '--' + 'x-' * 100000 + 'token=abcd',
          '-----BEGIN PRIVATE KEY-----\\n' * 7500, 'cookie:' * 29000,
          'a:=' * 66000, 'x=>' * 66000, 'password:=$' * 18000,
          'DB_PASSWORD=>[REDACTED]' * 9000]
results = []
for value in inputs:
    start = time.process_time()
    clean, count = redact.redact(value)
    results.append([time.process_time() - start, count])
print(json.dumps(results))
'''
    try:
        p = subprocess.run([sys.executable, '-I', '-c', source, os.path.join(ROOT, 'lib')],
                           capture_output=True, text=True, timeout=60)
        measurements = json.loads(p.stdout) if p.returncode == 0 else []
    except subprocess.TimeoutExpired:
        measurements = []
    check('redaction handles 200 KB adversarial names and assignments within two CPU seconds each',
          len(measurements) == 11 and all(t < 2.0 for t, n in measurements)
          and measurements[1][1] > 0, str(measurements))
    for text, marker in [('password => "Xy7!kq"', 'Xy7!kq'), ('TOKEN := X', 'X')]:
        clean, n = redact.redact(text)
        check('assignment operators redact the complete value: ' + text, n > 0 and marker not in clean)
    for name in ('paſsword', 'apİ_key', 'apı_key', 'api_Key', 'ä-password', 'ä-SECRET_KEY'):
        clean, n = redact.redact(name + '=syntheticvalue')
        check('linear name scanning retains Unicode boundary and case coverage: ' + name,
              n > 0 and 'syntheticvalue' not in clean)
    for first in ('Cookie: ', 'Set-Cookie: ', 'Cookie:', 'Set-Cookie:'):
        for nested in ('Cookie: sid=abc', 'cookie:cookie: sid=abcdef', 'Cookie:Cookie:sid=abcdef'):
            clean, n = redact.redact(first + nested)
            check('nested cookie headers cannot be skipped inside a candidate key: ' + first + nested,
                  n > 0 and 'abc' not in clean)
    for name in ('password', 'passwd', 'token', 'secret', 'api-key', 'apikey', 'access-key', 'auth-token', 'access-token',
                 'service-password', 'service-token', 'service-api-key'):
        for value in ('abc,def', 'abc}def', 'abc;def', 'abc=def'):
            text = '--' + name + '=' + value
            clean, n = redact.redact(text)
            check('equals-form credential flags mask the complete shell value: ' + text,
                  n > 0 and clean == '--' + name + '=[REDACTED]')
    for text, secret in (('--password=abc,--token synthetic-value', 'synthetic-value'),
                         ('--password=}X-Api-Key: synthetic-value', 'synthetic-value'),
                         ('_password=Cookie: sid=synthetic-value', 'synthetic-value'),
                         ("'_TOKEN = X-Api-Key: synthetic-value", 'synthetic-value'),
                         ('SECRET_KEY" = Set-Cookie: sid=synthetic-value', 'synthetic-value'),
                         ('TOKEN:=self.TOKEN = synthetic-value', 'synthetic-value'),
                         ('api_key:= Set-Cookie: sid=synthetic-value', 'synthetic-value'),
                         ('X-Api-Key: password\n:=synthetic-value', 'synthetic-value'),
                         ('X-Auth-Token: abc}--token synthetic-value', 'synthetic-value'),
                         ('X-Auth-Token: abc}X-Api-Key: synthetic-value', 'synthetic-value'),
                         (r'--password=\"\"abc--password=synthetic-value', 'synthetic-value')):
        clean, n = redact.redact(text)
        check('expanded redaction preserves overlapping credential coverage: ' + text,
              n > 0 and secret not in clean)


def test_witness_docs():
    readme = read(os.path.join(ROOT, 'README.md')).decode()
    limits = read(os.path.join(ROOT, 'docs', 'limits.md')).decode()
    relay = read(os.path.join(ROOT, 'docs', 'relay-tools.md')).decode()
    check('enforcement documentation separates edit checks from done witnessing',
          all('automatic slot claims' in text and 'Done evidence witnessing still runs' in text for text in (readme, limits))
          and 'done evidence witnessing still runs |' in readme)
    check('witness documentation requires a complete command and describes quoting and redirections',
          'complete command of this session must match' in relay and 'quoted whitespace must match' in relay
          and 'Output redirection preserves the witness; a pipeline does not.' in relay
          and "contains the evidence's command" not in relay)


def main():
    seeker = Seeker()
    run_test(test_runner_failure_reporting)
    run_test(test_layout)
    run_test(test_no_secrets_in_repo)
    run_test(test_credential_redaction, seeker)
    run_test(test_redaction_literals)
    run_test(test_redaction_assignment_safety)
    run_test(test_codex_tool_input_preservation)
    run_test(test_hooks, seeker)
    run_test(test_batches, seeker)
    run_test(test_upload_acceptance, seeker)
    run_test(test_upload_checkpoint_retry, seeker)
    run_test(test_upload_large_line, seeker)
    run_test(test_upload_individual_row, seeker)
    run_test(test_session_end_shared_budget)
    run_test(test_upload_deadline, seeker)
    run_test(test_installer_validation_and_credentials)
    run_test(test_witness_invocations)
    run_test(test_restore_concurrency)
    run_test(test_relay_off_and_next, seeker)
    run_test(test_nested_codex_run)
    run_test(test_backfill_failure_status, seeker)
    run_test(test_redaction_linear_budget)
    run_test(test_witness_docs)
    run_test(test_pre_tool, seeker)
    run_test(test_model_call_guard, seeker)
    run_test(test_spawned_agents, seeker)
    run_test(test_keys)
    run_test(test_edit_keys_across_harnesses, seeker)
    run_test(test_docs)
    run_test(test_release_wording)
    run_test(test_briefs_wording)
    run_test(test_wait_wording)
    run_test(test_takeover_wording)
    run_test(test_done_evidence, seeker)
    run_test(test_round6_hooks, seeker)
    run_test(test_handoff_redaction, seeker)
    run_test(test_round8_hooks, seeker)
    run_test(test_failure_safety, seeker)
    run_test(test_config_and_status, seeker)
    run_test(test_codex_rows, seeker)
    run_test(test_codex_hooks, seeker)
    run_test(test_local_mode, seeker)
    run_test(test_local_session_context, seeker)
    run_test(test_local_relay, seeker)
    run_test(test_local_health_retry)
    run_test(test_fixture_bind_without_dns)
    run_test(test_local_stub_slow_dns)
    run_test(test_local_stub_idle_connections)
    run_test(test_local_startup, seeker)
    run_test(test_local_startup_delayed)
    run_test(test_local_startup_concurrent)
    run_test(test_local_request_proxy)
    run_test(test_request_transport)
    run_test(test_mcp_url_transport)
    run_test(test_local_health_answers, seeker)
    run_test(test_local_binary_path)
    run_test(test_local_configure_status, seeker)
    run_test(test_codex_install)
    run_test(test_root_install)
    run_test(test_local_install)
    run_test(test_local_install_streams)
    run_test(test_local_install_download_errors)
    run_test(test_local_documentation)
    run_test(test_recall_search_scope)
    run_test(test_project_backup_exclusive)
    run_test(test_project_list_fences)
    run_test(test_init)
    run_test(test_codex_backfill, seeker)
    run_test(test_validate)
    run_test(test_codex_install_hardening)
    run_test(test_hardening, seeker)
    seeker.httpd.shutdown()
    seeker.httpd.server_close()
    fails = [r for r in results if not r[1]]
    print(f'\n{len(results) - len(fails)}/{len(results)} passed (CtrlB: synthetic fixtures in tests/fixtures/seeker)')
    return 1 if fails else 0


if __name__ == '__main__':
    sys.exit(main())
