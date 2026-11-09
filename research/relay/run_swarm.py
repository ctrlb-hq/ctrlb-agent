"""Run a swarm trial: N Claude Code agents (claude -p, Haiku) in one copy of the task template at the same time.

Arms:
  none   no shared board; agents see each other only through the files in the directory
  board  peers/post on the lake: a rule says to check the board and post what you take
  claim  peers/post/claim/release: a rule says to claim a task before working on it and release it after
  claim-files  the same tools with the rule CtrlB's agent package injects in the field (RELAY in
         ctrlb-agent lib/hooks_shared.py, read from FIELD_RULES): claim the file path the task names, release files,
         finish task names. {run} and {agent} are filled in per agent.
  claim-briefs  claim's rule and tools plus brief/briefs and the briefs paragraph of ctrlb-agent docs/relay-tools.md

Task sources (--tasks): the toy template (default: 12 independent functions, template/) or a DevBench project
(devbench:<project>, built by devbench/tasksets.py: one task per source module, the PRD and design docs as read-only
context, the reference unit and acceptance tests kept out of the agents' tree and copied in only for scoring).

Measures per trial: tasks completed (tests pass), task files edited by more than one agent (duplicated work),
REGISTRY.md lines present vs tasks completed (lost lines on the shared file), cost, wall time, turns; unit_pass and
accept_pass (pass rates of the reference unit and acceptance tests; the toy template has no acceptance tests, so its
accept_pass is its unit_pass); redone_share (grite, arXiv 2606.19616: the share of task completions that redo a task
another agent had already finished; a completion is an agent's applied edits of a task's file, it is a redo when
another agent's last edit of that file came before this agent's first).
Usage: python3 -I run_swarm.py <arm> <trials> [agents=4] [max_turns=60] [--tasks toy|devbench:<project>]
       [--budget-min M]
  -> <SWARM_OUT>/<arm>-<trial>.json (toy) or <arm>-<project>-<trial>.json (devbench). Without --budget-min each
  agent is waited for up to 30 minutes, as before; with it, all agents stop at M minutes after the start.
Env: RELAY_TEMPLATE (the toy template dir, TASKS.md + src/ + tests/; default template/ next to this file, not shipped),
SWARM_OUT (results; default results/swarm next to this file), FIELD_RULES (the field rule; default this repo's
lib/hooks_shared.py), CLAUDE_PROJECTS (default ~/.claude/projects), CTRLB_URL / CTRLB_AUTH (the lake, passed to
swarm_mcp.py), and devbench/tasksets.py's DEVBENCH_* for devbench task sets.
"""
import ast, calendar, glob, json, os, re, shutil, subprocess, sys, tempfile, time, uuid

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.environ.get('RELAY_TEMPLATE') or os.path.join(HERE, 'template')
CLAUDE_PROJECTS = os.environ.get('CLAUDE_PROJECTS') or os.path.expanduser('~/.claude/projects')
OUT = os.environ.get('SWARM_OUT') or os.path.join(HERE, 'results', 'swarm')
TOOLS_BY_ARM = {'none': '', 'solo': '', 'board': 'peers,post', 'claim': 'peers,post,claim,release',
                'claim-files': 'peers,post,claim,release,tasks',
                'claim-briefs': 'peers,post,claim,release,brief,briefs'}
BRIEFS_RULE = ('Before reading a file or document that other agents of the run may also need, call briefs for it and read '
               'the brief first. After reading a file you will edit, or one that others need, post a brief for it with '
               'up to a few hundred words: what it is for, what it exports or decides, and what not to touch.')
FIELD_RULES = os.environ.get('FIELD_RULES') or os.path.join(HERE, '..', '..', 'lib', 'hooks_shared.py')
# Arms whose trials register the template's tasks before the agents start (REGISTER, set by a harness that serves
# them: run_swarm_seeker.py posts each to Seeker as a task brief with its file).
REGISTER_ARMS = ('claim-files',)
REGISTER = None
TASK_LINE = re.compile(r'^- (task \d\d) `(\w+)`:.*File: (\S+?),', re.M)
# The task source of this process: None for the toy template, else load_taskset(spec) (set by use_tasks).
TASKSET = None
DEFAULT_WAIT_S = 1800


def load_taskset(spec):
    """A task source as a dict: name, tree (copied as the trial dir; no judge tests in it), tasks [(task name, its
    file, the task line)], prd (relpath of the doc feature arms edit), prompt ({agent} {n}), src_files."""
    if spec in (None, '', 'toy', 'template'):
        return dict(name='toy', spec='toy', tree=TEMPLATE, tasks=template_tasks(TEMPLATE), prd='TASKS.md', prompt=TOY_PROMPT,
                    src_files=[p for _, p, _ in template_tasks(TEMPLATE)])
    if spec.startswith('devbench:'):
        sys.path.insert(0, os.path.join(HERE, 'devbench'))
        import tasksets
        return dict(tasksets.load(spec), spec=spec)
    raise SystemExit(f'unknown task source {spec!r}: toy or devbench:<project>')


def score(cwd, spec, judge=None):
    """Reference tests of the task source against a trial tree: {unit_pass, accept_pass, per_task, ...}. DevBench
    copies its judge over a scratch copy of the tree and runs it with the scoring venv (timeout per suite)."""
    if spec in (None, '', 'toy', 'template'):
        per = {}
        for _, path, _ in template_tasks(TEMPLATE):
            t = os.path.basename(path)[:-3]
            try:
                r = subprocess.run([sys.executable, '-B', '-m', 'unittest', f'tests.test_{t}'], cwd=cwd,
                                   capture_output=True, text=True, timeout=60)
                per[t] = r.returncode == 0
            except subprocess.TimeoutExpired:
                per[t] = False
        unit = round(sum(per.values()) / len(per), 4) if per else None
        return dict(unit_pass=unit, accept_pass=unit, per_task=per,
                    acceptance='proxy: the toy template has no acceptance tests; accept_pass = unit_pass')
    sys.path.insert(0, os.path.join(HERE, 'devbench'))
    import tasksets
    return tasksets.score(cwd, spec, judge)


def score_contracts(cwd, spec):
    """The contracts arm's two judges for a devbench trial tree (devbench/contracts.py): dependent_old_pass /
    dependent_new_pass and provider_old_pass / provider_new_pass. Main-arm scoring (score) does not use it."""
    sys.path.insert(0, os.path.join(HERE, 'devbench'))
    import contracts
    return contracts.score(cwd, spec.split(':', 1)[1])


def use_tasks(spec):
    """Make spec this process's task source (trial, analyse and launch read it; harnesses that import this module
    and read PROMPT at call time get the task source's prompt)."""
    global TASKSET, PROMPT
    if spec in (None, '', 'toy', 'template'):
        TASKSET = None
        PROMPT = TOY_PROMPT
    else:
        TASKSET = load_taskset(spec)
        PROMPT = TASKSET['prompt']
    return TASKSET


def template_tasks(cwd=TEMPLATE):
    """[(name as TASKS.md gives it, e.g. 'task 03 median', its src file, the task line)] of a task directory."""
    with open(os.path.join(cwd, 'TASKS.md')) as fh:
        text = fh.read()
    return [(f'{m.group(1)} {m.group(2)}', m.group(3), m.group(0)[2:]) for m in TASK_LINE.finditer(text)]


def field_rule(name='RELAY'):
    """A string constant of the agent package's hooks_shared.py, read without importing it; or, when FIELD_RULES is a
    .txt file (a saved version under results/*/rule-versions/), that file's text."""
    with open(FIELD_RULES) as fh:
        if FIELD_RULES.endswith('.txt'):
            return fh.read().strip()
        tree = ast.parse(fh.read())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, 'id', '') == name for t in node.targets):
            return ast.literal_eval(node.value)
    raise KeyError(name)


def rule(arm, run_id, agent):
    """RULES[arm] for one agent: the field rule's {run} and {agent} filled in."""
    text = RULES[arm]
    return text.replace('{run}', run_id).replace('{agent}', agent) if '{run}' in text else text


RULES = {
    'none': 'Other agents are working in this same directory right now. Before starting a task, look at REGISTRY.md '
            'and the task file to see whether it is already done or in progress, and pick one that is not.',
    'board': 'Other agents are working in this same directory right now and share a board with you (the swarm tools). '
             'Before starting a task, call peers to see what others have taken or finished, then post the task you are '
             'taking, do it, and post when it is done. Never take a task another agent has posted.',
    'claim': 'Other agents are working in this same directory right now and share a board with you (the swarm tools). '
             'Before starting a task, call claim with the task name; work on it only if held is true, otherwise claim '
             'a different task. When its test passes and REGISTRY.md is updated, call release with finished=true.',
    'claim-files': field_rule(),
    'solo': '',  # one agent, no board and no rule (AgentRoom's Solo baseline)
}
RULES['claim-briefs'] = RULES['claim'] + ' ' + BRIEFS_RULE
PROMPT = ('You are {agent}, one of {n} agents working at the same time in this directory. Read TASKS.md and complete as '
          'many tasks as you can, one at a time: implement the function in its src file, run its unittest until it '
          'passes, then append your line to REGISTRY.md. Change files with the Edit or Write tool, not from Bash. '
          'Stop when every task is done. Reply with the list of tasks you completed.')
TOY_PROMPT = PROMPT
WRITE_RE = re.compile(r'(>>?\s*\S*src/\w+\.py|\btee\b[^|;&]*src/\w+\.py|sed -i[^|;&]*src/\w+\.py|python3? -c)')


def launch(arm, agent, n, run_id, cwd, max_turns):
    env = dict(os.environ, SWARM_RUN=run_id, SWARM_AGENT=agent, SWARM_TOOLS=TOOLS_BY_ARM[arm])
    env.pop('CLAUDECODE', None)
    cmd = ['claude', '-p', PROMPT.format(agent=agent, n=n), '--model', 'haiku', '--output-format', 'stream-json', '--verbose',
           '--max-turns', str(max_turns)] + (['--append-system-prompt', rule(arm, run_id, agent)] if RULES[arm] else []) + [
           '--allowedTools', 'Bash,Read,Edit,Write,MultiEdit,Glob,Grep,mcp__swarm__peers,mcp__swarm__post,'
                             'mcp__swarm__claim,mcp__swarm__release,mcp__swarm__brief,mcp__swarm__briefs']
    if arm not in ('none', 'solo'):
        mcp = os.path.join(cwd, f'.mcp-{agent}.json')
        with open(mcp, 'w') as fh:
            json.dump({'mcpServers': {'swarm': {'command': 'python3', 'args': ['-I', os.path.join(HERE, 'swarm_mcp.py')],
                                                'env': {'SWARM_RUN': run_id, 'SWARM_AGENT': agent,
                                                        'SWARM_TOOLS': TOOLS_BY_ARM[arm],
                                                        'CTRLB_URL': os.environ.get('CTRLB_URL', 'http://127.0.0.1:5160'),
                                                        'CTRLB_AUTH': os.environ.get('CTRLB_AUTH', '')}}}}, fh)
        cmd += ['--mcp-config', mcp, '--strict-mcp-config']
    elif arm == 'solo':
        cmd += ['--strict-mcp-config']  # no MCP servers at all
    log = open(os.path.join(cwd, f'.log-{agent}.jsonl'), 'w')
    return subprocess.Popen(cmd, cwd=cwd, env=env, stdout=log, stderr=subprocess.STDOUT, text=True), log


def ts_of(s):
    """ISO timestamp of a transcript line -> epoch seconds."""
    if not s:
        return None
    try:
        main, _, frac = s.rstrip('Z').partition('.')
        return calendar.timegm(time.strptime(main, '%Y-%m-%dT%H:%M:%S')) + (float('0.' + frac) if frac else 0.0)
    except ValueError:
        return None


def session_of(log):
    with open(log, errors='replace') as fh:
        for line in fh:
            try:
                m = json.loads(line)
            except ValueError:
                continue
            if m.get('type') == 'system' and m.get('session_id'):
                return m['session_id']
    return ''


def timed_edits(cwd, agent, files):
    """[(ts, task file)] of the agent's applied edits of task files (Edit/Write/MultiEdit, or a Bash write naming the
    file), from its Claude Code transcript (the stream log has no timestamps); None when there is no transcript."""
    sid = session_of(os.path.join(cwd, f'.log-{agent}.jsonl'))
    hits = glob.glob(os.path.join(CLAUDE_PROJECTS, '*', sid + '.jsonl')) if sid else []
    if not hits:
        return None
    real, calls, errs = os.path.realpath(cwd), {}, set()
    with open(hits[0], errors='replace') as fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            msg = rec.get('message')
            if rec.get('type') not in ('assistant', 'user') or not isinstance(msg, dict):
                continue
            ts = ts_of(rec.get('timestamp'))
            for b in msg.get('content') or []:
                if not isinstance(b, dict):
                    continue
                if b.get('type') == 'tool_use':
                    name, inp = b.get('name') or '', b.get('input') or {}
                    if name in ('Edit', 'Write', 'MultiEdit'):
                        fp = inp.get('file_path') or ''
                        rel = os.path.relpath(os.path.realpath(fp), real) if os.path.isabs(fp) else os.path.normpath(fp)
                        if rel in files:
                            calls[b.get('id')] = (ts, [rel])
                    elif name == 'Bash':
                        cmd = inp.get('command') or ''
                        hit = [f for f in files if bash_writes(cmd, f, cwd)]
                        if hit:
                            calls[b.get('id')] = (ts, hit)
                elif b.get('type') == 'tool_result' and b.get('is_error'):
                    errs.add(b.get('tool_use_id'))
    return sorted((ts, f) for cid, (ts, fs) in calls.items() if cid not in errs and ts for f in fs)


def redone(edits):
    """grite's redone share from {agent: [(ts, task file)] applied edits}: a completion is (agent, file) spanning the
    agent's first to last edit of the file; it redoes the task when another agent's completion of the same file ended
    before it began. Same rule as relay_proof.analyse."""
    comps = []
    for a, es in edits.items():
        for f in sorted({f for _, f in es}):
            ts = [t for t, g in es if g == f]
            comps.append(dict(agent=a, file=f, first=min(ts), last=max(ts)))
    redo = [c for c in comps if any(o['file'] == c['file'] and o['agent'] != c['agent'] and o['last'] < c['first']
                                    for o in comps)]
    return dict(completions=len(comps), redo_completions=[dict(agent=c['agent'], file=c['file']) for c in redo],
                redone_share=round(len(redo) / len(comps), 4) if comps else 0.0)


PY_C = re.compile(r"""\bpython[\d.]*\s+(?:-[A-Za-z]+\s+)*-c\s+(?:"((?:[^"\\]|\\.)*)"|'([^']*)'|\$'((?:[^'\\]|\\.)*)')""")
PY_HEREDOC = re.compile(r"""\bpython[\d.]*(?:\s+-)?\s*<<-?\s*['"]?(\w+)['"]?[^\n]*\n(.*?)\n\s*\1\b""", re.S)
MODE_W = r"""\s*,\s*(?:mode\s*=\s*)?(?:r|b|f)?['"][rbt]*[wax+]"""


def py_writes(prog, f):
    """Python source that writes file f: open(f, 'w'/'a'/'x'/'+' mode), Path(f).write_text/write_bytes/open('w'),
    shutil.copy*/move(..., f), os.replace/os.rename(..., f); f given as a string literal containing it, or through a
    variable assigned one."""
    q = r"""(?:[rfb]{1,2})?['"][^'"]*""" + re.escape(f) + r"""['"]"""
    lit = r'(?:(?:\w+\.)?Path\(\s*' + q + r'\s*\)|' + q + ')'
    names = [m.group(1) for m in re.finditer(r'\b(\w+)\s*=\s*' + lit, prog)]
    second = r'\([^,()]*(?:\([^()]*\))?[^,()]*,\s*'
    for t in [lit] + [r'\b' + re.escape(n) + r'\b' for n in names]:
        if re.search(r'\bopen\(\s*' + t + MODE_W, prog) or \
                re.search(t + r'\s*\.\s*(?:write_text|write_bytes)\s*\(', prog) or \
                re.search(t + r"""\s*\.\s*open\(\s*(?:mode\s*=\s*)?['"][rbt]*[wax+]""", prog) or \
                re.search(r'\bPath\(\s*' + t + r'\s*\)\s*\.\s*(?:write_text|write_bytes)\s*\(', prog) or \
                re.search(r'\bshutil\.(?:copy\w*|move)' + second + t, prog) or \
                re.search(r'\bos\.(?:replace|rename)' + second + t, prog):
            return True
    return False


CD_RE = re.compile(r'(?:^|[;&|(\n]|&&|\|\|)\s*cd(?:\s+([^\s;&|)]+))?')
ASSIGN_RE = re.compile(r'(?:^|[\s;&|(])(?:export\s+)?([A-Za-z_]\w*)=([^\s;&|]+)')
PREFIX_RE = re.compile(r'([^\s\'"<>|;&()=,]*)$')
PWD_VARS = ('$PWD', '${PWD}', '$(pwd)', '`pwd`')


def _expand(tok, here, env):
    """A path token of a Bash command with $PWD/$(pwd) and variables assigned earlier in the command expanded;
    None when it still names an unknown variable or command substitution."""
    tok = tok.strip('\'"')
    for v in PWD_VARS:
        tok = tok.replace(v, here or '')
    for name, val in sorted(env.items(), key=lambda kv: -len(kv[0])):
        tok = tok.replace('${' + name + '}', val).replace('$' + name, val)
    if '$' in tok or '`' in tok:
        return None
    if tok.startswith('~'):
        tok = os.path.expanduser(tok)
    return tok


def _occurrences_inside(cmd, f, cwd):
    """For each occurrence of the relative path f in cmd: whether it names the trial dir's f. The directory in force
    at an occurrence is the session cwd (the trial dir) changed by every `cd` before it (absolute, relative, or a
    variable assigned earlier in the command); a prefix before f (`/tmp/x/`, `$S/`, `../copy/`) is resolved against
    that directory. An unknown directory or an unresolved variable is not the trial dir."""
    roots = {os.path.normpath(cwd), os.path.realpath(cwd)}
    target = os.path.normpath(os.path.join(cwd, f))
    targets = {target, os.path.join(os.path.realpath(cwd), f)}
    events = [(m.start(), 'cd', m.group(1)) for m in CD_RE.finditer(cmd)]
    events += [(m.start(), 'set', (m.group(1), m.group(2))) for m in ASSIGN_RE.finditer(cmd)]
    events.sort(key=lambda e: e[0])
    out = []
    for m in re.finditer(re.escape(f), cmd):
        if m.end() < len(cmd) and re.match(r'[\w]', cmd[m.end()]):
            out.append(None)  # f is part of a longer name (utils.py.bak, utils.pyc)
            continue
        here, env = os.path.normpath(cwd), {}
        for pos, kind, arg in events:
            if pos >= m.start():
                break
            if kind == 'set':
                v = _expand(arg[1], here, env)
                if v is not None:
                    env[arg[0]] = v
                else:
                    env.pop(arg[0], None)
            elif arg is None or arg in ('~', '-'):
                here = None if arg == '-' else os.path.expanduser('~')
            elif here is not None or os.path.isabs(arg):
                d = _expand(arg, here, env)
                here = None if d is None else os.path.normpath(os.path.join(here or '/', d))
        pre = PREFIX_RE.search(cmd[:m.start()]).group(1)
        if pre and not pre.endswith('/'):
            out.append(None)  # f is the tail of another name (xreadtime/utils.py)
            continue
        p = _expand(pre, here, env) if pre else ''
        if p is None or (here is None and not os.path.isabs(p)):
            out.append(False)
            continue
        full = os.path.normpath(os.path.join(here or '/', p, f))
        out.append(full in targets or os.path.realpath(full) in targets)
    return out


def bash_writes(cmd, f, cwd=None):
    """A Bash command that writes task file f: a redirect into it, tee, sed -i, cp/mv/install onto it, or inline
    Python (python -c '...' or a python heredoc) whose program writes it (py_writes). Reading it (cat, grep, head,
    sed without -i), python -m pytest, or an import never count. With cwd (the trial dir, the session's cwd): each
    occurrence of f is resolved against the directory in force there (cd, variables assigned in the command, $PWD) and
    only an occurrence that names the trial dir's file can count, so a write into a scratch copy elsewhere (cd /tmp/x;
    cat > f, or cat > $S/f) does not (round 5, benchmark item 1)."""
    if f not in cmd:
        return False
    if cwd and not os.path.isabs(f):
        inside = _occurrences_inside(cmd, f, cwd)
        if not any(inside):
            return False
        # Mask the occurrences that name another directory's copy, so only the trial file's can match below.
        parts, last, i = [], 0, 0
        for m in re.finditer(re.escape(f), cmd):
            if not inside[i]:
                parts.append(cmd[last:m.start()] + '\x00' * len(f))
                last = m.end()
            i += 1
        cmd = ''.join(parts) + cmd[last:]
    e = re.escape(f)
    if re.search(r'(?<![-=<])>>?\s*\S*' + e + r'(?![\w./])', cmd) or re.search(r'\btee\b[^|;&\n]*' + e, cmd) or \
            re.search(r'\bsed\s+(?:-\w*\s+)*-i[^|;&\n]*' + e, cmd) or \
            re.search(r'\b(?:cp|mv|install)\b[^|;&\n]*\s\S*' + e + r'\s*(?:$|[|;&\n])', cmd):
        return True
    progs = [next(g for g in m.groups() if g is not None) for m in PY_C.finditer(cmd)]
    progs += [m.group(2) for m in PY_HEREDOC.finditer(cmd)]
    return any(f in p and py_writes(p.replace('\\"', '"'), f) for p in progs)


def analyse(cwd, agents):
    spec = TASKSET['spec'] if TASKSET else 'toy'
    sc = score(cwd, spec)
    if TASKSET is None:
        task_names = [os.path.basename(p)[:-3] for p in sorted(glob.glob(os.path.join(TEMPLATE, 'src', '*.py')))
                      if not p.endswith('__init__.py')]
        files = {t: f'src/{t}.py' for t in task_names}
        passed = {t: bool(sc['per_task'].get(t)) for t in task_names}
    else:
        files = {os.path.basename(p)[:-3]: p for _, p, _ in TASKSET['tasks']}
        task_names = list(files)
        passed = {t: sc.get('per_task', {}).get(t) for t in task_names}  # None: no reference test exercises it
    editors, per_agent = {t: set() for t in task_names}, {}
    for a in agents:
        turns, cost, tools, claims_ok, claims_no = 0, 0.0, {}, 0, 0
        attempts, errored = {}, set()  # tool_use id -> task names it would edit; ids whose result is an error
        with open(os.path.join(cwd, f'.log-{a}.jsonl')) as fh:
            for line in fh:
                try:
                    m = json.loads(line)
                except Exception:
                    continue
                if m.get('type') == 'assistant':
                    turns += 1
                    for b in (m.get('message') or {}).get('content') or []:
                        if b.get('type') == 'tool_use':
                            name = b.get('name', '')
                            tools[name] = tools.get(name, 0) + 1
                            inp = b.get('input') or {}
                            fp, cmd = inp.get('file_path') or '', inp.get('command') or ''
                            for t in task_names:
                                if (fp.endswith(files[t]) and name in ('Edit', 'Write', 'MultiEdit')) or \
                                        (name == 'Bash' and files[t] in cmd and
                                         (WRITE_RE.search(cmd) if TASKSET is None else bash_writes(cmd, files[t], cwd))):
                                    attempts.setdefault(b.get('id'), []).append(t)
                elif m.get('type') == 'user':
                    for b in (m.get('message') or {}).get('content') or []:
                        if b.get('type') == 'tool_result':
                            if b.get('is_error'):
                                errored.add(b.get('tool_use_id'))
                            c = b.get('content')
                            txt = c if isinstance(c, str) else ''.join(x.get('text', '') for x in c or [] if isinstance(x, dict))
                            if '"held": true' in txt:
                                claims_ok += 1
                            elif '"held": false' in txt:
                                claims_no += 1
                elif m.get('type') == 'result':
                    cost = m.get('total_cost_usd') or 0.0
                    turns = m.get('num_turns') or turns
        for tid, ts_ in attempts.items():
            if tid not in errored:  # a hook-denied or failed edit attempt edits nothing
                for t in ts_:
                    editors[t].add(a)
        per_agent[a] = dict(turns=turns, cost_usd=round(cost, 4), tools=tools, claims_held=claims_ok, claims_refused=claims_no)
    with open(os.path.join(cwd, 'REGISTRY.md')) as fh:
        reg = [l for l in fh.read().splitlines() if l.strip().startswith('task ')]
    timed = {a: timed_edits(cwd, a, set(files.values())) for a in agents}
    rd = redone({a: e for a, e in timed.items() if e is not None}) if all(e is not None for e in timed.values()) \
        else dict(completions=None, redo_completions=None, redone_share=None)
    return dict(tasks=len(task_names), completed=sum(1 for v in passed.values() if v), passed=passed,
                duplicated=sorted(t for t, s in editors.items() if len(s) > 1),
                editors={t: sorted(s) for t, s in editors.items()},
                registry_lines=len(reg), registry=reg, per_agent=per_agent,
                cost_usd=round(sum(v['cost_usd'] for v in per_agent.values()), 4),
                turns=sum(v['turns'] for v in per_agent.values()),
                unit_pass=sc.get('unit_pass'), accept_pass=sc.get('accept_pass'), **rd,
                tasks_source=spec, score={k: v for k, v in sc.items() if k not in ('per_task',)})


def out_name(arm, k):
    return f'{arm}-{k:02d}.json' if TASKSET is None else f"{arm}-{TASKSET['name']}-{k:02d}.json"


def trial(arm, k, n, max_turns, budget_s=None):
    """One trial on this process's task source (use_tasks). budget_s: all agents are stopped that many seconds after
    the start; None keeps the original wait of up to DEFAULT_WAIT_S per agent."""
    tag = f"{arm}-{TASKSET['name']}" if TASKSET else arm
    run_id = f'{tag}-{k:02d}-{uuid.uuid4().hex[:8]}'
    cwd = tempfile.mkdtemp(prefix=f'swarm-{run_id}-')
    shutil.copytree(TASKSET['tree'] if TASKSET else TEMPLATE, cwd, dirs_exist_ok=True)
    agents = [f'agent-{i + 1}' for i in range(n)]
    registered = []
    if REGISTER and arm in REGISTER_ARMS:
        registered = REGISTER(run_id, cwd, template_tasks(cwd))
    t0 = time.time()
    procs = [launch(arm, a, n, run_id, cwd, max_turns) for a in agents]
    stopped = []
    for i, (p, log) in enumerate(procs):
        wait = DEFAULT_WAIT_S if budget_s is None else max(0.0, t0 + budget_s - time.time())
        try:
            p.wait(timeout=wait)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()
            stopped.append(agents[i])
        log.close()
    res = dict(arm=arm, trial=k, run_id=run_id, agents=n, wall_s=round(time.time() - t0, 1), dir=cwd,
               registered_at_start=len(registered), budget_s=budget_s, stopped_at_budget=stopped,
               **analyse(cwd, agents))
    os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, out_name(arm, k))
    with open(path, 'w') as fh:
        json.dump(res, fh, indent=1)
    print(f"{arm} {res['tasks_source']} trial {k}: completed {res['completed']}/{res['tasks']}, unit {res['unit_pass']} "
          f"accept {res['accept_pass']}, duplicated {len(res['duplicated'])} {res['duplicated']}, redone_share "
          f"{res['redone_share']}, registry {res['registry_lines']} lines, ${res['cost_usd']}, {res['wall_s']} s, "
          f"{res['turns']} turns -> {path}", flush=True)
    return res


def options(argv):
    """Split --tasks X / --budget-min M (anywhere) from the positional arguments."""
    pos, opts, i = [], dict(tasks='toy', budget_min=None), 0
    while i < len(argv):
        a = argv[i]
        name, eq, val = a.partition('=')
        if name in ('--tasks', '--budget-min'):
            if not eq:
                i += 1
                val = argv[i] if i < len(argv) else ''
            opts[name[2:].replace('-', '_')] = val
        else:
            pos.append(a)
        i += 1
    return pos, opts


def main():
    pos, opts = options(sys.argv[1:])
    if len(pos) < 2:
        sys.exit(__doc__)
    arm, trials = pos[0], int(pos[1])
    n = int(pos[2]) if len(pos) > 2 else 4
    max_turns = int(pos[3]) if len(pos) > 3 else 60
    budget_s = float(opts['budget_min']) * 60 if opts['budget_min'] else None
    use_tasks(opts['tasks'])
    pattern = f'{arm}-*.json' if TASKSET is None else f"{arm}-{TASKSET['name']}-*.json"
    start = len(glob.glob(os.path.join(OUT, pattern)))
    if TASKSET is None:  # as before, less devbench files (<arm>-<project>-NN.json) in the same directory
        sets = os.environ.get('DEVBENCH_SETS') or os.path.join(HERE, 'devbench', 'sets')
        projects = os.listdir(sets) if os.path.isdir(sets) else []
        start -= len([p for p in glob.glob(os.path.join(OUT, pattern)) if any(
            re.fullmatch(rf'{re.escape(arm)}-{re.escape(x)}-\d+\.json', os.path.basename(p)) for x in projects)])
    for k in range(start + 1, start + trials + 1):
        trial(arm, k, n, max_turns, budget_s)


if __name__ == '__main__':
    main()
