"""relay-redirect-2026-11-09 (round 6): the Proof cells of ~/benchmarks/plans/relay-round6-prompt.md as one queue of
single trials, one at a time, each a `relay_proof.py <arm> --trials 1 --first k` under with_token.sh and relaykey.

Trials run in groups of identical relay settings (settings are shared per workspace in Mongo). Every trial sets its
arm's settings (relay_proof BASE plus the arm's, plus evidenceRequired) with RELAY_PROOF_NO_RESTORE=1; the group
restores BASE when it ends, and the run restores it at the end. A trial whose result file exists is skipped, so a rerun
resumes. Posts STATUS to Relay (run relay-redirect-2026-11-09) when a group starts (with the settings in force), after
every cell, at least every 15 minutes, and once after the first cell with the projected cost of the whole queue.
Stops before a trial when the spend so far is above --max-usd (40). At the end it writes the round's tables
(round6_analyse.py).

Every relay arm runs with --evidence-required --every-task-test: under evidenceRequired Seeker refuses a task
registered without a test command (test_required, decision 38), so every task gets one, and readtime's packages are
installed in its own venv (RELAY_PROOF_VENV_ROOT), first on the agents' PATH. Rule text v9 comes from the plugin
(RELAY_PLUGIN). REGISTRY.md is registered as nothing: the v9 text tells agents to claim slot:REGISTRY.md with
paths=["REGISTRY.md"] for the write (section F).

  G-A (holdRequired, evidenceRequired, enforce, leaseSeconds 600, waitMs 20, stallAfterSeconds 300, witnessRequired
       off, staleContracts on): pair-relay hone x5 (the first cell), pair-relay readtime x5, quad-relay hone x5,
       pair-relay readtime Sonnet x3
  G-D (G-A plus witnessRequired on): pair-relay-witness hone x5, readtime x5; false-evidence hone x1, readtime x1
  G-B (G-A but stallAfterSeconds 60): handoff1-stop toy x5 (SIGTERM: the SessionEnd hook fires), handoff1-kill toy x3
       (SIGKILL: it cannot)
  G-C (G-A): contracts-provider readtime x5 (staleContracts on), contracts-provider-staleoff readtime x5 (the
       control: staleContracts off)

Usage (python3 -I, standard library only; never reads or prints a key):
  python3 -I run_round6.py [--cells CELL ...] [--groups G-A ...] [--dry-run] [--max-usd 40]
Env: BENCH_TOOLS (dir with with_token.sh and relaykey), BENCH_SEEKER_DIR (the Seeker worktree whose .env relaykey
     reads; default ~/CtrlB/seeker-relay-redirect), RELAY_PLUGIN (the v9 plugin; default
     ~/CtrlB/ctrlb-agent-relay-redirect/plugin), RELAY_SESSIONS_DATASET (default relaycoord_542c9dfe).
"""
import argparse, json, os, statistics, subprocess, sys, threading, time

HERE = os.path.dirname(os.path.abspath(__file__))
RUN = 'relay-redirect-2026-11-09'
OUT = os.environ.get('ROUND6_OUT') or os.path.join(HERE, 'results', 'relay-redirect')
TOOLS = os.environ.get('BENCH_TOOLS', '')
SEEKER_DIR = os.path.expanduser(os.environ.get('BENCH_SEEKER_DIR', '~/CtrlB/seeker-relay-redirect'))
PLUGIN = os.path.expanduser(os.environ.get('RELAY_PLUGIN', '~/CtrlB/ctrlb-agent-relay-redirect/plugin'))
DATASET = os.environ.get('RELAY_SESSIONS_DATASET', 'relaycoord_542c9dfe')
VENVS = os.path.join(OUT, 'venvs')
HEARTBEAT_S = 15 * 60
EV = ['--evidence-required', '--every-task-test']
# What one trial costs, for the projection before the first cell is in (round 5 and the benchmark: Haiku pair
# $0.045-0.065, quad $0.07-0.09, toy handoff $0.05-0.06, Sonnet pair $0.41-0.43).
PRIOR_USD = {('pair', 'haiku'): 0.06, ('quad', 'haiku'): 0.09, ('toy', 'haiku'): 0.055, ('pair', 'sonnet'): 0.42}

S_A = ('holdRequired true, evidenceRequired true, enforce, leaseSeconds 600, waitMs 20, stallAfterSeconds 300, '
       'witnessRequired false, staleContracts true')
S_D = S_A.replace('witnessRequired false', 'witnessRequired true')
S_B = S_A.replace('stallAfterSeconds 300', 'stallAfterSeconds 60')
S_C = S_A + ' (control trials: staleContracts false)'


def J(cell, arm, project, model, trials, sub, budget=15):
    return [dict(cell=cell, arm=arm, project=project, model=model, k=k, sub=sub, flags=EV, budget=budget)
            for k in range(1, trials + 1)]


def interleave(*lists):
    out = []
    for i in range(max(len(x) for x in lists)):
        out += [x[i] for x in lists if i < len(x)]
    return out


def groups():
    rt, hone, toy = 'devbench:readtime', 'devbench:hone', 'toy'
    return [
        ('G-A', S_A,
         J('pair-relay hone', 'pair-relay', hone, 'haiku', 5, 'redirect')
         + J('pair-relay readtime', 'pair-relay', rt, 'haiku', 5, 'redirect')
         + J('quad-relay hone', 'quad-relay', hone, 'haiku', 5, 'redirect')
         + J('pair-relay readtime sonnet', 'pair-relay', rt, 'sonnet', 3, 'sonnet')),
        ('G-D', S_D,
         J('pair-relay-witness hone', 'pair-relay-witness', hone, 'haiku', 5, 'witness')
         + J('pair-relay-witness readtime', 'pair-relay-witness', rt, 'haiku', 5, 'witness')
         + J('false-evidence hone', 'false-evidence', hone, 'haiku', 1, 'witness')
         + J('false-evidence readtime', 'false-evidence', rt, 'haiku', 1, 'witness')),
        ('G-B', S_B,
         interleave(J('handoff1-stop toy', 'handoff1-stop', toy, 'haiku', 5, 'handoff', budget=20),
                    J('handoff1-kill toy', 'handoff1-kill', toy, 'haiku', 3, 'handoff', budget=20))),
        ('G-C', S_C,
         interleave(J('contracts-provider readtime', 'contracts-provider', rt, 'haiku', 5, 'contracts'),
                    J('contracts-provider-staleoff readtime', 'contracts-provider-staleoff', rt, 'haiku', 5,
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


def log(line):
    line = time.strftime('%H:%M:%SZ', time.gmtime()) + ' ' + line
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, 'run.log'), 'a') as fh:
        fh.write(line + '\n')
    print(line, flush=True)


def post(text, dry=False):
    if dry:
        print('would post:', text)
        return
    r = subprocess.run([sys.executable, '-I', os.path.join(HERE, 'post_status.py'), text], capture_output=True,
                       text=True, timeout=60, env=dict(os.environ, SWARM_RUN=RUN, SWARM_AGENT='orchestrator'))
    log(('posted: ' if r.returncode == 0 else f'POST FAILED ({r.returncode}): ') + text[:160])


SETTINGS_PY = ("import sys, json; sys.path.insert(0, %r); import relay_proof as r; "
               "st, d = r.http('GET', '/ai/relay/settings', cred='SEEKER_JWT'); "
               "print(json.dumps({k: d.get(k) for k in r.SETTING_KEYS} if st == 200 else {'error': st}))") % HERE
RESTORE_PY = ("import sys, json; sys.path.insert(0, %r); import relay_proof as r; "
              "print(json.dumps(r.put_settings({})))") % HERE


def child_env():
    return dict(os.environ, RELAY_SESSIONS_DATASET=DATASET, RELAY_PLUGIN=PLUGIN, RELAY_PROOF_VENV_ROOT=VENVS)


def under_token(code, key=False):
    cmd = [os.path.join(TOOLS, 'with_token.sh')] + ([os.path.join(TOOLS, 'relaykey'), '--'] if key else []) \
        + [sys.executable, '-I', '-c', code]
    r = subprocess.run(cmd, cwd=SEEKER_DIR, capture_output=True, text=True, timeout=120, env=child_env())
    try:
        return json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {'error': (r.stderr or r.stdout)[-200:]}


def settings_now():
    s = under_token(SETTINGS_PY)
    if 'error' in s:
        return f'settings unreadable ({s["error"]})'
    return ', '.join(f'{k} {v}' for k, v in s.items())


def job_cmd(j):
    return [os.path.join(TOOLS, 'with_token.sh'), os.path.join(TOOLS, 'relaykey'), '--', sys.executable, '-I',
            os.path.join(HERE, 'relay_proof.py'), j['arm'], '--project', j['project'], '--trials', '1', '--first',
            str(j['k']), '--model', j['model'], '--budget-min', str(j['budget'])] + j['flags']


def run_job(j):
    if load(j):
        return 'skipped (result exists)'
    os.makedirs(os.path.join(OUT, 'logs'), exist_ok=True)
    env = dict(child_env(), RELAY_PROOF_OUT=os.path.join(OUT, j['sub']), RELAY_PROOF_NO_RESTORE='1')
    name = f"{j['sub']}-{j['arm']}-{j['project'].split(':')[-1]}-{j['model']}-{j['k']:02d}"
    t0 = time.time()
    with open(os.path.join(OUT, 'logs', name + '.log'), 'w') as fh:
        r = subprocess.run(job_cmd(j), cwd=SEEKER_DIR, env=env, stdout=fh, stderr=subprocess.STDOUT)
    res = load(j)
    if r.returncode != 0 or not res:
        return f'FAILED exit {r.returncode} after {round(time.time() - t0)} s (log logs/{name}.log)'
    rd, wi, oh = res.get('redirect') or {}, res.get('witness') or {}, res.get('open_handoff') or {}
    extra = f" refused/hold {rd.get('refused_per_hold')} free-list refusals {rd.get('refusals_with_free')}"
    if oh:
        extra += f" hook handoffs {oh.get('handoffs')}"
    if wi.get('blocked_by_hook'):
        extra += f" done blocked {wi.get('blocked_by_hook')}"
    return (f"unit {res.get('unit_pass')} accept {res.get('accept_pass')} dup {res.get('duplicated_n')} "
            f"turns {res.get('turns')}{extra} ${res.get('cost_usd')} {res.get('wall_s')} s")


def mean(xs, nd=3):
    xs = [x for x in xs if isinstance(x, (int, float)) and not isinstance(x, bool)]
    return round(statistics.mean(xs), nd) if xs else None


def cell_line(cell, jobs):
    rs = [x for x in (load(j) for j in jobs) if x]
    if not rs:
        return f'{cell}: no results'
    return (f"{cell} n={len(rs)}/{len(jobs)}: unit {mean([r.get('unit_pass') for r in rs])} accept "
            f"{mean([r.get('accept_pass') for r in rs])} dup {mean([r.get('duplicated_n') for r in rs])} turns "
            f"{mean([r.get('turns') for r in rs], 1)} refused/hold "
            f"{mean([(r.get('redirect') or {}).get('refused_per_hold') for r in rs])} cost/trial "
            f"${mean([r.get('cost_usd') for r in rs])} wall {mean([r.get('wall_s') for r in rs], 0)} s")


def all_jobs():
    return [j for _, _, js in groups() for j in js]


def spent():
    rs = [x for x in (load(j) for j in all_jobs()) if x]
    return len(rs), round(sum(r.get('cost_usd') or 0 for r in rs), 2)


def kind_of(j):
    return 'toy' if j['project'] == 'toy' else 'quad' if j['arm'].startswith('quad') else 'pair'


def projection():
    """Projected cost of the whole queue: done trials at their cost, the rest at the mean of this round's trials of
    the same kind and model so far, else PRIOR_USD scaled by this round's Haiku pair ratio to its prior."""
    per, done_usd, left = {}, 0.0, {}
    for j in all_jobs():
        r = load(j)
        k = (kind_of(j), j['model'])
        if r:
            per.setdefault(k, []).append(r.get('cost_usd') or 0)
            done_usd += r.get('cost_usd') or 0
        else:
            left[k] = left.get(k, 0) + 1
    hp = per.get(('pair', 'haiku'))
    scale = (statistics.mean(hp) / PRIOR_USD[('pair', 'haiku')]) if hp else 1.0
    est = {k: (statistics.mean(per[k]) if per.get(k) else PRIOR_USD[k] * scale) for k in PRIOR_USD}
    rest = sum(n * est[k] for k, n in left.items())
    return dict(done_usd=round(done_usd, 2), remaining_trials=sum(left.values()), remaining_usd=round(rest, 2),
                total_usd=round(done_usd + rest, 2), per_trial_usd={f'{a} {b}': round(v, 4) for (a, b), v in est.items()},
                scale_vs_prior=round(scale, 2))


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
    ap.add_argument('--groups', nargs='*', help='group ids (G-A G-D G-B G-C); default all, in that order')
    ap.add_argument('--cells', nargs='*', help='cell names to run (e.g. "pair-relay hone"); default all of the groups')
    ap.add_argument('--dry-run', action='store_true', help='print the queue, the commands and the projection; run nothing')
    ap.add_argument('--max-usd', type=float, default=40.0)
    a = ap.parse_args()
    gs = [g for g in groups() if not a.groups or g[0] in a.groups]
    if a.cells:
        gs = [(g, s, [j for j in js if j['cell'] in a.cells]) for g, s, js in gs]
        gs = [g for g in gs if g[2]]
    if a.dry_run:
        for gid, sett, jobs in gs:
            print(gid, '|', sett, '|', len(jobs), 'trials,', sum(1 for j in jobs if load(j)), 'done')
            for j in jobs:
                print('   ', j['cell'], j['k'], os.path.relpath(result_path(j), OUT), f"budget {j['budget']} min")
        if gs and gs[0][2]:
            print('first command:', ' '.join(job_cmd(gs[0][2][0])))
        print('total', len(all_jobs()), '| seeker dir', SEEKER_DIR, '| plugin', PLUGIN, '| dataset', DATASET,
              '| tools', TOOLS or '(BENCH_TOOLS unset)')
        print('projection:', json.dumps(projection()))
        return
    if not (TOOLS and os.path.isdir(PLUGIN)):
        sys.exit('BENCH_TOOLS must be set and RELAY_PLUGIN must be the v9 plugin dir')
    os.makedirs(OUT, exist_ok=True)
    hb = Heartbeat()
    hb.start()
    posted_cells, logged_arms = set(), set()
    projected, stopped = os.path.exists(os.path.join(OUT, 'projection.json')), False
    for gid, sett, jobs in gs:
        hb.group = f'{gid} ({sett})'
        log(f'=== {gid}: {len(jobs)} trials; settings: {sett}')
        post(f'STATUS: {gid} starts, {len(jobs)} trials, one at a time. Settings in force for it: {sett}; '
             f'sessionsDataset {DATASET}. Rule text v9 ({PLUGIN}).')
        cells = {}
        for j in jobs:
            cells.setdefault(j['cell'], []).append(j)
        for j in jobs:
            n, usd = spent()
            if usd > a.max_usd:
                log(f'STOP: ${usd} spent, above ${a.max_usd}')
                post(f'BLOCKED: round 6 stopped before {j["cell"]} #{j["k"]}: ${usd} spent is above ${a.max_usd}.')
                stopped = True
                break
            log(f'start {j["cell"]} #{j["k"]}')
            out = run_job(j)
            log(f'{j["cell"]} #{j["k"]}: {out}')
            if j['arm'] not in logged_arms:
                # what the trial left in force (RELAY_PROOF_NO_RESTORE=1): the arm's settings as Seeker reads them
                logged_arms.add(j['arm'])
                log(f'settings in force after {j["arm"]} #{j["k"]}: {settings_now()}')
            j['_done'] = True
            cj = cells[j['cell']]
            if j['cell'] not in posted_cells and all(x.get('_done') for x in cj):
                posted_cells.add(j['cell'])
                post(f'STATUS: cell done. {cell_line(j["cell"], cj)}. Settings: {sett}.')
                if not projected:
                    p = projection()
                    with open(os.path.join(OUT, 'projection.json'), 'w') as fh:
                        json.dump(dict(p, after_cell=j['cell'], at=time.time()), fh, indent=1)
                    post(f'STATUS: projection after the first cell ({j["cell"]}): ${p["total_usd"]} for the whole '
                         f'queue (${p["done_usd"]} spent, {p["remaining_trials"]} trials left at about '
                         f'${p["remaining_usd"]}; per trial {p["per_trial_usd"]}). The stop is ${a.max_usd}.')
                    projected = True
        restored = under_token(RESTORE_PY)
        n, usd = spent()
        log(f'=== {gid} done ({n} trials, ${usd} so far); settings restored to BASE: {json.dumps(restored)}')
        if stopped:
            break
    hb.stop_ev.set()
    restored = under_token(RESTORE_PY)
    n, usd = spent()
    post(f'STATUS: round 6 cells run: {n} trials, ${usd}; settings restored to BASE: {json.dumps(restored)}.')
    r = subprocess.run([sys.executable, '-I', os.path.join(HERE, 'round6_analyse.py')], capture_output=True, text=True)
    log(f'round6_analyse.py exit {r.returncode}: {(r.stdout + r.stderr)[-300:]}')
    log('=== done')


if __name__ == '__main__':
    main()
