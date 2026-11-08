"""relay-proof Task 3: every arm of the plan, one trial at a time, against Seeker on :8096 (CtrlB Relay).

Usage (SEEKER_MCP_KEY, CTRLB_KEY and SEEKER_JWT in the environment; SEEKER_JWT, an admin user token, is needed only
to set the settings; the keys are read into request headers only, never printed or written):
  python3 -I research/relay/relay_proof.py <arm> --project toy|devbench:<p>
      --trials N --model haiku|sonnet [--budget-min 15] [--max-turns 200] [--evidence-required] [--task-tests]
      [--first K] [--lane NAME]
  python3 -I relay_proof.py --summary <arm>          (rebuild the arm's summary from its trial files)
Env: RELAY_SEEKER (http://127.0.0.1:8096), RELAY_SESSIONS_DATASET (agent_sessions), RELAY_PLUGIN (the ctrlb-agent
plugin to load; default this repo's plugin/), RELAY_PROOF_OUT (results; default results/relay-proof next to this
file), RELAY_PROOF_FEATURES (the feature arms' per-project data; default relay_proof_features.json next to this file),
RELAY_PROOF_NO_RESTORE=1 (a lane of a parallel pair leaves the settings for the queue to restore), CLAUDE_PROJECTS
(where Claude Code keeps transcripts; default ~/.claude/projects); run_swarm's RELAY_TEMPLATE and devbench's
DEVBENCH_* for the task sources.

Arms (ARMS below):
  solo, pair-none                       no Relay: no plugin, no ctrlb MCP; pair-none gets run_swarm's 'none' rule
  pair-relay, quad-relay                registered tasks, bundled claims, all relay tools, holdRequired on
  questions, questions-control          a PRD fact removed; the harness answers questions naming it every 10 s
                                        (peers kind=question open=true + answer); control: ask disallowed
  contracts, contracts-control          one PRD line changed after the first contract, plus a NOTE; control: contract
                                        disallowed, the change made when the trigger file's task is first finished
                                        (or at fallback_s)
  handoff, handoff-control              leaseSeconds 60; agent-1 told of a 4-minute shift and stopped at 5 min;
                                        control: no warning, stopped at 4 min
  fence-off, fence-on                   leaseSeconds 60, holdRequired false / true

Relay arms load the ctrlb-agent plugin (its SessionStart hook injects RELAY once; no --append-system-prompt) and an MCP
config sending the key. Every arm's transcripts are uploaded after the agents stop with the plugin's own
rows_claude.upload_file (CTRLB_RUN / CTRLB_AGENT set), so Seeker's run detail joins every arm's edits. Settings are set
per arm and restored to BASE in a finally. Results: <RELAY_PROOF_OUT>/<arm>/<project>/<model>-NN.json and
<RELAY_PROOF_OUT>/<arm>/summary.json. Stops only the process groups it started. Never prints or writes a key.
"""
import argparse, calendar, fcntl, glob, json, os, re, shutil, signal, statistics, subprocess, sys, tempfile, time
import urllib.error, urllib.parse, urllib.request, uuid

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import run_swarm as rs  # noqa: E402  (read only: TEMPLATE, template_tasks, RULES, PROMPT, WRITE_RE)

SEEKER = os.environ.get('RELAY_SEEKER', 'http://127.0.0.1:8096')
DS = os.environ.get('RELAY_SESSIONS_DATASET', 'agent_sessions')
RELAY_DS = '_ctrlb_relay_' + DS
PLUGIN = os.path.expanduser(os.environ.get('RELAY_PLUGIN') or os.path.join(HERE, '..', '..', 'plugin'))
OUT_ROOT = os.environ.get('RELAY_PROOF_OUT', os.path.join(HERE, 'results', 'relay-proof'))
FEATURES = os.environ.get('RELAY_PROOF_FEATURES') or os.path.join(HERE, 'relay_proof_features.json')
CLAUDE_PROJECTS = os.environ.get('CLAUDE_PROJECTS') or os.path.expanduser('~/.claude/projects')
BASE = dict(sessionsDataset=DS, waitMs=20, leaseSeconds=600, enforce='enforce', holdRequired=False,
            evidenceRequired=False, stallAfterSeconds=300)
SETTING_KEYS = ('sessionsDataset', 'waitMs', 'leaseSeconds', 'enforce', 'holdRequired', 'evidenceRequired',
                'stallAfterSeconds')
SERVER = 'ctrlb'
RELAY_TOOLS = ('peers', 'post', 'claim', 'release', 'handoff', 'ask', 'answer', 'brief', 'briefs', 'tasks', 'contract',
               'contracts')
BASE_TOOLS = 'Bash,Read,Edit,Write,MultiEdit,Glob,Grep'
EDIT_TOOLS = ('Edit', 'Write', 'MultiEdit', 'NotebookEdit')
ORCH = 'orchestrator'
# --evidence-required (Seeker decision 30): the run's trials set evidenceRequired true (restored to false after each)
# and allow the done tool. Off: every arm runs exactly as before.
EVIDENCE_REQUIRED = False
# --task-tests (Seeker decision 32): each task is registered with its test command (the task set's own unit test of
# the task's module, run with the stdlib unittest; the agents' python3 has no pytest), the test files are copied into
# the trial dir, and TASKS.md names them. Off: every arm runs exactly as before.
TASK_TESTS = False

SHIFT = (' You work a 4-minute shift. Before your 4 minutes are up, hand the task you hold to agent-2 with '
         'handoff(resource=<the task name>, to="agent-2"), post what is done and what is left on it, and stop.')
# relay-benchmark arm 3: a 3-minute shift with leaseSeconds 600, so the task outlives its holder; the harness polls
# the run detail for stalled tasks every 10 s (stall_poll)
SHIFT3 = (' You work a 3-minute shift. Before your 3 minutes are up, hand the task you hold to agent-2 with '
          'handoff(resource=<the task name>, to="agent-2"), post what is done and what is left on it, and stop.')
# supplementary to arm 3: the toy pair finishes in 85-125 s, so a 3-minute shift never bites; a 1-minute shift does
SHIFT1 = (' You work a 1-minute shift. Before your minute is up, hand the task you hold to agent-2 with '
          'handoff(resource=<the task name>, to="agent-2"), post what is done and what is left on it, and stop.')
ON = dict(relay=True, settings=dict(holdRequired=True))
ARMS = {
    'solo': dict(agents=1, relay=False),
    'pair-none': dict(agents=2, relay=False, rule=rs.RULES['none']),
    'pair-relay': dict(agents=2, **ON),
    'quad-relay': dict(agents=4, **ON),
    'questions': dict(agents=2, feature='questions', answer=True, ask_about=True, **ON),
    'questions-control': dict(agents=2, feature='questions', disallow=('ask',), **ON),
    'contracts': dict(agents=2, feature='contracts', **ON),
    'contracts-control': dict(agents=2, feature='contracts', disallow=('contract',), **ON),
    'handoff': dict(agents=2, relay=True, settings=dict(holdRequired=True, leaseSeconds=60), feature='handoff',
                    stop={'agent-1': 300}, extra={'agent-1': SHIFT}),
    'handoff-control': dict(agents=2, relay=True, settings=dict(holdRequired=True, leaseSeconds=60), feature='handoff',
                            stop={'agent-1': 240}),
    'handoff3': dict(agents=2, relay=True, settings=dict(holdRequired=True, leaseSeconds=600), feature='handoff',
                     stop={'agent-1': 240}, extra={'agent-1': SHIFT3}, stall_poll=True),
    'handoff3-control': dict(agents=2, relay=True, settings=dict(holdRequired=True, leaseSeconds=600,
                                                                 stallAfterSeconds=60), feature='handoff',
                             stop={'agent-1': 180}, stall_poll=True),
    'handoff1': dict(agents=2, relay=True, settings=dict(holdRequired=True, leaseSeconds=600, stallAfterSeconds=60),
                     feature='handoff',
                     stop={'agent-1': 90}, extra={'agent-1': SHIFT1}, stall_poll=True),
    'handoff1-control': dict(agents=2, relay=True, settings=dict(holdRequired=True, leaseSeconds=600,
                                                                 stallAfterSeconds=60), feature='handoff',
                             stop={'agent-1': 60}, stall_poll=True),
    'fence-off': dict(agents=2, relay=True, settings=dict(holdRequired=False, leaseSeconds=60)),
    'fence-on': dict(agents=2, relay=True, settings=dict(holdRequired=True, leaseSeconds=60)),
}


# ---------------------------------------------------------------- Seeker

def http(method, path, body=None, query=None, cred='SEEKER_MCP_KEY', timeout=30):
    """One call to Seeker as the orchestrator. The credential is read from the environment into the header only."""
    url = SEEKER + path + ('?' + urllib.parse.urlencode(query) if query else '')
    req = urllib.request.Request(url, method=method, data=None if body is None else json.dumps(body).encode(),
                                 headers={'Authorization': 'Bearer ' + os.environ[cred], 'X-CtrlB-Agent': ORCH,
                                          'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b'{}')
    except urllib.error.HTTPError as e:
        raw = e.read() or b'{}'
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, {'error': raw[:200].decode(errors='replace')}
    except (OSError, ValueError) as e:
        return 0, {'error': f'{type(e).__name__}: {str(e)[:200]}'}


def run_path(run_id, tail=''):
    return f'/ai/relay/{DS}/runs/{urllib.parse.quote(run_id, safe="")}{tail}'


def put_settings(over):
    """BASE plus the arm's overrides, written with the admin user token (the key is refused on PUT by design)."""
    st, cur = http('GET', '/ai/relay/settings', cred='SEEKER_JWT')
    if st != 200:
        raise RuntimeError(f'GET settings: {st} {cur}')
    want = dict(cur, **BASE)
    want.update(over)
    st, new = http('PUT', '/ai/relay/settings', want, cred='SEEKER_JWT')
    if st != 200:
        raise RuntimeError(f'PUT settings: {st} {new}')
    return {k: new.get(k) for k in SETTING_KEYS}


def register(run_id, tasks, tests=None, ask_about=None):
    """One BRIEF per task name with its file, as the orchestrator (registered tasks; claims of them bundle the file).
    tests ({task name: command}, --task-tests) adds each task's test command (Seeker decision 32); ask_about ({task
    name: [text]}, the questions arm) what its holder should ask about (decision 29)."""
    keys = []
    for name, path, line in tasks:
        body = {'agent': ORCH, 'resource': name, 'text': line, 'paths': [path]}
        if tests and tests.get(name):
            body['test'] = tests[name]
        if ask_about and ask_about.get(name):
            body['ask_about'] = ask_about[name]
        st, ans = http('POST', run_path(run_id, '/briefs'), body)
        if st not in (200, 201) or ans.get('paths') != ['path:' + path]:
            raise RuntimeError(f'registering {name}: {st} {ans}')
        if body.get('test') and ans.get('test') != body['test']:
            raise RuntimeError(f'registering {name}: the test command was not kept (Seeker before decision 32?): {ans}')
        keys.append(ans['key'])
    return keys


# DevBench's hidden unit test of a task's module: unit_tests/test_<module>.py. The agents' python3 has no pytest, and
# these files are unittest TestCases, so the command is the stdlib runner, run from the trial dir.
DEVBENCH_TEST = 'python3 -m unittest {}'
HIDDEN_TEST = 'test: hidden (judged after the run)'


def task_tests(project):
    """{task name: (test command, [(source file, path in the trial dir)])} for --task-tests. Toy: tests/test_<module>.py
    (already in the tree; TASKS.md names it). DevBench: unit_tests/test_<module>.py from the task set's judge,
    copied in; a task with no such file, or a project whose code imports packages the agents lack, gets none."""
    out = {}
    if project['name'] == 'toy':
        for name, path, _ in project['tasks']:
            rel = f'tests/test_{os.path.basename(path)[:-3]}.py'
            if os.path.exists(os.path.join(project['tree'], rel)):
                out[name] = (DEVBENCH_TEST.format(rel), [])
        return out
    if project.get('packages'):
        return out  # its modules import packages that are not installed for the agents: no test of it can run
    judge = os.path.join(os.path.dirname(project['tree']), 'judge')
    for name, path, _ in project['tasks']:
        rel = f'unit_tests/test_{os.path.basename(path)[:-3]}.py'
        if os.path.exists(os.path.join(judge, rel)):
            out[name] = (DEVBENCH_TEST.format(rel), [(os.path.join(judge, rel), rel)])
    return out


def give_task_tests(project, cwd, tests):
    """Copy each task's test file into the trial dir and name the test in its TASKS.md line (DevBench lines say the
    test is hidden). Scoring copies the judge over a scratch copy, so an edited test file changes no score. The
    registration text (the task line up to its file) is unchanged; the test goes in the registration's test."""
    for _, files in tests.values():
        for src, rel in files:
            os.makedirs(os.path.dirname(os.path.join(cwd, rel)), exist_ok=True)
            shutil.copy2(src, os.path.join(cwd, rel))
    md = os.path.join(cwd, 'TASKS.md')
    with open(md) as fh:
        text = fh.read()
    out = []
    for full in text.split('\n'):
        for name, _, line in project['tasks']:
            if name in tests and full.startswith('- ' + line) and HIDDEN_TEST in full:
                full = full.replace(HIDDEN_TEST, f'test: `{tests[name][0]}`')
        out.append(full)
    text = '\n'.join(out)
    if any(f for _, f in tests.values()):
        text = text.replace('No tests are given. After the run the project is judged by hidden unit and acceptance tests',
                            'A task whose line names a test has its unit test in unit_tests/: run that command from '
                            'this directory. After the run the project is judged by hidden unit and acceptance tests', 1)
    with open(md, 'w') as fh:
        fh.write(text)


def post_note(run_id, text):
    return http('POST', run_path(run_id, '/posts'), {'agent': ORCH, 'text': text})[0]


def lake_rows(run_id):
    """The run's relay rows straight from the lake (CHECK rows with every reason, REFUSED texts, HANDOFF to)."""
    import lib_lake as L
    sql = (f'SELECT _timestamp, agent, kind, key, text, nonce, held_by, "to" FROM {RELAY_DS} '
           f"WHERE run_id = '{L.q(run_id)}' ORDER BY _timestamp ASC")
    try:
        return L.search(RELAY_DS, sql, size=100000, timeout=60)
    except Exception as e:  # noqa: BLE001  (a run with no relay rows yet, or a column the dataset lacks)
        return [{'error': f'{type(e).__name__}: {str(e)[:200]}'}]


# ---------------------------------------------------------------- projects

def load_project(spec):
    """A task source as a dict: name, tree (copied as the trial dir), tasks [(name, src file, line)], prd (relpath),
    prompt (optional, {agent} {n}), score(cwd) -> {unit_pass, accept_pass, per_task?}. 'toy' is run_swarm's template;
    'devbench:<p>' is whatever run_swarm.load_taskset / run_swarm.score give (Task 2, agent devbench)."""
    if spec in ('toy', 'template'):
        tasks = rs.template_tasks(rs.TEMPLATE)
        return dict(name='toy', tree=rs.TEMPLATE, tasks=tasks, prd='TASKS.md', prompt=None, score=toy_score,
                    acceptance='proxy: the toy template has no acceptance tests; accept_pass = unit_pass')
    if spec.startswith('devbench:'):
        load, score = getattr(rs, 'load_taskset', None), getattr(rs, 'score', None)
        if not (load and score):
            sys.exit('devbench task sets are not available yet (run_swarm.load_taskset / run_swarm.score missing); '
                     'use --project toy')
        p = dict(load(spec))
        p.setdefault('name', spec.split(':', 1)[1])
        p['tasks'] = [tuple(t) for t in p['tasks']]
        p.setdefault('prd', None)
        p.setdefault('prompt', None)
        p['score'] = lambda cwd, _s=spec: score(cwd, _s)
        return p
    sys.exit(f'unknown project {spec}: toy or devbench:<project>')


# -I leaves the trial dir off sys.path, so the judge puts it there itself.
JUDGE = ('import sys, unittest; sys.path.insert(0, "."); '
         'sys.exit(0 if unittest.main(module=None, argv=["judge", sys.argv[1]], exit=False).result.wasSuccessful() else 1)')


def toy_score(cwd):
    """The template's own tests are the judge: copied fresh over the trial's tests/ before scoring."""
    shutil.copytree(os.path.join(rs.TEMPLATE, 'tests'), os.path.join(cwd, 'tests'), dirs_exist_ok=True)
    per = {}
    for name, path, _ in rs.template_tasks(rs.TEMPLATE):
        mod = os.path.basename(path)[:-3]
        r = subprocess.run([sys.executable, '-I', '-B', '-c', JUDGE, f'tests.test_{mod}'], cwd=cwd,
                           capture_output=True, text=True, timeout=60)
        per[mod] = r.returncode == 0
    unit = sum(per.values()) / len(per)
    return dict(unit_pass=round(unit, 4), accept_pass=round(unit, 4), per_task=per)


def features(project):
    """The feature arms' data for a project (RELAY_PROOF_FEATURES); {} when the file is absent (it is not shipped:
    devbench/features.py builds it from the downloaded projects)."""
    if not os.path.exists(FEATURES):
        return {}
    with open(FEATURES) as fh:
        data = json.load(fh)
    return data.get(project) or {}


# ---------------------------------------------------------------- agents

class Agent:
    def __init__(self, name, proc, log, t0):
        self.name, self.proc, self.log, self.t0 = name, proc, log, t0
        self.ended = None
        self.stopped_by_harness = None

    def alive(self):
        if self.ended is None and self.proc.poll() is not None:
            self.ended = time.time()
        return self.ended is None

    def stop(self, why):
        """SIGTERM to the agent's own process group (it was started in a new session, so the group is ours), SIGKILL
        after 15 s."""
        if not self.alive():
            return
        self.stopped_by_harness = why
        for sig, wait in ((signal.SIGTERM, 15), (signal.SIGKILL, 10)):
            try:
                os.killpg(self.proc.pid, sig)
            except ProcessLookupError:
                break
            try:
                self.proc.wait(timeout=wait)
                break
            except subprocess.TimeoutExpired:
                continue
        self.alive()


def prompt_for(project, agent, n):
    if project.get('prompt'):
        return project['prompt'].format(agent=agent, n=n)
    text = rs.PROMPT.format(agent=agent, n=n)
    if n == 1:
        text = text.replace(f'one of {n} agents working at the same time in this directory',
                            'working alone in this directory')
    return text


def launch(arm, cfg, project, agent, n, run_id, cwd, home, model, max_turns):
    env = dict(os.environ)
    for k in ('CLAUDECODE', 'SEEKER_JWT'):
        env.pop(k, None)  # the agent gets the MCP key only
    prompt = prompt_for(project, agent, n) + (cfg.get('extra') or {}).get(agent, '')
    cmd = ['claude', '-p', prompt, '--model', model, '--output-format', 'stream-json', '--verbose',
           '--max-turns', str(max_turns)]
    if cfg['relay']:
        mcp = os.path.join(cwd, f'.mcp-{agent}.json')
        with open(mcp, 'w') as fh:
            json.dump({'mcpServers': {SERVER: {'type': 'http', 'url': SEEKER + '/mcp?toolsets=agents',
                                               'headers': {'Authorization': 'Bearer ${SEEKER_MCP_KEY}',
                                                           'X-CtrlB-Run': run_id, 'X-CtrlB-Agent': agent}}}}, fh)
        env.update(CTRLB_URL=SEEKER, CTRLB_RUN=run_id, CTRLB_AGENT=agent, CTRLB_DATASET=DS, CTRLB_HOME=home,
                   CTRLB_MCP_URL=SEEKER + '/mcp?toolsets=agents')
        env.pop('CTRLB_OFF', None)
        env.pop('CTRLB_ENFORCE', None)
        tools = RELAY_TOOLS + ('done',)  # the v7 text tells agents to call done, whether or not evidence is required
        allowed = BASE_TOOLS + ',' + ','.join(f'mcp__{SERVER}__{t}' for t in tools if t not in cfg.get('disallow', ()))
        cmd += ['--plugin-dir', PLUGIN, '--mcp-config', mcp, '--strict-mcp-config', '--allowedTools', allowed]
        if cfg.get('disallow'):
            cmd += ['--disallowedTools', ','.join(f'mcp__{SERVER}__{t}' for t in cfg['disallow'])]
    else:
        env['CTRLB_OFF'] = '1'  # no ctrlb hook anywhere, even if one were installed globally
        cmd += ['--allowedTools', BASE_TOOLS, '--strict-mcp-config']
        if cfg.get('rule'):
            cmd += ['--append-system-prompt', cfg['rule']]
    log = open(os.path.join(cwd, f'.log-{agent}.jsonl'), 'w')
    proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=log, stderr=subprocess.STDOUT, text=True,
                            start_new_session=True)
    return Agent(agent, proc, log, time.time())


def upload_transcripts(paths, run_id, agent, home):
    """Upload with ctrlb-agent's own uploader in a child process (its lib is imported there, not here)."""
    env = dict(os.environ, CTRLB_URL=SEEKER, CTRLB_RUN=run_id, CTRLB_AGENT=agent, CTRLB_DATASET=DS, CTRLB_HOME=home)
    for k in ('CTRLB_OFF', 'SEEKER_JWT', 'CLAUDECODE'):
        env.pop(k, None)
    r = subprocess.run([sys.executable, '-I', os.path.abspath(__file__), '_upload', *paths], env=env,
                       capture_output=True, text=True, timeout=600)
    try:
        return json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {'error': (r.stderr or r.stdout)[-300:]}


def _upload_main(paths):
    sys.path.insert(0, os.path.join(PLUGIN, 'lib'))
    import common, rows_claude  # noqa: E401  (ctrlb-agent, standard library only)
    cfg = common.config()
    if cfg['off']:
        print(json.dumps({'error': 'ctrlb-agent config is off (url, key or dataset missing)'}))
        return
    total = dict(rows=0, lines=0, redacted=0, files=0)
    for p in paths:
        for f in rows_claude.session_files(p):
            c = rows_claude.upload_file(cfg, f, time.time() + 300)
            for k in ('rows', 'lines', 'redacted'):
                total[k] += c[k]
            total['files'] += 1
    print(json.dumps(total))


# ---------------------------------------------------------------- logs and transcripts

def ts_of(s):
    """ISO timestamp of a transcript line -> epoch seconds (float)."""
    if not s:
        return None
    try:
        main, _, frac = s.rstrip('Z').partition('.')
        return calendar.timegm(time.strptime(main, '%Y-%m-%dT%H:%M:%S')) + (float('0.' + frac) if frac else 0.0)
    except ValueError:
        return None


def text_of(c):
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return ''.join(x.get('text', '') for x in c if isinstance(x, dict))
    return str(c or '')


def parse_stream(log):
    """session id, cost, turns, tool calls by name, whether a result line came, and post texts, from a stream log."""
    out = dict(session_id='', cost_usd=None, turns=0, tools={}, result=False, posts=[], subtype='', claims_with_wait=0)
    with open(log, errors='replace') as fh:
        for line in fh:
            try:
                m = json.loads(line)
            except ValueError:
                continue
            if m.get('type') == 'system' and m.get('subtype') == 'init':
                out['session_id'] = m.get('session_id') or out['session_id']
            elif m.get('type') == 'assistant':
                out['turns'] += 1
                for b in (m.get('message') or {}).get('content') or []:
                    if isinstance(b, dict) and b.get('type') == 'tool_use':
                        out['tools'][b['name']] = out['tools'].get(b['name'], 0) + 1
                        if b['name'] == f'mcp__{SERVER}__post':
                            out['posts'].append(str((b.get('input') or {}).get('text') or ''))
                        if b['name'] == f'mcp__{SERVER}__claim' and (b.get('input') or {}).get('wait_s'):
                            out['claims_with_wait'] += 1
            elif m.get('type') == 'result':
                out.update(result=True, cost_usd=m.get('total_cost_usd'), turns=m.get('num_turns') or out['turns'],
                           subtype=m.get('subtype') or '')
    return out


def transcript_path(session_id):
    hits = glob.glob(os.path.join(CLAUDE_PROJECTS, '*', session_id + '.jsonl'))
    return hits[0] if hits else ''


DENY = re.compile(r'\((lapsed|unheld|stale_round)\)')


def evidence_refusals(log):
    """Tool results in an agent's stream log that answer reason evidence_required (Seeker decision 30)."""
    n = 0
    try:
        fh = open(log, errors='replace')
    except OSError:
        return 0
    with fh:
        for line in fh:
            if 'evidence_required' not in line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            msg = rec.get('message') if isinstance(rec, dict) else None
            for b in (msg.get('content') or []) if isinstance(msg, dict) and isinstance(msg.get('content'), list) else []:
                if isinstance(b, dict) and b.get('type') == 'tool_result' and 'evidence_required' in text_of(b.get('content')):
                    n += 1
    return n


def parse_transcript(path, cwd, files):
    """Timestamped file edits (applied or denied, with the hook's reason) and held claims, from a transcript."""
    real = os.path.realpath(cwd)
    calls, results = {}, {}
    last = None
    if not path or not os.path.exists(path):
        return dict(edits=[], claims=[], last_ts=None)
    with open(path, errors='replace') as fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get('type') not in ('assistant', 'user'):
                continue  # other record types repeat request content without a timestamp
            ts = ts_of(rec.get('timestamp'))
            if ts and (last is None or ts > last):
                last = ts
            for b in ((rec.get('message') or {}).get('content') or []) if isinstance(rec.get('message'), dict) else []:
                if not isinstance(b, dict):
                    continue
                if b.get('type') == 'tool_use':
                    inp, name = b.get('input') or {}, b.get('name') or ''
                    if name in EDIT_TOOLS:
                        fp = inp.get('file_path') or inp.get('notebook_path') or ''
                        rel = os.path.relpath(os.path.realpath(fp), real) if os.path.isabs(fp) else os.path.normpath(fp)
                        calls[b.get('id')] = ('edit', ts, name, [rel])
                    elif name == 'Bash':
                        # only commands that write the file (run_swarm.bash_writes): reading it, a python -c that
                        # only names it, pytest or an import are not edits
                        cmd = inp.get('command') or ''
                        hit = [f for f in files if rs.bash_writes(cmd, f, cwd)]
                        if hit:
                            calls[b.get('id')] = ('edit', ts, 'Bash', hit)
                    elif name == f'mcp__{SERVER}__claim':
                        calls[b.get('id')] = ('claim', ts, name, [str(inp.get('resource') or '')])
                elif b.get('type') == 'tool_result':
                    results[b.get('tool_use_id')] = (bool(b.get('is_error')), text_of(b.get('content')), ts)
    edits, claims = [], []
    for cid, (kind, ts, name, paths) in calls.items():
        err, txt, rts = results.get(cid, (False, '', ts))
        if kind == 'claim':
            if re.search(r'"held"\s*:\s*true', txt):
                claims.append(dict(ts=rts or ts, resource=paths[0]))
            continue
        denied = ''
        if err and 'CtrlB Relay' in txt:
            m = DENY.search(txt)
            denied = m.group(1) if m else 'held'
        for p in paths:
            edits.append(dict(ts=ts, tool=name, path=p, applied=not err, denied=denied))
    edits.sort(key=lambda e: e['ts'] or 0)
    claims.sort(key=lambda c: c['ts'] or 0)
    return dict(edits=edits, claims=claims, last_ts=last)


# ---------------------------------------------------------------- the orchestrator inside a trial

class Orchestrator:
    """What the harness does while the agents run, per feature arm."""

    def __init__(self, cfg, project, feat, run_id, cwd, t0):
        self.cfg, self.project, self.feat, self.run_id, self.cwd, self.t0 = cfg, project, feat, run_id, cwd, t0
        self.questions = {}      # id -> record
        self.prd_change = None   # record of the contracts change
        self.next_q = self.next_c = self.next_s = 0.0
        self.errors = []
        self.stall_seen = {}     # task key -> first reading with stalled true (stall_poll arms)
        self.stall_polls = 0

    def prepare(self):
        """Before the agents start: remove the fact (questions arms)."""
        if self.cfg.get('feature') == 'questions':
            q = self.feat['questions']
            # devbench entries scrub the fact from every doc: a list of edits, each with its own file
            for e in q.get('edits') or [dict(old=q['old'], new=q['new'])]:
                self.edit_prd(e['old'], e['new'], e.get('file'), e.get('count', 1))

    def edit_prd(self, old, new, file=None, count=1):
        path = os.path.join(self.cwd, file or self.project['prd'] or self.feat['prd'])
        with open(path) as fh:
            text = fh.read()
        if old not in text or (count != 1 and text.count(old) != count):
            raise RuntimeError(f'the PRD line to change is not in {path} ({count}x): {old[:80]}')
        with open(path, 'w') as fh:
            fh.write(text.replace(old, new, 1 if count == 1 else -1))

    def tick(self, now):
        f = self.cfg.get('feature')
        try:
            if f == 'questions' and self.cfg.get('answer') and now >= self.next_q:
                self.next_q = now + 10
                self.answer_questions(now)
            if f == 'contracts' and self.prd_change is None and now >= self.next_c:
                self.next_c = now + 5
                self.maybe_change_prd(now)
            if self.cfg.get('stall_poll') and now >= self.next_s:
                self.next_s = now + 10
                self.poll_stalls(now)
        except Exception as e:  # noqa: BLE001  (one failed poll is recorded, the trial goes on)
            self.errors.append(f'{round(now - self.t0)}s {type(e).__name__}: {str(e)[:200]}')

    def answer_questions(self, now):
        q = self.feat['questions']
        st, ans = http('GET', run_path(self.run_id, '/peers'), query={'kind': 'question', 'open': 'true',
                                                                      'limit': 200, 'agent': ORCH})
        if st != 200:
            raise RuntimeError(f'peers {st} {ans}')
        for e in ans.get('events') or []:
            qid = e.get('id')
            if not qid or qid in self.questions and self.questions[qid].get('answered_at'):
                continue
            rec = self.questions.setdefault(qid, dict(id=qid, agent=e.get('agent'), text=e.get('text'),
                                                      asked_ts=e.get('ts'), seen_at_s=round(now - self.t0, 1)))
            text = (e.get('text') or '').lower()
            rec['names_fact'] = any(k.lower() in text for k in q['keywords'])
            if rec['names_fact']:
                st, a = http('POST', run_path(self.run_id, '/answers'), {'agent': ORCH, 'id': qid, 'text': q['answer']})
                if st in (200, 201):
                    rec['answered_at'] = time.time()
                    rec['answered_at_s'] = round(rec['answered_at'] - self.t0, 1)
                else:
                    self.errors.append(f'answer {qid}: {st} {a}')

    def poll_stalls(self, now):
        """The run detail's stalled detector as of now (decision 31): the first reading of each stalled task."""
        st, d = http('GET', run_path(self.run_id), query={'from': int((self.t0 - 600) * 1e6),
                                                          'to': int((now + 60) * 1e6)})
        if st != 200:
            raise RuntimeError(f'run detail {st}')
        self.stall_polls += 1
        for x in (d.get('detectors') or {}).get('stalled') or []:
            rec = self.stall_seen.setdefault(x.get('key'), dict(task=x.get('task'), holder=x.get('holder'),
                                                                first_seen=now, first_seen_s=round(now - self.t0, 1),
                                                                last_activity=x.get('last_activity'),
                                                                last_activity_ts=x.get('last_activity_ts'),
                                                                stalled_s=x.get('stalled_s')))
            if x.get('lease_lapsed') and 'lease_lapsed_seen_s' not in rec:
                rec['lease_lapsed_seen_s'] = round(now - self.t0, 1)

    def maybe_change_prd(self, now):
        c = self.feat['contracts']
        trigger = None
        if 'contract' not in self.cfg.get('disallow', ()):
            st, ans = http('GET', run_path(self.run_id, '/contracts'))
            if st == 200 and ans.get('contracts'):
                first = min(ans['contracts'], key=lambda x: x.get('ts') or 0)
                trigger = dict(by='first contract', contract=first.get('name'), agent=first.get('agent'))
        else:
            st, ans = http('GET', run_path(self.run_id, '/tasks'))
            for t in (ans.get('tasks') or []) if st == 200 else []:
                if t.get('state') == 'finished' and any(p.endswith(c['trigger_file']) for p in t.get('paths') or []):
                    trigger = dict(by='trigger task finished', task=t.get('name'), agent=t.get('finished_by'))
            if trigger is None and now - self.t0 >= c.get('fallback_s', 180):
                trigger = dict(by=f"fallback {c.get('fallback_s', 180)} s")
        if trigger is None:
            return
        self.edit_prd(c['old'], c['new'], c.get('file'))
        for e in c.get('edits') or []:  # the matching lines in the other docs, so they do not contradict it
            self.edit_prd(e['old'], e['new'], e.get('file'), e.get('count', 1))
        prd = c.get('file') or self.project['prd'] or self.feat['prd']
        also = sorted({e['file'] for e in c.get('edits') or []})
        st = post_note(self.run_id, f'NOTE from the orchestrator: the PRD changed. In {prd}, the line "{c["old"]}" now '
                                    f'reads "{c["new"]}".' + (f' The matching lines in {", ".join(also)} changed too.'
                                                              if also else '') + ' Check anything built on it.')
        self.prd_change = dict(trigger, at=time.time(), at_s=round(time.time() - self.t0, 1), note_status=st)


# ---------------------------------------------------------------- one trial

def trial(arm, spec, k, model, budget_min, max_turns):
    cfg, project = ARMS[arm], load_project(spec)
    feat = features(project['name'])
    if cfg.get('feature') in ('questions', 'contracts') and cfg['feature'] not in feat:
        sys.exit(f"{FEATURES} has no {cfg['feature']} entry for project {project['name']}")
    n = cfg['agents']
    agents = [f'agent-{i + 1}' for i in range(n)]
    run_id = f"rp-{arm}-{project['name']}-{model}-{k:02d}-{uuid.uuid4().hex[:6]}"
    cwd = tempfile.mkdtemp(prefix=f'relay-proof-{arm}-{k:02d}-')
    home = tempfile.mkdtemp(prefix='ctrlb-home-')
    shutil.copytree(project['tree'], cwd, dirs_exist_ok=True)
    files = [p for _, p, _ in project['tasks']]
    res = dict(arm=arm, project=project['name'], model=model, trial=k, run_id=run_id, agents=n, dir=cwd,
               budget_s=budget_min * 60, max_turns=max_turns, dataset=DS)
    procs = []
    try:
        res['settings'] = put_settings(dict(cfg.get('settings') or {}, **({'evidenceRequired': True} if EVIDENCE_REQUIRED else {})))
        if EVIDENCE_REQUIRED:
            st_, cur_ = http('GET', '/ai/relay/settings', cred='SEEKER_JWT')
            res['evidence_required'] = cur_.get('evidenceRequired') if st_ == 200 else None
            res['stall_after_seconds'] = cur_.get('stallAfterSeconds') if st_ == 200 else None
        tests = {}
        if TASK_TESTS:
            tests = task_tests(project)
            give_task_tests(project, cwd, tests)
            res['task_tests'] = {name: cmd for name, (cmd, _) in tests.items()}
            res['task_tests_missing'] = sorted(name for name, _, _ in project['tasks'] if name not in tests)
        ask = {}
        if cfg.get('ask_about') and feat.get('questions'):
            q = feat['questions']
            holders = set(q.get('holders') or [])
            text = q.get('ask_about') or ('the default reading speed (words per minute) used when none is given: '
                                          'ask the orchestrator, it is not in the docs')
            ask = {name: [text] for name, path, _ in project['tasks'] if path in holders}
            res['ask_about'] = ask
        res['registered'] = len(register(run_id, project['tasks'],
                                         {name: cmd for name, (cmd, _) in tests.items()}, ask)) if cfg['relay'] else 0
        orch = Orchestrator(cfg, project, feat, run_id, cwd, time.time())
        orch.prepare()
        t0 = orch.t0 = time.time()
        procs = [launch(arm, cfg, project, a, n, run_id, cwd, home, model, max_turns) for a in agents]
        deadline = t0 + budget_min * 60
        while any(p.alive() for p in procs):
            now = time.time()
            for p in procs:
                stop_at = (cfg.get('stop') or {}).get(p.name)
                if stop_at is not None and p.alive() and now - t0 >= stop_at:
                    p.stop(f'arm stop at {stop_at} s')
            if now >= deadline:
                for p in procs:
                    p.stop('budget')
                break
            orch.tick(now)
            time.sleep(1)
        wall = time.time() - t0
    finally:
        for p in procs:
            p.stop('harness exit')
            p.log.close()
        # A lane of a parallel pair leaves the arm's settings in place (the other lane runs with the same ones);
        # the queue restores BASE after both lanes end.
        res['settings_restored'] = None if os.environ.get('RELAY_PROOF_NO_RESTORE') == '1' else put_settings(
            {'evidenceRequired': False} if EVIDENCE_REQUIRED else {})
    res['wall_s'] = round(wall, 1)

    # Session rows: every arm's transcripts go up under the run id (Relay arms' Stop hook may have sent some already;
    # the uploader's offsets in CTRLB_HOME keep it from sending a line twice).
    per_agent, timeline = {}, {}
    for p in procs:
        s = parse_stream(os.path.join(cwd, f'.log-{p.name}.jsonl'))
        tpath = transcript_path(s['session_id']) if s['session_id'] else ''
        s['upload'] = upload_transcripts([tpath], run_id, p.name, home) if tpath else {'error': 'no transcript'}
        timeline[p.name] = parse_transcript(tpath, cwd, files)
        s.update(exit_s=round((p.ended or time.time()) - t0, 1), stopped_by_harness=p.stopped_by_harness,
                 returncode=p.proc.returncode)
        per_agent[p.name] = s
    qjudge = (feat.get('questions') or {}).get('judge') if cfg.get('feature') == 'questions' else None
    if qjudge and spec.startswith('devbench:'):
        # the questions arms of a counterfactual fact (readtime: default WPM 240) are judged by the judge
        # regenerated for that value (relay_proof_features.json questions.judge)
        res['score'] = rs.score(cwd, spec, judge=qjudge)
    else:
        res['score'] = project['score'](cwd)
    if cfg.get('feature') == 'questions':
        res['judge'] = qjudge if qjudge and spec.startswith('devbench:') else 'judge'
    res['unit_pass'], res['accept_pass'] = res['score'].get('unit_pass'), res['score'].get('accept_pass')
    if cfg.get('feature') == 'contracts' and spec.startswith('devbench:') and getattr(rs, 'score_contracts', None):
        # both judges: the agent's dependent modules with the old-line and the new-line provider (redone vs left
        # behind), and the agent's provider against each interface test
        res['contracts_judges'] = rs.score_contracts(cwd, spec)
        for key in ('dependent_old_pass', 'dependent_new_pass', 'provider_old_pass', 'provider_new_pass',
                    'dependent_state'):
            res[key] = res['contracts_judges'].get(key)
    if project.get('acceptance'):
        res['acceptance_note'] = project['acceptance']
    time.sleep(3)  # the last uploaded rows become readable
    st, detail = http('GET', run_path(run_id), query={'from': int((t0 - 600) * 1e6), 'to': int((time.time() + 60) * 1e6)})
    rows = lake_rows(run_id)
    res.update(analyse(cfg, project, feat, agents, per_agent, timeline, detail if st == 200 else {}, rows, orch, t0))
    # done refused for missing evidence (decision 30): Seeker writes nothing for a refusal, so count the answers the
    # agents saw (done, and a finished release) in their stream logs.
    res['evidence_refusals'] = sum(evidence_refusals(os.path.join(cwd, f'.log-{p.name}.jsonl')) for p in procs)
    res['run_detail_status'] = st
    if cfg.get('stall_poll'):
        last = {a: (timeline.get(a) or {}).get('last_ts') for a in agents}
        seen = []
        for key, r in orch.stall_seen.items():
            g = last.get(r.get('holder'))
            seen.append(dict(r, key=key, holder_last_activity_s=round(g - t0, 1) if g else None,
                             detect_after_last_activity_s=round(r['first_seen'] - g, 1) if g else None))
        res['stall'] = dict(polls=orch.stall_polls, seen=seen, agents_last_activity_s={
            a: (round(v - t0, 1) if v else None) for a, v in last.items()})
    res['abandoned'], res['abandoned_per_agent'] = abandonment(res)
    per_task = res['score'].get('per_task') or {}
    res['questions']['tasks_needing_fact_passed'] = {t: per_task.get(t) for t in res['questions']['tasks_needing_fact']}
    if st != 200:
        res['run_detail_error'] = detail
    out = os.path.join(OUT_ROOT, arm, project['name'])
    os.makedirs(out, exist_ok=True)
    path = os.path.join(out, f'{model}-{k:02d}.json')
    with open(path, 'w') as fh:
        json.dump(res, fh, indent=1)
    print(f"{arm} {project['name']} {model} #{k}: unit {res['unit_pass']} accept {res['accept_pass']} "
          f"abandoned {res['abandoned']} duplicated {res['duplicated_n']} redone_share {res['redone_share']} "
          f"edits_unheld {res['edits_unheld']} lapsed {res['edits_on_lapsed_lease']} ${res['cost_usd']} "
          f"{res['wall_s']} s -> {path}", flush=True)
    return res, path


# ---------------------------------------------------------------- measures

PROSE = dict(question=re.compile(r'\?|\bquestion\b|\bask(ing)?\b', re.I),
             contract=re.compile(r'\bcontract\b|\binterface\b|\bsignature\b|\bexports?\b', re.I),
             handoff=re.compile(r'\bhand(ing)?[ -]?off\b|\bhanded\b|\btak(e|ing) over\b|\bleft to do\b|\bremaining\b', re.I))


def similar(a, b):
    ta, tb = set(re.findall(r'[a-z0-9]+', a.lower())), set(re.findall(r'[a-z0-9]+', b.lower()))
    return bool(ta and tb) and len(ta & tb) / len(ta | tb) >= 0.6


def analyse(cfg, project, feat, agents, per_agent, timeline, detail, rows, orch, t0):
    files = [p for _, p, _ in project['tasks']]
    task_of = {p: name for name, p, _ in project['tasks']}
    applied = {a: [e for e in timeline[a]['edits'] if e['applied'] and e['path'] in task_of] for a in agents}
    written = {a: sorted({e['path'] for e in applied[a]}) for a in agents}
    editors = {f: sorted(a for a in agents if f in written[a]) for f in files}
    duplicated = sorted(f for f, s in editors.items() if len(s) > 1)

    # redone_share: a completion is (agent, task) at the agent's last applied edit of the task's file; it redoes the
    # task when another agent had completed it before this agent's first edit of the file.
    comps = []
    for a in agents:
        for f in written[a]:
            ts = [e['ts'] for e in applied[a] if e['path'] == f and e['ts']]
            if ts:
                comps.append(dict(agent=a, file=f, first=min(ts), last=max(ts)))
    redo = [c for c in comps if any(o['file'] == c['file'] and o['agent'] != c['agent'] and o['last'] < c['first']
                                    for o in comps)]
    src_written = sorted({f for a in agents for f in written[a]})

    # tool calls by kind
    tools = {}
    for a in agents:
        for name, c in per_agent[a]['tools'].items():
            kind = name.split('__')[-1] if name.startswith('mcp__') else name
            tools[kind] = tools.get(kind, 0) + c
    posts = [t for a in agents for t in per_agent[a]['posts']]
    prose = dict(total=len(posts), **{k: sum(1 for t in posts if r.search(t)) for k, r in PROSE.items()})

    # refusals
    good = [r for r in rows if 'error' not in r]
    checks, claims_ref = {}, {}
    for r in good:
        if r.get('kind') == 'CHECK':
            checks[r.get('text') or 'held'] = checks.get(r.get('text') or 'held', 0) + 1
        elif r.get('kind') == 'REFUSED':
            t = r.get('text') or ''
            why = ('finished' if 'finished' in t else 'unknown_task' if 'not a task' in t or 'unknown' in t
                   else 'held' if r.get('held_by') else 'other')
            claims_ref[why] = claims_ref.get(why, 0) + 1
    not_visible = sum(1 for r in good if r.get('kind') == 'RELEASE' and (r.get('text') or '') == 'not_visible')
    denied = {}
    for a in agents:
        for e in timeline[a]['edits']:
            if e['denied']:
                denied[e['denied']] = denied.get(e['denied'], 0) + 1
    # an edit refused by the hook, then a held claim by the same agent, then an applied edit of the same file
    after_reclaim = 0
    for a in agents:
        for e in timeline[a]['edits']:
            if not e['denied'] or not e['ts']:
                continue
            claim = next((c for c in timeline[a]['claims'] if (c['ts'] or 0) > e['ts']), None)
            if claim and any(x['applied'] and x['path'] == e['path'] and (x['ts'] or 0) > claim['ts']
                             for x in timeline[a]['edits']):
                after_reclaim += 1

    run = detail.get('run') or {}
    agents_detail = {x.get('agent'): x for x in detail.get('agents') or []}
    events = detail.get('events') or []

    # questions
    qs = detail.get('questions') or []
    ttas = sorted(round((q['answered_ts'] - q['ts']) / 1e6, 1) for q in qs if q.get('answered_ts') and q.get('ts'))
    dup_q = sum(1 for i, q in enumerate(qs) if any(similar(q.get('text') or '', o.get('text') or '') for o in qs[i + 1:]))
    need = (feat.get('questions') or {}).get('tasks_needing') or []
    questions = dict(asked=len(qs), answered=sum(1 for q in qs if q.get('answered_ts')), duplicates=dup_q,
                     time_to_answer_s=ttas, mean_time_to_answer_s=round(statistics.mean(ttas), 1) if ttas else None,
                     answered_by_harness=sum(1 for q in orch.questions.values() if q.get('answered_at')),
                     naming_fact=sum(1 for q in orch.questions.values() if q.get('names_fact')),
                     open_at_end=run.get('open_questions'), harness_log=list(orch.questions.values()),
                     tasks_needing_fact=need)

    # contracts
    cons = detail.get('contracts') or []
    contracts = dict(written=tools.get('contract', 0), listed=len(cons),
                     changed_since_nonempty=sum(1 for c in cons if c.get('changed_since')),
                     list=[{k: c.get(k) for k in ('name', 'agent', 'paths', 'changed_since', 'text')} for c in cons],
                     prd_change=orch.prd_change)
    if orch.prd_change and feat.get('contracts'):
        dep = feat['contracts'].get('dependent_files') or []
        at = orch.prd_change['at']
        contracts['dependent_redone'] = any(e['path'] in dep and e['applied'] and (e['ts'] or 0) > at
                                            for a in agents for e in timeline[a]['edits'])

    # handoffs: giver's last applied edit of each handed file before the handoff, receiver's first after it
    hand_rows = [r for r in good if r.get('kind') == 'HANDOFF']
    gaps = []
    for r in hand_rows:
        key = r.get('key') or ''
        if not key.startswith('path:'):
            continue
        f, ts = key[5:], int(r['_timestamp']) / 1e6
        giver, recv = r.get('agent'), r.get('to')
        g = [e['ts'] for e in timeline.get(giver, {}).get('edits', []) if e['applied'] and e['path'] == f and e['ts'] and e['ts'] <= ts]
        v = [e['ts'] for e in timeline.get(recv, {}).get('edits', []) if e['applied'] and e['path'] == f and e['ts'] and e['ts'] >= ts]
        gaps.append(dict(file=f, giver=giver, to=recv, giver_last_edit_s=round(max(g) - t0, 1) if g else None,
                         receiver_first_edit_s=round(min(v) - t0, 1) if v else None,
                         gap_s=round(min(v) - max(g), 1) if g and v else None))
    # without a handoff (control, or a giver that never handed off): files agent-1 edited that agent-2 edited later
    takeover = []
    if cfg.get('feature') == 'handoff' and len(agents) >= 2:
        a1, a2 = agents[0], agents[1]
        for f in written[a1]:
            g = max(e['ts'] for e in applied[a1] if e['path'] == f)
            v = [e['ts'] for e in applied[a2] if e['path'] == f and e['ts'] > g]
            takeover.append(dict(file=f, giver_last_edit_s=round(g - t0, 1),
                                 receiver_first_edit_s=round(min(v) - t0, 1) if v else None,
                                 gap_s=round(min(v) - g, 1) if v else None))
    handoffs = dict(made=len({r.get('nonce') for r in hand_rows}), rows=len(hand_rows), gaps=gaps, takeover=takeover)

    cost_known = all(per_agent[a]['cost_usd'] is not None for a in agents)
    cost = sum((per_agent[a]['cost_usd'] if per_agent[a]['cost_usd'] is not None
                else (agents_detail.get(a) or {}).get('cost_usd') or 0.0) for a in agents)
    edits_unheld, lapsed = run.get('edits_unheld'), run.get('edits_on_lapsed_lease')
    out = dict(
        src_files_written=len(src_written), src_written=src_written, editors=editors,
        duplicated=duplicated, duplicated_n=len(duplicated),
        completions=len(comps), redo_completions=[dict(agent=c['agent'], file=c['file']) for c in redo],
        redone_share=round(len(redo) / len(comps), 4) if comps else 0.0,
        edits_unheld=edits_unheld, edits_on_lapsed_lease=lapsed,
        edit_attempts_denied=denied,
        edits_unheld_applied=None if edits_unheld is None else max(0, edits_unheld - denied.get('unheld', 0)),
        edits_on_lapsed_lease_applied=None if lapsed is None else max(0, lapsed - denied.get('lapsed', 0)),
        edits_after_refusal_and_reclaim=after_reclaim,
        tool_calls=tools, prose_notes=prose,
        questions=questions, contracts=contracts, handoffs=handoffs,
        refusals=dict(checks_by_reason=checks, claims_refused=claims_ref, not_visible=not_visible,
                      hook_denials_seen_by_agents=denied),
        holds=sum(1 for r in good if r.get('kind') in ('HOLD', 'REOPEN')),
        done_rows=sum(1 for r in good if r.get('kind') == 'DONE'),
        relay_rows=len(good), relay_rows_error=[r['error'] for r in rows if 'error' in r],
        run_detail_edit_events=sum(1 for e in events if e.get('kind') == 'edit'),
        run_detail_check_events=sum(1 for e in events if e.get('kind') == 'check'),
        run_detail_resources_held=sum(1 for r in detail.get('resources') or [] if r.get('holds')),
        wait_s_total=run.get('wait_s_total'), hot_keys=detail.get('hot_keys') or [],
        served_waits=run.get('served_waits'), served_wait_s_total=run.get('served_wait_s_total'),
        timed_out_waits=len({r.get('nonce') for r in good if r.get('kind') == 'RELEASE'
                             and (r.get('text') or '') == 'timed_out'}),
        claims_with_wait=sum(per_agent[a].get('claims_with_wait', 0) for a in agents),
        claims_held=run.get('claims_held'), claims_refused=run.get('claims_refused'),
        done_with_task_test=run.get('done_with_task_test'),
        relay_rows_compact=[{k: r.get(k) for k in ('_timestamp', 'agent', 'kind', 'key', 'nonce', 'held_by', 'to')}
                            | {'text': (r.get('text') or '')[:200]} for r in good],
        # decisions 30 and 31 (None from a Seeker without them)
        done_with_evidence=run.get('done_with_evidence'), done_without=run.get('done_without'),
        stalled_tasks=run.get('stalled_tasks'), stalled=(detail.get('detectors') or {}).get('stalled'),
        finished_tasks=[{k: t.get(k) for k in ('name', 'finished_by', 'evidence', 'round')}
                        for t in detail.get('tasks') or [] if t.get('state') == 'finished'],
        finished_with_command_and_exit=sum(1 for t in detail.get('tasks') or [] if t.get('state') == 'finished'
                                           and (t.get('evidence') or {}).get('command')
                                           and (t.get('evidence') or {}).get('exit_code') is not None),
        run_detail_summary=run,
        run_detail_agents={a: {k: v for k, v in x.items() if k in ('edits_unheld', 'edits_on_lapsed_lease', 'claims_held',
                                                                     'claims_refused', 'wait_s', 'tool_calls', 'turns',
                                                                     'cost_usd')}
                           for a, x in agents_detail.items()},
        cost_usd=round(cost, 4), cost_known_all=cost_known,
        turns=sum(per_agent[a]['turns'] for a in agents),
        per_agent={a: dict(per_agent[a], files_written=written[a], posts=len(per_agent[a]['posts'])) for a in agents},
        orchestrator_errors=orch.errors)
    return out


def abandonment(res):
    """AgentRoom Tier I adapted to tests: fewer than 3 source files written, or every agent exited on its own before
    200 s with unit pass under 30%. Per agent: the same with the agent's own files and exit."""
    unit = res.get('unit_pass') or 0.0
    per = {}
    for a, s in res['per_agent'].items():
        early = not s.get('stopped_by_harness') and (s.get('exit_s') or 0) < 200
        per[a] = len(s.get('files_written') or []) < 3 or (early and unit < 0.3)
    all_early = all(not s.get('stopped_by_harness') and (s.get('exit_s') or 0) < 200 for s in res['per_agent'].values())
    trial_ab = res['src_files_written'] < 3 or (all_early and unit < 0.3)
    return trial_ab, per


# ---------------------------------------------------------------- summary

NUMERIC = ('unit_pass', 'accept_pass', 'duplicated_n', 'redone_share', 'edits_unheld', 'edits_on_lapsed_lease',
           'edits_unheld_applied', 'edits_on_lapsed_lease_applied', 'edits_after_refusal_and_reclaim', 'cost_usd',
           'wall_s', 'turns', 'src_files_written', 'holds', 'dependent_old_pass', 'dependent_new_pass',
           'provider_old_pass', 'provider_new_pass', 'done_with_evidence', 'done_without', 'stalled_tasks',
           'evidence_refusals', 'finished_with_command_and_exit', 'served_waits', 'served_wait_s_total',
           'wait_s_total', 'timed_out_waits', 'claims_with_wait', 'claims_held', 'claims_refused',
           'done_with_task_test')


def stats(xs):
    xs = [x for x in xs if isinstance(x, (int, float)) and not isinstance(x, bool)]
    if not xs:
        return None
    return dict(n=len(xs), mean=round(statistics.mean(xs), 4), sd=round(statistics.stdev(xs), 4) if len(xs) > 1 else 0.0,
                min=min(xs), max=max(xs))


def summarise(arm):
    files = sorted(glob.glob(os.path.join(OUT_ROOT, arm, '*', '*.json')))
    trials = [json.load(open(f)) for f in files if not f.endswith('summary.json')]
    groups = {}
    for t in trials:
        groups.setdefault((t['project'], t['model']), []).append(t)
        groups.setdefault(('ALL', t['model']), []).append(t)
    out = dict(arm=arm, trials=len(trials), groups={})
    for (proj, model), ts in sorted(groups.items()):
        g = {k: stats([t.get(k) for t in ts]) for k in NUMERIC}
        g.update(n=len(ts), abandonment_rate=round(sum(bool(t.get('abandoned')) for t in ts) / len(ts), 4),
                 questions_asked=stats([t['questions']['asked'] for t in ts]),
                 questions_answered=stats([t['questions']['answered'] for t in ts]),
                 time_to_answer_s=stats([x for t in ts for x in t['questions']['time_to_answer_s']]),
                 contracts_written=stats([t['contracts']['written'] for t in ts]),
                 contracts_changed_since=stats([t['contracts']['changed_since_nonempty'] for t in ts]),
                 handoffs=stats([t['handoffs']['made'] for t in ts]),
                 tool_calls_per_trial={k: round(sum(t['tool_calls'].get(k, 0) for t in ts) / len(ts), 2)
                                       for k in sorted({k for t in ts for k in t['tool_calls']})},
                 refusals_checks={k: sum(t['refusals']['checks_by_reason'].get(k, 0) for t in ts)
                                  for k in sorted({k for t in ts for k in t['refusals']['checks_by_reason']})},
                 hook_denials={k: sum(t['refusals']['hook_denials_seen_by_agents'].get(k, 0) for t in ts)
                               for k in sorted({k for t in ts for k in t['refusals']['hook_denials_seen_by_agents']})},
                 cost_total=round(sum(t['cost_usd'] for t in ts), 4))
        out['groups'][f'{proj}/{model}'] = g
    os.makedirs(os.path.join(OUT_ROOT, arm), exist_ok=True)
    with open(os.path.join(OUT_ROOT, arm, 'summary.json'), 'w') as fh:
        json.dump(out, fh, indent=1)
    return out


# ---------------------------------------------------------------- main

def main():
    if len(sys.argv) > 1 and sys.argv[1] == '_upload':
        return _upload_main(sys.argv[2:])
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('arm', nargs='?', choices=sorted(ARMS))
    ap.add_argument('--project', default='toy')
    ap.add_argument('--trials', type=int, default=1)
    ap.add_argument('--model', default='haiku', choices=('haiku', 'sonnet'))
    ap.add_argument('--budget-min', type=float, default=15)
    ap.add_argument('--max-turns', type=int, default=200)
    ap.add_argument('--out', help='also copy the last trial file here (the smoke run)')
    ap.add_argument('--summary', metavar='ARM')
    ap.add_argument('--first', type=int, help='first trial number (a lane of a parallel pair); default: after the last')
    ap.add_argument('--evidence-required', action='store_true',
                    help='Seeker decision 30: set evidenceRequired true for these trials (false again after each) and '
                         'allow the done tool; the trial records evidence_required and evidence_refusals')
    ap.add_argument('--task-tests', action='store_true',
                    help='Seeker decision 32: register each task with its test command (the task set\'s unit test of '
                         'its module, python3 -m unittest <file>), copy DevBench\'s test files into the trial dir and '
                         'name them in TASKS.md; the trial records task_tests and task_tests_missing')
    ap.add_argument('--lane', default='', help='lane name of a parallel pair: its own lock file; only arms with '
                    'identical relay settings may run as two lanes at once, with RELAY_PROOF_NO_RESTORE=1')
    a = ap.parse_args()
    global EVIDENCE_REQUIRED, TASK_TESTS
    EVIDENCE_REQUIRED = a.evidence_required
    TASK_TESTS = a.task_tests
    if a.summary:
        print(json.dumps(summarise(a.summary), indent=1))
        return
    if not a.arm:
        ap.error('an arm is required')
    for var in ('SEEKER_MCP_KEY', 'CTRLB_KEY', 'SEEKER_JWT'):
        if not os.environ.get(var):
            sys.exit(f'{var} is not set')
    os.makedirs(OUT_ROOT, exist_ok=True)
    lock = open(os.path.join(OUT_ROOT, '.relay_proof.lock' + (f'.{a.lane}' if a.lane else '')), 'w')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)  # settings are per workspace: one trial at a time
    except OSError:
        sys.exit('another relay_proof.py is running (settings are shared per workspace)')
    name = load_project(a.project)['name']
    done = glob.glob(os.path.join(OUT_ROOT, a.arm, name, f'{a.model}-*.json'))
    start = (a.first - 1) if a.first else max([int(os.path.basename(f)[len(a.model) + 1:-5]) for f in done] or [0])
    path = None
    for k in range(start + 1, start + a.trials + 1):
        res, path = trial(a.arm, a.project, k, a.model, a.budget_min, a.max_turns)
    summarise(a.arm)
    if a.out and path:
        shutil.copyfile(path, a.out)


if __name__ == '__main__':
    main()
