"""Tests for ctrlb-agent. Run: python3 -I tests/run_tests.py

Seeker is a local fake that answers with the synthetic responses in tests/fixtures/seeker/ (relay-plan section 4).
Nothing is sent anywhere else, Claude is never called, and no real config is read: every CTRLB_* variable from the
calling environment is dropped and CTRLB_HOME points into tests/work/."""
import base64, glob, hashlib, http.server, json, os, re, shutil, socket, stat, subprocess, sys, tempfile, threading, time
sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FIX = os.path.join(HERE, 'fixtures')
WORK = os.path.join(HERE, 'work')
HOOK = os.path.join(ROOT, 'plugin', 'hooks', 'hook.py')
CODEX_HOOK = os.path.join(ROOT, 'codex', 'hook.py')
INSTALL = os.path.join(ROOT, 'codex', 'install.py')
SID = '5f0c7a52-0000-4000-8000-000000000001'
CSID = '01a07695-3fee-7191-bd75-a4df7b3baa7e'        # the thread id of tests/fixtures/codex-rollout.jsonl
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
# the fake Seeker's keys and the user each one belongs to; a row naming any other user_name is refused with 403
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


def load(name):
    with open(os.path.join(FIX, name)) as fh:
        return json.load(fh)


# ---------------------------------------------------------------- fake Seeker

class Seeker:
    """Answers the routes ctrlb-agent calls from tests/fixtures/seeker/. state['check'] picks the check answer;
    'slow' sleeps 5 s before answering."""

    def __init__(self):
        self.records, self.check_fx, self.runs_fx = load('seeker/records.json'), load('seeker/check.json'), load('seeker/runs.json')
        self.calls, self.rows, self.refused, self.state = [], [], [], dict(check='allow', handoff='ok')
        # Round 8 routes: each answers 'ok', or 'slow' (sleeps SLOW_S, longer than any hook budget), 'error' (500),
        # 'missing' (404, a Seeker without the route); claims also 'held' (another agent holds it).
        self.state.update(briefs='ok', claims='ok', release='ok', posts='ok', tasks='ok', contracts='ok', peers='ok',
                          artifacts='ok')
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _answer(self, status, body):
                raw = json.dumps(body).encode()
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
                m = re.match(r'^/ai/sessions/([A-Za-z0-9_]+)/records$', self.path)
                if m and method == 'POST':
                    lim = outer.records['limits']
                    if not isinstance(body, list) or len(body) > lim['max_rows'] or len(raw) > lim['max_bytes']:
                        return self._answer(**outer.records['responses']['too_many_rows'])
                    if any(isinstance(r, dict) and r.get('user_name') not in (None, '', owner) for r in body):
                        outer.refused.append(body)
                        return self._answer(**outer.records['responses']['other_user'])
                    outer.rows.extend(body)
                    return self._answer(202, dict(outer.records['responses']['accepted']['body'], accepted=len(body)))
                m = re.match(r'^/ai/relay/([A-Za-z0-9_]+)/runs/([^/]+)/check$', self.path)
                if m and method == 'POST':
                    s = outer.state['check']
                    if s == 'slow':
                        time.sleep(5)
                        s = 'allow'
                    ans = outer.check_fx['responses'][s]
                    if outer.state.get('echo') and ans['body'].get('conflicts'):
                        # the hooks protocol matches conflicts by path: answer for the paths asked about
                        c0 = ans['body']['conflicts'][0]
                        ans = dict(ans, body=dict(ans['body'], conflicts=[dict(c0, path=x, key='path:' + x) for x in body.get('paths') or []]))
                    return self._answer(**ans)
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

        self.httpd = http.server.ThreadingHTTPServer(('127.0.0.1', 0), H)
        self.httpd.daemon_threads = True
        self.url = f'http://127.0.0.1:{self.httpd.server_address[1]}'
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def reset(self):
        self.calls.clear()
        self.rows.clear()
        self.refused.clear()


SLOW_S = 5
R8_ARTIFACT = '# plan\nstep 1: parse\n'


def r8_answer(route, method, body, query, state):
    """(status, body) of the fake round 8 routes."""
    body = body if isinstance(body, dict) else {}
    if route == 'briefs' and method == 'POST':
        res = body.get('resource') or ''
        return 201, {'ok': True, 'key': res if res.startswith('contract:') else 'name:' + ' '.join(res.lower().split()),
                     'resource': res, 'paths': ['path:' + p for p in body.get('paths') or []]}
    if route == 'claims':
        res = body.get('resource') or ''
        if body.get('next') and not res:      # claim(next=true) of the hooks protocol: no task is free here
            return 200, {'held': False, 'free_tasks': [], 'message': 'There is no free task in this run'}
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
    env = {k: v for k, v in os.environ.items() if not k.startswith('CTRLB_')}
    env.update(CTRLB_HOME=home, CTRLB_URL=url, CTRLB_KEY=KEY)
    env.update(extra)
    return env


def hook(mode, inp, env, timeout=20, script=HOOK):
    t = time.time()
    p = subprocess.run([sys.executable, '-I', script, mode], input=inp if isinstance(inp, str) else json.dumps(inp),
                       capture_output=True, text=True, env=env, timeout=timeout)
    return p, time.time() - t


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


def v10_rule(run='<run>', agent='<agent>'):
    """The round 8 rule text (v10), kept in docs/relay-tools.md as the reference for a harness without hooks; the
    plugin injects the hooks text (hooks_shared.RELAY) instead."""
    doc = open(os.path.join(ROOT, 'docs', 'relay-tools.md')).read()
    sec = doc.split('## Rule text for a harness without hooks', 1)[1]
    text = sec.split('```text\n', 1)[1].split('\n```', 1)[0]
    return text.replace('<run>', run).replace('<agent>', agent).replace('<scratch directory>', '{scratch}')


def hooks_rule(text, run, agent):
    """The hooks protocol's injected text: ids, the hooks hold files and tasks (never by hand), a blocked edit names
    the holder, the peers line with the ids, and nothing about claim, release, renew or done."""
    return (f'your run id is {run} and your agent id is {agent}' in text
            and 'Relay holds files and tasks for you through the plugin\'s hooks, so never claim or release anything by hand' in text
            and 'If an edit is blocked, the message names the agent that holds the file' in text
            and f'pass run_id="{run}" and agent="{agent}" to the ctrlb tools peers' in text
            and 'post (a note to all), ask (a question to one agent) and answer' in text
            and not any(w in text for w in ('claim(', 'release(', 'done(', 'renew', 'finished=true', 'next=true', 'takeover')))


def registered_rule(text, md=False):
    """Rule 3 after Seeker decision 18: claim the registered task, its files come with it, one claim held, release it
    finished; a file of no task is claimed by its path; no separate claim of the task's file."""
    f = '`finished=true`' if md else 'finished=true'
    return (REGISTERED in text and 'Its files come with it' in text and 'Work only while you hold it.' in text
            and f'release it with {f}' in text and 'A file that belongs to no task is claimed by its path' in text
            and 'Then claim the file path the task names' not in text and 'Work only while you hold both' not in text)


def test_layout():
    pj = load('../../plugin/.claude-plugin/plugin.json')
    check('plugin.json: name ctrlb-agent, version 0.2.0', pj.get('name') == 'ctrlb-agent' and pj.get('version') == '0.2.0')
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
    check('hooks.json: PostToolUse matcher is the ctrlb claim, handoff and release tools',
          hk['PostToolUse'][0].get('matcher') == 'mcp__ctrlb__claim|mcp__ctrlb__handoff|mcp__ctrlb__release')
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
          'its files are handed back with it' in relay and 'Finish tasks, just release files' in relay
          and 'A file is never finished' in relay and '`reopen=true`' in relay and 'call `release` with `finished=true`' not in relay
          and '`reason: unknown_task`' in relay and '`suggestion`' in relay and '`paths`' in relay)
    check('no stdio recall server shipped', not glob.glob(os.path.join(ROOT, '**', 'recall_mcp.py'), recursive=True))
    check('plugin/lib and plugin/bin point at the shared lib and bin',
          os.path.realpath(os.path.join(ROOT, 'plugin', 'lib')) == os.path.join(ROOT, 'lib')
          and os.path.realpath(os.path.join(ROOT, 'plugin', 'bin')) == os.path.join(ROOT, 'bin'))


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
    # research/ is the research code under its own names (research/relay/README.md); it is scanned for secrets and URLs above
    named = sorted({os.path.relpath(f, ROOT) for f in files + [os.path.abspath(__file__)]
                    if not f.endswith('relay-plan.md') and not os.path.relpath(f, ROOT).startswith('research' + os.sep)
                    and banned.search(open(f, errors='replace').read())})
    check('product names only: none of the retired research names outside the plan copy', not named, ', '.join(named))


def test_hooks(seeker):
    main, sub = setup_transcripts()
    I = inputs(main, sub)
    home = os.path.join(WORK, 'home')
    env = base_env(seeker.url, home)

    # session-start: the rule names the session, run and agent ids
    p, dt = hook('session-start', I['session_start'], env)
    o = out_of(p)
    ctx = o.get('additionalContext', '')
    check('session-start: rule names session id, run id and agent id', o.get('hookEventName') == 'SessionStart'
          and f'session_id {SID}' in ctx and f'run id is {SID}' in ctx and f'agent id is {SID}' in ctx and p.returncode == 0)
    check('session-start without a run id: ids and the peers line only, no hooks line (the hooks never claim)',
          f'pass run_id="{SID}" and agent="{SID}" to the ctrlb tools peers' in ctx and "through the plugin's hooks" not in ctx
          and 'claim' not in ctx.split('CtrlB Relay:', 1)[1])
    rule = v10_rule()
    check('v10 rule (docs, harness without hooks): claims the registered task, its files come with it, holds one claim',
          registered_rule(rule))
    check('v10 rule: refusals, unknown_task, reopen a finished task',
          'held is false with finished_by, the task is done' in rule and 'held is false with held_by, another agent has it' in rule
          and 'If reason is unknown_task, claim again with the suggestion' in rule and 'claim its name with reopen=true' in rule)
    check('v10 rule: slots for things that are not files', 'is claimed as slot:<name> and released when you are done with it' in rule
          and rule.index('slot:<name>') < rule.index('reopen=true'))
    check('relay skill and tools: slots, two-minute lease, never finished',
          'claimed as `slot:<name>` and released when you are done with it' in open(os.path.join(ROOT, 'plugin', 'skills', 'relay', 'SKILL.md')).read()
          and all(n in open(os.path.join(ROOT, 'docs', 'relay-tools.md')).read() for n in
                  ('## Slots', '`slot:devserver`', '`slot:port-3000`', '`slot:db-migrate`', '`slot:screenshots`', '`slot:commit`', 'lasts two minutes')))
    check('v10 rule: names the relay tools, contract and contracts among them',
          'relay tools (peers, post, claim, release, handoff, ask, answer, brief, briefs, tasks, contract, contracts)' in rule)
    ctx_session = ctx
    ctx = rule
    check('v10 rule: the coordination tools before a prose note, one sentence each',
          'Use the coordination tools before a prose note: handoff when you must leave a task you hold, so it moves to the agent '
          'you name in one step; ask when you are blocked on something only another agent or the orchestrator can decide; '
          "answer, with the question's id, when you can settle an open question; contract when you finish an interface others "
          'will build on (a type, a route, a config shape), with its name, the shape in a few lines and the files that define '
          "it. Before you build on a contract, read it with `contracts`. If it is stale, read the file it names and check your "
          "assumptions against it. Post a note only for what none of them covers." in ctx)
    ctx = ctx_session
    check('session-start under 1 s', dt < 1.0, f'{dt:.2f}s')
    p, _ = hook('session-start', I['session_start_compact'], dict(env, CTRLB_RUN='relay-run-7', CTRLB_AGENT='builder-1'))
    ctx = out_of(p).get('additionalContext', '')
    check('session-start: CTRLB_RUN and CTRLB_AGENT win', 'run id is relay-run-7' in ctx and 'agent id is builder-1' in ctx
          and 'run_id="relay-run-7"' in ctx)
    check('session-start with a run id: the hooks text (ids, hooks hold files and tasks, blocked edit names the holder, '
          'peers), nothing about claim, release, renew or done', hooks_rule(ctx, 'relay-run-7', 'builder-1'), ctx[-500:])
    check('session-start after a compaction sends nothing to Seeker (the same session goes on)', not seeker.calls)

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
    check('user_name left to Seeker when the key carries no user', all('user_name' not in r for r in rows))
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
    check('no planted secret reaches Seeker (body and command)', not leaked and len(seeker.rows) == 3,
          f'leaked {leaked}' if leaked else f"{sent.count('[REDACTED]')} redactions")
    check('non-secret token counts kept', '\\"input_tokens\\": 1234' in sent)
    import redact as _redact
    pre = [_redact.redact(t)[1] for t in ('aws_secret_access_key = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"',
                                          'db_password: hunter2hunter', 'password=$PW', 'api_key=<your key>',
                                          'the access key rotation doc')]
    check('prefixed lower-case secret assignments are redacted (round 8: aws_secret_access_key = "...")', pre == [1, 1, 0, 0, 0], str(pre))

    # user_name comes only from the key's own user (its email claim); CTRLB_USER is the agent label
    seeker.reset()
    hook('upload', I['stop'], dict(env, CTRLB_HOME=os.path.join(WORK, 'home-user'), CTRLB_KEY=JWT_KEY))
    check("a key from the environment with an email claim: rows carry that user as user_name",
          seeker.rows and all(r.get('user_name') == KEY_USER for r in seeker.rows) and not seeker.refused,
          f'{len(seeker.rows)} rows, {len(seeker.refused)} refused')
    seeker.reset()
    hook('upload', I['stop'], dict(env, CTRLB_HOME=os.path.join(WORK, 'home-user-noclaim'), CTRLB_KEY=JWT_NOCLAIM))
    check('a key with no email claim: rows carry no user_name, Seeker fills it',
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
    check('pre-tool off (Seeker mode): exit 0, no output', p.returncode == 0 and not p.stdout.strip())
    p, _ = run('enforce', e=dict(env, CTRLB_ENFORCE='off'))
    check('pre-tool CTRLB_ENFORCE=off: no call, no output', not seeker.calls and not p.stdout.strip())
    p, _ = run('self')
    check('pre-tool: a hold of the caller itself is not a conflict', not p.stdout.strip())
    p, dt = run('slow')
    check('pre-tool timeout: fails open within two seconds', p.returncode == 0 and not p.stdout.strip() and dt < 3.0, f'{dt:.2f}s')
    with open(os.path.join(home, 'hooks.log')) as fh:
        check('pre-tool timeout is logged to hooks.log', 'pre-tool fail open' in fh.read())
    p, dt = hook('pre-tool', I['pre_tool_edit'], dict(env, CTRLB_URL=f'http://127.0.0.1:{closed_port()}'))
    check('pre-tool Seeker down: fails open', p.returncode == 0 and not p.stdout.strip() and dt < 3.0, f'{dt:.2f}s')
    p, _ = run('error')
    check('pre-tool Seeker 500: fails open', p.returncode == 0 and not p.stdout.strip())
    p, _ = hook('pre-tool', I['pre_tool_edit'], dict(env, CTRLB_KEY='wrong'))
    check('pre-tool refused key (401): fails open', p.returncode == 0 and not p.stdout.strip())
    p, _ = run('enforce', 'pre_tool_read')
    check('pre-tool on a non-edit tool: no call', not seeker.calls and not p.stdout.strip())
    for name, want in (('pre_tool_write', 'src/panels/index.ts'), ('pre_tool_multiedit', 'src/panels/Latency.test.tsx'),
                       ('pre_tool_notebook', 'notebooks/latency.ipynb')):
        p, _ = run('enforce', name)
        check(f'pre-tool {name[9:]}: path checked and denied', seeker.calls and seeker.calls[0]['body']['paths'] == [want]
              and out_of(p).get('permissionDecision') == 'deny')
    # Fencing (decision 21): lapsed, unheld and stale_round name the reason and the claim call.
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
    check('post-tool claim: no output, no Seeker call', p.returncode == 0 and not p.stdout.strip() and not seeker.calls)
    run('allow')
    body = seeker.calls[0]['body'] if seeker.calls else {}
    check('pre-tool sends the token of the held task for its file', body.get('tokens') == {'src/panels/Latency.tsx': 'tok-round-1'},
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
    """The four connect pages (Task 9): front matter for the docs site, the two placeholders, no other placeholder in
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
    """Seeker never finishes a file path (relay-plan decision 15): no agent-facing text may tell agents to release
    with finished=true as the rule for every claim, and the tool docs carry reopen."""
    old = re.compile(r'(call|then) `?release`? with `?finished=true`?', re.I)
    # research/: the harness keeps the rules its earlier arms were measured with, word for word
    files = [f for f in glob.glob(os.path.join(ROOT, '**', '*'), recursive=True)
             if os.path.isfile(f) and f.endswith(('.md', '.py', '.json')) and '/tests/' not in f and '/.git/' not in f
             and not f.endswith('relay-plan.md') and not os.path.relpath(f, ROOT).startswith('research' + os.sep)]
    bad = [os.path.relpath(f, ROOT) for f in files if old.search(open(f, errors='replace').read())]
    check('no agent-facing text says to release with finished=true for every claim', not bad, ', '.join(bad))
    tools = open(os.path.join(ROOT, 'docs', 'relay-tools.md')).read()
    check('relay-tools: claim documents reopen and release says files are always handed back',
          '| `reopen` |' in tools and '"reopened": true' in tools and 'A file path is always handed back' in tools)
    lim = open(os.path.join(ROOT, 'docs', 'limits.md')).read()
    check('limits: a file path is never done; reopen starts a new round; no "use a new run id"',
          'A file path is never done' in lim and 'reopen=true' in lim and 'use a new run id' not in lim)


def test_briefs_wording():
    """Briefs (Seeker relay-plan decision 16): the skill and the tool docs tell the agent to read the brief of a
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
    check('relay-tools: fourteen relay tools, handoff, ask and answer after release, then brief, briefs, task, tasks, history, contract, contracts',
          relay == ['peers', 'post', 'claim', 'release', 'handoff', 'ask', 'answer', 'brief', 'briefs', 'task', 'tasks', 'history', 'contract', 'contracts'],
          ','.join(relay))
    check('relay-tools: contract and contracts documented, instead of a CONTRACT note',
          '{contracts: [{name, key, agent, ts, age_s, text, paths, changed_since, text_since, text_changed_at?, changed_since_read?}], last_read?, read_at?}' in tools
          and 'instead of a `CONTRACT:` note' in tools
          and 'changed_since?}]}' in tools)
    check('relay-tools: peers kind and open, ask and answer documented',
          '| `open` |' in tools and 'peers(kind="question", open=true)' in tools and 'answered: {agent, ts, text}' in tools
          and 'Answer: `{id, run_id, agent}`' in tools and 'Do not post a QUESTION note' in tools)
    check('relay-tools: hold required, its four reasons and the claim token',
          '## Edits need a claim (hold required)' in tools and all(f'| `{r}` |' in tools for r in ('held', 'lapsed', 'unheld', 'stale_round'))
          and 'A held claim answers `token`' in tools)
    check('relay-tools: handoff documents to, handed and held_by',
          '| `to` |' in tools and '{handed: true, resource, key, to, lease_until, token, paths?}' in tools
          and '{handed: false, resource, key, to, held_by?, message}' in tools)
    check('relay-tools: eighteen tools, the writes listed, briefs answers the newest brief per key',
          'gives an agent eighteen tools' in tools and '`post`, `claim`, `release`, `handoff`, `ask`, `answer`, `brief`, `task` and `contract` write' in tools
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
    """lib/paths.py against Seeker's shared table (tests/fixtures/keys.json, Task 2), and the edit paths the hooks and
    parsers send against the same keys."""
    import paths
    cases = load('keys.json')['cases']
    bad = [c for c in cases if paths.key(c['resource'], c['cwd'])[0] != c['key']]
    check(f'paths.key passes Seeker keys.json ({len(cases)} cases)', len(cases) >= 40 and not bad,
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
    check('edit_path: Seeker keys what the package sends as the key of the path itself',
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
    cwd = '/work/engine'
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
    check('Seeker down: flush exits 0 within its limit', p.returncode == 0 and dt < 7, f'{dt:.2f}s')
    with open(os.path.join(WORK, 'home-down', 'hooks.log')) as fh:
        check('Seeker down: error logged to hooks.log', 'upload' in fh.read())
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
    check('configure.py: Seeker check fails with the fixture key, and says so', 'Seeker check failed: HTTP 401' in p.stdout)
    p = subprocess.run([sys.executable, '-I', os.path.join(ROOT, 'bin', 'status.py')], capture_output=True, text=True,
                       env=dict(env, CTRLB_KEY=KEY), timeout=30)
    check('status.py: Seeker answers with the latest runs; key masked', 'Seeker answered: OK' in p.stdout and 'relay-demo-1' in p.stdout
          and KEY not in p.stdout and 'CTRLB_MCP_URL in the environment: NO' in p.stdout, p.stdout[-300:])
    check("status.py: says the key carries no user when it has no email claim", "key's user (written as user_name): (none in the key" in p.stdout)

    # the key's user: the email claim of the JWT payload, decoded without a signature check
    check('key_user: a fixture JWT with the email claim decodes to the user', common.key_user(JWT_KEY) == KEY_USER)
    check('key_user: no email claim, a non-JWT key, a broken payload or no key give no user',
          common.key_user(JWT_NOCLAIM) == '' and common.key_user(KEY) == '' and common.key_user('aaa.!!!.ccc') == ''
          and common.key_user('a.' + base64.urlsafe_b64encode(b'[1]').decode() + '.c') == '' and common.key_user('') == ''
          and common.key_user(fake_jwt(dict(email=7))) == '')
    check("key_user reads the claim Seeker reads (email)", common.USER_CLAIM == 'email')
    home2 = os.path.join(WORK, 'home-cfg-jwt')
    env2 = dict(env, CTRLB_HOME=home2)
    p = subprocess.run([sys.executable, '-I', os.path.join(ROOT, 'bin', 'configure.py'), '--url', seeker.url, '--dataset',
                        'agent_sessions', '--user', 'some-label', '--run', '', '--key-stdin'], input=JWT_KEY + '\n',
                       capture_output=True, text=True, env=env2, timeout=30)
    with open(os.path.join(home2, 'config.json')) as fh:
        c = json.load(fh)
    check("configure.py stores the key's user as user, and ignores --user", p.returncode == 0 and c.get('user') == KEY_USER
          and '--user is ignored' in p.stdout, p.stdout[-300:] + p.stderr[-200:])
    check('configure.py with a JWT key: Seeker answers, and the key is never printed', 'Seeker answered: OK' in p.stdout
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
        fh.write(crec('session_meta', dict(id=CSUB, session_id=CSUB, cwd='/work/engine', parent_thread_id=CSID, cli_version='0.166.0',
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
          all(r['cwd'] == '/work/engine' and r['git_branch'] == 'new-rti-design' for r in rows))
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
          and t6['tool_use_id'] == 'call_q3vwniym1SlF48fk0rdhN7C7' and t6['command'].split('\n')[:3] == [
              'pwd', "rg --files -g AGENTS.md -g '!indexing/**' -g '!.git/**' -g '!target/**' -g '!vendor/**' .",
              'git status --short --branch'], repr(t6['command'][:120]))
    check('codex exec output: tool_result with the tool name', by[(8, 0)]['block_type'] == 'tool_result' and by[(8, 0)]['tool_name'] == 'exec'
          and by[(8, 0)]['tool_use_id'] == 'call_q3vwniym1SlF48fk0rdhN7C7' and by[(8, 0)]['is_error'] is False)
    check('codex apply_patch inside exec: paths from the *** Add File line', by[(13, 0)]['paths'] == '["/tmp/engine-rti-pr-body.md"]',
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
        fh.write(citem(dict(type='reasoning', summary=[dict(type='summary_text', text='**Checking the histogram buckets**')],
                            content=None, encrypted_content='x'), '2026-09-06T12:05:00.000Z'))
        fh.write(citem(dict(type='function_call', name='shell', call_id='call_f1',
                            arguments=json.dumps({'command': ['bash', '-lc', 'cargo test -p engine'], 'workdir': '/work/engine'})),
                       '2026-09-06T12:05:01.000Z'))
        fh.write(citem(dict(type='function_call_output', call_id='call_f1',
                            output=json.dumps({'output': 'test failed', 'metadata': {'exit_code': 101}})), '2026-09-06T12:05:09.000Z'))
        fh.write(citem(dict(type='custom_tool_call', name='apply_patch', call_id='call_f2', status='completed', input=patch),
                       '2026-09-06T12:05:10.000Z'))
        fh.write(citem(dict(type='custom_tool_call_output', call_id='call_f2', output='Script failed\nboom'), '2026-09-06T12:05:11.000Z'))
        fh.write(crec('token_usage_record', dict(response_id='resp_02751da85fb3796d016a9d56ab184487d08429826177051e8e',
                                                  usage=dict(input_tokens=1, output_tokens=1)), '2026-09-06T12:05:12.000Z'))
        fh.write(crec('compacted', dict(message='Summary: PR 235 drafted; three review comments posted.'), '2026-09-06T12:05:13.000Z'))
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
          and 'histogram buckets' in nb.get(20, {}).get('body', ''))
    check('codex shell function_call: command from argv, error from exit code', nb.get(21, {}).get('command') == 'cargo test -p engine'
          and nb.get(22, {}).get('is_error') is True and nb.get(22, {}).get('tool_name') == 'shell')
    check('codex apply_patch tool: Update, Move to and Delete paths in order', nb.get(23, {}).get('paths') ==
          '["src/a.rs", "src/b.rs", "src/c.rs", "src/d.rs"]' and nb.get(23, {}).get('tool_name') == 'apply_patch',
          nb.get(23, {}).get('paths'))
    check('codex failed exec script: is_error', nb.get(24, {}).get('is_error') is True)
    check('codex compacted message: is_summary text row', nb.get(26, {}).get('is_summary') is True and nb[26]['block_type'] == 'text')
    sent = json.dumps(new)
    leaked = [x for x in s if x in sent]
    check('codex: no planted secret reaches Seeker (body and command)', not leaked and nb.get(28, {}).get('command', '').count('[REDACTED]') == 1,
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

    p, dt = chook('session-start', I['session_start'], env)
    o = out_of(p)
    ctx = o.get('additionalContext', '')
    check('codex session-start: rule names session id, run id and agent id', o.get('hookEventName') == 'SessionStart'
          and f'session_id {CSID}' in ctx and f'run id is {CSID}' in ctx and f'agent id is {CSID}' in ctx and p.returncode == 0, f'{dt:.2f}s')
    check('codex session-start without a run id: ids and the peers line, no hooks line',
          f'pass run_id="{CSID}" and agent="{CSID}" to the ctrlb tools peers' in ctx and "through the plugin's hooks" not in ctx)
    p, _ = chook('session-start', I['session_start'], dict(env, CTRLB_RUN='relay-run-8', CTRLB_AGENT='builder-1'))
    ctx = out_of(p).get('additionalContext', '')
    check('codex session-start: CTRLB_RUN and CTRLB_AGENT win', 'run id is relay-run-8' in ctx and 'agent id is builder-1' in ctx)
    check('codex session-start with a run id: the hooks text, nothing about claim, release, renew or done',
          hooks_rule(ctx, 'relay-run-8', 'builder-1'), ctx[-400:])
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

    def run(state, name='pre_tool_patch', e=None):
        seeker.reset()
        seeker.state['check'] = state
        return chook('pre-tool', I[name], e or env)

    p, dt = run('allow')
    c = seeker.calls[0] if seeker.calls else {}
    check('codex pre-tool allow: exit 0, no output', p.returncode == 0 and not p.stdout.strip(), f'{dt:.2f}s')
    check('codex pre-tool posts the patch paths, cwd and agent to /ai/relay/{dataset}/runs/{run}/check',
          c.get('path') == f'/ai/relay/agent_sessions/runs/{CSID}/check'
          and c.get('body') == {'agent': CSID, 'paths': ['src/service/rti_ab.rs'], 'cwd': '/work/engine'}, json.dumps(c.get('body')))
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
    check('codex pre-tool timeout: fails open within two seconds', p.returncode == 0 and not p.stdout.strip() and dt < 3.0, f'{dt:.2f}s')
    with open(os.path.join(home, 'hooks.log')) as fh:
        check('codex pre-tool timeout is logged to hooks.log', 'codex pre-tool fail open' in fh.read())
    p, dt = chook('pre-tool', I['pre_tool_patch'], dict(env, CTRLB_URL=f'http://127.0.0.1:{closed_port()}'))
    check('codex pre-tool Seeker down: fails open', p.returncode == 0 and not p.stdout.strip() and dt < 3.0, f'{dt:.2f}s')
    p, _ = run('error')
    check('codex pre-tool Seeker 500: fails open', p.returncode == 0 and not p.stdout.strip())
    p, _ = run('enforce', e=dict(env, CTRLB_ENFORCE='off'))
    check('codex pre-tool CTRLB_ENFORCE=off: no call, no output', not seeker.calls and not p.stdout.strip())
    p, _ = run('enforce', 'pre_tool_patch_many')
    check('codex pre-tool: every path of a patch is checked, in order', seeker.calls and seeker.calls[0]['body']['paths'] ==
          ['docs/rti.md', 'src/old_name.rs', 'src/new_name.rs', 'src/unused.rs'] and out_of(p).get('permissionDecision') == 'deny',
          json.dumps(seeker.calls[0]['body'] if seeker.calls else {}))
    p, _ = run('enforce', 'pre_tool_bash_patch')
    check('codex pre-tool: a patch piped to apply_patch from a shell command is checked',
          seeker.calls and seeker.calls[0]['body']['paths'] == ['src/service/rti_ab.rs'] and out_of(p).get('permissionDecision') == 'deny')
    p, _ = run('enforce', 'pre_tool_bash')
    check('codex pre-tool: a shell command without a patch makes no call', not seeker.calls and not p.stdout.strip())
    seeker.state['echo'] = True
    p, _ = run('enforce', 'pre_tool_subagent_patch', dict(env, CTRLB_RUN='relay-run-8'))
    seeker.state['echo'] = False
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


def test_root_install():
    """python3 -I install.py --codex (the command the connect page shows) is codex/install.py with the same options."""
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


def test_codex_install():
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
    p = subprocess.run([sys.executable, '-I', os.path.join(ROOT, 'bin', 'backfill.py'), '--codex', '--project', 'engine', '--yes'],
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
    """Round 3 (Seeker relay-plan decisions 25, 27, 28, 29), as round 6 (decisions 35 and 37) left it: claim no longer
    waits (wait_s is gone from claim; a refused task claim lists free_tasks), so the session rule, the relay skill and
    the tool docs never offer claim(wait_s), timed_out or a place in line; peers keeps wait_s. They keep what
    belongs_to_task means, ask_about, and contracts (now: read it before building on it, and check a stale one)."""
    sys.path.insert(0, os.path.join(ROOT, 'lib'))
    import hooks_shared as H
    rule = v10_rule('{run}', '{agent}')   # the v10 text, now in docs/relay-tools.md (a harness without hooks)
    check('relay rule v9: no wait_s, no waiting in line on a claim', 'wait_s' not in rule and 'wait in line' not in rule.replace('Never wait in line', ''))
    check('relay rule: belongs_to_task means claim the task it names', 'If reason is belongs_to_task' in rule and 'claim that task (task in the answer)' in rule)
    check('relay rule v9: read a contract before building on it; a stale one means read its file',
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
    """Round 5 (Seeker decisions 33 and 34): the v8 rule text and the relay skill tell the agent to take over a stalled
    task (or a file held by an agent that stopped) with takeover=true instead of waiting in line; the tool docs give
    takeover, not_stalled, other_user, taken_over_from and stall; a held refusal of an edit carries the takeover hint;
    a held takeover's token is kept like a claim's."""
    sys.path.insert(0, os.path.join(ROOT, 'lib'))
    import hooks_shared as H
    rule = v10_rule('{run}', '{agent}')   # the v10 text, now in docs/relay-tools.md (a harness without hooks)
    v8 = ("If tasks shows a task you need as stalled, claim it with takeover=true, read its evidence and notes, and "
          "continue it. Never wait in line on a stalled task. If a file you need is held by an agent that has stopped, "
          "the hold shows as stalled; take it over the same way.")
    check('relay rule v8: take over a stalled task or file, never wait in line on it', v8 in rule)
    check('relay rule v8: the claim(wait_s=30) sentence is gone, every other sentence kept',
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
    """Seeker decisions 30 to 32: the v7 rule text tells the agent to run the task's test command and give it, its
    exit code and its last output line as the done evidence (the check it ran when the task has none); the
    PostToolUse hook remembers the agent's last test-looking Bash command, its exit code and last output line, and when
    done (or a finished release) is refused for evidence it quotes that command, without calling Seeker or filling the
    evidence in, and prefers the task's test command when the refusal quotes it; a done drops the agent's tokens
    like a release."""
    sys.path.insert(0, os.path.join(ROOT, 'lib'))
    import hooks_shared as H
    rule = v10_rule('{run}', '{agent}')   # the v10 text, now in docs/relay-tools.md (a harness without hooks)
    v7 = ("Run the task's test command (tasks and claim show it as test); when it passes, call done on the task name "
          "with that command, its exit code and its last output line as the evidence. When the task has no test "
          "command, give the check you ran. A task is finished only when the record shows what was checked.")
    check('relay rule v7: run the task\'s test command and give it as the done evidence; the check you ran when there is none',
          v7 in rule)
    check('relay rule v7: the v6 sentence is replaced', "call done on the task name with the command you ran and its result" not in rule)
    check('relay rule v7: every earlier sentence kept (v8 drops the wait_s one)', registered_rule(rule)
          and 'If reason is belongs_to_task' in rule and "Before you build on a contract, read it with `contracts`. If it is stale, read the file it names" in rule and 'If the claim answers ask_about' in rule
          and rule.index('release it with finished=true') < rule.index(v7))
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
    check('post-tool Bash test: no output, no Seeker call', p.returncode == 0 and not p.stdout.strip() and not seeker.calls, p.stdout + p.stderr)
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
    check('post-tool done refused: Seeker is not called and nothing is filled in for the agent', not seeker.calls)
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
    # Seeker decision 32: the refusal quotes the task's test command. It wins over a last test command that is not it.
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
    hook('post-tool', dict(base, tool_name='Bash', tool_use_id='b3', tool_input={'command': 'cd /work/dash && ' + task_test + ' 2>&1'},
                           tool_response={'stdout': '...\n2 passed in 0.01s\n', 'stderr': ''}), fresh)
    p, _ = hook('post-tool', dict(done_in, tool_response=[{'type': 'text', 'text': json.dumps(quoted)}]), fresh)
    ctx = out_of(p).get('additionalContext', '')
    check('post-tool done refused: a last test command that runs the task\'s test is quoted with its result',
          f'`cd /work/dash && {task_test} 2>&1` (exit code 0), the task\'s test' in ctx and '"summary": "2 passed in 0.01s"' in ctx, ctx[:400])
    check('names_test: white space collapsed, a cd before and a flag after still match',
          H.names_test('cd x &&  python3 -m pytest   tests/test_lat.py -q', task_test) and not H.names_test('python3 -I -c "x"', task_test)
          and not H.names_test('pytest', ''))
    alone = ['python3 -m pytest tests/test_lat.py', '  python3 -m  pytest tests/test_lat.py ', 'cd x && python3 -m pytest tests/test_lat.py',
             'cd x; python3 -m pytest tests/test_lat.py 2>&1', 'python3 -m pytest tests/test_lat.py 2>&1']
    not_alone = ['python3 -m pytest tests/test_lat.py 2>&1 | tail -8', 'python3 -m pytest tests/test_lat.py -q', 'echo; python3 -m pytest tests/test_lat.py',
                 'python3 -m pytest tests/test_lat.py; echo ok', 'python3 -m pytest tests/test_lat.py || true', 'cd x && cd y && python3 -m pytest tests/test_lat.py',
                 'cd $(pwd) && python3 -m pytest tests/test_lat.py', 'cd `pwd` && python3 -m pytest tests/test_lat.py', '']
    check('test_alone: the test, a cd before and 2>&1 after only (Seeker\'s IsTestAlone)',
          all(H.test_alone(c, task_test) for c in alone) and not any(H.test_alone(c, task_test) for c in not_alone)
          and not H.test_alone('pytest', ''), [c for c in alone if not H.test_alone(c, task_test)] + [c for c in not_alone if H.test_alone(c, task_test)])
    o = H.witness({'bash': [{'command': task_test + ' 2>&1 | tail -3', 'exit_code': 0, 'summary': 'FAILED (errors=3)'}]},
                  {'resource': 'task 07 latency', 'evidence': {'command': task_test, 'exit_code': 0}})
    reason = (o or {}).get('hookSpecificOutput', {}).get('permissionDecisionReason', '')
    check('witness: the evidence command ran only inside a pipe: denied, saying its exit code is not the test\'s',
          (o or {}).get('hookSpecificOutput', {}).get('permissionDecision') == 'deny' and 'only inside' in reason and 'Run it alone' in reason, reason[:300])
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
    """Round 6 (Seeker decisions 36 and 38). The session-end hook hands off every hold of the agent in one call
    (handoff all=true, no receiver) with a text from the session's own record (edited files, last test and exit code,
    last note), then flushes; Seeker down or slow means nothing is written and the hook ends inside the SessionEnd
    budget. Every Bash command is recorded with its exit code (PostToolUse and PostToolUseFailure). PreToolUse on done
    witnesses the evidence: a matching command and exit code of this session adds witnessed=true (and a missing exit
    code), a mismatch is denied naming what the hook saw, no evidence command gives no output, and a witnessed the
    agent set itself is dropped."""
    sys.path.insert(0, os.path.join(ROOT, 'lib'))
    import hooks_shared as H
    rule = v10_rule('{run}', '{agent}')   # the v10 text, now in docs/relay-tools.md (a harness without hooks)
    check('relay rule v9: redirect to a free task; an empty list means note and stop',
          'If a claim is refused and the answer lists free tasks, claim one of them next. If the list is empty, post a note '
          'saying you have no task and stop.' in rule and 'wait_s' not in rule)
    # Rule v10 (round 8) drops the v9 REGISTRY.md sentence: the hooks claim autoSlots files (test_round8_hooks).
    check('relay rule v10: the v9 REGISTRY.md slot sentence is gone (the hooks handle it)',
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
    check('post-tool-failure Bash: no output, exits 0, no Seeker call', p.returncode == 0 and not p.stdout.strip() and not seeker.calls)
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
    check('session-end, Seeker slow: gives up inside the 1.5 s SessionEnd budget, exits 0 quietly',
          p.returncode == 0 and not p.stdout.strip() and not p.stderr.strip() and dt < 1.5, f'{dt:.2f}s')
    down = base_env(f'http://127.0.0.1:{closed_port()}', home, CTRLB_RUN=run, CTRLB_AGENT='agent-1')
    p, dt = hook('session-end', dict(base, hook_event_name='SessionEnd', reason='other'), down)
    check('session-end, Seeker down: nothing written, exits 0 quietly, fast', p.returncode == 0 and not p.stdout.strip()
          and not p.stderr.strip() and dt < 1.5, f'{dt:.2f}s')
    seeker.reset()
    plain = base_env(seeker.url, os.path.join(WORK, 'home-r6-plain'))
    hook('session-end', dict(base, hook_event_name='SessionEnd', reason='other'), plain)
    check('session-end outside a relay run (no CTRLB_RUN, no relay tool called): no handoff call',
          not [c for c in seeker.calls if c['path'].endswith('/handoffs')])
    fresh = dict(rec, bash=[], edited=[], note=None)
    check('handoff text with nothing recorded', H.handoff_text(fresh) == 'Session ended. Edited: nothing. Last test: none run.')

    # The witness.
    pre = dict(base, hook_event_name='PreToolUse', tool_name='mcp__ctrlb__done')
    hook('post-tool', dict(post, tool_name='Bash', tool_input={'command': 'cd /work/dash && python3 -m pytest tests/test_lat.py 2>&1'},
                           tool_response={'stdout': '..\n2 passed in 0.01s\n', 'stderr': ''}), env)
    ev_in = {'resource': 'task 07 latency', 'run_id': run, 'agent': 'agent-1',
             'evidence': {'command': 'python3 -m pytest tests/test_lat.py', 'exit_code': 0, 'summary': '2 passed in 0.01s'}}
    p, _ = hook('pre-tool', dict(pre, tool_input=ev_in), env)
    o = out_of(p)
    check('witness: a command of this session containing the evidence, same exit code: allow with witnessed=true, the rest kept',
          o.get('permissionDecision') == 'allow' and o.get('updatedInput', {}).get('witnessed') is True
          and o['updatedInput'].get('resource') == 'task 07 latency' and o['updatedInput']['evidence']['summary'] == '2 passed in 0.01s', json.dumps(o)[:300])
    no_code = dict(ev_in, evidence={'command': 'python3 -m pytest tests/test_lat.py'})
    o = out_of(hook('pre-tool', dict(pre, tool_input=no_code), env)[0])
    check('witness: no exit code given: filled from the record (the newest run, exit 0)',
          o.get('permissionDecision') == 'allow' and o['updatedInput']['evidence'].get('exit_code') == 0 and o['updatedInput']['witnessed'] is True,
          json.dumps(o)[:300])
    lie = dict(ev_in, witnessed=True, evidence={'command': 'python3 -m pytest tests/test_other.py', 'exit_code': 0})
    o = out_of(hook('pre-tool', dict(pre, tool_input=lie), env)[0])
    reason = o.get('permissionDecisionReason', '')
    check('witness: a command this session never ran is denied, naming the last test it did run, even with witnessed set',
          o.get('permissionDecision') == 'deny' and 'never ran it' in reason
          and '`cd /work/dash && python3 -m pytest tests/test_lat.py 2>&1` (exit code 0)' in reason and 'updatedInput' not in o, reason)
    wrong = dict(ev_in, evidence={'command': 'python3 -m pytest tests/test_lat.py', 'exit_code': 3})
    hook('post-tool-failure', dict(base, hook_event_name='PostToolUseFailure', tool_name='Bash',
                                   tool_input={'command': 'python3 -m pytest tests/test_lat.py'}, error='Exit code 1\n1 failed'), env)
    o = out_of(hook('pre-tool', dict(pre, tool_input=wrong), env)[0])
    reason = o.get('permissionDecisionReason', '')
    check('witness: the command ran but with another exit code: denied, naming the exit code it saw',
          o.get('permissionDecision') == 'deny' and 'with exit code 3' in reason and 'this session ran it with exit code 1' in reason, reason)
    p, _ = hook('pre-tool', dict(pre, tool_input={'resource': 'task 07 latency', 'run_id': run}), env)
    check('witness: a done with no evidence command: no output (Seeker answers evidence_required)', p.returncode == 0 and not p.stdout.strip())
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
          len(hk['PreToolUse']) == 3 and hk['PreToolUse'][1]['matcher'] == 'mcp__ctrlb__done|mcp__plugin_ctrlb-agent_ctrlb__done'
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


def _wait_for(pred, timeout=6.0):
    t = time.time()
    while time.time() - t < timeout:
        if pred():
            return True
        time.sleep(0.05)
    return pred()


def _log(home):
    try:
        with open(os.path.join(home, 'hooks.log')) as fh:
            return fh.read()
    except OSError:
        return ''


def test_round8_hooks(seeker):
    """Round 8 (Seeker decisions 43 to 47, docs/relay-hooks.md). A: PreToolUse on the Agent tool registers a task named
    from the description, with the prompt as its text and the workspace paths it names, and posts an interface block
    as a contract; it prints nothing and returns inside its budget. B: PostToolUse on Write/Edit under the scratchpad
    or the scratch directory posts a small text file as an artifact note (detached), once per content; binaries, big
    files and secrets are skipped and logged. C: SessionStart in a relay run adds the record block and writes the
    artefacts back. D: the scratch directory. E: autoSlots claim before the edit, deny when held, release after."""
    sys.path.insert(0, os.path.join(ROOT, 'lib'))
    import hooks_shared as H
    import relay_hooks as RH
    rule = v10_rule('{run}', '{agent}')   # the v10 text, now in docs/relay-tools.md (a harness without hooks)
    check('relay rule v10: claim(next=true) first, done with next=true, peers only to wait',
          'Start with `claim(next=true)`. When your test passes, call `done` with the evidence and `next=true`. Call `peers` '
          'only when you are waiting for an answer or a handoff.' in rule)
    check('relay rule v10: helper scripts and binaries go to the named scratch directory, not /tmp',
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
    check(f'pre-agent budget: Seeker slower ({SLOW_S}s) than the {RH.REGISTER_S}s registration: returns inside the 5 s hook timeout, '
          'no output, the spawn proceeds', p.returncode == 0 and not p.stdout.strip() and dt < RH.REGISTER_S + 1.0, f'{dt:.2f}s')
    down = base_env(f'http://127.0.0.1:{closed_port()}', home, CTRLB_RUN=run, CTRLB_AGENT='agent-1')
    p, dt = hook('pre-agent', agent_in, down)
    check('pre-agent, Seeker down: no output, fast', p.returncode == 0 and not p.stdout.strip() and dt < 2, f'{dt:.2f}s')
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

    # C under the hooks protocol: no record block; this agent's artefacts are written back and one line says where.
    gets = sorted(c['path'].split('?')[0].rsplit('/', 1)[-1] for c in seeker.calls if c['method'] == 'GET')
    check('session-start (relay run): reads only this agent\'s artifacts (no tasks, contracts or peers), and claims the next task',
          gets == ['artifacts'] and any('agent=agent-1' in c['path'] for c in seeker.calls if '/artifacts' in c['path'])
          and any(c['path'].endswith('/claims') and (c['body'] or {}).get('next') is True for c in seeker.calls), ','.join(gets))
    check('session-start: no record block (open tasks, contracts, handoffs, notes) is injected',
          'CtrlB Relay record of run' not in ctx and 'Open tasks:' not in ctx and 'Contracts:' not in ctx, ctx[-300:])
    restored = os.path.join(default, 'plan.md')
    check('session-start: artefacts written back to scratchDir (a path escaping it is not), and one line says where',
          os.path.isfile(restored) and open(restored).read() == R8_ARTIFACT and not os.path.exists(os.path.join(home, 'relay', run, 'escape.md'))
          and f'Your artefacts from the previous session are in {default}.' in ctx, ctx[-300:])
    seeker.reset()
    for r in ('tasks', 'contracts', 'peers', 'artifacts'):
        seeker.state[r] = 'slow'
    p, dt = hook('session-start', dict(base, hook_event_name='SessionStart', source='resume'), env)
    for r in ('tasks', 'contracts', 'peers', 'artifacts'):
        seeker.state[r] = 'ok'
    ctx = out_of(p).get('additionalContext', '')
    check(f'session-start budget: Seeker slower ({SLOW_S}s) than the {RH.RECORD_S}s artefacts read: returns inside it with the rule',
          p.returncode == 0 and dt < RH.RECORD_S + 1.0 and 'your run id is run-r8' in ctx, f'{dt:.2f}s')
    fresh = base_env(f'http://127.0.0.1:{closed_port()}', os.path.join(WORK, 'home-r8-fresh'), CTRLB_RUN=run, CTRLB_AGENT='agent-1')
    p, dt = hook('session-start', dict(base, hook_event_name='SessionStart', source='startup'), fresh)
    ctx = out_of(p).get('additionalContext', '')
    check('session-start, Seeker down and no cache: the rule alone, fast', 'your run id is run-r8' in ctx and 'CtrlB Relay record' not in ctx
          and dt < 2, f'{dt:.2f}s')
    seeker.reset()
    hook('session-start', dict(base, hook_event_name='SessionStart', source='startup'), base_env(seeker.url, os.path.join(WORK, 'home-r8-plain')))
    check('session-start without CTRLB_RUN: no read and no claim', not seeker.calls)

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
    check(f'post-tool artifact budget: Seeker slower ({SLOW_S}s) than the hook: returns at once (the post is detached)',
          p.returncode == 0 and not p.stdout.strip() and dt < 1.5, f'{dt:.2f}s')
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
    check('session-start, a Seeker without the artifacts read: artefacts restored from the local copies of what this machine posted',
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
    check(f'autoSlots budget: Seeker slower ({SLOW_S}s) than the {RH.SLOT_S}s claim: fails open inside the pre-tool budget (edit allowed)',
          p.returncode == 0 and not p.stdout.strip() and dt < H.CHECK_S + 1.0 and 'auto slot fail open' in _log(ehome), f'{dt:.2f}s')
    seeker.state['claims'] = 'error'
    p, _ = hook('pre-tool', pre, eenv)
    seeker.state['claims'] = 'ok'
    check('autoSlots: Seeker error: fails open (edit allowed)', p.returncode == 0 and not p.stdout.strip())
    seeker.reset()
    hook('pre-tool', pre, eenv)
    seeker.state['release'] = 'slow'
    p, dt = hook('post-tool', epost, eenv)
    seeker.state['release'] = 'ok'
    check(f'autoSlots release budget: Seeker slower ({SLOW_S}s) than the {RH.RELEASE_S}s release: returns inside it (the slot lapses)',
          p.returncode == 0 and not p.stdout.strip() and dt < RH.RELEASE_S + 1.0, f'{dt:.2f}s')
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
    check('codex session-start (relay run): the hooks text, the scratch directory, no record block',
          'CtrlB Relay record of run' not in ctx and hooks_rule(ctx, run, 'agent-1')
          and os.path.join(WORK, 'home-r8-codex', 'relay', run, 'scratch') in ctx, ctx[-300:])
    doc = open(os.path.join(ROOT, 'docs', 'relay-hooks.md')).read()
    check('docs/relay-hooks.md: every round 8 hook with its event, fields, output and budget, citing the hooks reference',
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



# ---------------------------------------------------------------- the hooks protocol (tests/fixtures/machine.json)

class ScriptedSeeker:
    """A fake Seeker for the machine fixtures: each route answers from a script (answers used in order, the last
    repeats), and every call is recorded as (method, route, body)."""

    def __init__(self):
        self.calls, self.script = [], {}
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _handle(self, method):
                n = int(self.headers.get('Content-Length') or 0)
                body = json.loads(self.rfile.read(n) or b'null') if n else None
                path = self.path.split('?')[0]
                route = 'records' if path.startswith('/ai/sessions/') else path.rstrip('/').rsplit('/', 1)[-1]
                outer.calls.append((method, route, body))
                q = outer.script.get(route) or []
                a = (q.pop(0) if len(q) > 1 else q[0]) if q else (
                    {'status': 202, 'body': {'accepted': len(body or [])}} if route == 'records' else
                    {'body': {'artifacts': []}} if route == 'artifacts' else {'status': 404, 'body': {'error': 'no script'}})
                if a.get('sleep'):
                    time.sleep(a['sleep'])
                raw = json.dumps(a.get('body', {})).encode()
                try:
                    self.send_response(a.get('status', 200))
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def do_POST(self):
                self._handle('POST')

            def do_GET(self):
                self._handle('GET')

            def do_DELETE(self):
                self._handle('DELETE')

        self.httpd = http.server.ThreadingHTTPServer(('127.0.0.1', 0), H)
        self.httpd.daemon_threads = True
        self.url = f'http://127.0.0.1:{self.httpd.server_address[1]}'
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()


PROTOCOL_ROUTES = ('claims', 'check', 'done', 'handoffs')
MSID = '7a1c0de0-0000-4000-8000-00000000000a'


def _expand(v, defs, now):
    """Fixture values: "$name" is a def, "{now+N}" is now plus N seconds in µs."""
    if isinstance(v, str):
        if v.startswith('$') and v[1:] in defs:
            return _expand(defs[v[1:]], defs, now)
        m = re.fullmatch(r'\{now([+-]\d+)\}', v)
        if m:
            return int((now + int(m.group(1))) * 1e6)
        return v
    if isinstance(v, list):
        return [_expand(x, defs, now) for x in v]
    if isinstance(v, dict):
        return {k: _expand(x, defs, now) for k, x in v.items()}
    return v


def _subset(want, got):
    if isinstance(want, dict):
        return isinstance(got, dict) and all(_subset(v, got.get(k)) for k, v in want.items())
    return want == got


def _machine_input(case, step, ws, home):
    harness = case.get('harness', 'claude')
    i = dict(step.get('input') or {})
    inp = dict(session_id=MSID, cwd=ws, transcript_path=None, hook_event_name={
        'session-start': 'SessionStart', 'subagent-start': 'SubagentStart', 'pre-tool': 'PreToolUse', 'post-tool': 'PostToolUse',
        'post-tool-failure': 'PostToolUseFailure', 'session-end': 'SessionEnd', 'upload': 'Stop'}[step['event']])
    for k in ('source', 'agent_id', 'agent_type', 'reason'):
        if k in i:
            inp[k] = i[k]
    if 'file' in i:
        f = os.path.join(ws, i['file'])
        inp['tool_name'] = i['tool_name']
        inp['tool_input'] = ({'input': f"*** Begin Patch\n*** Update File: {i['file']}\n@@\n-a\n+b\n*** End Patch\n"}
                             if harness == 'codex' else {'file_path': f, 'old_string': 'a', 'new_string': 'b'})
    if 'tool_input_raw' in i:
        inp['tool_name'], inp['tool_input'], inp['tool_response'] = i['tool_name'], i['tool_input_raw'], i['tool_response']
        inp['tool_use_id'] = i.get('tool_use_id', '')
    if 'command' in i:
        inp['tool_name'] = i['tool_name']
        inp['tool_input'] = {'command': i['command']}
        if 'error' in i:
            inp['error'] = i['error']
        elif 'tool_response' in i:
            inp['tool_response'] = i['tool_response']
        else:
            inp['tool_response'] = {'stdout': i.get('stdout', ''), 'stderr': '', 'interrupted': False}
    if 'rollout' in i:
        path = os.path.join(home, 'rollout.jsonl')
        with open(path, 'w') as fh:
            for kind, cid, arg in i['rollout']:
                if kind == 'function_call':
                    p = dict(type='function_call', name='exec_command', call_id=cid, arguments=json.dumps(arg))
                elif kind == 'exec':
                    p = dict(type='custom_tool_call', name='exec', call_id=cid, input=arg)
                elif kind == 'exec_output':
                    p = dict(type='custom_tool_call_output', call_id=cid, output=arg)
                else:
                    p = dict(type='function_call_output', call_id=cid, output=arg)
                fh.write(json.dumps(dict(timestamp='2026-11-10T10:00:00Z', type='response_item', payload=p)) + '\n')
        inp['transcript_path'] = path
    return inp


def run_machine_case(case, defs, seeker, root=ROOT, quiet=False):
    """Run one fixture case through the hooks (of the plugin tree at root). Returns a list of failure strings."""
    harness = case.get('harness', 'claude')
    script = os.path.join(root, 'plugin', 'hooks', 'hook.py') if harness == 'claude' else os.path.join(root, 'codex', 'hook.py')
    base = os.path.join(WORK, 'machine')
    os.makedirs(base, exist_ok=True)
    home = tempfile.mkdtemp(prefix='home-', dir=base)
    ws = tempfile.mkdtemp(prefix='ws-', dir=base)
    for f in ('src/util.py', 'src/parse.py', 'src/emit.py'):
        os.makedirs(os.path.dirname(os.path.join(ws, f)), exist_ok=True)
        open(os.path.join(ws, f), 'w').write('a\n')
    env = base_env(seeker.url, home, CTRLB_RUN='run-m', CTRLB_AGENT='agent-1', CTRLB_AUTO_SLOTS='')
    for k, v in (case.get('env') or {}).items():
        if v is None:
            env.pop(k, None)
        else:
            env[k] = v
    now = time.time()
    if case.get('state_before'):
        os.makedirs(os.path.join(home, 'state'), exist_ok=True)
        with open(os.path.join(home, 'state', MSID + '.json'), 'w') as fh:
            json.dump(_expand(case['state_before'], defs, now), fh)
    fails = []
    for n, step in enumerate(case['steps']):
        seeker.calls.clear()
        seeker.script = {k: [_expand(a, defs, time.time()) for a in v] for k, v in (step.get('answers') or {}).items()}
        inp = _machine_input(case, step, ws, home)
        t = time.time()
        p = subprocess.run([sys.executable, '-I', script, step['event']], input=json.dumps(inp), capture_output=True,
                           text=True, env=env, timeout=30)
        dt = time.time() - t
        try:
            o = json.loads(p.stdout or '{}').get('hookSpecificOutput', {})
        except ValueError:
            o = {'_unparsed': p.stdout}
        text = o.get('additionalContext') or o.get('permissionDecisionReason') or ''
        e = step['expect']
        tag = f'step {n + 1}'
        calls = [c for c in seeker.calls if c[1] in PROTOCOL_ROUTES]
        want = e.get('calls', [])
        if len(calls) != len(want) or not all(m == c[0] and r == c[1] and _subset(b, c[2] or {}) for (m, r, b), c in zip(want, calls)):
            fails.append(f'{tag} calls {[(c[0], c[1], c[2]) for c in calls]}')
        for h in e.get('context_has', []):
            if h not in text:
                fails.append(f'{tag} context lacks {h!r}')
        for h in e.get('context_not', []):
            if h in text.split('CtrlB Relay', 1)[-1] if h == 'claim(' else h in text:
                fails.append(f'{tag} context has {h!r}')
        if e.get('context_empty') and (text or o.get('permissionDecision')):
            fails.append(f'{tag} said {text[:120]!r}')
        if 'decision' in e and (o.get('permissionDecision') or None) != e['decision']:
            fails.append(f'{tag} decision {o.get("permissionDecision")}')
        if e.get('max_s') and dt > e['max_s']:
            fails.append(f'{tag} took {dt:.2f}s')
        sf = os.path.join(home, 'state', (e.get('state_file') or '{sid}').replace('{sid}', MSID) + '.json')
        try:
            st = json.load(open(sf))
        except (OSError, ValueError):
            st = None
        if e.get('state') is not None:
            ws_ = dict(e['state'])
            if st is None:
                fails.append(f'{tag} no state file')
            else:
                for k in ('tasks', 'holds'):
                    if k in ws_ and sorted(st.get(k) or {}) != sorted(ws_.pop(k)):
                        fails.append(f'{tag} state {k} {sorted(st.get(k) or {})}')
                if not _subset(ws_, st):
                    fails.append(f'{tag} state {json.dumps({k: st.get(k) for k in ws_})}')
        for name, secs in (e.get('lease_after') or {}).items():
            until = ((st or {}).get('tasks', {}).get(name) or {}).get('lease_until') or 0
            if until / 1e6 - time.time() < secs:
                fails.append(f'{tag} {name} lease not renewed')
        if p.returncode != 0:
            fails.append(f'{tag} exit {p.returncode}')
    return fails


def test_machine():
    fx = load('machine.json')
    defs, cases = fx['defs'], fx['cases']
    seeker = ScriptedSeeker()
    for c in cases:
        fails = run_machine_case(c, defs, seeker)
        check(f'machine: {c["name"]}', not fails, '; '.join(fails)[:400])
    rows = {c['row'] for c in cases}
    check('machine: every row of the state table has a fixture',
          all(r in rows for r in ('idle SessionStart run', 'idle SubagentStart run', 'idle SessionStart no run', 'idle SessionStart none free',
                                  'holding SessionStart compact', 'edit unheld', 'edit belongs_to_task', 'edit own', 'edit other advisory',
                                  'edit other enforce', 'edit renew', 'bash test pass', 'done retry', 'session end', 'ended SessionStart',
                                  'bash test piped', 'bash test cd prefix', 'bash test redirect', 'bash test then other', 'edit after own done',
                                  "edit after other's done", 'codex bash test piped')))
    test_mutants(defs, cases, seeker)
    seeker.httpd.shutdown()


# Each action of the machine removed in turn (a text patch of a copy of the plugin): some fixture must fail.
MUTANTS = [
    ('SessionStart claim(next=true)', 'lib/hooks_shared.py', "ans = self._call('claim', 'next=true',", "ans = {} if True else self._call('claim', 'next=true',"),
    ('SessionStart brief of the held task', 'lib/hooks_shared.py', '            task = self.change(lambda d: self._take_task(d, ans, at=at))\n            return brief_text(task)',
     '            task = self.change(lambda d: self._take_task(d, ans, at=at))\n            return ""'),
    ('no run id: ids and peers only', 'lib/hooks_shared.py', "return (RELAY if cfg.get('run') else RELAY_NO_RUN)", 'return (RELAY)'),
    ('read-only sub-agent claims nothing', 'lib/hooks_shared.py', 'if agent_type in READ_ONLY_AGENTS:', 'if False:'),
    ('compaction: briefs again, no call', 'lib/hooks_shared.py', "return '\\n'.join(brief_text(t) for t in st['tasks'].values())", "return ''"),
    ('claim on write', 'lib/hooks_shared.py', "ans = self._call('claim', p, lambda:", "return 'held', None\n        ans = self._call('claim', p, lambda:"),
    ('belongs_to_task: claim the task', 'lib/hooks_shared.py', "if ans.get('reason') == 'belongs_to_task' and ans.get('task'):", 'if False:'),
    ('belongs_to_task: inject its brief', 'lib/hooks_shared.py', "return 'brief', brief_text(task)", "return 'held', None"),
    ('own hold: nothing', 'lib/hooks_shared.py', '            if p in own:\n', '            if p in own and False:\n'),
    ('another holder: name it', 'lib/hooks_shared.py', '        for p, holder, said in blocked:\n', '        for p, holder, said in []:\n'),
    ('enforce: deny', 'lib/hooks_shared.py', "if enforce and (blocked or failed or waiting):", 'if False:'),
    ('waiting_on: treated like a hold', 'lib/hooks_shared.py', "if t.get('reason') == 'waiting_on':", 'if False:'),
    ('brief: the tasks it comes after', 'lib/hooks_shared.py', "    if after:\n        parts.append(f'It comes after", "    if False:\n        parts.append(f'It comes after"),
    ('done: next waits on unfinished tasks, said', 'lib/hooks_shared.py', "if nxt.get('reason') == 'waiting_on':", 'if False:'),
    ('renew near the lease end', 'lib/hooks_shared.py', '        if _now_us() < until - RENEW_AT * lease * 1e6:\n            return', '        if True:\n            return'),
    ('done on a passing test', 'lib/hooks_shared.py', "                elif code == 0:\n                    ev = dict(command=", "                elif False:\n                    ev = dict(command="),
    ('done only for the registered test', 'lib/hooks_shared.py', "if not (t.get('test') and names_test(command, t['test'])):\n                    continue\n                if not test_alone(",
     "if not t.get('test'):\n                    continue\n                if False and not test_alone("),
    ('done only when the test ran alone', 'lib/hooks_shared.py', "                if not test_alone(command, t['test']):\n", "                if False:\n"),
    ('not alone: said once per task per session', 'lib/hooks_shared.py', "            if name in (d.get('nudged') or []):\n                return False", "            if False:\n                return False"),
    ('finisher keeps its files', 'lib/hooks_shared.py', "if t.get('finished_by') == self.agent and not t.get('held_by'):", 'if False:'),
    ('done witnessed', 'lib/hooks_shared.py', 'witnessed=True, next_=True, timeout=DONE_S', 'witnessed=False, next_=True, timeout=DONE_S'),
    ('done: next task brief', 'lib/hooks_shared.py', "return brief_text(task, lead=head + ' Next: ')", 'return head'),
    ('failed done kept for the next Bash event', 'lib/hooks_shared.py', 'self.change(lambda d: d.update(pending_done=dict(task=name, evidence=ev)))', 'pass'),
    ('refused done: message injected', 'lib/hooks_shared.py', "return f'CtrlB Relay (run {self.run}): task \"{name}\" was not marked finished: {msg}'", "return ''"),
    ('SessionEnd ends the machine', 'lib/hooks_shared.py', "self.change(lambda d: d.update(state='ended', tasks={}, holds={}, pending_done=None))", 'pass'),
    ('ended: SessionStart as idle', 'lib/hooks_shared.py', "        if st['state'] == 'ended':\n            self.change(lambda d: d.update(self._blank()))\n", ''),
    ('claim on write failure: said', 'lib/hooks_shared.py', '        if failed:\n            lines.append(', '        if failed and False:\n            lines.append('),
    ('codex PostToolUse on Bash', 'codex/hook.py', "    if (inp.get('tool_name') or '') not in SHELL_NAMES:\n        return None", "    return None"),
    ('codex rollout scan', 'codex/hook.py', "    for r in runs:\n        if r['ts'] and", "    for r in []:\n        if r['ts'] and"),
    ('codex code mode: each command with its own chunk', 'codex/hook.py', '    if code_js:\n', '    if False:\n'),
    ('codex scan skips runs older than the last done', 'codex/hook.py', "        if r['ts'] and r['ts'] <= (m.read().get('last_done_ts') or 0):", '        if False:'),
    ('codex PostToolUse scans the rollout', 'codex/hook.py', '    said += stop_fallback(cfg, inp)\n', ''),
    ('codex SessionEnd scan before the handoff', 'codex/hook.py', "        try:      # a passing test the PostToolUse hook missed is finished before the handoff releases the task\n            stop_fallback(cfg, inp)",
     "        try:      # a passing test the PostToolUse hook missed is finished before the handoff releases the task\n            pass"),
]


def _mutant_tree(base, rel, old, new):
    d = tempfile.mkdtemp(prefix='mutant-', dir=base)
    for sub in ('lib', 'codex', os.path.join('plugin', 'hooks')):
        shutil.copytree(os.path.join(ROOT, sub), os.path.join(d, sub), symlinks=True)
    os.symlink(os.path.join('..', 'lib'), os.path.join(d, 'plugin', 'lib'))
    f = os.path.join(d, rel)
    text = open(f).read()
    if text.count(old) != 1:
        return None
    open(f, 'w').write(text.replace(old, new))
    return d


def test_mutants(defs, cases, seeker):
    """The mutant check: each action removed in turn, the fixtures that fail. Writes tests/work/mutants.md."""
    base = os.path.join(WORK, 'mutants')
    shutil.rmtree(base, ignore_errors=True)
    os.makedirs(base)
    rows, survived = [], []
    for name, rel, old, new in MUTANTS:
        d = _mutant_tree(base, rel, old, new)
        if d is None:
            rows.append((name, 'patch did not apply', ''))
            survived.append(name)
            continue
        killed = [c['row'] for c in cases if run_machine_case(c, defs, seeker, root=d)]
        rows.append((name, f'{len(killed)} of {len(cases)}', ', '.join(killed)))
        if not killed:
            survived.append(name)
    with open(os.path.join(WORK, 'mutants.md'), 'w') as fh:
        fh.write('| Action removed | Fixtures failing | Which |\n|---|---|---|\n')
        for r in rows:
            fh.write(f'| {r[0]} | {r[1]} | {r[2]} |\n')
    for r in rows:
        print(f'  mutant: {r[0]}: {r[1]} fail ({r[2][:140]})')
    check(f'mutants: each of the {len(MUTANTS)} actions removed fails a fixture', not survived, ', '.join(survived))

def main():
    seeker = Seeker()
    test_layout()
    test_no_secrets_in_repo()
    test_hooks(seeker)
    test_batches(seeker)
    test_pre_tool(seeker)
    test_keys()
    test_edit_keys_across_harnesses(seeker)
    test_docs()
    test_release_wording()
    test_briefs_wording()
    test_wait_wording()
    test_takeover_wording()
    test_done_evidence(seeker)
    test_round6_hooks(seeker)
    test_round8_hooks(seeker)
    test_failure_safety(seeker)
    test_config_and_status(seeker)
    test_codex_rows(seeker)
    test_codex_hooks(seeker)
    test_codex_install()
    test_root_install()
    test_codex_backfill(seeker)
    test_validate()
    test_machine()      # last: earlier tests clear tests/work, where the mutant table is written
    seeker.httpd.shutdown()
    fails = [r for r in results if not r[1]]
    print(f'\n{len(results) - len(fails)}/{len(results)} passed (Seeker: synthetic fixtures in tests/fixtures/seeker)')
    return 1 if fails else 0


if __name__ == '__main__':
    sys.exit(main())
