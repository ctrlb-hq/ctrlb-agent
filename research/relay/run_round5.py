"""relay-stall-takeover-2026-11-09 (round 5): the Proof cells of ~/benchmarks/plans/relay-stall-takeover-prompt.md as
one queue of single trials, the way run_benchmark.py ran the benchmark.

Trials run in groups of identical relay settings (settings are shared per workspace in Mongo); inside a group two lanes
(A and B) pull single trials from one queue, each a `relay_proof.py <arm> --trials 1 --first k --lane L` under
with_token.sh and relaykey with RELAY_PROOF_NO_RESTORE=1. A trial whose result file exists is skipped, so a rerun
resumes. Posts STATUS to Relay (run relay-stall-takeover-2026-11-09) when a group starts, after every cell, and at least
every 15 minutes, with the settings in force; at the end it restores the BASE settings and writes the round's tables
(round5_analyse.py).

  G1 stall 60 (holdRequired true, evidenceRequired true, leaseSeconds 600, stallAfterSeconds 60):
     handoff1-control toy x5 (takeover proof), handoff1-registry toy x3 (path-hold proof)
  G2 Relay default (holdRequired true, evidenceRequired true, leaseSeconds 600, stallAfterSeconds 300):
     pair-relay readtime Sonnet x3 (v8 text alone), contracts-reader / -control and contracts-provider / -control
     readtime Haiku x5 each (section C)

The relay arms run with --evidence-required --task-tests, as in the benchmark (its handoff1-control, pair-relay Sonnet
and arm 6 cells), so the numbers sit beside the benchmark's.

Usage (python3 -I, standard library only; never reads or prints a key):
  python3 -I run_round5.py [--only G1 G2] [--dry]
Env: BENCH_TOOLS (dir with with_token.sh and relaykey), BENCH_SEEKER_DIR (Seeker worktree whose .env relaykey reads;
     default ~/CtrlB/seeker-relay-takeover), RELAY_PLUGIN (the v8 plugin; default
     ~/CtrlB/ctrlb-agent-relay-takeover/plugin).
"""
import argparse, json, os, queue, statistics, subprocess, sys, threading, time

HERE = os.path.dirname(os.path.abspath(__file__))
RUN = 'relay-stall-takeover-2026-11-09'
OUT = os.path.join(HERE, 'results', 'relay-stall-takeover')
TOOLS = os.environ.get('BENCH_TOOLS', '')
SEEKER_DIR = os.path.expanduser(os.environ.get('BENCH_SEEKER_DIR', '~/CtrlB/seeker'))
PLUGIN = os.path.expanduser(os.environ.get('RELAY_PLUGIN') or os.path.join(HERE, '..', '..', 'plugin'))
HEARTBEAT_S = 15 * 60
EV = ['--evidence-required', '--task-tests']


def J(cell, arm, project, model, trials, sub, budget=15):
    return [dict(cell=cell, arm=arm, project=project, model=model, k=k, sub=sub, flags=EV, budget=budget,
                 plugin=PLUGIN) for k in range(1, trials + 1)]


def interleave(*lists):
    out = []
    for i in range(max(len(x) for x in lists)):
        out += [x[i] for x in lists if i < len(x)]
    return out


def groups():
    rt, toy = 'devbench:readtime', 'toy'
    return [
        ('G1 stall 60', 'holdRequired true, evidenceRequired true, leaseSeconds 600, stallAfterSeconds 60',
         interleave(J('handoff1-control toy haiku', 'handoff1-control', toy, 'haiku', 5, 'takeover', budget=20),
                    J('handoff1-registry toy haiku', 'handoff1-registry', toy, 'haiku', 3, 'path-hold', budget=20))),
        ('G2 Relay default', 'holdRequired true, evidenceRequired true, leaseSeconds 600, stallAfterSeconds 300',
         J('pair-relay readtime sonnet', 'pair-relay', rt, 'sonnet', 3, 'sonnet')
         + interleave(J('contracts-reader readtime', 'contracts-reader', rt, 'haiku', 5, 'contracts'),
                      J('contracts-reader-control readtime', 'contracts-reader-control', rt, 'haiku', 5, 'contracts'),
                      J('contracts-provider readtime', 'contracts-provider', rt, 'haiku', 5, 'contracts'),
                      J('contracts-provider-control readtime', 'contracts-provider-control', rt, 'haiku', 5,
                        'contracts'))),
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
                       text=True, timeout=60, env=dict(os.environ, SWARM_RUN=RUN, SWARM_AGENT='orchestrator'))
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
    tk = res.get('takeover') or {}
    extra = (f" takeovers {tk.get('n')} tasks {tk.get('tasks_completed')}/{tk.get('tasks')}" if tk else '')
    return (f"unit {res.get('unit_pass')} accept {res.get('accept_pass')} dup {res.get('duplicated_n')}{extra} "
            f"${res.get('cost_usd')} {res.get('wall_s')} s")


def cell_line(cell, jobs):
    rs = [x for x in (load(j) for j in jobs) if x]
    if not rs:
        return f'{cell}: no results'
    m = lambda k: round(statistics.mean([r[k] for r in rs if isinstance(r.get(k), (int, float))] or [0]), 3)
    line = (f"{cell} n={len(rs)}/{len(jobs)}: unit {m('unit_pass')} accept {m('accept_pass')} dup {m('duplicated_n')} "
            f"cost/trial ${m('cost_usd')} wall {round(m('wall_s'))} s")
    tks = [r['takeover'] for r in rs if r.get('takeover')]
    if tks:
        line += (f"; takeovers {sum(t['n'] for t in tks)} (tasks {sum(t['n_tasks'] for t in tks)}, paths "
                 f"{sum(t['n_paths'] for t in tks)}); 12/12 in {sum(t['tasks_completed'] == t['tasks'] for t in tks)}"
                 f"/{len(tks)}")
    return line


def all_jobs():
    return [j for _, _, js in groups() for j in js]


def spent():
    rs = [x for x in (load(j) for j in all_jobs()) if x]
    return len(rs), round(sum(r.get('cost_usd') or 0 for r in rs), 2)


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
    ap.add_argument('--only', nargs='*', help='group ids to run (G1 G2); default all')
    ap.add_argument('--dry', action='store_true')
    a = ap.parse_args()
    os.makedirs(OUT, exist_ok=True)
    gs = [g for g in groups() if not a.only or g[0].split()[0] in a.only]
    if a.dry:
        for gid, sett, jobs in gs:
            print(gid, '|', sett, '|', len(jobs), 'trials,', sum(1 for j in jobs if load(j)), 'done')
            for j in jobs:
                print('   ', j['cell'], j['k'], result_path(j)[len(OUT) + 1:], ' '.join(j['flags']),
                      f"budget {j['budget']} min", j['plugin'])
        print('total', len(all_jobs()), '| seeker dir', SEEKER_DIR, '| tools', TOOLS or '(BENCH_TOOLS unset)')
        return
    if not (TOOLS and os.path.isdir(PLUGIN)):
        sys.exit('BENCH_TOOLS must be set and RELAY_PLUGIN must be the v8 plugin dir')
    hb = Heartbeat()
    hb.start()
    posted_cells = set()
    for gid, sett, jobs in gs:
        hb.group = f'{gid} ({sett})'
        log(f'=== {gid}: {len(jobs)} trials; settings: {sett}')
        post(f'STATUS: {gid} starts, {len(jobs)} trials in two lanes. Settings in force for it: {sett}; '
             f'sessionsDataset relaycoord_542c9dfe, waitMs 20, enforce. Rule text v8 ({PLUGIN}).')
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

        ts = [threading.Thread(target=lane, args=(n,)) for n in ('A', 'B')]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        n, usd = spent()
        log(f'=== {gid} done ({n} trials, ${usd} so far)')
    hb.stop_ev.set()
    restored = under_token(RESTORE_PY)
    n, usd = spent()
    post(f'STATUS: round 5 cells run: {n} trials, ${usd}; settings restored to BASE: {json.dumps(restored)}.')
    r = subprocess.run([sys.executable, '-I', os.path.join(HERE, 'round5_analyse.py')], capture_output=True, text=True)
    log(f'round5_analyse.py exit {r.returncode}: {(r.stdout + r.stderr)[-300:]}')
    log('=== done')


if __name__ == '__main__':
    main()
