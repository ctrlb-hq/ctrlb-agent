"""relay-benchmark-2026-11-09: every arm of ~/benchmarks/plans/relay-benchmark-prompt.md as one queue of single trials.

Trials run in groups of identical relay settings (settings are shared per workspace in Mongo); inside a group two lanes
(A and B) pull single trials from one queue, each a `relay_proof.py <arm> --trials 1 --first k --lane L` under
with_token.sh and relaykey with RELAY_PROOF_NO_RESTORE=1. A trial whose result file exists is skipped, so a rerun
resumes. Posts STATUS to Relay after every cell and every benchmark arm and at least every 15 minutes, with the settings
in force; after arm 1 on hone it posts the cost per trial and the projection for the rest, and stops with BLOCKED when
the projection is above $150. At the end it restores the BASE settings and replays arm 3 through stall_replay.py.

Usage (python3 -I, standard library only; never reads or prints a key):
  python3 -I run_benchmark.py [--only GROUP ...] [--dry]
Env: BENCH_TOOLS (dir with with_token.sh and relaykey), BENCH_SEEKER_DIR (Seeker worktree whose .env relaykey reads),
     RELAY_PLUGIN (v7 plugin), BENCH_PLUGIN_NOWAIT (the control copy without the wait_s sentence).
"""
import argparse, json, os, queue, statistics, subprocess, sys, threading, time

HERE = os.path.dirname(os.path.abspath(__file__))
RUN = 'relay-benchmark-2026-11-09'
OUT = os.path.join(HERE, 'results', 'relay-benchmark')
TOOLS = os.environ.get('BENCH_TOOLS', '')
SEEKER_DIR = os.path.expanduser(os.environ.get('BENCH_SEEKER_DIR', '~/CtrlB/seeker'))
PLUGIN = os.path.expanduser(os.environ.get('RELAY_PLUGIN') or os.path.join(HERE, '..', '..', 'plugin'))
PLUGIN_NOWAIT = os.environ.get('BENCH_PLUGIN_NOWAIT', '')
LIMIT_USD = 150.0
HEARTBEAT_S = 15 * 60

EV = ['--evidence-required', '--task-tests']   # the benchmark's relay settings: evidenceRequired true, task tests
TT = ['--task-tests']                          # no-Relay arms get the same task information (tests named in TASKS.md)


def J(bench, cell, arm, project, model, trials, sub, flags, budget=15, plugin=None):
    return [dict(bench=bench, cell=cell, arm=arm, project=project, model=model, k=k, sub=sub, flags=flags,
                 budget=budget, plugin=plugin or PLUGIN) for k in range(1, trials + 1)]


def interleave(*lists):
    out = []
    for i in range(max(len(x) for x in lists)):
        out += [x[i] for x in lists if i < len(x)]
    return out


def groups():
    hb, rt, toy = 'devbench:hone', 'devbench:readtime', 'toy'
    return [
        ('G1 hone no-Relay', 'BASE (no Relay in these arms)',
         J(1, 'solo hone haiku', 'solo', hb, 'haiku', 5, 'arm1', TT)
         + J(1, 'pair-none hone haiku', 'pair-none', hb, 'haiku', 10, 'arm1', TT)),
        ('G2 hone Relay', 'holdRequired true, evidenceRequired true, leaseSeconds 600, stallAfterSeconds 300',
         interleave(J(1, 'pair-relay hone haiku', 'pair-relay', hb, 'haiku', 10, 'arm1', EV),
                    J(1, 'quad-relay hone haiku', 'quad-relay', hb, 'haiku', 5, 'arm1', EV))),
        ('G3 readtime no-Relay', 'BASE (no Relay in these arms)',
         J(1, 'pair-none readtime sonnet', 'pair-none', rt, 'sonnet', 5, 'arm1', TT)
         + J(1, 'solo readtime haiku', 'solo', rt, 'haiku', 5, 'arm1', TT)
         + J(1, 'pair-none readtime haiku', 'pair-none', rt, 'haiku', 10, 'arm1', TT)),
        ('G4 Relay default', 'holdRequired true, evidenceRequired true, leaseSeconds 600, stallAfterSeconds 300',
         J(1, 'pair-relay readtime sonnet', 'pair-relay', rt, 'sonnet', 5, 'arm1', EV)
         + interleave(J(1, 'pair-relay readtime haiku', 'pair-relay', rt, 'haiku', 10, 'arm1', EV),
                      J(1, 'quad-relay readtime haiku', 'quad-relay', rt, 'haiku', 5, 'arm1', EV))
         + J(3, 'handoff3 toy haiku', 'handoff3', toy, 'haiku', 5, 'arm3', EV, budget=20)
         + interleave(J(2, 'quad-relay toy v7', 'quad-relay', toy, 'haiku', 5, 'arm2/v7', EV),
                      J(2, 'quad-relay toy v7-nowait', 'quad-relay', toy, 'haiku', 5, 'arm2/v7-nowait', EV,
                        plugin=PLUGIN_NOWAIT))
         + interleave(J(6, 'questions readtime', 'questions', rt, 'haiku', 5, 'arm6', EV),
                      J(6, 'questions-control readtime', 'questions-control', rt, 'haiku', 5, 'arm6', EV),
                      J(6, 'contracts readtime', 'contracts', rt, 'haiku', 5, 'arm6', EV),
                      J(6, 'contracts-control readtime', 'contracts-control', rt, 'haiku', 5, 'arm6', EV))),
        ('G5 stall 60', 'holdRequired true, evidenceRequired true, leaseSeconds 600, stallAfterSeconds 60',
         J(3, 'handoff3-control toy haiku', 'handoff3-control', toy, 'haiku', 5, 'arm3', EV, budget=20)),
        ('G6 evidence off', 'holdRequired true, evidenceRequired false, leaseSeconds 600, stallAfterSeconds 300',
         J(4, 'pair-relay hone evidence-off', 'pair-relay', hb, 'haiku', 5, 'arm4', TT)),
        ('G7 fence-off', 'holdRequired false, evidenceRequired true, leaseSeconds 60, stallAfterSeconds 300',
         interleave(J(5, 'fence-off hone', 'fence-off', hb, 'haiku', 3, 'arm5', EV),
                    J(5, 'fence-off readtime', 'fence-off', rt, 'haiku', 3, 'arm5', EV))),
        ('G8 fence-on', 'holdRequired true, evidenceRequired true, leaseSeconds 60, stallAfterSeconds 300',
         interleave(J(5, 'fence-on hone', 'fence-on', hb, 'haiku', 3, 'arm5', EV),
                    J(5, 'fence-on readtime', 'fence-on', rt, 'haiku', 3, 'arm5', EV))),
        # supplementary (decision 21:55Z): arm 3's toy pair finished before the 3-minute shift in all 5 handoff3 trials
        ('G9 stall 60 short shift', 'holdRequired true, evidenceRequired true, leaseSeconds 600, stallAfterSeconds 60',
         interleave(J(3, 'handoff1 toy haiku', 'handoff1', toy, 'haiku', 5, 'arm3-short', EV, budget=20),
                    J(3, 'handoff1-control toy haiku', 'handoff1-control', toy, 'haiku', 5, 'arm3-short', EV,
                      budget=20))),
    ]


def result_path(j):
    name = j['project'].split(':', 1)[-1]
    return os.path.join(OUT, j['sub'], j['arm'], name, f"{j['model']}-{j['k']:02d}.json")


def load(j):
    try:
        with open(result_path(j)) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


LOG_LOCK = threading.Lock()


def log(line):
    line = time.strftime('%H:%M:%SZ', time.gmtime()) + ' ' + line
    with LOG_LOCK:
        with open(os.path.join(OUT, 'run.log'), 'a') as fh:
            fh.write(line + '\n')
        print(line, flush=True)


def post(text):
    r = subprocess.run([sys.executable, '-I', os.path.join(HERE, 'post_status.py'), text], capture_output=True,
                       text=True, timeout=60, env=dict(os.environ, SWARM_RUN=RUN))
    log(('posted: ' if r.returncode == 0 else f'POST FAILED ({r.returncode}): ') + text[:160])


SETTINGS_PY = ("import sys, json; sys.path.insert(0, %r); import relay_proof as r; "
               "st, d = r.http('GET', '/ai/relay/settings', cred='SEEKER_JWT'); "
               "print(json.dumps({k: d.get(k) for k in r.SETTING_KEYS} if st == 200 else {'error': st}))") % HERE
RESTORE_PY = ("import sys, json; sys.path.insert(0, %r); import relay_proof as r; "
              "print(json.dumps(r.put_settings({})))") % HERE


def under_token(code, key=False):
    cmd = [os.path.join(TOOLS, 'with_token.sh')] + ([os.path.join(TOOLS, 'relaykey'), '--'] if key else []) \
        + [sys.executable, '-I', '-c', code]
    r = subprocess.run(cmd, cwd=SEEKER_DIR, capture_output=True, text=True, timeout=120)
    try:
        return json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {'error': (r.stderr or r.stdout)[-200:]}


def settings_now():
    s = under_token(SETTINGS_PY)
    if 'error' in s:
        return f'settings unreadable ({s["error"]})'
    return ', '.join(f'{k} {v}' for k, v in s.items())


def run_job(j, lane):
    if load(j):
        return 'skipped (result exists)'
    os.makedirs(os.path.join(OUT, 'logs'), exist_ok=True)
    env = dict(os.environ, RELAY_PROOF_OUT=os.path.join(OUT, j['sub']), RELAY_PLUGIN=j['plugin'],
               RELAY_PROOF_NO_RESTORE='1')
    cmd = [os.path.join(TOOLS, 'with_token.sh'), os.path.join(TOOLS, 'relaykey'), '--', sys.executable, '-I',
           os.path.join(HERE, 'relay_proof.py'), j['arm'], '--project', j['project'], '--trials', '1', '--first',
           str(j['k']), '--model', j['model'], '--budget-min', str(j['budget']), '--lane', lane] + j['flags']
    name = f"{j['sub'].replace('/', '_')}-{j['arm']}-{j['project'].split(':')[-1]}-{j['model']}-{j['k']:02d}"
    t0 = time.time()
    with open(os.path.join(OUT, 'logs', name + '.log'), 'w') as fh:
        r = subprocess.run(cmd, cwd=SEEKER_DIR, env=env, stdout=fh, stderr=subprocess.STDOUT)
    res = load(j)
    if r.returncode != 0 or not res:
        return f'FAILED exit {r.returncode} after {round(time.time() - t0)} s (log logs/{name}.log)'
    return (f"unit {res.get('unit_pass')} accept {res.get('accept_pass')} dup {res.get('duplicated_n')} "
            f"${res.get('cost_usd')} {res.get('wall_s')} s")


def cell_line(cell, jobs):
    rs = [x for x in (load(j) for j in jobs) if x]
    if not rs:
        return f'{cell}: no results'
    m = lambda k: round(statistics.mean([r[k] for r in rs if isinstance(r.get(k), (int, float))] or [0]), 3)
    return (f"{cell} n={len(rs)}/{len(jobs)}: unit {m('unit_pass')} accept {m('accept_pass')} abandoned "
            f"{sum(bool(r.get('abandoned')) for r in rs)} dup {m('duplicated_n')} redone {m('redone_share')} "
            f"cost/trial ${m('cost_usd')} wall {round(m('wall_s'))} s")


def all_jobs():
    return [j for _, _, js in groups() for j in js]


def spent():
    rs = [x for x in (load(j) for j in all_jobs()) if x]
    return len(rs), round(sum(r.get('cost_usd') or 0 for r in rs), 2)


def projection():
    """After arm 1 on hone: measured cost and wall per trial by arm, and the rest of the run projected from them."""
    hone = {}
    for j in all_jobs():
        if j['bench'] == 1 and j['project'] == 'devbench:hone':
            r = load(j)
            if r:
                hone.setdefault(j['arm'], []).append(r)
    c = {a: statistics.mean(r['cost_usd'] for r in rs) for a, rs in hone.items()}
    w = {a: statistics.mean(r['wall_s'] for r in rs) for a, rs in hone.items()}
    relay, quad = c.get('pair-relay', 0.3), c.get('quad-relay', 0.5)
    # Sonnet: the proof run's readtime figures (pair-none $0.313, pair-relay $0.374) scaled by this run's hone
    # pair-relay over the proof run's hone pair-relay is not known before arm 1, so they are taken as measured there,
    # times 1.25 for the evidence step.
    est = {'solo': c.get('solo', 0.2), 'pair-none': c.get('pair-none', 0.3), 'pair-relay': relay, 'quad-relay': quad,
           'handoff3': relay * 2, 'handoff3-control': relay * 2, 'questions': relay * 1.2,
           'questions-control': relay * 1.2, 'contracts': relay * 1.2, 'contracts-control': relay * 1.2,
           'fence-off': relay, 'fence-on': relay}
    sonnet = {'pair-none': 0.313 * 1.25, 'pair-relay': 0.374 * 1.25}
    wall = {a: w.get(a, w.get('pair-relay', 300)) for a in est}
    wall.update(handoff3=1200, **{'handoff3-control': 1200})
    rest_cost, rest_wall, n = 0.0, 0.0, 0
    for j in all_jobs():
        if load(j):
            continue
        n += 1
        if j['model'] == 'sonnet':
            rest_cost += sonnet[j['arm']]
        elif j['project'] == 'toy' and j['arm'] == 'quad-relay':
            rest_cost += quad * 2
        else:
            rest_cost += est[j['arm']]
        rest_wall += wall.get(j['arm'], 300) + 40  # setup, scoring and upload per trial
    done_n, done_cost = spent()
    return dict(cost_per_trial={a: round(v, 3) for a, v in c.items()}, wall_per_trial_s={a: round(v) for a, v in w.items()},
                done=done_n, spent=done_cost, remaining_trials=n, remaining_cost=round(rest_cost, 2),
                projected_total=round(done_cost + rest_cost, 2), remaining_wall_h=round(rest_wall / 2 / 3600, 1))


class Heartbeat(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.group, self.stop_ev = '', threading.Event()

    def run(self):
        while not self.stop_ev.wait(HEARTBEAT_S):
            n, usd = spent()
            post(f'STATUS: heartbeat. {self.group}; {n}/{len(all_jobs())} trials done, ${usd} so far. '
                 f'Settings in force: {settings_now()}.')


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--only', nargs='*', help='group ids to run (G1 ... G8); default all')
    ap.add_argument('--dry', action='store_true')
    a = ap.parse_args()
    if not (TOOLS and PLUGIN_NOWAIT and os.path.isdir(PLUGIN_NOWAIT)):
        sys.exit('BENCH_TOOLS and BENCH_PLUGIN_NOWAIT must be set')
    os.makedirs(OUT, exist_ok=True)
    gs = [g for g in groups() if not a.only or g[0].split()[0] in a.only]
    if a.dry:
        for gid, sett, jobs in gs:
            print(gid, '|', sett, '|', len(jobs), 'trials,', sum(1 for j in jobs if load(j)), 'done')
            for j in jobs:
                print('   ', j['bench'], j['cell'], j['k'], result_path(j)[len(OUT) + 1:], ' '.join(j['flags']),
                      os.path.basename(os.path.dirname(j['plugin'])) + '/' + os.path.basename(j['plugin']))
        print('total', len(all_jobs()))
        return
    hb = Heartbeat()
    hb.start()
    bench_jobs = {}
    for j in all_jobs():
        bench_jobs.setdefault(j['bench'], []).append(j)
    posted_arms, posted_cells = set(), set()
    for gid, sett, jobs in gs:
        hb.group = f'{gid} ({sett})'
        log(f'=== {gid}: {len(jobs)} trials; settings: {sett}')
        post(f'STATUS: {gid} starts, {len(jobs)} trials in two lanes. Settings in force for it: {sett}; '
             f'sessionsDataset relaycoord_542c9dfe, waitMs 20, enforce.')
        q = queue.Queue()
        for j in jobs:
            q.put(j)
        cells = {}
        for j in jobs:
            cells.setdefault(j['cell'], []).append(j)
        lock = threading.Lock()

        def lane(name):
            while True:
                try:
                    j = q.get_nowait()
                except queue.Empty:
                    return
                log(f'[{name}] start {j["cell"]} #{j["k"]}')
                out = run_job(j, name)
                log(f'[{name}] {j["cell"]} #{j["k"]}: {out}')
                with lock:
                    cj = cells[j['cell']]
                    j['_done'] = True
                    if j['cell'] not in posted_cells and all(x.get('_done') for x in cj):
                        posted_cells.add(j['cell'])
                        post(f'STATUS: cell done. {cell_line(j["cell"], cj)}. Settings: {sett}.')
                    bj = bench_jobs[j['bench']]
                    if j['bench'] not in posted_arms and all(load(x) or x.get('_done') for x in bj):
                        posted_arms.add(j['bench'])
                        n, usd = spent()
                        post(f'STATUS: arm {j["bench"]} done ({len(bj)} trials). Run so far: {n} trials, ${usd}.')

        ts = [threading.Thread(target=lane, args=(n,)) for n in ('A', 'B')]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        log(f'=== {gid} done')
        if gid.startswith('G2'):
            p = projection()
            over = p['projected_total'] > LIMIT_USD
            post('STATUS: arm 1 on hone done. Cost per trial (hone, Haiku): '
                 + ', '.join(f'{k} ${v}' for k, v in p['cost_per_trial'].items())
                 + '; wall per trial: ' + ', '.join(f'{k} {v} s' for k, v in p['wall_per_trial_s'].items())
                 + f'. Spent ${p["spent"]} on {p["done"]} trials; projected for the {p["remaining_trials"]} left '
                   f'${p["remaining_cost"]}, total ${p["projected_total"]}, about {p["remaining_wall_h"]} h of wall '
                   f'in two lanes. ' + ('Over the limit.' if over else 'Continuing.'))
            with open(os.path.join(OUT, 'projection.json'), 'w') as fh:
                json.dump(p, fh, indent=1)
            if over:
                post(f'BLOCKED: projected total ${p["projected_total"]} is above ${LIMIT_USD} (spent ${p["spent"]}, '
                     f'{p["remaining_trials"]} trials left at ${p["remaining_cost"]}; per trial '
                     f'{json.dumps(p["cost_per_trial"])}). Waiting for a go-ahead.')
                hb.stop_ev.set()
                sys.exit(3)
    hb.stop_ev.set()
    restored = under_token(RESTORE_PY)
    post(f'STATUS: all groups run; settings restored to BASE: {json.dumps(restored)}.')
    log('=== done')


if __name__ == '__main__':
    main()
