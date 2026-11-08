"""Contracts arm judges for the DevBench task sets: an interface one task's module provides and another's calls.

Per project (INTERFACES): the provider module, the dependent (caller) modules, how the reference changes under the
new doc line (edits that make an adapted reference), and an interface unit test in two versions (old line, new line).
build() writes sets/<p>/contracts/{old,new}/ (old: the reference provider and dependents; new: the adapted ones;
each with its interface test under unit_tests/) and checks them.

score(cwd, project) scores a trial tree against both judges:
  dependent_old_*  the agent's dependent modules + the reference (old-line) provider and other modules, base judge
  dependent_new_*  the agent's dependent modules + the adapted (new-line) provider and other modules, base judge
  provider_old_*   the agent's provider + reference others, the old interface test
  provider_new_*   the agent's provider + reference others, the new interface test
The base unit and acceptance tests do not change with the line (the interface is internal; outputs stay the same),
so only the interface test differs between the two judges. A dependent left behind passes old and fails new; one that
was redone passes new and fails old. Main-arm scoring (tasksets.score) is not touched.

Usage: python3 -I devbench/contracts.py   (build and check both projects)
"""
import json, os, shutil, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import tasksets as T  # noqa: E402

READTIME_TEST_OLD = '''import unittest
from readtime.result import Result


class TestResultInterface(unittest.TestCase):
    def test_result_from_seconds(self):
        r = Result(seconds=143, wpm=265)
        self.assertEqual((r.seconds, r.minutes, r.text, r.wpm), (143, 3, '3 min', 265))
'''
READTIME_TEST_NEW = '''import unittest
from readtime.result import Result


class TestResultInterface(unittest.TestCase):
    def test_result_from_words(self):
        r = Result(words=530, images=2, wpm=265)  # 120 s for the words, 12 + 11 s for the images
        self.assertEqual((r.seconds, r.minutes, r.text, r.wpm), (143, 3, '3 min', 265))
        self.assertEqual(Result(265, 0, 265).seconds, 60)
'''
HONE_TEST_OLD = '''import os
import unittest
from hone.utils.csv_utils import CSVUtils

PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data_file', 'small_cats_dataset', 'dataset.csv')


class TestCSVUtilsInterface(unittest.TestCase):
    def test_get_data_rows_lists(self):
        rows = CSVUtils(PATH).get_data_rows()
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[0], ['Tommy', '5', '3.6', '11', 'April', '2011', 'TRUE', '2012'])
'''
HONE_TEST_NEW = '''import os
import unittest
from hone.utils.csv_utils import CSVUtils

PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data_file', 'small_cats_dataset', 'dataset.csv')


class TestCSVUtilsInterface(unittest.TestCase):
    def test_get_data_rows_dicts(self):
        rows = CSVUtils(PATH).get_data_rows()
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[0], {'name': 'Tommy', 'age (years)': '5', 'weight (kg)': '3.6', 'birth day': '11',
                                   'birth month': 'April', 'birth year': '2011', 'adopted': 'TRUE',
                                   'adopted_since': '2012'})
'''

INTERFACES = {
    'readtime': dict(
        provider='readtime/result.py', dependents=['readtime/utils.py', 'readtime/api.py'],
        interface='Result constructor: Result(seconds, wpm) -> Result(words, images, wpm), seconds computed by Result',
        test=('unit_tests/test_contract_result.py', READTIME_TEST_OLD, READTIME_TEST_NEW),
        adapt=[
            dict(file='readtime/result.py',
                 old='''    def __init__(self, seconds=None, wpm=None):
        self.wpm = wpm
        self.delta = timedelta(seconds=seconds)
        self._add_operator_methods()
''',
                 new='''    def __init__(self, words, images, wpm):
        self.wpm = wpm
        seconds = math.ceil(words / wpm * 60)
        delta = 12  # extra seconds for inline images
        for _ in range(images):
            seconds += delta
            if delta > 3:
                delta -= 1
        self.delta = timedelta(seconds=seconds)
        self._add_operator_methods()

    @classmethod
    def _of_seconds(cls, seconds, wpm):
        r = cls.__new__(cls)
        r.wpm = wpm
        r.delta = timedelta(seconds=seconds)
        return r
'''),
            dict(file='readtime/result.py', old='            return Result(seconds=delta.total_seconds(), wpm=self.wpm)',
                 new='            return Result._of_seconds(delta.total_seconds(), self.wpm)'),
            dict(file='readtime/utils.py', old='        seconds = read_time_as_seconds(content, wpm=wpm)',
                 new='        words, images = count_words(content), 0'),
            dict(file='readtime/utils.py', old='        seconds = read_time_as_seconds(text, images=images, wpm=wpm)',
                 new='        words = count_words(text)', count=2),
            dict(file='readtime/utils.py', old='    return Result(seconds=seconds, wpm=wpm)',
                 new='    return Result(words=words, images=images, wpm=wpm)'),
            dict(file='readtime/utils.py', old='def read_time_as_seconds(text, images=0, wpm=265):',
                 new='''def count_words(text):
    try:
        return len(re.split(WORD_DELIMITER, text.strip()))
    except (AttributeError, TypeError):
        return 0


def read_time_as_seconds(text, images=0, wpm=265):'''),
        ]),
    'hone': dict(
        provider='hone/utils/csv_utils.py', dependents=['hone/hone.py'],
        interface='CSVUtils.get_data_rows(): a list of lists -> a list of dicts keyed by column name',
        test=('unit_tests/test_contract_csv_utils.py', HONE_TEST_OLD, HONE_TEST_NEW),
        adapt=[
            dict(file='hone/utils/csv_utils.py', old='            data_rows = parsed_csv[1:]  # discard column names',
                 new='            columns = parsed_csv[0] if parsed_csv else []\n'
                     '            data_rows = [dict(zip(columns, row)) for row in parsed_csv[1:]]'),
            dict(file='hone/hone.py', old='                cell = self.escape_quotes(row[i])',
                 new='                cell = self.escape_quotes(row[column_names[i]])'),
        ]),
}


def apply(root, edits):
    for e in edits:
        path = os.path.join(root, e['file'])
        with open(path) as fh:
            text = fh.read()
        n = text.count(e['old'])
        if n != e.get('count', 1):
            raise SystemExit(f"{e['file']}: expected {e.get('count', 1)} of {e['old'][:60]!r}, found {n}")
        with open(path, 'w') as fh:
            fh.write(text.replace(e['old'], e['new']))


def cdir(project, version):
    return os.path.join(T.set_dir(project), 'contracts', version)


def build(project):
    """sets/<p>/contracts/{old,new}: every task module of the reference (old) or adapted reference (new), and the
    interface test of that version."""
    spec, src = INTERFACES[project], os.path.join(T.SRC, project)
    modules = T.meta_of(project)['modules']
    root = os.path.join(T.set_dir(project), 'contracts')
    shutil.rmtree(root, ignore_errors=True)
    for version in ('old', 'new'):
        d = cdir(project, version)
        for m in modules:
            os.makedirs(os.path.dirname(os.path.join(d, m)), exist_ok=True)
            shutil.copy2(os.path.join(src, m), os.path.join(d, m))
        if version == 'new':
            apply(d, spec['adapt'])
        path, old, new = spec['test']
        os.makedirs(os.path.dirname(os.path.join(d, path)), exist_ok=True)
        with open(os.path.join(d, path), 'w') as fh:
            fh.write(old if version == 'old' else new)


def run_combo(cwd, project, version, mine, suites):
    """Score a scratch tree: the trial tree, every task module from contracts/<version> except `mine` (kept from the
    trial), judged by the base judge suites and/or the interface test of that version."""
    spec, d = INTERFACES[project], cdir(project, version)
    tmp = tempfile.mkdtemp(prefix=f'devbench-contract-{project}-')
    tree = os.path.join(tmp, 'tree')
    shutil.copytree(cwd, tree, ignore=shutil.ignore_patterns('__pycache__', '.log-*', '.mcp-*'))
    for x in T.SUITES:
        shutil.rmtree(os.path.join(tree, x), ignore_errors=True)
    shutil.copytree(os.path.join(T.set_dir(project), 'judge'), tree, dirs_exist_ok=True)
    for m in T.meta_of(project)['modules']:
        if m not in mine:
            shutil.copy2(os.path.join(d, m), os.path.join(tree, m))
    out = {}
    if 'base' in suites:
        counts = T.meta_of(project)['reference']['counts']
        p = q = 0
        for s in T.SUITES:
            r = T.run_suite(tree, s)
            ok = sum(v == 'passed' for v in r['tests'].values())
            key = 'unit' if s == 'unit_tests' else 'accept'
            out[f'{key}_pass'] = round(min(ok, counts[s]['total']) / counts[s]['total'], 4)
            p, q = p + min(ok, counts[s]['total']), q + counts[s]['total']
        out['pass'] = round(p / q, 4)
    if 'interface' in suites:
        os.makedirs(os.path.join(tree, 'contract_tests'))
        shutil.copy2(os.path.join(d, spec['test'][0]), os.path.join(tree, 'contract_tests', os.path.basename(spec['test'][0])))
        r = T.run_suite(tree, 'contract_tests')
        n = len(r['tests'])
        out['pass'] = round(sum(v == 'passed' for v in r['tests'].values()) / n, 4) if n else 0.0
    shutil.rmtree(tmp, ignore_errors=True)
    return out


def score(cwd, project):
    spec = INTERFACES[project]
    res = dict(interface=spec['interface'], provider=spec['provider'], dependents=spec['dependents'],
               doc_changed=T.contracts_changed(cwd, project))
    for v in ('old', 'new'):
        dep = run_combo(cwd, project, v, spec['dependents'], ('base',))
        prov = run_combo(cwd, project, v, [spec['provider']], ('interface',))
        res[f'dependent_{v}_pass'], res[f'dependent_{v}_unit'], res[f'dependent_{v}_accept'] = (
            dep['pass'], dep['unit_pass'], dep['accept_pass'])
        res[f'provider_{v}_pass'] = prov['pass']
    res['dependent_state'] = ('redone' if res['dependent_new_pass'] == 1.0 and res['dependent_old_pass'] < 1.0 else
                              'left_behind' if res['dependent_old_pass'] == 1.0 and res['dependent_new_pass'] < 1.0 else
                              'both' if res['dependent_old_pass'] == 1.0 else 'neither')
    return res


def verify(project):
    """Reference passes the old judge; the adapted reference passes the new one; each fails the other's."""
    out = {}
    for version in ('old', 'new'):
        tmp = tempfile.mkdtemp(prefix=f'devbench-contract-verify-{project}-')
        shutil.copytree(os.path.join(T.set_dir(project), 'tree'), tmp, dirs_exist_ok=True)
        for m in T.meta_of(project)['modules']:
            shutil.copy2(os.path.join(cdir(project, version), m), os.path.join(tmp, m))
        out[f'{version}_modules'] = score(tmp, project)
        shutil.rmtree(tmp, ignore_errors=True)
    o, n = out['old_modules'], out['new_modules']
    ok = (o['dependent_old_pass'] == 1.0 and o['provider_old_pass'] == 1.0 and o['dependent_new_pass'] < 1.0 and
          o['provider_new_pass'] < 1.0 and n['dependent_new_pass'] == 1.0 and n['provider_new_pass'] == 1.0 and
          n['dependent_old_pass'] < 1.0 and n['provider_old_pass'] < 1.0)
    return out, ok


def main():
    if len(sys.argv) > 1:
        sys.exit(__doc__)
    allok = True
    for p in INTERFACES:
        build(p)
        out, ok = verify(p)
        allok &= ok
        print(p, 'ok' if ok else 'FAILED', json.dumps({k: {x: v[x] for x in ('dependent_old_pass', 'dependent_new_pass',
                                                                            'provider_old_pass', 'provider_new_pass',
                                                                            'dependent_state')}
                                                      for k, v in out.items()}))
    sys.exit(0 if allok else 1)


if __name__ == '__main__':
    main()
