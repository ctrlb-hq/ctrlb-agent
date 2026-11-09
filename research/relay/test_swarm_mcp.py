"""Tests of the research prototype swarm_mcp.py against Seeker's rules.

  parity  read_key on every case of Seeker's tests/fixtures/key_states.json (the same file Go's ReadKey passes), the
          belongs_to_task decision on its cases with a claim (decision 27), DONE rows with evidence folding as without
          (decision 30), the takeover decision and the edit check on its takeover and check cases (decision 33), and
          relay_paths.key on every case of tests/fixtures/keys.json
  live    claim, release, reopen, the read-your-write guard, the late finish, waiting in line (wait_s) and registered
          tasks (belongs_to_task), done with evidence, takeover of a stalled hold (decision 33) against the lake (stream
          swarm_board, a fresh run id); skipped when the lake does not answer

Usage: FIXTURES=<seeker>/tests/fixtures python3 -I test_swarm_mcp.py
  FIXTURES   Seeker's tests/fixtures (default: ../../../seeker/tests/fixtures, a Seeker checkout beside this repo)
  CTRLB_URL  the lake (default http://127.0.0.1:5160); CTRLB_ORG, CTRLB_AUTH as lib_lake reads them
"""
import json, os, subprocess, sys, time, unittest, uuid
sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import lib_lake as L  # noqa: E402
import relay_paths  # noqa: E402
import swarm_mcp as M  # noqa: E402

FIXTURES = os.environ.get('FIXTURES') or os.path.join(HERE, '..', '..', '..', 'seeker', 'tests', 'fixtures')


def fixture(name):
    path = os.path.join(FIXTURES, name)
    if not os.path.isfile(path):
        raise unittest.SkipTest(f'{path} not found: set FIXTURES to a Seeker checkout\'s tests/fixtures')
    with open(path) as fh:
        return json.load(fh)


def sig(r):
    return None if r is None else dict(ts=int(r['ts']), agent=r['agent'], kind=r['kind'], nonce=r['nonce'])


class Parity(unittest.TestCase):
    def test_key_states(self):
        cases = fixture('key_states.json')['cases']
        self.assertGreaterEqual(len(cases), 30)
        kinds, passed = set(), 0
        for c in cases:
            rows = [dict(r) for r in c['rows']]
            kinds.update(r['kind'] for r in rows)
            st = M.read_key(rows, c['now_us'], c['lease_s'], key=c['key'])
            got = dict(live=[sig(r) for r in st['live']], open=[sig(r) for r in st['open']],
                       finished=sig(st['finished']), reopened=sig(st['reopened']))
            with self.subTest(c['name']):
                self.assertEqual(got, c['want'])
                passed += 1
        self.assertEqual(passed, len(cases))
        print(f'\n  key_states.json: read_key gives the state of {passed}/{len(cases)} cases', file=sys.stderr)
        # Every kind the fold reads, and kinds it ignores (the fixture grows with new row kinds).
        self.assertTrue({'NOTE', 'HOLD', 'RELEASE', 'DONE', 'REOPEN', 'REFUSED', 'CHECK', 'BRIEF', 'HANDOFF'} <= kinds, kinds)

    def test_belongs_to_task(self):
        """Seeker decision 27: the fixture's cases with a claim (Go's claimOf: the rows are the task key's, the claim is
        made at now_us in a run with the case's registered tasks)."""
        cases = [c for c in fixture('key_states.json')['cases'] if c.get('claim')]
        if not cases:
            self.skipTest(f'{FIXTURES} predates decision 27 (no belongs_to_task case); use a fixture from feat/relay-wait')
        self.assertGreaterEqual(len(cases), 7)
        for c in cases:
            tasks = [dict(key=t['key'], resource=t['resource'], paths=t['paths']) for t in c['tasks']]
            cl = c['claim']
            key, kind = relay_paths.key(cl['resource'])
            task = next((t for t in tasks if t['key'] == key), None) if kind == 'name' else None
            paths = cl.get('paths') or (task['paths'] if task else [])
            keys = [key] + M.bundle_keys(key, paths)

            def state_of(k, c=c):
                return M.read_key([dict(r) for r in c['rows']] if k == c['key'] else [], c['now_us'], c['lease_s'], key=k)

            b = M.belongs_to_task(keys, task, tasks, state_of, cl['agent'])
            got = dict(reason='') if b is None else dict(reason=M.BELONGS_TO_TASK, task=b['task'])
            if b and b['held_by']:
                got['held_by'] = b['held_by']
            with self.subTest(c['name']):
                self.assertEqual(got, c['want_claim'])
        print(f'\n  key_states.json: belongs_to_task decided as the fixture says on {len(cases)} claim cases', file=sys.stderr)

    def test_registered_tasks(self):
        rows = [dict(ts=1, agent='o', kind='BRIEF', key='name:t', resource='T', paths='["path:a.py"]'),
                dict(ts=2, agent='o', kind='BRIEF', key='name:t', resource='T', paths=''),          # no paths: kept
                dict(ts=3, agent='o', kind='BRIEF', key='contract:c', resource='c', paths='["path:b.py"]'),
                dict(ts=4, agent='o', kind='BRIEF', key='name:u', resource='U', paths='["path:b.py","slot:x"]')]
        self.assertEqual([(t['key'], t['resource'], t['paths']) for t in M.registered_tasks(rows)],
                         [('name:t', 'T', ['path:a.py']), ('name:u', 'U', ['path:b.py', 'slot:x'])])
        tasks = M.registered_tasks(rows)
        self.assertEqual(M.deciding_keys(['name:t', 'path:a.py', 'path:z.py'], tasks[0], tasks), [True, False, True])
        self.assertEqual(M.deciding_keys(['name:u', 'path:b.py', 'slot:x'], tasks[1], tasks), [True, False, True])
        self.assertEqual(M.bundle_keys('name:t', ['./b.py', 'slot:X', 'Makefile', 'b.py']), ['path:Makefile', 'path:b.py', 'slot:x'])
        with self.assertRaises(ValueError):
            M.bundle_keys('name:t', ['name:other'])
        with self.assertRaises(ValueError):
            M.bundle_keys('name:t', [f'f{i}.py' for i in range(33)])

    def test_evidence_ignored(self):
        """Seeker decision 30: the fixture's DONE rows with evidence in text fold as they do without it."""
        cases = [c for c in fixture('key_states.json')['cases'] if any(r.get('text') for r in c['rows'])]
        if not cases:
            self.skipTest(f'{FIXTURES} predates decision 30 (no DONE row with evidence); use a fixture from feat/relay-done-stall')
        self.assertGreaterEqual(len(cases), 2)
        for c in cases:
            plain = [{k: v for k, v in r.items() if k != 'text'} for r in c['rows']]
            a = M.read_key([dict(r) for r in c['rows']], c['now_us'], c['lease_s'], key=c['key'])
            b = M.read_key(plain, c['now_us'], c['lease_s'], key=c['key'])
            with self.subTest(c['name']):
                self.assertEqual({k: [sig(r) for r in v] if isinstance(v, list) else sig(v) for k, v in a.items()},
                                 {k: [sig(r) for r in v] if isinstance(v, list) else sig(v) for k, v in b.items()})

    def test_takeover(self):
        """Seeker decision 33: the fixture's takeover cases (rows of the key, a claim with takeover=true at now_us):
        takeover_plan gives the reason, the holder, its idle seconds and the rows written (tokens are random: the
        taker's own rows are compared with their nonce as <token>, and ts is never compared)."""
        cases = [c for c in fixture('key_states.json')['cases'] if c.get('takeover')]
        if not cases:
            self.skipTest(f'{FIXTURES} predates decision 33 (no takeover case); use a fixture from feat/relay-stall-takeover')
        for c in cases:
            t = c['takeover']
            age = t.get('activity_age_s')
            act = None if age is None else c['now_us'] - int(age) * 1_000_000
            got = M.takeover_plan([dict(r) for r in c['rows']], c['now_us'], c['lease_s'], c['key'], t['agent'],
                                  t['stall_after_s'], act, token='<token>')
            want = c['want_takeover']

            def norm(rows):
                return [dict(agent=r['agent'], kind=r['kind'], nonce=r.get('nonce') or '', to=r.get('to') or '')
                        for r in rows]
            with self.subTest(c['name']):
                self.assertEqual(dict(held=got['held'], reason=got['reason'], holder=got['holder'], idle_s=got['idle_s'],
                                      rows=norm(got['rows'])),
                                 dict(held=want.get('held', False), reason=want.get('reason', ''),
                                      holder=want.get('holder', ''), idle_s=want.get('idle_s', 0),
                                      rows=norm(want.get('rows') or [])))
                # The takeover's rows folded with the key's: a held taker is first live, in the holder's place.
                ts = c['now_us']
                written = [dict(r, ts=ts, token='<token>' if r['kind'] == 'HANDOFF' else '') for r in got['rows']]
                st = M.read_key([dict(r) for r in c['rows']] + written, ts, c['lease_s'], key=c['key'])
                self.assertEqual(bool(st['live']) and st['live'][0]['agent'] == t['agent'], got['held'])
        print(f'\n  key_states.json: takeover_plan decided as the fixture says on {len(cases)} takeover cases', file=sys.stderr)

    def test_check_after_takeover(self):
        """Seeker decision 33: the fixture's check cases (the edit check of an agent with holdRequired at now_us), such
        as the stalled holder's check after the takeover: held by the taker."""
        cases = [c for c in fixture('key_states.json')['cases'] if c.get('check')]
        if not cases:
            self.skipTest(f'{FIXTURES} predates decision 33 (no check case); use a fixture from feat/relay-stall-takeover')
        for c in cases:
            got = M.check_state([dict(r) for r in c['rows']], c['now_us'], c['lease_s'], c['key'], c['check']['agent'])
            want = c['want_check']
            with self.subTest(c['name']):
                self.assertEqual((got['reason'], got['held_by']), (want.get('reason', ''), want.get('held_by', '')))
        print(f'\n  key_states.json: check_state decided as the fixture says on {len(cases)} check cases', file=sys.stderr)

    def test_takeover_plan_rules(self):
        """The takeover rule without the fixture: stalled first holder handed off (its renewal released); a working
        holder refused not_stalled with nothing written; nothing to take over on a free, finished or own key."""
        now, s = 1_800_000_000_000_000, 1_000_000
        h = lambda age, agent, kind, nonce: dict(ts=now - age * s, agent=agent, kind=kind, nonce=nonce)
        rows = [h(400, 'a', 'HOLD', 'a1'), h(350, 'a', 'HOLD', 'a2'), h(100, 'b', 'HOLD', 'b1')]
        got = M.takeover_plan(rows, now, 600, 'name:t', 'c', 300, token='tk')
        self.assertEqual((got['reason'], got['holder'], got['idle_s']), ('', 'a', 350))
        self.assertEqual([(r['agent'], r['kind'], r['nonce'], r['to']) for r in got['rows']],
                         [('a', 'RELEASE', 'a2', ''), ('a', 'HANDOFF', 'a1', 'c')])
        st = M.read_key(rows + [dict(r, ts=now) for r in got['rows']], now, 600, 'name:t')
        self.assertEqual([r['agent'] for r in st['live']], ['c', 'b'])
        self.assertEqual(M.check_state(rows + [dict(r, ts=now) for r in got['rows']], now, 600, 'name:t', 'a'),
                         dict(reason='held', held_by='c'))
        busy = M.takeover_plan(rows, now, 600, 'name:t', 'c', 300, activity_us=now - 40 * s)
        self.assertEqual((busy['reason'], busy['idle_s'], busy['rows']), ('not_stalled', 40, []))
        free = M.takeover_plan([], now, 600, 'name:t', 'c', 300, token='tk')
        self.assertEqual((free['held'], [(r['agent'], r['kind'], r['nonce']) for r in free['rows']]), (True, [('c', 'HOLD', 'tk')]))
        self.assertEqual([r['kind'] for r in M.takeover_plan(rows, now, 600, 'name:t', 'a', 300)['rows']], ['HOLD'])
        done = rows[:1] + [h(300, 'a', 'DONE', 'a1')]
        self.assertEqual(M.takeover_plan(done, now, 600, 'name:t', 'c', 300)['rows'], [])
        self.assertEqual(M.takeover_plan(rows, now, 600, 'name:t', 'c', 300, user='other')['reason'], 'other_user')

    def test_evidence_shape(self):
        self.assertEqual(M._evidence({'command': ' pytest -q ', 'exit_code': 0, 'summary': '1 passed', 'x': 1}),
                         {'command': 'pytest -q', 'exit_code': 0, 'summary': '1 passed'})
        self.assertEqual(M._evidence('all green'), {'summary': 'all green'})
        self.assertEqual(M._evidence(None), {})
        with self.assertRaises(ValueError):
            M.done('src/x.py', {'command': 'pytest'})

    def test_keys(self):
        cases = fixture('keys.json')['cases']
        for c in cases:
            with self.subTest(c['resource']):
                self.assertEqual(relay_paths.key(c['resource'], c['cwd'])[0], c['key'])

    def test_kinds(self):
        self.assertEqual(fixture('keys.json')['kinds'], relay_paths.KINDS)
        self.assertEqual(relay_paths.lease_seconds_for('slot:devserver', 600), 120)
        self.assertEqual(relay_paths.key('Slot:X'), ('name:slot:x', 'name'))
        self.assertEqual(relay_paths.key('slot:'), ('', ''))
        with self.assertRaises(ValueError):
            M.claim('contract:seeker-4 routes')


def lake_up():
    try:
        L.search(M.STREAM, f"SELECT kind FROM {M.STREAM} LIMIT 1", size=1, timeout=5)
        return True
    except Exception:
        return False


@unittest.skipUnless(lake_up(), 'the lake does not answer')
class Live(unittest.TestCase):
    def setUp(self):
        M.RUN = f'proto-test-{uuid.uuid4().hex[:8]}'

    def as_agent(self, agent, fn, *a, **k):
        M.AGENT = agent
        return json.loads(fn(*a, **k))

    def spawn(self, agent, tool, **args):
        """One tools/call to the prototype's stdio server as agent, in its own process (a waiting claim blocks)."""
        env = dict(os.environ, SWARM_RUN=M.RUN, SWARM_AGENT=agent, SWARM_TOOLS='claim,release',
                   SWARM_WAIT_MS=str(M.WAIT_MS), SWARM_LEASE_S=str(M.LEASE_S))
        p = subprocess.Popen([sys.executable, '-I', os.path.join(HERE, 'swarm_mcp.py')], env=env, text=True,
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        p.stdin.write(json.dumps(dict(jsonrpc='2.0', id=1, method='tools/call', params=dict(name=tool, arguments=args))) + '\n')
        p.stdin.close()
        return p

    def result(self, p, timeout=90):
        with p.stdout:
            out = json.loads(p.stdout.read())
        p.wait(timeout)
        res = out['result']
        self.assertFalse(res['isError'], res)
        return json.loads(res['content'][0]['text'])

    def rows_of(self, key, kind):
        return L.search(M.STREAM, f"SELECT * FROM {M.STREAM} WHERE run_id = '{M.RUN}' AND key = '{key}' AND kind = "
                                  f"'{kind}' ORDER BY _timestamp", size=100, timeout=10)

    def test_path_is_a_lease(self):
        self.assertTrue(self.as_agent('a', M.claim, './src/x.py')['held'])
        got = self.as_agent('b', M.claim, 'src/x.py')
        self.assertEqual((got['held'], got.get('held_by'), got['key']), (False, 'a', 'path:src/x.py'))
        rel = self.as_agent('a', M.release, 'src/x.py', True)
        self.assertEqual((rel['released'], rel['finished']), (1, False))
        self.assertIn('file path', rel['message'])
        self.assertTrue(self.as_agent('b', M.claim, 'src/x.py')['held'])

    def test_task_rounds(self):
        self.assertTrue(self.as_agent('a', M.claim, 'Task One')['held'])
        self.assertEqual(self.as_agent('a', M.release, 'task one', True)['finished'], True)
        got = self.as_agent('b', M.claim, 'task one')
        self.assertEqual((got['held'], got.get('finished_by')), (False, 'a'))
        self.assertIn('reopen=true', got['message'])
        got = self.as_agent('b', M.claim, 'task one', True)
        self.assertEqual((got['held'], got.get('reopened')), (True, True))
        got = self.as_agent('c', M.claim, 'task one', True)
        self.assertEqual((got['held'], got.get('held_by')), (False, 'b'))
        self.assertTrue(self.as_agent('b', M.claim, 'task one').get('renewed'))

    def test_guard(self):
        real = M.hold_rows
        M.hold_rows = lambda key: [r for r in real(key) if r['agent'] != 'a' or r['kind'] != 'HOLD']
        try:
            got = self.as_agent('a', M.claim, 'task guarded')
        finally:
            M.hold_rows = real
        self.assertEqual((got['held'], got.get('reason')), (False, 'not_visible'))
        time.sleep(0.05)
        rows = real('name:task guarded')
        self.assertEqual(sorted(r['kind'] for r in rows), ['HOLD', 'RELEASE'])
        self.assertTrue(self.as_agent('b', M.claim, 'task guarded')['held'])

    def test_briefs(self):
        self.assertEqual(self.as_agent('a', M.briefs, 'src/x.py')['briefs'], [])
        got = self.as_agent('a', M.brief, './src/x.py', 'first')
        self.assertEqual((got['ok'], got['key']), (True, 'path:src/x.py'))
        time.sleep(0.01)
        self.as_agent('b', M.brief, 'src/x.py', 'second ' + 'y' * 3000)
        time.sleep(0.01)
        self.as_agent('a', M.brief, 'Spec Notes', 'the spec')
        time.sleep(0.05)
        got = self.as_agent('c', M.briefs, 'src/x.py')['briefs']
        self.assertEqual([(b['key'], b['agent'], len(b['text'])) for b in got], [('path:src/x.py', 'b', 2000)])
        got = self.as_agent('c', M.briefs)['briefs']
        self.assertEqual([b['key'] for b in got], ['name:spec notes', 'path:src/x.py'])
        self.assertEqual(len(self.as_agent('c', M.briefs, None, 1)['briefs']), 1)
        kinds = [e['kind'] for e in self.as_agent('c', M.peers)['events']]
        self.assertEqual(kinds.count('BRIEF'), 3)

    def test_finish_after_lease(self):
        old = int((time.time() - 700) * 1e6)
        L.ingest(M.STREAM, [dict(run_id=M.RUN, agent='a', kind='HOLD', resource='task late', key='name:task late',
                                 nonce='late-a1', text='', _timestamp=old)], timeout=10)
        time.sleep(0.05)
        rel = self.as_agent('a', M.release, 'task late', True)
        self.assertEqual((rel['released'], rel['finished'], rel.get('finished_after_lease')), (1, True, True))
        self.assertEqual(self.as_agent('b', M.claim, 'task late').get('finished_by'), 'a')

    def test_done_with_evidence(self):
        self.assertTrue(self.as_agent('a', M.claim, 'Task Ev')['held'])
        got = self.as_agent('a', M.done, 'task ev', {'command': 'pytest -q', 'exit_code': 0, 'summary': '2 passed'})
        self.assertEqual((got['done'], got['evidence']), (True, {'command': 'pytest -q', 'exit_code': 0, 'summary': '2 passed'}))
        time.sleep(0.05)
        rows = L.search(M.STREAM, f"SELECT kind, text FROM {M.STREAM} WHERE run_id = '{M.RUN}' AND kind = 'DONE'", size=10, timeout=10)
        self.assertEqual([r.get('text') for r in rows], ['{"command":"pytest -q","exit_code":0,"summary":"2 passed"}'])
        self.assertEqual(self.as_agent('b', M.claim, 'task ev').get('finished_by'), 'a')
        # The alias: release with finished still finishes a task (no evidence).
        self.assertTrue(self.as_agent('b', M.claim, 'task alias')['held'])
        self.assertTrue(self.as_agent('b', M.release, 'task alias', True)['finished'])

    def test_evidence_required(self):
        M.EVIDENCE_REQUIRED = True
        try:
            self.assertTrue(self.as_agent('a', M.claim, 'task req')['held'])
            got = self.as_agent('a', M.done, 'task req', 'it works')
            self.assertEqual((got['done'], got['reason']), (False, 'evidence_required'))
            got = self.as_agent('a', M.release, 'task req', True)
            self.assertEqual((got['released'], got['reason']), (0, 'evidence_required'))
            time.sleep(0.05)
            self.assertEqual([r['kind'] for r in M.hold_rows('name:task req')], ['HOLD'])
            self.assertTrue(self.as_agent('a', M.done, 'task req', {'command': 'go test ./...', 'exit_code': 0})['done'])
        finally:
            M.EVIDENCE_REQUIRED = False

    def test_handoff(self):
        self.assertTrue(self.as_agent('a', M.claim, 'src/h.py')['held'])
        self.assertFalse(self.as_agent('c', M.handoff, 'src/h.py', 'b')['handed'])
        got = self.as_agent('a', M.handoff, 'src/h.py', 'b')
        self.assertEqual((got['handed'], got['to']), (True, 'b'))
        time.sleep(0.05)
        self.assertEqual(self.as_agent('c', M.claim, 'src/h.py').get('held_by'), 'b')
        self.assertTrue(self.as_agent('b', M.claim, 'src/h.py').get('renewed'))
        self.assertEqual(self.as_agent('b', M.release, 'src/h.py', False)['released'], 2)
        self.assertTrue(self.as_agent('c', M.claim, 'src/h.py')['held'])

    def test_takeover(self):
        """Decision 33 against the lake: a working holder is not taken over; once it made no progress for SWARM_STALL_S
        the taker holds the task and its file in its place, and the stalled holder is refused on its file."""
        M.task('task to', 'stall me', ['src/to.py'])
        time.sleep(0.05)
        self.assertTrue(self.as_agent('a', M.claim, 'task to')['held'])
        time.sleep(0.05)
        old = M.STALL_S
        try:
            M.STALL_S = 60
            got = self.as_agent('b', M.claim, 'task to', takeover=True)
            self.assertEqual((got['held'], got.get('reason'), got.get('held_by')), (False, 'not_stalled', 'a'))
            self.assertEqual(self.rows_of('name:task to', 'HANDOFF'), [])
            M.STALL_S = 0
            got = self.as_agent('b', M.claim, 'task to', takeover=True)
            self.assertEqual((got['held'], got.get('taken_over_from'), got.get('paths')), (True, 'a', ['path:src/to.py']))
        finally:
            M.STALL_S = old
        time.sleep(0.05)
        for k in ('name:task to', 'path:src/to.py'):
            st = M.read_key(M.hold_rows(k), M.now_us(), M.LEASE_S, k)
            self.assertEqual(st['live'][0]['agent'], 'b', k)
            self.assertEqual(M.check_state(M.hold_rows(k), M.now_us(), M.LEASE_S, k, 'a'), dict(reason='held', held_by='b'))
        h = self.rows_of('name:task to', 'HANDOFF')
        self.assertEqual([(r['agent'], r['to'], r['text']) for r in h], [('a', 'b', 'stalled: taken over by b')])
        self.assertTrue(self.as_agent('b', M.done, 'task to', {'command': 'pytest', 'exit_code': 0})['done'])

    def test_questions(self):
        qid = self.as_agent('a', M.ask, 'which port?', 'b')['id']
        time.sleep(0.05)
        with self.assertRaises(ValueError):
            self.as_agent('b', M.answer, 'no-such-id', 'x')
        self.assertEqual(self.as_agent('b', M.answer, qid, '3000'), dict(ok=True, id=qid, run_id=M.RUN, agent='b'))
        time.sleep(0.05)
        rows = L.search(M.STREAM, f"SELECT kind, nonce, to FROM {M.STREAM} WHERE run_id = '{M.RUN}' ORDER BY _timestamp",
                        size=10, timeout=10)
        self.assertEqual([(r['kind'], r['nonce'], r.get('to') or '') for r in rows], [('QUESTION', qid, 'b'), ('ANSWER', qid, '')])

    def test_contracts(self):
        got = self.as_agent('a', M.contract, 'seeker-4 routes', 'GET /contracts', ['internal/api/router.go'])
        self.assertEqual((got['key'], got['paths']), ('contract:seeker-4 routes', ['path:internal/api/router.go']))
        time.sleep(0.05)
        self.assertEqual(self.as_agent('b', M.contracts)['contracts'][0]['changed_since'], [])
        self.assertTrue(self.as_agent('b', M.claim, 'internal/api/router.go')['held'])
        time.sleep(0.05)
        self.assertEqual(self.as_agent('c', M.contracts, ['seeker-4 routes'])['contracts'][0]['changed_since'],
                         ['path:internal/api/router.go'])

    def test_wait_served_after_release(self):
        """Decision 25: a waiter stays in line and is held once the holder releases; a served wait writes no REFUSED."""
        self.assertTrue(self.as_agent('a', M.claim, 'task wait')['held'])
        t0 = time.time()
        waiter = self.spawn('b', 'claim', resource='task wait', wait_s=20)
        time.sleep(1.0)
        rel = self.as_agent('a', M.release, 'task wait', False)
        self.assertEqual((rel['released'], rel['finished']), (1, False))
        got = self.result(waiter)
        self.assertEqual((got['held'], got.get('held_by'), got.get('reason')), (True, None, None), got)
        self.assertGreaterEqual(got['waited_ms'], 800)
        self.assertLess(time.time() - t0, 15)
        time.sleep(0.05)
        self.assertEqual(self.rows_of('name:task wait', 'REFUSED'), [])
        self.assertTrue(self.as_agent('b', M.claim, 'task wait').get('renewed'))

    def test_wait_times_out_with_position(self):
        """Decision 25: a second waiter whose wait_s runs out is refused timed_out with held_by and its position,
        after a RELEASE (text timed_out) and a REFUSED row; the first waiter is then served in order."""
        self.assertTrue(self.as_agent('a', M.claim, 'src/wait.py')['held'])
        first = self.spawn('b', 'claim', resource='src/wait.py', wait_s=30)
        time.sleep(1.0)
        second = self.spawn('c', 'claim', resource='src/wait.py', wait_s=2)
        got = self.result(second)
        self.assertEqual((got['held'], got.get('reason'), got.get('held_by'), got.get('position')),
                         (False, 'timed_out', 'a', 3), got)
        self.assertGreaterEqual(got['waited_ms'], 1500)
        time.sleep(0.05)
        rel = [r for r in self.rows_of('path:src/wait.py', 'RELEASE') if r['agent'] == 'c']
        self.assertEqual([r.get('text') for r in rel], ['timed_out'])
        ref = self.rows_of('path:src/wait.py', 'REFUSED')
        self.assertEqual([(r['agent'], r.get('held_by')) for r in ref], [('c', 'a')])
        self.as_agent('a', M.release, 'src/wait.py', False)
        got = self.result(first)
        self.assertTrue(got['held'], got)
        self.assertGreaterEqual(got['waited_ms'], 1500)

    def test_wait_ends_finished(self):
        """Decision 25: a DONE while the claim waits ends it refused with finished_by."""
        self.assertTrue(self.as_agent('a', M.claim, 'task wait done')['held'])
        waiter = self.spawn('b', 'claim', resource='task wait done', wait_s=20)
        time.sleep(1.0)
        self.assertTrue(self.as_agent('a', M.release, 'task wait done', True)['finished'])
        got = self.result(waiter)
        self.assertEqual((got['held'], got.get('finished_by')), (False, 'a'), got)

    def test_wait_capped(self):
        real = M.MAX_WAIT_S
        M.MAX_WAIT_S = 1
        try:
            self.assertTrue(self.as_agent('a', M.claim, 'task cap')['held'])
            t0 = time.time()
            got = self.as_agent('b', M.claim, 'task cap', wait_s=300)
        finally:
            M.MAX_WAIT_S = real
        self.assertEqual((got['held'], got.get('reason'), got.get('position')), (False, 'timed_out', 2), got)
        self.assertLess(time.time() - t0, 5)
        with self.assertRaises(ValueError):
            self.as_agent('b', M.claim, 'task cap', wait_s=-1)

    def test_belongs_to_task(self):
        """Decision 27: a bare claim of a file registered to a task is refused with the task's name unless the caller
        holds the task; the task's claim takes the file with it, decided on the task key, and its release hands it back."""
        got = self.as_agent('o', M.task, 'Task Reg', 'the registered task', ['src/reg.py'])
        self.assertEqual((got['key'], got['paths']), ('name:task reg', ['path:src/reg.py']))
        time.sleep(0.05)
        self.assertEqual([(t['name'], t['state']) for t in self.as_agent('o', M.tasks)['tasks']], [('Task Reg', 'open')])
        got = self.as_agent('b', M.claim, './src/reg.py')
        self.assertEqual((got['held'], got.get('reason'), got.get('task'), got.get('held_by')),
                         (False, 'belongs_to_task', 'Task Reg', None), got)
        time.sleep(0.05)
        self.assertEqual(self.rows_of('path:src/reg.py', 'HOLD'), [])                 # nothing was written but REFUSED
        ref = self.rows_of('path:src/reg.py', 'REFUSED')
        self.assertEqual([(r['agent'], r.get('nonce') or '') for r in ref], [('b', '')])
        got = self.as_agent('a', M.claim, 'task reg')
        self.assertEqual((got['held'], got.get('paths')), (True, ['path:src/reg.py']), got)
        time.sleep(0.05)
        holds = self.rows_of('path:src/reg.py', 'HOLD')
        self.assertEqual([(r['agent'], r['nonce']) for r in holds], [('a', got['token'])])   # the file's HOLD is written
        got = self.as_agent('b', M.claim, 'src/reg.py')
        self.assertEqual((got['held'], got.get('reason'), got.get('task'), got.get('held_by')),
                         (False, 'belongs_to_task', 'Task Reg', 'a'))
        # Another task's bundle naming the file is refused too, with the file as conflict_key.
        got = self.as_agent('b', M.claim, 'task other', False, ['src/reg.py'])
        self.assertEqual((got['held'], got.get('reason'), got.get('conflict_key')), (False, 'belongs_to_task', 'path:src/reg.py'))
        # The task's holder claims its file as a renew.
        got = self.as_agent('a', M.claim, 'src/reg.py')
        self.assertEqual((got['held'], got.get('renewed')), (True, True), got)
        self.assertEqual([(t['name'], t['state'], t.get('held_by')) for t in self.as_agent('o', M.tasks)['tasks']],
                         [('Task Reg', 'held', 'a')])
        rel = self.as_agent('a', M.release, 'task reg', True)
        self.assertEqual((rel['finished'], rel.get('paths')), (True, ['path:src/reg.py']))
        time.sleep(0.05)
        self.assertEqual(M.read_key(M.hold_rows('path:src/reg.py'), M.now_us(), M.LEASE_S, 'path:src/reg.py')['open'], [])
        got = self.as_agent('b', M.claim, 'src/reg.py')
        self.assertEqual((got['held'], got.get('reason')), (False, 'belongs_to_task'))

    def test_bundle_race_one_key(self):
        """Two claims of one registered task race on the task key alone: exactly one holds it and its file."""
        self.as_agent('o', M.task, 'task race', 'x', ['src/race.py'])
        time.sleep(0.05)
        ps = [self.spawn(a, 'claim', resource='task race') for a in ('a', 'b', 'c')]
        got = [self.result(p) for p in ps]
        self.assertEqual(sum(g['held'] for g in got), 1, got)

if __name__ == '__main__':
    unittest.main(verbosity=2)
