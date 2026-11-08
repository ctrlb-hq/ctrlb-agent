"""relay-benchmark-2026-11-09: tables for the report, computed from the trial JSONs under results/relay-benchmark/ and
the baselines (results/relay-proof, results/relay-protocol/rerun). Writes results/relay-benchmark/tables.md and
analysis.json. Read only on the trial files; re-reads agent transcripts to recount Bash writes (see dup_recount).

  python3 -I bench_analyse.py
"""
import glob, json, os, re, statistics, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import relay_proof as rp  # noqa: E402  (transcript_path, parse helpers; read only)
import run_swarm as rs  # noqa: E402

R = os.path.join(HERE, 'results')
B = os.path.join(R, 'relay-benchmark')


def trials(root, arm, proj, model='haiku'):
    return [json.load(open(f)) for f in sorted(glob.glob(os.path.join(root, arm, proj, f'{model}-[0-9]*.json')))]


def mean(xs, nd=3):
    xs = [x for x in xs if isinstance(x, (int, float)) and not isinstance(x, bool)]
    return round(statistics.mean(xs), nd) if xs else None


def spread(xs, nd=3):
    xs = [x for x in xs if isinstance(x, (int, float)) and not isinstance(x, bool)]
    if not xs:
        return 'n/a'
    sd = statistics.stdev(xs) if len(xs) > 1 else 0.0
    return f'{statistics.mean(xs):.{nd}f} ({min(xs):.2f}-{max(xs):.2f}, sd {sd:.3f})'


def ratio(num, den):
    return f'{num}/{den} = {num / den:.2f}' if den else f'{num}/0'


# ---------------------------------------------------------------- duplication recount

PREFIX = re.compile(r'([^\s\'"<>|;&()=]*)$')


def outside_writes(cmd, rel, cwd):
    """True when every occurrence of rel in a Bash command is under another directory (a scratch copy: $S/rel,
    /tmp/x/rel), not the trial dir (rel, ./rel, <cwd>/rel)."""
    real = os.path.realpath(cwd)
    seen = False
    for m in re.finditer(re.escape(rel), cmd):
        pre = PREFIX.search(cmd[:m.start()]).group(1)
        seen = True
        if pre in ('', './') or pre.rstrip('/') in (cwd.rstrip('/'), real):
            return False
    return seen


def dup_recount(t):
    """duplicated_n with Bash writes into a scratch copy left out (the counter matched the path suffix). Returns
    (duplicated_n, redone_share, [dropped edits])."""
    files = [p for p in t['editors']]
    applied, dropped = {}, []
    for a, s in t['per_agent'].items():
        tp = rp.transcript_path(s.get('session_id') or '') if s.get('session_id') else ''
        if not tp:
            return t['duplicated_n'], t['redone_share'], ['no transcript']
        cmds = {}
        with open(tp, errors='replace') as fh:
            for line in fh:
                if '"Bash"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                for b in ((rec.get('message') or {}).get('content') or []) if isinstance(rec.get('message'), dict) else []:
                    if isinstance(b, dict) and b.get('type') == 'tool_use' and b.get('name') == 'Bash':
                        cmds[rp.ts_of(rec.get('timestamp'))] = (b.get('input') or {}).get('command') or ''
        tl = rp.parse_transcript(tp, t['dir'], files)
        keep = []
        for e in tl['edits']:
            if not e['applied'] or e['path'] not in t['editors']:
                continue
            if e['tool'] == 'Bash' and outside_writes(cmds.get(e['ts'], ''), e['path'], t['dir']):
                dropped.append(dict(agent=a, path=e['path']))
                continue
            keep.append(e)
        applied[a] = keep
    written = {a: {e['path'] for e in es} for a, es in applied.items()}
    dup = sum(1 for f in files if sum(f in w for w in written.values()) > 1)
    comps = []
    for a, es in applied.items():
        for f in written[a]:
            ts = [e['ts'] for e in es if e['path'] == f and e['ts']]
            comps.append(dict(agent=a, file=f, first=min(ts), last=max(ts)))
    redo = [c for c in comps if any(o['file'] == c['file'] and o['agent'] != c['agent'] and o['last'] < c['first']
                                    for o in comps)]
    return dup, round(len(redo) / len(comps), 4) if comps else 0.0, dropped


# ---------------------------------------------------------------- sections

def row_main(label, ts, recount=True):
    if not ts:
        return None
    rc = [dup_recount(t) for t in ts] if recount else [(t['duplicated_n'], t['redone_share'], []) for t in ts]
    acc = [t['accept_pass'] for t in ts]
    held = sum(t.get('claims_held') or 0 for t in ts)
    ref = sum(t.get('claims_refused') or 0 for t in ts)
    return dict(
        cell=label, n=len(ts), abandoned=f"{sum(bool(t['abandoned']) for t in ts)}/{len(ts)}",
        accept=spread(acc), unit=spread([t['unit_pass'] for t in ts]),
        redone_share_raw=mean([t['redone_share'] for t in ts]), redone_share=mean([r[1] for r in rc]),
        duplicated_raw=mean([t['duplicated_n'] for t in ts]), duplicated=mean([r[0] for r in rc]),
        dup_dropped=[d for r in rc for d in r[2]],
        edits_unheld_applied=mean([t.get('edits_unheld_applied') for t in ts]),
        cost=mean([t['cost_usd'] for t in ts], 4), wall=mean([t['wall_s'] for t in ts], 1),
        turns=mean([t['turns'] for t in ts], 1),
        done_with_evidence=sum(t.get('done_with_evidence') or 0 for t in ts),
        done_without=sum(t.get('done_without') or 0 for t in ts),
        done_with_task_test=sum(t.get('done_with_task_test') or 0 for t in ts),
        task_tests_registered=sum(len(t.get('task_tests') or {}) for t in ts),
        served_waits=sum(t.get('served_waits') or 0 for t in ts),
        served_wait_s=round(sum(t.get('served_wait_s_total') or 0 for t in ts), 1),
        timed_out_waits=sum(t.get('timed_out_waits') or 0 for t in ts),
        claims_with_wait=sum(t.get('claims_with_wait') or 0 for t in ts),
        wait_s_total=round(sum(t.get('wait_s_total') or 0 for t in ts), 1),
        refused_per_hold=ratio(ref, held), evidence_refusals=sum(t.get('evidence_refusals') or 0 for t in ts),
        cost_sum=round(sum(t['cost_usd'] for t in ts), 4))


def md_table(rows, cols, head=None):
    rows = [r for r in rows if r]
    out = ['| ' + ' | '.join(head or cols) + ' |', '|' + '---|' * len(cols)]
    for r in rows:
        out.append('| ' + ' | '.join(str(r.get(c, '')) for c in cols) + ' |')
    return '\n'.join(out)


def arm1():
    rows, base = [], []
    for proj in ('hone', 'readtime'):
        for arm in ('solo', 'pair-none', 'pair-relay', 'quad-relay'):
            rows.append(row_main(f'{arm} {proj} haiku', trials(os.path.join(B, 'arm1'), arm, proj)))
            base.append(row_main(f'{arm} {proj} haiku (proof)', trials(os.path.join(R, 'relay-proof'), arm, proj), False))
    for arm in ('pair-none', 'pair-relay'):
        rows.append(row_main(f'{arm} readtime sonnet', trials(os.path.join(B, 'arm1'), arm, 'readtime', 'sonnet')))
        base.append(row_main(f'{arm} readtime sonnet (proof)', trials(os.path.join(R, 'relay-proof'), arm, 'readtime', 'sonnet'), False))
    return rows, base


def arm2():
    cells = [('quad-relay hone v7 (arm 1)', trials(os.path.join(B, 'arm1'), 'quad-relay', 'hone')),
             ('quad-relay toy v7', trials(os.path.join(B, 'arm2/v7'), 'quad-relay', 'toy')),
             ('quad-relay toy v7-nowait', trials(os.path.join(B, 'arm2/v7-nowait'), 'quad-relay', 'toy')),
             ('pair-relay hone/readtime haiku (protocol rerun, v5)',
              trials(os.path.join(R, 'relay-protocol/rerun'), 'pair-relay', 'hone')
              + trials(os.path.join(R, 'relay-protocol/rerun'), 'pair-relay', 'readtime'))]
    return [row_main(c, ts, recount=not c.startswith('pair-relay hone/readtime')) for c, ts in cells]


def task_name_key(t):
    return {f"name:{n}" for n in (t.get('task_tests') or {})}


def arm3():
    out = []
    for sub, arm in (('arm3', 'handoff3'), ('arm3', 'handoff3-control'), ('arm3-short', 'handoff1'),
                     ('arm3-short', 'handoff1-control')):
        for t in trials(os.path.join(B, sub), arm, 'toy'):
            rows = t.get('relay_rows_compact') or []
            t0 = (int(rows[0]['_timestamp']) / 1e6) if rows else 0
            stop = (t['per_agent'].get('agent-1') or {}).get('exit_s')
            # tasks agent-1 still held when it stopped: a HOLD or REOPEN of a name key with no later RELEASE, DONE or
            # HANDOFF of that key by agent-1 (a refused claim releases its own HOLD)
            finished = {x['name']: x for x in t.get('finished_tasks') or []}
            ts_ = lambda r: int(r['_timestamp']) / 1e6
            left = []
            for h in rows:
                if h['agent'] != 'agent-1' or h['kind'] not in ('HOLD', 'REOPEN') or not (h['key'] or '').startswith('name:'):
                    continue
                k = h['key']
                if any(r['agent'] == 'agent-1' and r['key'] == k and r['kind'] in ('RELEASE', 'DONE', 'HANDOFF')
                       and ts_(r) >= ts_(h) for r in rows) or k in [x['key'] for x in left]:
                    continue
                later = [r for r in rows if r['key'] == k and r['agent'] not in ('agent-1', 'orchestrator')
                         and ts_(r) > ts_(h)]
                how = ('reopen' if any(r['kind'] == 'REOPEN' for r in later)
                       else 'claim' if any(r['kind'] == 'HOLD' for r in later) else 'not picked up')
                left.append(dict(key=k, held_from_s=round(ts_(h) - t0, 1), how=how,
                                 finished_by=(finished.get(k[5:]) or {}).get('finished_by'),
                                 other_first_hold_s=round(ts_(later[0]) - t0, 1) if later else None))
            handed = [dict(key=r['key'], to=r['to'], at_s=round(ts_(r) - t0, 1)) for r in rows if r['kind'] == 'HANDOFF']
            per = t['score'].get('per_task') or {}
            out.append(dict(arm=arm, trial=t['trial'], run_id=t['run_id'], unit=t['unit_pass'],
                            tasks_passed=sum(1 for v in per.values() if v), tasks=len(per),
                            handoffs=t['handoffs']['made'], gaps=[g.get('gap_s') for g in t['handoffs']['gaps']],
                            takeover_gaps=[g.get('gap_s') for g in t['handoffs']['takeover'] if g.get('gap_s') is not None],
                            agent1_exit_s=stop, agent1_stopped=(t['per_agent'].get('agent-1') or {}).get('stopped_by_harness'),
                            agent1_last_activity_s=((t.get('stall') or {}).get('agents_last_activity_s') or {}).get('agent-1'),
                            stalled_tasks_end=t.get('stalled_tasks'), stall_seen=(t.get('stall') or {}).get('seen'),
                            left_by_agent1=left, handoff_rows=handed, wall=t['wall_s'], cost=t['cost_usd'],
                            edits_on_lapsed_lease=t.get('edits_on_lapsed_lease'),
                            duplicated=t['duplicated_n']))
    return out


def arm4():
    def per_task_split(ts):
        named, other = [], []
        for t in ts:
            per = t['score'].get('per_task') or {}
            tests = t.get('task_tests') or {}
            for f in t.get('finished_tasks') or []:
                mod = f['name'].split(' ', 2)[-1]
                ev = f.get('evidence') or {}
                cmd = ev.get('command') if isinstance(ev, dict) else None
                ok = per.get(mod)
                if ok is None:
                    continue
                if tests.get(f['name']) and cmd and tests[f['name']] in cmd:
                    named.append(ok)
                else:
                    other.append(ok)
        return f'{sum(named)}/{len(named)}', f'{sum(other)}/{len(other)}'
    out = []
    for label, ts in (('evidenceRequired true (arm 1 pair-relay hone)', trials(os.path.join(B, 'arm1'), 'pair-relay', 'hone')),
                      ('evidenceRequired false (arm 4)', trials(os.path.join(B, 'arm4'), 'pair-relay', 'hone')),
                      ('proof run v4 text, no done (pair-relay hone)', trials(os.path.join(R, 'relay-proof'), 'pair-relay', 'hone'))):
        if not ts:
            continue
        r = row_main(label, ts, recount=False)
        r['unit_tests_named'], r['unit_tests_other'] = per_task_split(ts)
        r['done_calls'] = sum((t.get('tool_calls') or {}).get('done', 0) for t in ts)
        out.append(r)
    return out


def arm5():
    out = []
    for root, tag in ((os.path.join(B, 'arm5'), ''), (os.path.join(R, 'relay-proof'), ' (proof)')):
        for arm in ('fence-off', 'fence-on'):
            for proj in ('hone', 'readtime'):
                ts = trials(root, arm, proj)
                if not ts:
                    continue
                ch, cl = {}, {}
                for t in ts:
                    for k, v in t['refusals']['checks_by_reason'].items():
                        ch[k] = ch.get(k, 0) + v
                    for k, v in t['refusals']['claims_refused'].items():
                        cl[k] = cl.get(k, 0) + v
                out.append(dict(cell=f'{arm} {proj}{tag}', n=len(ts),
                                edits_unheld='/'.join(str(t.get('edits_unheld')) for t in ts),
                                edits_unheld_applied='/'.join(str(t.get('edits_unheld_applied')) for t in ts),
                                edits_on_lapsed_lease='/'.join(str(t.get('edits_on_lapsed_lease')) for t in ts),
                                lapsed_applied='/'.join(str(t.get('edits_on_lapsed_lease_applied')) for t in ts),
                                checks=json.dumps(ch), claims_refused=json.dumps(cl),
                                accept=mean([t['accept_pass'] for t in ts]), cost=mean([t['cost_usd'] for t in ts], 4),
                                wall=mean([t['wall_s'] for t in ts], 1)))
    return out


def arm6():
    out = []
    for root, tag, proj_q, proj_c in ((os.path.join(B, 'arm6'), '', 'readtime', 'readtime'),
                                      (os.path.join(R, 'relay-proof'), ' (proof)', 'readtime', 'hone')):
        for arm in ('questions', 'questions-control'):
            ts = trials(root, arm, proj_q)
            if not ts:
                continue
            need = [v for t in ts for v in (t['questions'].get('tasks_needing_fact_passed') or {}).values()]
            tta = [x for t in ts for x in t['questions']['time_to_answer_s']]
            # the fact's own tests (judge-questions-240): unit test_custom_wpm (3) and test_transitions (1). per_task needs
            # every test file of a module to pass in full, and acceptance test_can_add fails in most readtime trials
            wpm = [4 - sum(('custom_wpm' in x or 'transitions' in x) for x in (t['score'].get('unit') or {}).get('failed') or [])
                   for t in ts]
            out.append(dict(cell=f'{arm} {proj_q}{tag}', n=len(ts), asked='/'.join(str(t['questions']['asked']) for t in ts),
                            duplicates=sum(t['questions']['duplicates'] for t in ts),
                            answered=sum(t['questions']['answered'] for t in ts),
                            time_to_answer_s=f'{mean(tta, 1)} ({min(tta)}-{max(tta)})' if tta else 'n/a',
                            needing_fact_passed=f'{sum(1 for v in need if v)}/{len(need)}',
                            wpm_tests_passed=', '.join(f'{w}/4' for w in wpm),
                            ask_about_registered=sum(len(t.get('ask_about') or {}) for t in ts),
                            unit=mean([t['unit_pass'] for t in ts]), accept=mean([t['accept_pass'] for t in ts]),
                            cost=mean([t['cost_usd'] for t in ts], 4)))
        for arm in ('contracts', 'contracts-control'):
            ts = trials(root, arm, proj_c)
            if not ts:
                continue
            states = {}
            for t in ts:
                states[t.get('dependent_state')] = states.get(t.get('dependent_state'), 0) + 1
            out.append(dict(cell=f'{arm} {proj_c}{tag}', n=len(ts),
                            written='/'.join(str(t['contracts']['written']) for t in ts),
                            listed='/'.join(str(t['contracts']['listed']) for t in ts),
                            changed_since='/'.join(str(t['contracts']['changed_since_nonempty']) for t in ts),
                            trigger=json.dumps({k: sum(1 for t in ts if ((t['contracts'].get('prd_change') or {}).get('by') or 'none').startswith(k))
                                                for k in ('first contract', 'trigger task', 'fallback', 'none')}),
                            dependent_redone='/'.join(str(t['contracts'].get('dependent_redone')) for t in ts),
                            dependent_state=json.dumps(states),
                            dep_new=mean([t.get('dependent_new_pass') for t in ts]),
                            prov_new=mean([t.get('provider_new_pass') for t in ts]),
                            unit=mean([t['unit_pass'] for t in ts]), accept=mean([t['accept_pass'] for t in ts]),
                            cost=mean([t['cost_usd'] for t in ts], 4)))
    return out


def costs():
    out, total, n = [], 0.0, 0
    for f in sorted(glob.glob(os.path.join(B, '**', '*.json'), recursive=True)):
        if f.endswith(('summary.json', 'projection.json', 'analysis.json')) or '/logs/' in f or 'stall_replay' in f:
            continue
        t = json.load(open(f))
        if 'cost_usd' not in t:
            continue
        cell = os.path.relpath(os.path.dirname(f), B) + '/' + t['model']
        out.append((cell, t['cost_usd'], t['wall_s']))
        total += t['cost_usd']
        n += 1
    cells = {}
    for c, usd, w in out:
        cells.setdefault(c, []).append((usd, w))
    rows = [dict(cell=c, n=len(v), cost_mean=round(statistics.mean(x[0] for x in v), 4),
                 cost_range=f'{min(x[0] for x in v):.4f}-{max(x[0] for x in v):.4f}',
                 wall_mean=round(statistics.mean(x[1] for x in v), 1), cost_sum=round(sum(x[0] for x in v), 4))
            for c, v in sorted(cells.items())]
    return rows, round(total, 4), n


def main():
    a1, a1b = arm1()
    res = dict(arm1=a1, arm1_proof=a1b, arm2=arm2(), arm3=arm3(), arm4=arm4(), arm5=arm5(), arm6=arm6())
    res['costs'], res['total_usd'], res['trials'] = costs()
    with open(os.path.join(B, 'analysis.json'), 'w') as fh:
        json.dump(res, fh, indent=1)
    main_cols = ['cell', 'n', 'abandoned', 'accept', 'unit', 'redone_share', 'duplicated', 'duplicated_raw', 'cost',
                 'wall', 'turns']
    md = ['## arm 1', md_table(a1, main_cols), '', 'proof', md_table(a1b, main_cols), '',
          md_table(a1, ['cell', 'done_with_evidence', 'done_without', 'done_with_task_test', 'task_tests_registered',
                        'served_waits', 'served_wait_s', 'timed_out_waits', 'claims_with_wait', 'refused_per_hold',
                        'wait_s_total', 'edits_unheld_applied', 'evidence_refusals']),
          '', 'dup dropped: ' + json.dumps({r['cell']: r['dup_dropped'] for r in a1 if r and r['dup_dropped']}),
          '## arm 2', md_table(res['arm2'], ['cell', 'n', 'served_waits', 'served_wait_s', 'timed_out_waits',
                                             'claims_with_wait', 'refused_per_hold', 'wait_s_total', 'duplicated',
                                             'duplicated_raw', 'turns', 'wall', 'cost', 'unit']),
          '## arm 3', md_table(res['arm3'], ['arm', 'trial', 'tasks_passed', 'handoffs', 'gaps', 'takeover_gaps',
                                             'agent1_exit_s', 'agent1_last_activity_s', 'stalled_tasks_end',
                                             'stall_seen', 'left_by_agent1', 'edits_on_lapsed_lease', 'wall', 'cost']),
          '## arm 4', md_table(res['arm4'], ['cell', 'n', 'done_calls', 'done_with_evidence', 'done_without',
                                             'done_with_task_test', 'evidence_refusals', 'unit_tests_named',
                                             'unit_tests_other', 'accept', 'cost']),
          '## arm 5', md_table(res['arm5'], ['cell', 'n', 'edits_unheld', 'edits_unheld_applied',
                                             'edits_on_lapsed_lease', 'lapsed_applied', 'checks', 'claims_refused',
                                             'accept', 'cost', 'wall']),
          '## arm 6', md_table([r for r in res['arm6'] if r['cell'].startswith('questions')],
                               ['cell', 'n', 'asked', 'duplicates', 'answered', 'time_to_answer_s', 'wpm_tests_passed',
                                'needing_fact_passed', 'ask_about_registered', 'unit', 'accept', 'cost']),
          md_table([r for r in res['arm6'] if r['cell'].startswith('contracts')],
                   ['cell', 'n', 'written', 'listed', 'changed_since', 'trigger', 'dependent_redone',
                    'dependent_state', 'dep_new', 'prov_new', 'unit', 'accept', 'cost']),
          '## costs', md_table(res['costs'], ['cell', 'n', 'cost_mean', 'cost_range', 'wall_mean', 'cost_sum']),
          f"total ${res['total_usd']} over {res['trials']} trials"]
    with open(os.path.join(B, 'tables.md'), 'w') as fh:
        fh.write('\n'.join(md) + '\n')
    print(os.path.join(B, 'tables.md'))


if __name__ == '__main__':
    main()
