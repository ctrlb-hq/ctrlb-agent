"""Tests of the round 6 harness on synthetic data: relay_proof's round 6 measures (relay_flow, redirect_measures,
witness_measures, open_handoff_measures, every_task_tests) and round6_analyse.py's tables. No Seeker, no lake, no agents.

  python3 -I test_round6.py
"""
import importlib, json, os, shutil, sys, tempfile, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import relay_proof as rp  # noqa: E402

T0 = 1_800_000_000.0


def iso(t):
    return time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime(t)) + f'.{int((t % 1) * 1000):03d}Z'


class Transcript:
    """A Claude Code transcript: tool_use records then their tool_result records, with timestamps."""
    def __init__(self):
        self.lines, self.n = [], 0

    def call(self, t, name, inp, result, error=False):
        self.n += 1
        cid = f'toolu_{self.n}'
        self.lines.append(dict(type='assistant', timestamp=iso(T0 + t), message=dict(
            content=[dict(type='tool_use', id=cid, name=name, input=inp)])))
        text = result if isinstance(result, str) else json.dumps(result)
        self.lines.append(dict(type='user', timestamp=iso(T0 + t + 0.2), message=dict(
            content=[dict(type='tool_result', tool_use_id=cid, is_error=error, content=[dict(type='text', text=text)])])))
        return self

    def write(self, path):
        with open(path, 'w') as fh:
            for x in self.lines:
                fh.write(json.dumps(x) + '\n')
        return path


FREE = [dict(name='task 02 json_utils', key='name:task 02 json_utils', paths=['path:hone/utils/json_utils.py'],
             test='python3 -m unittest harness_tests/test_json_utils_smoke.py')]


class MeasureTests(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix='test-round6-')

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def flows(self):
        a1 = (Transcript()
              .call(5, 'mcp__ctrlb__claim', {'resource': 'task 01 csv_utils'}, {'held': True, 'token': 'n1'})
              .call(40, 'mcp__ctrlb__done', {'resource': 'task 01 csv_utils', 'evidence': {'command': 'make verify-all',
                                                                                           'exit_code': 0}},
                    'PreToolUse:mcp__ctrlb__done hook error: CtrlB Relay witness: no Bash command `make verify-all` '
                    'in this session', error=True)
              .call(50, 'mcp__ctrlb__done', {'resource': 'task 01 csv_utils', 'evidence': {'command': 'python3 -m '
                                                                                           'unittest x', 'exit_code': 0}},
                    {'done': True, 'evidence': {'command': 'python3 -m unittest x', 'exit_code': 0, 'witnessed': True}})
              .call(60, 'mcp__ctrlb__claim', {'resource': 'task 03 hone'},
                    {'held': False, 'held_by': 'agent-2', 'free_tasks': FREE})
              .call(61, 'mcp__ctrlb__claim', {'resource': 'task 02 json_utils'}, {'held': True})
              .call(90, 'mcp__ctrlb__claim', {'resource': 'task 03 hone'},
                    {'held': False, 'held_by': 'agent-2', 'free_tasks': [],
                     'message': 'task 03 hone is already held by agent-2; there is no free task in this run'})
              .call(91, 'mcp__ctrlb__post', {'text': 'agent-1: no free task left, stopping'}, {'ok': True})
              .call(92, 'mcp__ctrlb__claim', {'resource': 'x', 'wait_s': 30}, 'Error: wait_s is no longer accepted',
                    error=True))
        a2 = (Transcript()
              .call(6, 'mcp__ctrlb__claim', {'resource': 'task 03 hone'}, {'held': True})
              .call(70, 'mcp__ctrlb__done', {'resource': 'task 03 hone'}, {'done': False, 'reason': 'witness_required'}))
        return {'agent-1': dict(flow=rp.relay_flow(a1.write(os.path.join(self.d, 'a1.jsonl')), T0), exit_s=95.0),
                'agent-2': dict(flow=rp.relay_flow(a2.write(os.path.join(self.d, 'a2.jsonl')), T0), exit_s=120.0)}

    def test_relay_flow(self):
        f = self.flows()['agent-1']['flow']
        self.assertEqual([x['tool'] for x in f], ['claim', 'done', 'done', 'claim', 'claim', 'claim', 'post', 'claim'])
        self.assertEqual(f[0]['ts_s'], 5.0)
        self.assertTrue(f[1]['error'])
        self.assertEqual(f[3]['answer']['free_tasks'][0]['name'], 'task 02 json_utils')

    def test_redirect(self):
        rows = [dict(_timestamp=1, kind='HOLD'), dict(_timestamp=2, kind='HOLD'), dict(_timestamp=3, kind='REFUSED',
                text='x; there is no free task in this run'), dict(_timestamp=4, kind='REFUSED', text='held')]
        m = rp.redirect_measures(self.flows(), rows, T0)
        self.assertEqual((m['refusals_with_free'], m['next_claim_free'], m['refusals_empty']), (1, 1, 1))
        self.assertEqual((m['holds'], m['refused'], m['refused_no_free'], m['refused_per_hold']), (2, 2, 1, 1.0))
        self.assertEqual(m['claims_wait_s'], 1)
        self.assertEqual(m['claim_errors'], 1)
        self.assertEqual([(n['agent'], n['ts_s']) for n in m['no_task_notes']], [('agent-1', 91.0)])

    def test_witness(self):
        m = rp.witness_measures(self.flows(), {'run': {'done_witnessed': 1, 'done': 2, 'done_with_evidence': 2}})
        self.assertEqual((m['done'], m['blocked_by_hook'], m['witness_required']), (1, 1, 1))
        self.assertIn('make verify-all', m['blocked'][0]['text'])
        self.assertEqual(m['done_witnessed'], 1)
        self.assertTrue(any(c.get('witnessed') for c in m['calls']))

    def test_open_handoff(self):
        project = dict(tasks=[('task 05 is_palindrome', 'src/is_palindrome.py', 'line')])
        stop = T0 + 50
        us = lambda t: int((T0 + t) * 1e6)  # noqa: E731
        rows = [dict(_timestamp=us(40), agent='agent-1', kind='HOLD', key='name:task 05 is_palindrome'),
                dict(_timestamp=us(50.3), agent='agent-1', kind='RELEASE', key='name:task 05 is_palindrome',
                     text='handed off: Session ended. Edited: src/is_palindrome.py.'),
                dict(_timestamp=us(50.3), agent='agent-1', kind='RELEASE', key='path:src/is_palindrome.py',
                     text='handed off: Session ended. Edited: src/is_palindrome.py.'),
                dict(_timestamp=us(50.3), agent='agent-1', kind='NOTE', key='name:task 05 is_palindrome',
                     text='handed off task 05 is_palindrome: Session ended.'),
                dict(_timestamp=us(58), agent='agent-2', kind='HOLD', key='name:task 05 is_palindrome')]
        per_agent = {'agent-1': dict(stop_at_s=50.0, stop_signal='TERM', exit_s=50.6,
                                     stopped_by_harness='stop_on_hold: holds task 05 is_palindrome'),
                     'agent-2': dict(stop_at_s=None)}
        timeline = {'agent-2': dict(edits=[dict(ts=T0 + 45, path='src/is_palindrome.py', applied=True),
                                           dict(ts=T0 + 63, path='src/is_palindrome.py', applied=True)])}
        res = dict(score=dict(per_task={'is_palindrome': True}))
        m = rp.open_handoff_measures(rows, per_agent, timeline, project, res, T0)
        self.assertEqual(m['handoffs'], 1)
        w = m['writes'][0]
        self.assertEqual(w['files'], ['src/is_palindrome.py'])
        self.assertEqual((w['next_holder'], w['next_hold_after_stop_s']), ('agent-2', 8.0))
        self.assertEqual((w['first_edit_by'], w['first_edit_after_stop_s']), ('agent-2', 13.0))
        self.assertTrue(w['text_present'])
        self.assertEqual(len(m['notes']), 1)
        self.assertEqual(round(w['after_stop_s'], 1), 0.3)
        self.assertLess(stop, T0 + 51)

    def test_every_task_tests(self):
        root = os.path.join(self.d, 'set')
        tree, judge = os.path.join(root, 'tree'), os.path.join(root, 'judge')
        for p in ('unit_tests/test_csv_utils.py', 'unit_tests/test_hone.py'):
            os.makedirs(os.path.dirname(os.path.join(judge, p)), exist_ok=True)
            open(os.path.join(judge, p), 'w').write('import unittest\n')
        os.makedirs(os.path.join(tree, 'hone', 'utils'), exist_ok=True)
        json.dump(dict(exercised_by_test_file={'unit_tests/test_csv_utils.py': ['hone/hone.py', 'hone/utils/csv_utils.py'],
                                               'unit_tests/test_hone.py': ['hone/hone.py', 'hone/utils/csv_utils.py']}),
                  open(os.path.join(root, 'taskset.json'), 'w'))
        project = dict(name='hone', tree=tree, tasks=[
            ('task 01 csv_utils', 'hone/utils/csv_utils.py', 'task 01 `csv_utils`: class CSVUtils(csv_filepath): x. File: '
                                                             'hone/utils/csv_utils.py, test: hidden'),
            ('task 02 json_utils', 'hone/utils/json_utils.py', 'task 02 `json_utils`: output_json(json_struct, '
                                                               'json_filepath): write JSON. File: hone/utils/json_utils.py, '
                                                               'test: hidden'),
            ('task 03 hone', 'hone/hone.py', 'task 03 `hone`: class Hone(delimiters): convert(csv_filepath). File: '
                                             'hone/hone.py, test: hidden')])
        cwd = os.path.join(self.d, 'trial')
        os.makedirs(cwd)
        tests, smoke = rp.every_task_tests(project, cwd)
        self.assertEqual(tests['task 01 csv_utils'][0], 'python3 -m unittest unit_tests/test_csv_utils.py')
        self.assertEqual(tests['task 03 hone'][0], 'python3 -m unittest unit_tests/test_hone.py')
        self.assertEqual(tests['task 02 json_utils'][0], 'python3 -m unittest harness_tests/test_json_utils_smoke.py')
        self.assertEqual(smoke, ['task 02 json_utils'])
        # the smoke test passes on a module with the name and fails on a stub
        mod = os.path.join(cwd, 'hone', 'utils')
        os.makedirs(mod, exist_ok=True)
        open(os.path.join(cwd, 'hone', '__init__.py'), 'w').close()
        open(os.path.join(mod, '__init__.py'), 'w').close()
        open(os.path.join(mod, 'json_utils.py'), 'w').write('"""stub"""\n')
        import subprocess
        run = lambda: subprocess.run([sys.executable, '-m', 'unittest', 'harness_tests/test_json_utils_smoke.py'],  # noqa
                                     cwd=cwd, capture_output=True, text=True).returncode
        self.assertNotEqual(run(), 0)
        open(os.path.join(mod, 'json_utils.py'), 'w').write('def output_json(s, p=None):\n    pass\n')
        self.assertEqual(run(), 0)

    def test_give_packages(self):
        cwd = self.d
        p = dict(packages='beautifulsoup4, lxml')
        with open(os.path.join(cwd, 'TASKS.md'), 'w') as fh:
            fh.write('No tests.' + rp.PACKAGES_NOT_INSTALLED.format(p=p['packages']))
        self.assertTrue(rp.give_packages(p, cwd))
        with open(os.path.join(cwd, 'TASKS.md')) as fh:
            self.assertIn('are installed for python3 here', fh.read())

    def test_arms(self):
        self.assertEqual(rp.ARMS['handoff1-kill']['stop_signal'], 'KILL')
        self.assertEqual(rp.ARMS['handoff1-stop']['stop_signal'], 'TERM')
        self.assertTrue(rp.ARMS['pair-relay-witness']['settings']['witnessRequired'])
        self.assertFalse(rp.ARMS['contracts-provider-staleoff']['settings']['staleContracts'])
        self.assertIn('make verify-all', rp.ARMS['false-evidence']['extra']['agent-1'])
        self.assertEqual(rp.BASE['witnessRequired'], False)
        self.assertEqual(rp.BASE['staleContracts'], True)


# ---------------------------------------------------------------- round6_analyse on a synthetic results tree

def stream_log(path, calls, cost):
    """A stream-json agent log: one assistant message per tool call (with usage), its results, and a result line."""
    with open(path, 'w') as fh:
        for i, (name, inp, err, text) in enumerate(calls):
            fh.write(json.dumps(dict(type='assistant', message=dict(id=f'm{i}', usage=dict(input_tokens=10, output_tokens=5),
                                                                    content=[dict(type='tool_use', id=f't{i}', name=name,
                                                                                  input=inp)]))) + '\n')
            fh.write(json.dumps(dict(type='user', message=dict(content=[dict(type='tool_result', tool_use_id=f't{i}',
                                                                             is_error=err, content=text)]))) + '\n')
        fh.write(json.dumps(dict(type='result', num_turns=len(calls) + 1, total_cost_usd=cost)) + '\n')


def trial_json(root, sub, arm, proj, model, k, tdir, **kw):
    d = os.path.join(root, sub, arm, proj)
    os.makedirs(d, exist_ok=True)
    t = dict(arm=arm, project=proj, model=model, trial=k, dir=tdir, turns=kw.pop('turns', 10), cost_usd=kw.pop('cost', 0.05),
             wall_s=100.0, unit_pass=1.0, accept_pass=1.0, duplicated_n=0, claims_held=3, claims_refused=1,
             per_agent={}, score=dict(per_task={'a': True}), contracts={}, run_detail_summary={})
    t.update(kw)
    json.dump(t, open(os.path.join(d, f'{model}-{k:02d}.json'), 'w'))


class AnalyseTests(unittest.TestCase):
    def test_tables(self):
        root = tempfile.mkdtemp(prefix='test-round6-results-')
        try:
            out, bench = os.path.join(root, 'relay-redirect'), os.path.join(root, 'relay-benchmark')
            relay_calls = [('mcp__ctrlb__claim', {'resource': 'task 01 csv_utils'}, False, '{"held": true}'),
                           ('mcp__ctrlb__claim', {'resource': 'slot:REGISTRY.md', 'paths': ['REGISTRY.md']}, False,
                            '{"held": true}'),
                           ('Edit', {'file_path': 'REGISTRY.md'}, False, 'ok'),
                           ('mcp__ctrlb__release', {'resource': 'slot:REGISTRY.md'}, False, '{"released": true}'),
                           ('Edit', {'file_path': 'hone/hone.py'}, False, 'ok')]
            none_calls = [('Edit', {'file_path': 'hone/hone.py'}, False, 'ok')]
            for k in (1, 2):
                td = os.path.join(root, f'tr{k}')
                os.makedirs(td)
                stream_log(os.path.join(td, '.log-agent-1.jsonl'), relay_calls, 0.03)
                trial_json(out, 'redirect', 'pair-relay', 'hone', 'haiku', k, td, turns=6, claims_held=4, claims_refused=2,
                           redirect=dict(refusals_with_free=2, next_claim_free=1, refusals_empty=1, holds=4, refused=2,
                                         no_task_notes=[dict(agent='agent-2', ts_s=80.0, exit_s=85.0, text='no task')]),
                           tasks_end=dict(free_tasks=0, idle_agents=['agent-2']))
                tn = os.path.join(root, f'tn{k}')
                os.makedirs(tn)
                stream_log(os.path.join(tn, '.log-agent-1.jsonl'), none_calls, 0.02)
                trial_json(bench, 'arm1', 'pair-none', 'hone', 'haiku', k, tn, turns=2, claims_held=0, claims_refused=0)
                trial_json(bench, 'arm1', 'pair-relay', 'hone', 'haiku', k, td, turns=58, claims_held=10, claims_refused=3,
                           claims_with_wait=7)
            # B
            trial_json(out, 'handoff', 'handoff1-stop', 'toy', 'haiku', 1, root,
                       hold_watch=dict(held_at_stop=True, task='task 05 x', stop_s=50.0),
                       per_agent={'agent-1': dict(exit_s=50.6)},
                       open_handoff=dict(stop_at_s=50.0, stop_signal='TERM', notes=[{}], tasks_completed=12, tasks=12,
                                         writes=[dict(after_stop_s=0.3, keys=['name:task 05 x', 'path:src/x.py'],
                                                      text='handed off: Session ended.', text_present=True,
                                                      next_holder='agent-2', next_hold_after_stop_s=8.0,
                                                      first_edit_after_stop_s=13.0)]),
                       takeover=dict(takeovers=[], tasks_completed=12, tasks=12, duplicated_other=[],
                                     giver_edits_after_takeover=0))
            trial_json(out, 'handoff', 'handoff1-kill', 'toy', 'haiku', 1, root,
                       hold_watch=dict(held_at_stop=True, task='task 05 x', stop_s=50.0),
                       per_agent={'agent-1': dict(exit_s=50.1)},
                       open_handoff=dict(stop_at_s=50.0, stop_signal='KILL', writes=[], notes=[]),
                       takeover=dict(takeovers=[dict(at_s=140.0, kind='name')], tasks_completed=12, tasks=12,
                                     duplicated_other=[], giver_edits_after_takeover=0))
            # C
            trial_json(out, 'contracts', 'contracts-provider', 'readtime', 'haiku', 1, root, dependent_state='redone',
                       dependent_new_pass=1.0, provider_new_pass=1.0, run_detail_summary=dict(stale_contracts=1),
                       contracts=dict(triggered=True, provider_republished=True, dependent_reads_after=2,
                                      next_read_stale=True, stale_seen_s=90.0, dependent_edited_after_ref=True))
            # D
            trial_json(out, 'witness', 'false-evidence', 'hone', 'haiku', 1, root,
                       witness=dict(calls=[{}, {}], done=1, blocked_by_hook=1, witness_required=0, done_witnessed=3,
                                    done_total=3, blocked=[dict(agent='agent-1', ts_s=40.0, evidence={'command':
                                                                'make verify-all'}, text='no Bash command')]))
            os.environ['ROUND6_RESULTS'] = root
            import round6_analyse
            importlib.reload(round6_analyse)
            round6_analyse.main()
            md = open(os.path.join(out, 'tables.md')).read()
            an = json.load(open(os.path.join(out, 'analysis.json')))
            self.assertIn('pair-relay hone (round 6, v9)', md)
            self.assertIn('4/8 = 0.5', md)  # refused per hold over the cell
            self.assertEqual(an['redirect']['round6']['pair-relay hone']['followed'], 2)
            self.assertIn('pair-relay hone haiku (benchmark, v7)', md)
            self.assertIn('6/20 = 0.3', md)
            self.assertEqual(an['handoff']['trials']['handoff1-stop'][0]['first_edit_after_stop_s'], 13.0)
            self.assertEqual(an['handoff']['trials']['handoff1-kill'][0]['takeover_after_stop_s'], [90.0])
            self.assertEqual(an['contracts']['contracts-provider (staleContracts on)']['next_stale'], 1)
            self.assertEqual(an['witness']['cells']['false-evidence hone']['blocked'], 1)
            reg = an['registry']['pair-relay hone haiku']
            self.assertEqual(reg['reg_abs'], 2.0)  # the slot claim and its release
            self.assertAlmostEqual(reg['extra'], 4.0)
            self.assertEqual(an['cost']['trials'], 6)
        finally:
            os.environ.pop('ROUND6_RESULTS', None)
            shutil.rmtree(root, ignore_errors=True)


if __name__ == '__main__':
    unittest.main(verbosity=1)
