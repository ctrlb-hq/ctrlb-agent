"""Tests for ctrlb-agent. Run: python3 -I tests/run_tests.py

Seeker is a local fake that answers with the synthetic responses in tests/fixtures/seeker/ (relay-plan section 4).
Nothing is sent anywhere else, Claude is never called, and no real config is read: every CTRLB_* variable from the
calling environment is dropped and CTRLB_HOME points into tests/work/."""
import glob, http.server, json, os, re, shutil, socket, stat, subprocess, sys, threading, time
sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FIX = os.path.join(HERE, 'fixtures')
WORK = os.path.join(HERE, 'work')
HOOK = os.path.join(ROOT, 'plugin', 'hooks', 'hook.py')
SID = '5f0c7a52-0000-4000-8000-000000000001'
KEY = 'test-key-not-a-real-key'
sys.path.insert(0, os.path.join(ROOT, 'lib'))
os.environ['CTRLB_HOME'] = os.path.join(WORK, 'home-import')
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
        self.calls, self.rows, self.state = [], [], dict(check='allow')
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
                if self.headers.get('Authorization') != 'Bearer ' + KEY:
                    return self._answer(**outer.records['responses']['no_key'])
                m = re.match(r'^/ai/sessions/([A-Za-z0-9_]+)/records$', self.path)
                if m and method == 'POST':
                    lim = outer.records['limits']
                    if not isinstance(body, list) or len(body) > lim['max_rows'] or len(raw) > lim['max_bytes']:
                        return self._answer(**outer.records['responses']['too_many_rows'])
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


def hook(mode, inp, env, timeout=20):
    t = time.time()
    p = subprocess.run([sys.executable, '-I', HOOK, mode], input=inp if isinstance(inp, str) else json.dumps(inp),
                       capture_output=True, text=True, env=env, timeout=timeout)
    return p, time.time() - t


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

def test_layout():
    pj = load('../../plugin/.claude-plugin/plugin.json')
    check('plugin.json: name ctrlb-agent, version 0.2.0', pj.get('name') == 'ctrlb-agent' and pj.get('version') == '0.2.0')
    mk = load('../../.claude-plugin/marketplace.json')
    ent = mk.get('plugins') or []
    check('marketplace.json: one entry, ctrlb-agent at ./plugin', len(ent) == 1 and ent[0]['name'] == 'ctrlb-agent'
          and ent[0]['source'] == './plugin')
    hk = load('../../plugin/hooks/hooks.json')['hooks']
    events = {'SessionStart': 'session-start', 'SubagentStart': 'subagent-start', 'PreCompact': 'flush', 'SessionEnd': 'flush',
              'Stop': 'upload', 'SubagentStop': 'upload', 'PreToolUse': 'pre-tool'}
    ok = set(hk) == set(events) and all(hk[e][0]['hooks'][0]['command'].endswith('hook.py" ' + m) for e, m in events.items())
    check('hooks.json: the six hooks plus PreToolUse, each with its mode', ok, ','.join(sorted(hk)))
    check('hooks.json: PreToolUse matcher Edit|Write|MultiEdit|NotebookEdit',
          hk['PreToolUse'][0].get('matcher') == 'Edit|Write|MultiEdit|NotebookEdit')
    check('hooks.json: every command runs python3 -I', all(h['command'].startswith('python3 -I ') for e in hk.values() for g in e for h in g['hooks']))
    mcp = load('../../plugin/.mcp.json')
    srv = (mcp.get('mcpServers') or mcp).get('ctrlb', {})
    check('.mcp.json: ctrlb server, section 6 url and headers', srv.get('url') == '${CTRLB_MCP_URL}' and srv.get('headers') == {
        'Authorization': 'Bearer ${CTRLB_KEY}', 'X-CtrlB-Run': '${CTRLB_RUN}', 'X-CtrlB-Agent': '${CTRLB_AGENT}'})
    skills = {n: open(os.path.join(ROOT, 'plugin', 'skills', n, 'SKILL.md')).read() for n in ('setup', 'recall', 'relay')}
    check('skills setup, recall, relay have front matter with their names',
          all(s.startswith('---\n') and f'\nname: {n}\n' in s for n, s in skills.items()))
    relay = skills['relay']
    check('relay skill: run id argument and the three rules', '$ARGUMENTS' in relay and all(f'\n{i}. ' in relay for i in (1, 2, 3))
          and 'call `claim` with the file path' in relay and 'finished=true' in relay)
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
    check('session-start: tells the agent to claim the file path a task names', 'claim with the file path the task names' in ctx)
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
    check('paths on Edit, Write, MultiEdit, NotebookEdit tool_use rows', paths.get('Edit') == '["/work/dash/src/panels/Latency.tsx"]'
          and paths.get('Write') == '["/work/dash/src/panels/index.ts"]'
          and paths.get('MultiEdit') == '["/work/dash/src/panels/Latency.test.tsx"]'
          and paths.get('NotebookEdit') == '["/work/dash/notebooks/latency.ipynb"]', json.dumps(paths))
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
    check('user_name left to Seeker when CTRLB_USER is unset', all('user_name' not in r for r in rows))
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

    # user name, when configured, is written
    seeker.reset()
    p, _ = hook('upload', I['stop'], dict(env, CTRLB_HOME=os.path.join(WORK, 'home-user'), CTRLB_USER='dev_one'))
    check('CTRLB_USER is written as user_name', seeker.rows and all(r.get('user_name') == 'dev_one' for r in seeker.rows))

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
    for name, want in (('pre_tool_write', '/work/dash/src/panels/index.ts'), ('pre_tool_multiedit', '/work/dash/src/panels/Latency.test.tsx'),
                       ('pre_tool_notebook', '/work/dash/notebooks/latency.ipynb')):
        p, _ = run('enforce', name)
        check(f'pre-tool {name[9:]}: path checked and denied', seeker.calls and seeker.calls[0]['body']['paths'] == [want]
              and out_of(p).get('permissionDecision') == 'deny')
    p, _ = run('enforce', 'pre_tool_subagent_edit', dict(env, CTRLB_RUN='relay-run-7'))
    c = seeker.calls[0] if seeker.calls else {}
    check('pre-tool in a subagent: CTRLB_RUN in the route, agent-<id> as agent', c.get('path') == '/ai/relay/agent_sessions/runs/relay-run-7/check'
          and c.get('body', {}).get('agent') == 'agent-abc123' and 'agent="agent-abc123"' in out_of(p).get('permissionDecisionReason', ''))


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
    p = subprocess.run([sys.executable, '-I', os.path.join(ROOT, 'bin', 'backfill.py'), '--count'], capture_output=True, text=True,
                       env=dict(env, CTRLB_KEY=KEY, CLAUDE_PROJECTS_DIR=os.path.join(WORK, 'projects')), timeout=30)
    check('backfill.py --count lists the fixture transcripts', p.returncode == 0 and 'transcript files' in p.stdout, p.stdout.strip()[:200])


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
    test_failure_safety(seeker)
    test_config_and_status(seeker)
    test_validate()
    seeker.httpd.shutdown()
    fails = [r for r in results if not r[1]]
    print(f'\n{len(results) - len(fails)}/{len(results)} passed (Seeker: synthetic fixtures in tests/fixtures/seeker)')
    return 1 if fails else 0


if __name__ == '__main__':
    sys.exit(main())
