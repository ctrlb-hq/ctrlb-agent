"""Decision 42 gate: per-group turns from Seeker's patterns against the hand-made overhead accounting.

Reads the per-run answers TestPatternsAgainstLake wrote (out/<run>.json), the trial JSONs (run id per trial) and
results/relay-stall-takeover/overhead.json (overhead.py's per-trial buckets), and writes gate.md.
Usage: python3 -I gate.py
"""
import glob, json, os, sys

HERE = os.path.join(os.path.expanduser(os.environ.get('RELAY_SWARM', '~/benchmarks/swarm')), 'results', 'relay-record', 'patterns')
SWARM = os.path.expanduser(os.environ.get('RELAY_SWARM', '~/benchmarks/swarm'))  # the harness directory (results/ under it)
RES = os.path.join(SWARM, 'results')
sys.path.insert(0, SWARM)
import overhead  # noqa: E402  (GROUPS: the accounting's own group lists)

OURS = ['task protocol', 'hot key', 'file claims', 'tool loading and talk', 'checks', 'work', 'final answer']
CELLS = [('readtime-sonnet', 'readtime', 'sonnet'), ('hone-haiku', 'hone', 'haiku')]


def answer(run):
    p = os.path.join(HERE, 'out', run + '.json')
    return json.load(open(p))['patterns'] if os.path.exists(p) else None


def groups_of(ans):
    g = {k: 0.0 for k in OURS}
    for x in ans['groups']:
        g[x['group']] += x['turns']
    return g


def mean(xs):
    return sum(xs) / len(xs) if xs else 0.0


def main():
    ov = json.load(open(os.path.join(RES, 'relay-stall-takeover', 'overhead.json')))
    md = ['# Decision 42 gate: patterns against the overhead accounting', '',
          'Produced by `gate.py` from `out/<run>.json` (Seeker `relayrecord.Analyzer.Patterns` on the `:5160` corpus, '
          'decompose 0.1.0 binary) and `relay-stall-takeover/overhead.json` (overhead.py, the stream logs). '
          'Turns are means per trial over the cell\'s pair-relay runs. The hot key group is whatever key had the most '
          'lines in that run; nothing names it.', '']
    verdicts = []
    for key, project, model in CELLS:
        cell = ov[key]['pair-relay']
        trials = {}
        for t in cell['trials']:
            tj = json.load(open(os.path.join(RES, 'relay-benchmark', 'arm1', t['file'])))
            trials[tj['run_id']] = t
        ours, theirs, hot = [], [], []
        for run, t in sorted(trials.items()):
            a = answer(run)
            if a is None:
                continue
            ours.append(groups_of(a))
            th = {}
            for (name, buckets), o in zip(overhead.GROUPS, OURS):
                th[o] = sum(t['turns'][b] for b in buckets)
            theirs.append(th)
            hot.append((run, (a.get('hot_key') or {}).get('key', ''), (a.get('hot_pattern') or {})))
        md += [f'## {project}, {model}, pair-relay ({len(ours)} runs)', '',
               '| group | patterns (mean/trial) | overhead.md (mean/trial) | diff | within 10% |', '|---|---|---|---|---|']
        tot_o = tot_t = 0
        for o in OURS:
            a, b = mean([x[o] for x in ours]), mean([x[o] for x in theirs])
            tot_o += a
            tot_t += b
            ok = (abs(a - b) <= 0.1 * b) if b else (a == 0)
            verdicts.append((key, o, ok))
            md.append(f'| {o} | {a:.2f} | {b:.2f} | {a - b:+.2f} ({(100 * (a - b) / b) if b else 0:+.1f}%) | {"yes" if ok else "**no**"} |')
        md.append(f'| total | {tot_o:.2f} | {tot_t:.2f} | {tot_o - tot_t:+.2f} | |')
        md += ['', 'Per trial (patterns / overhead), and the run\'s hot key and hot pattern:', '',
               '| run | ' + ' | '.join(OURS) + ' | hot key | hot pattern |', '|---|' + '---|' * (len(OURS) + 2)]
        for (run, hk, hp), x, y in zip(hot, ours, theirs):
            cells = ' | '.join(f'{x[o]:g}/{y[o]:g}' for o in OURS)
            hpd = f'#{hp.get("id")} {hp.get("template", "").split(" ")[0]} ({hp.get("turns")})' + (f' key {hp["key"]}' if hp.get('key') else '')
            md.append(f'| {run} | {cells} | {hk} | {hpd} |')
        md.append('')
    # handoff1-registry: the loop.
    md += ['## handoff1-registry (round 5): the wait-in-line loop', '',
           '| build | run | timed-out claims (trial JSON) | decompose flags (pattern: kind) | relay retry_loop | hot key |',
           '|---|---|---|---|---|---|']
    for build in ('path-hold', 'supp'):
        for f in sorted(glob.glob(os.path.join(RES, 'relay-stall-takeover', build, 'handoff1-registry', 'toy', '*.json'))):
            tj = json.load(open(f))
            a = answer(tj['run_id'])
            if a is None:
                continue
            tmpl = {p['id']: p['template'].split(' ')[0] + ('+reason' if 'reason=' in p['template'] else '') for p in a['patterns']}
            dec = '; '.join(f'{tmpl.get(x["pattern_id"])}: {x["kind"]}' for x in a['anomalies'] if x['source'] == 'decompose') or 'none'
            loop = '; '.join(x['detail'] for x in a['anomalies'] if x['kind'] == 'retry_loop') or 'none'
            md.append(f'| {"planned" if build == "path-hold" else "70074f5"} | {tj["run_id"]} | {tj.get("timed_out_waits")} | {dec} | {loop} | {(a.get("hot_key") or {}).get("key", "")} |')
    md.append('')
    with open(os.path.join(HERE, 'gate.md'), 'w') as fh:
        fh.write('\n'.join(md) + '\n')
    for v in verdicts:
        print(*v)


if __name__ == '__main__':
    main()
