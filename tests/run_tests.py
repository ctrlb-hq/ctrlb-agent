"""Tests for ctrlb-agent. Run: python3 -I tests/run_tests.py

Seeker is a local fake that answers with the synthetic responses in tests/fixtures/seeker/ (relay-plan section 4).
Nothing is sent anywhere else, Claude is never called, and no real config is read: every CTRLB_* variable from the
calling environment is dropped and CTRLB_HOME points into tests/work/."""
import base64, glob, http.server, json, os, re, shutil, socket, stat, subprocess, sys, threading, time
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
        self.calls, self.rows, self.refused, self.state = [], [], [], dict(check='allow')
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
                    return self._answer(**outer.check_fx['responses'][s])
                if self.path.startswith('/ai/relay/') and self.path.split('?')[0].endswith('/runs') and method == 'GET':
                    return self._answer(**outer.runs_fx['responses']['ok'])
                return self._answer(404, {'error': 'no route'})

            def do_POST(self):
                self._handle('POST')

            def do_GET(self):
                self._handle('GET')

        self.httpd = http.server.ThreadingHTTPServer(('127.0.0.1', 0), H)
        self.httpd.daemon_threads = True
        self.url = f'http://127.0.0.1:{self.httpd.server_address[1]}'
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def reset(self):
        self.calls.clear()
        self.rows.clear()
        self.refused.clear()


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
    events = {'SessionStart': 'session-start', 'SubagentStart': 'subagent-start', 'PreCompact': 'flush', 'SessionEnd': 'flush',
              'Stop': 'upload', 'SubagentStop': 'upload', 'PreToolUse': 'pre-tool', 'PostToolUse': 'post-tool'}
    ok = set(hk) == set(events) and all(hk[e][0]['hooks'][0]['command'].endswith('hook.py" ' + m) for e, m in events.items())
    check('hooks.json: the six hooks plus PreToolUse and PostToolUse, each with its mode', ok, ','.join(sorted(hk)))
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
    named = sorted({os.path.relpath(f, ROOT) for f in files + [os.path.abspath(__file__)]
                    if not f.endswith('relay-plan.md') and banned.search(open(f, errors='replace').read())})
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
    check('session-start: claims the registered task, its files come with it, holds one claim', registered_rule(ctx))
    check('session-start: refusals, unknown_task, reopen a finished task',
          'held is false with finished_by, the task is done' in ctx and 'held is false with held_by, another agent has it' in ctx
          and 'If reason is unknown_task, claim again with the suggestion' in ctx and 'claim its name with reopen=true' in ctx)
    check('session-start: slots for things that are not files', 'is claimed as slot:<name> and released when you are done with it' in ctx
          and ctx.index('slot:<name>') < ctx.index('reopen=true'))
    check('relay skill and tools: slots, two-minute lease, never finished',
          'claimed as `slot:<name>` and released when you are done with it' in open(os.path.join(ROOT, 'plugin', 'skills', 'relay', 'SKILL.md')).read()
          and all(n in open(os.path.join(ROOT, 'docs', 'relay-tools.md')).read() for n in
                  ('## Slots', '`slot:devserver`', '`slot:port-3000`', '`slot:db-migrate`', '`slot:screenshots`', '`slot:commit`', 'lasts two minutes')))
    check('session-start: names the relay tools, contract and contracts among them',
          'relay tools (peers, post, claim, release, handoff, ask, answer, brief, briefs, tasks, contract, contracts)' in ctx)
    check('session-start: write a contract when an interface is done, read contracts before building on one',
          'When you finish an interface others will build on (a type, a route, a config shape), call contract with its name, the shape '
          'in a few lines, and the files that define it. Before building on one, call contracts.' in ctx)
    check('session-start: ask when blocked, never a QUESTION note; answer open questions',
          'When you are blocked on something only another agent or the orchestrator can decide, call ask. Do not post a QUESTION note. '
          'When you can answer an open question, call answer with its id.' in ctx)
    check('session-start under 1 s', dt < 1.0, f'{dt:.2f}s')
    p, _ = hook('session-start', I['session_start_compact'], dict(env, CTRLB_RUN='relay-run-7', CTRLB_AGENT='builder-1'))
    ctx = out_of(p).get('additionalContext', '')
    check('session-start: CTRLB_RUN and CTRLB_AGENT win', 'run id is relay-run-7' in ctx and 'agent id is builder-1' in ctx
          and 'run_id="relay-run-7"' in ctx)
    check('session-start sends nothing to Seeker', not seeker.calls)

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
    files = [f for f in glob.glob(os.path.join(ROOT, '**', '*'), recursive=True)
             if os.path.isfile(f) and f.endswith(('.md', '.py', '.json')) and '/tests/' not in f and '/.git/' not in f
             and not f.endswith('relay-plan.md')]
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
          and 'call `contract` with its name' in skill and 'Before building on one, call `contracts`' in skill
          and 'call `ask`. Do not post a QUESTION note. When you can answer an open question, call `answer` with its id' in skill)
    check('relay skill: call briefs before reading a shared file, post a brief after reading one',
          'call `briefs` with it' in skill and 'post a `brief` for it' in skill and 'up to a few hundred words' in skill
          and 'what it exports or decides, and what not to touch' in skill and 'not a claim and reserves nothing' in skill)
    tools = open(os.path.join(ROOT, 'docs', 'relay-tools.md')).read()
    sections = re.findall(r'^### (\w+)$', tools, re.M)
    relay = sections[sections.index('peers'):] if 'peers' in sections else []
    check('relay-tools: thirteen relay tools, handoff, ask and answer after release, then brief, briefs, task, tasks, contract, contracts',
          relay == ['peers', 'post', 'claim', 'release', 'handoff', 'ask', 'answer', 'brief', 'briefs', 'task', 'tasks', 'contract', 'contracts'],
          ','.join(relay))
    check('relay-tools: contract and contracts documented, instead of a CONTRACT note',
          '{contracts: [{name, key, agent, ts, age_s, text, paths, changed_since}]}' in tools and 'instead of a `CONTRACT:` note' in tools
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
    check('relay-tools: seventeen tools, the writes listed, briefs answers the newest brief per key',
          'gives an agent seventeen tools' in tools and '`post`, `claim`, `release`, `handoff`, `ask`, `answer`, `brief`, `task` and `contract` write' in tools
          and '{briefs: [{key, resource, agent, text, ts, age_s, paths?, changed_since?}]}' in tools and '`BRIEF` for a brief' in tools)
    check('relay-tools: the briefs paragraph for the rules', 'call briefs for it and read the brief first' in tools)
    check('relay-tools: rule 3 claims the registered task with its files, and finishes it', registered_rule(tools))
    check('relay-tools: claim documents paths, conflict_key and unknown_task; task and tasks documented',
          '| `paths` |' in tools and '"conflict_key"' in tools and '"reason": "unknown_task"' in tools and '"suggestion"' in tools
          and '{tasks: [{name, key, text, paths, agent, ts, state, held_by?, finished_by?, round}]}' in tools)
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
    check('codex session-start: claims the registered task, its files come with it', registered_rule(ctx))
    check('codex session-start: names the relay tools, ask and answer among them',
          'relay tools (peers, post, claim, release, handoff, ask, answer, brief, briefs, tasks, contract, contracts)' in ctx and 'call ask' in ctx)
    check('codex session-start: slots', 'slot:<name>' in ctx)
    check('codex session-start: release it finished; reopen a finished task',
          'release it with finished=true' in ctx and 'claim its name with reopen=true' in ctx)
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
    want = {'SessionStart': 'session-start', 'SubagentStart': 'subagent-start', 'PreCompact': 'flush', 'SessionEnd': 'flush',
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
    test_failure_safety(seeker)
    test_config_and_status(seeker)
    test_codex_rows(seeker)
    test_codex_hooks(seeker)
    test_codex_install()
    test_root_install()
    test_codex_backfill(seeker)
    test_validate()
    seeker.httpd.shutdown()
    fails = [r for r in results if not r[1]]
    print(f'\n{len(results) - len(fails)}/{len(results)} passed (Seeker: synthetic fixtures in tests/fixtures/seeker)')
    return 1 if fails else 0


if __name__ == '__main__':
    sys.exit(main())
