"""DevBench task sets for the swarm harness: build, load and score.

A task set is one DevBench Python project turned into the toy template's shape:
  sets/<p>/tree/          the agents' tree: every source module replaced by a stub, docs/ and examples/ and the
                          project's data files as read-only context, TASKS.md (one task per source module, `task NN
                          <module>` naming the file it owns), REGISTRY.md. No tests.
  sets/<p>/judge/         the reference unit_tests/ and acceptance_tests/ plus judge-side files (test helpers and the
                          data files the tests read), copied over a scratch copy of the agents' tree only for scoring.
  sets/<p>/contracts/      the contracts arm's two judges (devbench/contracts.py); the base judge never changes.
  sets/<p>/taskset.json   modules, tasks, the import graph, contract points, reference test counts and runtimes.

The reference tests run with the scoring venv (DEVBENCH_VENV_PY, a Python with pytest and the projects' packages),
which agents never see, through pytest, in a scratch directory, with a clean environment and a timeout. Nothing is run
from inside the DevBench checkout.

Env: DEVBENCH_SRC (DevBench's benchmark_data/python; default devbench-src/benchmark_data/python next to research/relay),
DEVBENCH_VENV_PY (default devbench-venv/bin/python there), DEVBENCH_SETS (the built task sets; default sets/ next to
this file), RELAY_PROOF_FEATURES (as relay_proof.py reads it). None of them is shipped.

Usage: python3 -I devbench/tasksets.py build <project>...   (builds and checks against the reference solution)
       python3 -I devbench/tasksets.py score <trial dir> <project>
"""
import ast, json, os, shutil, subprocess, sys, tempfile, time
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
SWARM = os.path.dirname(HERE)
SRC = os.environ.get('DEVBENCH_SRC', os.path.join(SWARM, 'devbench-src', 'benchmark_data', 'python'))
SETS = os.environ.get('DEVBENCH_SETS') or os.path.join(HERE, 'sets')
VENV_PY = os.environ.get('DEVBENCH_VENV_PY', os.path.join(SWARM, 'devbench-venv', 'bin', 'python'))
FEATURES = os.environ.get('RELAY_PROOF_FEATURES') or os.path.join(SWARM, 'relay_proof_features.json')
TEST_TIMEOUT = 120
SUITES = ('unit_tests', 'acceptance_tests')

# Per project: the source modules (one task each, in task order), what each is for (from the project's
# architecture_design.md), the doc the feature arms edit, files that stay in the tree but are not tasks, judge-side
# files, and the third-party packages the code imports (not installed for the agents).
PROJECTS = {
    'readtime': dict(
        modules=[('readtime/result.py', 'class Result(seconds, wpm): the read-time result with seconds, minutes, text, '
                                        'str() and arithmetic operators delegated to its timedelta'),
                 ('readtime/utils.py', 'read_time(content, format, wpm), read_time_as_seconds(text, images, wpm), '
                                       'parse_html(el): the read-time calculation for text, markdown and html'),
                 ('readtime/api.py', 'of_text(text, wpm), of_html(html, wpm), of_markdown(markdown, wpm): the public '
                                     'functions')],
        doc='docs/PRD.md', keep=['readtime/__init__.py'], judge_extra=['samples'],
        data=['samples'], packages='beautifulsoup4, lxml, markdown2, pyquery'),
    'hone': dict(
        modules=[('hone/utils/csv_utils.py', 'class CSVUtils(csv_filepath): read a CSV file\'s column names and data '
                                             'rows'),
                 ('hone/utils/json_utils.py', 'output_json(json_struct, json_filepath): write JSON to a file, or to '
                                              'standard output when the path is "-"'),
                 ('hone/hone.py', 'class Hone(delimiters): convert(csv_filepath, schema=None) and '
                                  'get_schema(csv_filepath), the CSV to nested JSON conversion')],
        doc='docs/README.md', keep=['hone/__init__.py', 'hone/utils/__init__.py', 'hone/utils/test_utils.py'],
        judge_extra=['hone/utils/test_utils.py', 'data_file'], data=['data_file'], packages=''),
    # Below the >= 3-module floor; buildable on request.
    'particle-swarm-optimization': dict(
        modules=[('pso/cost_functions.py', 'sphere(x): the test cost function'),
                 ('pso/pso_simple.py', 'minimize(cost_func, initial, bounds, ...): particle swarm optimisation')],
        doc='docs/PRD.md', keep=['pso/__init__.py'], judge_extra=[], data=[], packages=''),
}
DROP = ('unit_tests', 'acceptance_tests', 'repo_config.json', 'setup_shell_script.sh', '.gitignore', 'README.md',
        '__pycache__')


def set_dir(project):
    return os.path.join(SETS, project)


def project_of(spec):
    p = spec.split(':', 1)[1] if spec.startswith('devbench:') else spec
    if p not in PROJECTS:
        raise SystemExit(f'unknown devbench project {p!r}; known: {", ".join(sorted(PROJECTS))}')
    return p


# ---------------------------------------------------------------- import graph

def py_files(root):
    out = []
    for d, dirs, files in os.walk(root):
        dirs[:] = sorted(x for x in dirs if x not in ('__pycache__',))
        out += [os.path.relpath(os.path.join(d, f), root) for f in sorted(files) if f.endswith('.py')]
    return out


def resolve(root, mod):
    """'a.b.c' -> relpath of a/b/c.py or a/b/c/__init__.py under root, else None."""
    base = mod.replace('.', '/')
    for cand in (base + '.py', base + '/__init__.py'):
        if os.path.isfile(os.path.join(root, cand)):
            return cand
    return None


def imports_of(root, rel):
    """Relpaths (under root) a .py file imports, directly."""
    with open(os.path.join(root, rel), encoding='utf-8', errors='replace') as fh:
        tree = ast.parse(fh.read(), rel)
    pkg = os.path.dirname(rel).replace('/', '.')
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                parts = a.name.split('.')
                for i in range(1, len(parts) + 1):
                    r = resolve(root, '.'.join(parts[:i]))
                    if r:
                        out.add(r)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = pkg.split('.') if pkg else []
                base = base[:len(base) - (node.level - 1)] if node.level > 1 else base
                mod = '.'.join(base + ([node.module] if node.module else []))
            else:
                mod = node.module or ''
            r = resolve(root, mod) if mod else None
            if r:
                out.add(r)
                parts = mod.split('.')
                for i in range(1, len(parts)):
                    p = resolve(root, '.'.join(parts[:i]))
                    if p:
                        out.add(p)
            for a in node.names:
                sub = resolve(root, f'{mod}.{a.name}' if mod else a.name)
                if sub:
                    out.add(sub)
    out.discard(rel)
    return sorted(out)


def closure(graph, start):
    seen, todo = set(), list(start)
    while todo:
        x = todo.pop()
        if x in seen:
            continue
        seen.add(x)
        todo += graph.get(x, [])
    return seen


# ---------------------------------------------------------------- running the reference tests

def run_suite(tree, suite, timeout=TEST_TIMEOUT):
    """Run one test directory of a scoring copy with the scoring venv's pytest. Returns per-test outcomes."""
    tmp = tempfile.mkdtemp(prefix='devbench-judge-')
    ini, xml = os.path.join(tmp, 'pytest.ini'), os.path.join(tmp, 'junit.xml')
    with open(ini, 'w') as fh:
        fh.write('[pytest]\n')
    env = {'PATH': os.path.dirname(VENV_PY) + ':/usr/bin:/bin', 'HOME': tmp, 'LANG': 'C.UTF-8',
           'PYTHONDONTWRITEBYTECODE': '1', 'TMPDIR': tmp}
    cmd = [VENV_PY, '-m', 'pytest', suite, '-q', '-p', 'no:cacheprovider', '--noconftest', '-c', ini,
           '--rootdir', tree, '--junitxml', xml, '-o', 'junit_family=xunit1']
    t0, timed_out = time.time(), False
    try:
        r = subprocess.run(cmd, cwd=tree, env=env, capture_output=True, text=True, timeout=timeout)
        rc, tail = r.returncode, (r.stdout + r.stderr)[-1500:]
    except subprocess.TimeoutExpired:
        rc, tail, timed_out = None, 'timeout', True
    wall = round(time.time() - t0, 2)
    tests = {}
    if os.path.exists(xml):
        for tc in ET.parse(xml).getroot().iter('testcase'):
            name = f"{tc.get('classname', '')}::{tc.get('name', '')}"
            bad = [c.tag for c in tc if c.tag in ('failure', 'error', 'skipped')]
            tests[name] = 'passed' if not bad else bad[0]
    shutil.rmtree(tmp, ignore_errors=True)
    return dict(tests=tests, rc=rc, wall_s=wall, timed_out=timed_out, tail=tail)


def file_of_test(name):
    """'unit_tests.test_x.Class::test_y' or 'unit_tests.test_x::test_y' -> 'unit_tests/test_x.py'."""
    cls = name.split('::', 1)[0].split('.')
    for i in range(len(cls), 0, -1):
        if cls[i - 1].startswith('test') or i == 2:
            return '/'.join(cls[:i]) + '.py'
    return '/'.join(cls) + '.py'


# ---------------------------------------------------------------- build

def build(project):
    cfg, src = PROJECTS[project], os.path.join(SRC, project)
    out = set_dir(project)
    shutil.rmtree(out, ignore_errors=True)
    tree, judge = os.path.join(out, 'tree'), os.path.join(out, 'judge')
    modules = [m for m, _ in cfg['modules']]

    # import graph over the reference solution (source files only; tests separately)
    all_py = [f for f in py_files(src) if not f.startswith(SUITES + ('examples/', 'docs/'))]
    graph = {f: imports_of(src, f) for f in all_py}
    test_files = [f for f in py_files(src) if f.startswith(SUITES)]
    test_imports = {f: imports_of(src, f) for f in test_files}
    imported_by = {m: sorted(f for f in all_py if m in graph[f] and f != m) for m in modules}
    edges = sorted((a, b) for a in modules for b in graph[a] if b in modules)
    contract_points = sorted(m for m in modules if any(f in modules for f in imported_by[m]))
    exercised = {t: sorted(x for x in closure({**graph, t: test_imports[t]}, [t]) if x in modules) for t in test_files}

    # the agents' tree
    def ignore(d, names):
        rel = os.path.relpath(d, src)
        return [n for n in names if (rel == '.' and n in DROP) or n == '__pycache__']
    shutil.copytree(src, tree, ignore=ignore)
    lines = [f'# Tasks: DevBench project `{project}`', '',
             f'Build the `{project}` project that the documents in docs/ describe (PRD.md, UML_class.md, '
             'UML_sequence.md, architecture_design.md), with examples/ as usage. There is one task per source module. '
             'Modules import each other, so use exactly the module paths, names and signatures the docs give.', '',
             'Read-only context (do not edit): docs/, examples/, LICENSE' +
             ''.join(f', {d}/' for d in cfg['data']) +
             ''.join(f', {k}' for k in cfg['keep'] if os.path.basename(k) != '__init__.py') + '. '
             'The package __init__.py files are given as they are.',
             'No tests are given. After the run the project is judged by hidden unit and acceptance tests written '
             'against the docs. You may check your work with your own throwaway scripts, but do not install packages.' +
             (f' The third-party packages the docs name ({cfg["packages"]}) are not installed here, so code that '
              'imports them cannot be run in this environment.' if cfg['packages'] else ''), '']
    for i, (rel, desc) in enumerate(cfg['modules'], 1):
        name = os.path.basename(rel)[:-3]
        with open(os.path.join(tree, rel), 'w') as fh:
            fh.write(f'"""Task {i:02d} `{name}`: not written yet. Write this module ({rel}) as the docs in docs/ '
                     f'describe: {desc}."""\n')
        lines.append(f'- task {i:02d} `{name}`: {desc}. File: {rel}, test: hidden (judged after the run)')
    lines += ['', 'A task is done when its module is written and checked as far as you can, and REGISTRY.md has '
                  'its line. When a task is done, append exactly one line to REGISTRY.md: '
                  '`task NN <name> done by <your agent id>`.', '']
    with open(os.path.join(tree, 'TASKS.md'), 'w') as fh:
        fh.write('\n'.join(lines))
    with open(os.path.join(tree, 'REGISTRY.md'), 'w') as fh:
        fh.write('# Registry of finished tasks\n')

    # the judge
    for x in list(SUITES) + cfg['judge_extra']:
        s, d = os.path.join(src, x), os.path.join(judge, x)
        if os.path.isdir(s):
            shutil.copytree(s, d, ignore=shutil.ignore_patterns('__pycache__'))
        else:
            os.makedirs(os.path.dirname(d), exist_ok=True)
            shutil.copy2(s, d)

    # check: the reference solution under the judge passes; the stubbed tree fails
    ref = reference_run(project, src)
    partial = dict(modules=modules, exercised_by_test_file=exercised)
    stub = score_tree(tree, project, ref_counts=ref['counts'], meta=partial)
    solved = tempfile.mkdtemp(prefix=f'devbench-solved-{project}-')
    shutil.copytree(tree, solved, dirs_exist_ok=True)
    for m in modules:
        shutil.copy2(os.path.join(src, m), os.path.join(solved, m))
    solved_score = score_tree(solved, project, ref_counts=ref['counts'], meta=partial)
    shutil.rmtree(solved, ignore_errors=True)
    meta = dict(project=project, source=os.path.relpath(src, SWARM), doc=cfg['doc'], packages=cfg['packages'],
                modules=modules, tasks=[[f'task {i:02d} {os.path.basename(m)[:-3]}', m] for i, m in enumerate(modules, 1)],
                import_graph={f: graph[f] for f in all_py}, test_imports=test_imports, exercised_by_test_file=exercised,
                imported_by=imported_by, cross_module_edges=edges, cross_module_imports=len(edges),
                contract_points=contract_points, reference=ref, stub_tree_score=stub,
                solved_tree_score=solved_score)
    with open(os.path.join(out, 'taskset.json'), 'w') as fh:
        json.dump(meta, fh, indent=1)
    with open(os.path.join(out, 'import_graph.json'), 'w') as fh:
        json.dump(dict(project=project, modules=modules, import_graph=meta['import_graph'], imported_by=imported_by,
                       cross_module_edges=edges, contract_points=contract_points, test_imports=test_imports), fh,
                  indent=1)
    return meta


def reference_run(project, src):
    """The reference solution's own tests, in a scratch copy of the project, timed."""
    tmp = tempfile.mkdtemp(prefix=f'devbench-ref-{project}-')
    shutil.copytree(src, tmp, dirs_exist_ok=True, ignore=shutil.ignore_patterns('__pycache__'))
    out, counts = {}, {}
    for suite in SUITES:
        r = run_suite(tmp, suite)
        per_file = {}
        for name, st in r['tests'].items():
            f = file_of_test(name)
            per_file[f] = per_file.get(f, 0) + 1
        out[suite] = dict(total=len(r['tests']), passed=sum(v == 'passed' for v in r['tests'].values()),
                          wall_s=r['wall_s'], timed_out=r['timed_out'], per_file=per_file, tests=sorted(r['tests']))
        counts[suite] = dict(total=len(r['tests']), per_file=per_file)
    shutil.rmtree(tmp, ignore_errors=True)
    out['counts'] = counts
    return out


# ---------------------------------------------------------------- load and score (the harness interface)

def meta_of(project):
    path = os.path.join(set_dir(project), 'taskset.json')
    if not os.path.exists(path):
        raise SystemExit(f'no task set for {project}: run python3 -I {os.path.join(HERE, "tasksets.py")} build {project}')
    with open(path) as fh:
        return json.load(fh)


PROMPT = ('You are {agent}. Agents working in this directory right now, you included: {n}. Read TASKS.md and the docs '
          'it names, then complete as many tasks as you can, one at a time: write the module the task names so the '
          'project behaves as the docs describe, check it as far as you can, then append your line to REGISTRY.md. '
          'Change files with the Edit or Write tool, not from Bash. Do not edit docs/, examples/ or data files, and '
          'do not install packages. Stop when every task is done. Reply with the list of tasks you completed.')


def load(spec):
    """The task set as relay_proof.load_project and run_swarm expect it."""
    project = project_of(spec)
    meta = meta_of(project)
    tree = os.path.join(set_dir(project), 'tree')
    from_tasks_md = task_lines(tree)
    return dict(name=project, tree=tree, tasks=from_tasks_md, prd=meta['doc'], prompt=PROMPT,
                src_files=list(meta['modules']), contract_points=meta['contract_points'],
                import_graph=meta['import_graph'], packages=meta['packages'],
                acceptance='DevBench reference acceptance_tests, run with pytest in the scoring venv')


def task_lines(tree):
    import re
    rx = re.compile(r'^- (task \d\d) `(\w+)`:.*File: (\S+?),', re.M)
    with open(os.path.join(tree, 'TASKS.md')) as fh:
        text = fh.read()
    return [(f'{m.group(1)} {m.group(2)}', m.group(3), m.group(0)[2:]) for m in rx.finditer(text)]


def contracts_changed(cwd, project):
    """True when the trial's doc carries the contracts arm's new line (reported; scoring does not depend on it)."""
    try:
        with open(FEATURES) as fh:
            c = (json.load(fh).get(project) or {}).get('contracts') or {}
        with open(os.path.join(cwd, c.get('file') or c.get('prd') or PROJECTS[project]['doc'])) as fh:
            text = fh.read()
    except (OSError, ValueError):
        return False
    return bool(c.get('new')) and c['new'] in text and (c.get('old', '') not in text or c.get('old', '') in c['new'])


def score_tree(cwd, project, ref_counts=None, timeout=TEST_TIMEOUT, meta=None, judge='judge'):
    """Copy the trial tree to a scratch dir, lay the judge over it, run unit and acceptance tests. Pass rate = tests
    passed / tests in the reference suite (a module that fails to import fails every test it would have run)."""
    sd = set_dir(project)
    if ref_counts is None:
        ref_counts = (meta or meta_of(project))['reference']['counts']
    tmp = tempfile.mkdtemp(prefix=f'devbench-score-{project}-')
    scratch = os.path.join(tmp, 'tree')
    shutil.copytree(cwd, scratch, symlinks=True,
                    ignore=shutil.ignore_patterns('__pycache__', '.log-*', '.mcp-*', '.pytest_cache'))
    for x in SUITES:
        shutil.rmtree(os.path.join(scratch, x), ignore_errors=True)
    shutil.copytree(os.path.join(sd, judge), scratch, dirs_exist_ok=True)  # 'judge', or a variant judge dir
    changed = contracts_changed(cwd, project)
    if meta is None:
        meta = meta_of(project)
    res = dict(contracts_doc_changed=changed)
    per_file_pass = {}
    for suite in SUITES:
        r = run_suite(scratch, suite, timeout)
        total = ref_counts[suite]['total']
        passed = sum(v == 'passed' for v in r['tests'].values())
        for name, st in r['tests'].items():
            f = file_of_test(name)
            per_file_pass[f] = per_file_pass.get(f, 0) + (st == 'passed')
        key = 'unit' if suite == 'unit_tests' else 'accept'
        res[f'{key}_pass'] = round(min(passed, total) / total, 4) if total else None
        res[key] = dict(passed=passed, total=total, ran=len(r['tests']), wall_s=r['wall_s'], timed_out=r['timed_out'],
                        failed=sorted(n for n, v in r['tests'].items() if v != 'passed'),
                        tail=r['tail'] if passed < total else '')
    # per task: every reference test file that exercises the module passes in full; None when no test exercises it
    if meta.get('modules'):
        per = {}
        for m in meta['modules']:
            files = [f for f, ms in meta['exercised_by_test_file'].items() if m in ms]
            need = {f: ref_counts[f.split('/')[0]]['per_file'].get(f, 0) for f in files}
            need = {f: n for f, n in need.items() if n}
            per[os.path.basename(m)[:-3]] = all(per_file_pass.get(f, 0) >= n for f, n in need.items()) if need else None
        res['per_task'] = per
    shutil.rmtree(tmp, ignore_errors=True)
    return res


def score(cwd, spec, judge=None):
    """judge: None for the reference judge (sets/<p>/judge), else a variant judge dir under sets/<p>/ (e.g. the
    questions arm's counterfactual judge)."""
    res = score_tree(cwd, project_of(spec), judge=judge or 'judge')
    if judge:
        res['judge'] = judge
    return res


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else ''
    if cmd == 'build':
        for p in sys.argv[2:]:
            m = build(p)
            r = m['reference']
            print(f"{p}: modules {len(m['modules'])}, cross-module imports {m['cross_module_imports']}, contract points "
                  f"{m['contract_points']}; reference unit {r['unit_tests']['passed']}/{r['unit_tests']['total']} "
                  f"{r['unit_tests']['wall_s']} s, accept {r['acceptance_tests']['passed']}/"
                  f"{r['acceptance_tests']['total']} {r['acceptance_tests']['wall_s']} s; stub tree unit "
                  f"{m['stub_tree_score']['unit_pass']} accept {m['stub_tree_score']['accept_pass']}; reference modules in "
                  f"the tree unit {m['solved_tree_score']['unit_pass']} accept {m['solved_tree_score']['accept_pass']}")
    elif cmd == 'score':
        print(json.dumps(score_tree(sys.argv[2], project_of(sys.argv[3])), indent=1))
    else:
        sys.exit(__doc__)


if __name__ == '__main__':
    main()
