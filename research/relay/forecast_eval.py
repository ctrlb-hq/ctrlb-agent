"""Round 7 (relay-record-2026-11-10), decision 41: evaluate the contention forecast on held-out runs.

History is every run up to and including the full benchmark (results/relay-proof, relay-protocol, relay-done-stall,
relay-benchmark); held out are rounds 5 and 6 (results/relay-stall-takeover, results/relay-redirect, supp included).
For each held-out run with registered tasks, the forecast is asked of Seeker (POST /ai/relay/{dataset}/forecast) with
the run's registered tasks and as_of = the end of the history (the last row of the full benchmark's runs), so it
sees history alone. Both co-change modes are asked: "run" (decision 41 as written) and "hold".

Compared, per held-out run:
  hot keys: a task path is forecast hot when its hot_keys score is >= the threshold (0.25, 0.5, 0.75); it was
    actually hot when, in the run, a claim on it (its path key, or a task whose bundle names it) was refused, or two
    agents edited it. Precision and recall over (run, path) pairs, against refusals, duplicated edits, and either.
  sizing: recommended_agents against the agents with at least one done in the run.
Bundles, on the pair-none runs (history, no Relay): each is forecast leave-one-out (as_of = its first row, the run
excluded) with its project's registered tasks; the task files both agents edited are the double-edited files; a
double-edited file is covered when its task is in a bundle proposal.

Writes results/relay-record/forecast_eval.json and forecast_eval.md. Standard library only; run under
with_token.sh (SEEKER_JWT) against the round 7 Seeker; reads the lake directly for the actual outcomes.
Usage: python3 -I forecast_eval.py
"""
import collections, glob, json, os, statistics, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import relay_proof as rp  # noqa: E402  (http with SEEKER_JWT)
import lib_lake as L  # noqa: E402

DS = 'relaycoord_542c9dfe'
RELAY = '_ctrlb_relay_' + DS
OUT = os.path.join(HERE, 'results', 'relay-record')
HISTORY = ['relay-proof', 'relay-protocol', 'relay-done-stall', 'relay-benchmark']
HELD_OUT = ['relay-stall-takeover', 'relay-redirect']
THRESHOLDS = [0.25, 0.5, 0.75]
EDIT_TOOLS = "('Edit', 'Write', 'MultiEdit', 'NotebookEdit', 'apply_patch')"


def trials(tops):
    out = {}
    for top in tops:
        for f in glob.glob(os.path.join(HERE, 'results', top, '**', '*.json'), recursive=True):
            try:
                d = json.load(open(f))
            except (OSError, ValueError):
                continue
            if isinstance(d, dict) and isinstance(d.get('run_id'), str) and d.get('arm'):
                out[d['run_id']] = dict(round=top, arm=d['arm'], project=str(d.get('project') or '').split(':')[-1],
                                        model=d.get('model'), supp='/supp/' in f)
    return out


def q(s):
    return s.replace("'", "''")


def in_list(ids):
    return ', '.join("'" + q(i) + "'" for i in ids)


def lake_rows(ds, sql, size=200000):
    return L.search(ds, sql, size=size, timeout=300)


def rel(p, cwds=()):
    p = p[5:] if p.startswith('path:') else p
    if not p.startswith('/'):
        return p[2:] if p.startswith('./') else p
    for c in cwds:
        c = c.rstrip('/')
        if c and p.startswith(c + '/'):
            return p[len(c) + 1:]
    # a worktree under /tmp/relay-proof-*/ (the harness's trial dirs)
    parts = p.split('/')
    if len(parts) > 3 and parts[1] == 'tmp':
        return '/'.join(parts[3:])
    return p


def run_facts(run_ids):
    """Per run: registered tasks {key: [rel paths]}, refusals by key, agents with a done, edits by rel path -> agents,
    first and last row."""
    facts = {r: dict(tasks={}, refused=collections.Counter(), done_agents=set(), edited=collections.defaultdict(set),
                     first=None, last=None) for r in run_ids}
    ids = list(run_ids)
    for i in range(0, len(ids), 150):
        chunk = ids[i:i + 150]
        for row in lake_rows(RELAY, f"SELECT _timestamp, run_id, agent, kind, key, paths FROM \"{RELAY}\" WHERE run_id IN ({in_list(chunk)})"):
            f = facts[row['run_id']]
            ts = int(row['_timestamp'])
            f['first'] = ts if f['first'] is None else min(f['first'], ts)
            f['last'] = ts if f['last'] is None else max(f['last'], ts)
            k, kind = row.get('key') or '', row.get('kind')
            if kind == 'BRIEF' and k.startswith('name:') and row.get('paths'):
                try:
                    f['tasks'][k] = sorted({rel(x) for x in json.loads(row['paths'])})
                except ValueError:
                    pass
            elif kind == 'REFUSED':
                f['refused'][k] += 1
            elif kind == 'DONE' and k.startswith('name:'):
                f['done_agents'].add(row['agent'])
        for row in lake_rows(DS, f"SELECT _timestamp, run_id, agent, session_id, paths, cwd FROM \"{DS}\" WHERE run_id IN ({in_list(chunk)}) "
                                 f"AND block_type = 'tool_use' AND tool_name IN {EDIT_TOOLS}"):
            f = facts[row['run_id']]
            ts = int(row['_timestamp'])
            f['first'] = ts if f['first'] is None else min(f['first'], ts)
            f['last'] = ts if f['last'] is None else max(f['last'], ts)
            agent = row.get('agent') or row.get('session_id')
            try:
                ps = json.loads(row.get('paths') or '[]')
            except ValueError:
                ps = []
            for p in ps:
                f['edited'][rel(p, [row.get('cwd') or ''])].add(agent)
    return facts


def actual_hot(f, p):
    keys = [k for k, ps in f['tasks'].items() if p in ps]
    refused = any(f['refused'][k] for k in keys) or any(n for k, n in f['refused'].items() if k.startswith('path:') and rel(k) == p)
    dup = len(f['edited'].get(p, ())) > 1
    return refused, dup


_cache = {}


def forecast(tasks, as_of, mode, exclude=()):
    body = dict(tasks=[dict(name=k[5:], paths=ps) for k, ps in sorted(tasks.items())], as_of=as_of, since='3650d',
                co_change=mode, exclude_runs=list(exclude))
    key = json.dumps(body, sort_keys=True)
    if key not in _cache:
        st, d = rp.http('POST', f'/ai/relay/{DS}/forecast', body, cred='SEEKER_JWT', timeout=300)
        if st != 200:
            raise SystemExit(f'forecast {st}: {d}')
        _cache[key] = d
    return _cache[key]


def prf(tp, fp, fn):
    p = tp / (tp + fp) if tp + fp else None
    r = tp / (tp + fn) if tp + fn else None
    return dict(tp=tp, fp=fp, fn=fn, precision=None if p is None else round(p, 3), recall=None if r is None else round(r, 3))


def main():
    hist, held = trials(HISTORY), trials(HELD_OUT)
    t0 = time.time()
    hf = run_facts(hist)
    cutoff = max(f['last'] for r, f in hf.items() if f['last'] and hist[r]['round'] == 'relay-benchmark') + 1
    held_f = run_facts(held)
    print(f'history {len(hist)} runs, held out {len(held)} runs, cutoff {cutoff} ({time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime(cutoff / 1e6))})')
    rows, per_cell = [], collections.defaultdict(list)
    for r, meta in sorted(held.items()):
        f = held_f[r]
        if not f['tasks']:
            continue
        for mode in ('run', 'hold'):
            fc = forecast(f['tasks'], cutoff, mode)
            score = {h['key'][5:]: h['score'] for h in fc['hot_keys']}
            paths = sorted({p for ps in f['tasks'].values() for p in ps})
            per_path = []
            for p in paths:
                refused, dup = actual_hot(f, p)
                per_path.append(dict(path=p, score=score.get(p, 0.0), refused=refused, dup=dup))
            useful = len(f['done_agents'])
            bundled = sorted({t for b in fc['bundle_proposals'] for t in b['tasks']})
            row = dict(run=r, round=meta['round'], arm=meta['arm'], project=meta['project'], supp=meta['supp'], mode=mode,
                       tasks=len(f['tasks']), history_runs=fc['history_runs'], recommended_agents=fc['recommended_agents'],
                       useful_agents=useful, bundles=[b['tasks'] for b in fc['bundle_proposals']], bundled_tasks=len(bundled),
                       paths=per_path)
            rows.append(row)
            per_cell[(meta['project'], meta['arm'], mode)].append(row)
    # hot keys: precision/recall per mode, threshold, target
    hot = {}
    for mode in ('run', 'hold'):
        for th in THRESHOLDS:
            for target in ('refused', 'dup', 'either'):
                for proj in ('ALL', 'hone', 'readtime', 'toy'):
                    tp = fp = fn = tn = 0
                    for row in rows:
                        if row['mode'] != mode or (proj != 'ALL' and row['project'] != proj):
                            continue
                        for x in row['paths']:
                            pred = x['score'] >= th
                            act = x['refused'] if target == 'refused' else x['dup'] if target == 'dup' else (x['refused'] or x['dup'])
                            tp += pred and act
                            fp += pred and not act
                            fn += (not pred) and act
                            tn += (not pred) and not act
                    d = prf(tp, fp, fn)
                    d['tn'] = tn
                    d['base_rate'] = round((tp + fn) / (tp + fp + fn + tn), 3) if tp + fp + fn + tn else None
                    hot[f'{mode}|{th}|{target}|{proj}'] = d
    # sizing
    sizing = {}
    for (proj, arm, mode), rs in sorted(per_cell.items()):
        sizing[f'{proj}|{arm}|{mode}'] = dict(n=len(rs), recommended=sorted(collections.Counter(x['recommended_agents'] for x in rs).items()),
                                             useful=sorted(collections.Counter(x['useful_agents'] for x in rs).items()),
                                             exact=sum(x['recommended_agents'] == x['useful_agents'] for x in rs),
                                             mean_abs_err=round(statistics.mean(abs(x['recommended_agents'] - x['useful_agents']) for x in rs), 2),
                                             tasks=rs[0]['tasks'], bundled_tasks=sorted({x['bundled_tasks'] for x in rs}))
    # bundles against pair-none double edits (leave one out)
    proj_tasks = {}
    for r, meta in hist.items():
        if meta['arm'] == 'pair-relay' and hf[r]['tasks'] and meta['project'] not in proj_tasks:
            proj_tasks[meta['project']] = hf[r]['tasks']
    pn = {}
    for r, meta in sorted(hist.items()):
        if meta['arm'] != 'pair-none' or meta['project'] not in proj_tasks or not hf[r]['first']:
            continue
        tasks = proj_tasks[meta['project']]
        f = hf[r]
        dbl = sorted(p for ps in tasks.values() for p in ps if len(f['edited'].get(p, ())) > 1)
        for mode in ('run', 'hold'):
            fc = forecast(tasks, f['first'], mode, exclude=[r])
            in_bundle = {p for b in fc['bundle_proposals'] for p in (x[5:] for x in b['paths'])}
            k = (meta['project'], mode)
            e = pn.setdefault(k, dict(runs=0, double_edited=0, covered=0, bundled_tasks=collections.Counter(), tasks=len(tasks)))
            e['runs'] += 1
            e['double_edited'] += len(dbl)
            e['covered'] += sum(p in in_bundle for p in dbl)
            e['bundled_tasks'][sum(1 for b in fc['bundle_proposals'] for _ in b['tasks'])] += 1
    pn_out = {f'{p}|{m}': dict(v, bundled_tasks=sorted(v['bundled_tasks'].items())) for (p, m), v in pn.items()}
    res = dict(run='relay-record-2026-11-10', cutoff_us=cutoff, history_runs=len(hist), held_out_runs=len(held),
               held_out_with_tasks=len({x['run'] for x in rows}), thresholds=THRESHOLDS, hot=hot, sizing=sizing,
               pair_none_bundles=pn_out, rows=rows, elapsed_s=round(time.time() - t0, 1))
    os.makedirs(OUT, exist_ok=True)
    json.dump(res, open(os.path.join(OUT, 'forecast_eval.json'), 'w'), indent=1)
    md = [f'# Forecast evaluation (decision 41), relay-record-2026-11-10', '',
          f'History: {len(hist)} runs up to the end of the full benchmark (cutoff {cutoff} µs, '
          f'{time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime(cutoff / 1e6))}). Held out: {len(held)} runs of rounds 5 and 6, '
          f'{res["held_out_with_tasks"]} with registered tasks. Every forecast is from history alone.', '',
          '## Hot keys (task paths), precision / recall', '',
          'Forecast hot: hot_keys score >= threshold. Actually hot: a refused claim on it (its path, or a task naming it) / two agents edited it / either. Base rate: share of (run, path) pairs actually hot.', '',
          '| mode | threshold | target | project | precision | recall | tp | fp | fn | tn | base rate |', '|---|---|---|---|---|---|---|---|---|---|---|']
    for k, d in hot.items():
        mode, th, target, proj = k.split('|')
        md.append(f"| {mode} | {th} | {target} | {proj} | {d['precision']} | {d['recall']} | {d['tp']} | {d['fp']} | {d['fn']} | {d['tn']} | {d['base_rate']} |")
    md += ['', '## Sizing: recommended_agents against agents with at least one done', '',
           '| project | arm | mode | runs | tasks | recommended (value: runs) | useful agents (value: runs) | exact | mean abs error | tasks bundled |',
           '|---|---|---|---|---|---|---|---|---|---|']
    for k, d in sizing.items():
        proj, arm, mode = k.split('|')
        md.append(f"| {proj} | {arm} | {mode} | {d['n']} | {d['tasks']} | {d['recommended']} | {d['useful']} | {d['exact']}/{d['n']} | {d['mean_abs_err']} | {d['bundled_tasks']} |")
    md += ['', '## Bundles against double-edited files in pair-none (leave one out)', '',
           '| project | mode | pair-none runs | tasks | double-edited task files | in a bundle proposal | tasks bundled (value: runs) |', '|---|---|---|---|---|---|---|']
    for k, d in pn_out.items():
        proj, mode = k.split('|')
        md.append(f"| {proj} | {mode} | {d['runs']} | {d['tasks']} | {d['double_edited']} | {d['covered']} | {d['bundled_tasks']} |")
    open(os.path.join(OUT, 'forecast_eval.md'), 'w').write('\n'.join(md) + '\n')
    print('\n'.join(md))


if __name__ == '__main__':
    main()
