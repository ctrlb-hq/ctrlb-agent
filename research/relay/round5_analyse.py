"""relay-stall-takeover-2026-11-09 (round 5): tables for the report from the trial JSONs under
results/relay-stall-takeover/ (run_round5.py), beside the benchmark's (results/relay-benchmark). Writes
results/relay-stall-takeover/tables.md and analysis.json. Read only on the trial files.

  python3 -I round5_analyse.py
"""
import glob, json, os, statistics

HERE = os.path.dirname(os.path.abspath(__file__))
R = os.path.join(HERE, 'results')
OUT = os.path.join(R, 'relay-stall-takeover')
BENCH = os.path.join(R, 'relay-benchmark')


def trials(root, sub, arm, proj, model='haiku'):
    return [json.load(open(f)) for f in sorted(glob.glob(os.path.join(root, sub, arm, proj, f'{model}-[0-9]*.json')))]


def mean(xs, nd=3):
    xs = [x for x in xs if isinstance(x, (int, float)) and not isinstance(x, bool)]
    return round(statistics.mean(xs), nd) if xs else None


def rng(xs, nd=1):
    xs = [x for x in xs if isinstance(x, (int, float)) and not isinstance(x, bool)]
    if not xs:
        return 'n/a'
    return f'{round(min(xs), nd)}-{round(max(xs), nd)}' if len(xs) > 1 else f'{round(xs[0], nd)}'


def table(head, rows):
    out = ['| ' + ' | '.join(head) + ' |', '|' + '---|' * len(head)]
    out += ['| ' + ' | '.join('' if c is None else str(c) for c in r) + ' |' for r in rows]
    return '\n'.join(out)


def fmt(x):
    return 'n/a' if x is None else x


# ---------------------------------------------------------------- takeover proof (handoff1-control, toy)

def waits_after_stall(t, key, first_seen_s):
    """Claims of key that waited in line and timed out (REFUSED 'after waiting') at or after the key's first stalled
    reading: what the takeover is meant to replace."""
    rows = sorted(t.get('relay_rows_compact') or [], key=lambda r: r['_timestamp'])
    if not rows or first_seen_s is None:
        return 0
    t0 = rows[0]['_timestamp']
    return sum(1 for r in rows if r['kind'] == 'REFUSED' and r['key'] == key and 'after waiting' in (r.get('text') or '')
               and (r['_timestamp'] - t0) / 1e6 >= first_seen_s - 1)


TAKEOVER_TITLE = ('## Takeover proof: handoff1-control (toy, 2 agents, leaseSeconds 600, stallAfterSeconds 60, agent-1 '
                  'stopped at 60 s, rule text v8)\n')


def takeover_section(md, an, sub='takeover', arm='handoff1-control', title=TAKEOVER_TITLE, key='takeover',
                     baseline=True):
    ts = trials(OUT, sub, arm, 'toy')
    base = trials(BENCH, 'arm3-short', 'handoff1-control', 'toy') if baseline else []
    rows, per = [], []
    for t in ts:
        tk = t.get('takeover') or {}
        seen = (t.get('stall') or {}).get('seen') or []
        tos = tk.get('takeovers') or []
        r = dict(trial=t['trial'], stopped=(t['per_agent'].get('agent-1') or {}).get('stopped_by_harness'),
                 stalled_keys=len(seen), takeovers=len(tos),
                 taken=', '.join(f"{x['key'].split(':', 1)[1]} ({x['taker']})" for x in tos) or '-',
                 after_reading=[x['after_stalled_reading_s'] for x in tos],
                 after_activity=[x['after_holder_last_activity_s'] for x in tos],
                 tasks=f"{tk.get('tasks_completed')}/{tk.get('tasks')}", gate=bool(tk.get('tasks')) and tk.get('tasks_completed') == tk.get('tasks'),
                 dup_other=len(tk.get('duplicated_other') or []), dup_takeover=len(tk.get('duplicated_by_takeover') or []),
                 giver_after=tk.get('giver_edits_after_takeover'), refused=len(tk.get('refused') or []),
                 wall=t['wall_s'], cost=t['cost_usd'])
        per.append(r)
        rows.append([r['trial'], r['stalled_keys'], r['takeovers'], r['taken'],
                     ', '.join(str(x) for x in r['after_reading']) or '-',
                     ', '.join(str(x) for x in r['after_activity']) or '-', r['tasks'], r['dup_other'],
                     r['dup_takeover'], r['giver_after'], r['refused'], r['wall'], f"${r['cost']}"])
    md.append(title)
    md.append('Stalled keys: keys the 10 s poll of the run detail saw stalled. After reading / after activity: seconds '
              'from the first stalled reading of the key (poll) and from the holder\'s last activity (the takeover '
              'answer\'s stall reading) to the takeover row. Duplicated other: files edited by both agents that were '
              'not part of a taken-over task (gate 0); by takeover: the stopped agent\'s file continued by the taker. '
              'Giver after: the stopped agent\'s applied edits after the takeover (a fencing failure; must be 0).\n')
    md.append(table(['trial', 'stalled keys', 'takeovers', 'taken (by)', 'after reading s', 'after activity s',
                     'tasks', 'dup other', 'dup by takeover', 'giver edits after', 'not_stalled refusals', 'wall s',
                     'cost'], rows))
    if ts:
        gate = sum(r['gate'] for r in per)
        md.append(f"\nGate 12 of 12 in every trial: {gate}/{len(per)}. Takeovers {sum(r['takeovers'] for r in per)} "
                  f"in {sum(1 for r in per if r['takeovers'])}/{len(per)} trials; duplicated other "
                  f"{sum(r['dup_other'] for r in per)}; giver edits after a takeover {sum(r['giver_after'] or 0 for r in per)}.")
    if base:
        b = [dict(trial=t['trial'], stalls=len((t.get('stall') or {}).get('seen') or []),
                  completed=sum(1 for v in (t['score'].get('per_task') or {}).values() if v), wall=t['wall_s'],
                  handoffs=(t.get('handoffs') or {}).get('made')) for t in base]
        md.append('\nBenchmark (v7, no takeover), same shape: ' + '; '.join(
            f"#{x['trial']} stalled {x['stalls']}, {x['completed']}/12, {x['wall']} s" for x in b) + '.')
    an[key] = dict(trials=per, benchmark=[dict(trial=t['trial'], wall_s=t['wall_s']) for t in base])


# ---------------------------------------------------------------- path-hold proof (handoff1-registry)

PATH_TITLE = '\n## Path-hold proof: handoff1-registry (agent-1 stopped while it holds path:REGISTRY.md)\n'


def path_section(md, an, sub='path-hold', title=PATH_TITLE, key='path_hold', baseline=True):
    ts = trials(OUT, sub, 'handoff1-registry', 'toy')
    rows, per = [], []
    for t in ts:
        w = t.get('hold_watch') or {}
        seen = {x['key']: x for x in (t.get('stall') or {}).get('seen') or []}
        reg = seen.get('path:REGISTRY.md') or {}
        tos = [x for x in (t.get('takeover') or {}).get('takeovers') or [] if x['key'] == 'path:REGISTRY.md']
        tk = t.get('takeover') or {}
        r = dict(trial=t['trial'], held_at_stop=w.get('held_at_stop'), stop_s=w.get('stop_s'),
                 stalled_kind=reg.get('kind'), stalled_seen_s=reg.get('first_seen_s'),
                 taken_by=tos[0]['taker'] if tos else None, taken_s=tos[0]['at_s'] if tos else None,
                 after_reading=tos[0]['after_stalled_reading_s'] if tos else None,
                 tasks=f"{tk.get('tasks_completed')}/{tk.get('tasks')}", wall=t['wall_s'], cost=t['cost_usd'],
                 waits_after_stall=waits_after_stall(t, 'path:REGISTRY.md', reg.get('first_seen_s')))
        per.append(r)
        rows.append([r['trial'], fmt(r['held_at_stop']), fmt(r['stop_s']), fmt(r['stalled_kind']),
                     fmt(r['stalled_seen_s']), r['waits_after_stall'], fmt(r['taken_by']), fmt(r['taken_s']),
                     fmt(r['after_reading']), r['tasks'], r['wall'], f"${r['cost']}"])
    md.append(title)
    md.append(table(['trial', 'held at stop', 'stop s', 'stalled as', 'stalled seen s', 'timed-out waits after',
                     'taken over by', 'taken s', 'after reading s', 'tasks', 'wall s', 'cost'], rows))
    if baseline:
        md.append('\nBenchmark G9 control trials 4 and 5 (v7, no takeover): agent-1 stopped holding REGISTRY.md, '
                  'agent-2 waited in line 10 times for about 9.5 minutes; walls 572 and 666 s; the detector reported '
                  '0 stalls.')
    an[key] = per


# ---------------------------------------------------------------- contracts (section C)

CELLS = ('contracts-reader', 'contracts-reader-control', 'contracts-provider', 'contracts-provider-control')


def contracts_section(md, an):
    rows, out = [], {}
    for cell in CELLS:
        ts = trials(OUT, 'contracts', cell, 'readtime')
        if not ts:
            rows.append([cell, 0] + [''] * 12)
            continue
        cs = [t['contracts'] for t in ts]
        trig = [c for c in cs if c.get('triggered')]
        prov = cell.startswith('contracts-provider')
        ctl = cell.endswith('control')
        o = dict(n=len(ts), triggered=len(trig), trigger_s=[c.get('change_at_s') for c in trig],
                 republished=sum(1 for c in trig if c.get('provider_republished')) if prov else None,
                 read_after=sum(1 for c in trig if c.get('dependent_reads_after')),
                 changed_since=sum(1 for c in trig if c.get('changed_since_nonempty')),
                 changed_since_read=sum(1 for c in trig if c.get('changed_since_read') is True),
                 new_sig=sum(1 for c in trig if c.get('next_read_has_new_sig')),
                 dep_edited=sum(1 for c in trig if c.get('dependent_edited_after_ref')),
                 states=[t.get('dependent_state') for t in ts],
                 dep_new=mean([t.get('dependent_new_pass') for t in ts]), prov_new=mean([t.get('provider_new_pass') for t in ts]),
                 unit=mean([t['unit_pass'] for t in ts]), accept=mean([t['accept_pass'] for t in ts]),
                 written=[c.get('written') for c in cs], calls=[c.get('calls') for c in cs],
                 cost=mean([t['cost_usd'] for t in ts]), wall=mean([t['wall_s'] for t in ts], 0))
        out[cell] = o
        st = {}
        for s in o['states']:
            st[s] = st.get(s, 0) + 1
        rows.append([cell, o['n'], o['triggered'], rng(o['trigger_s']),
                     'n/a' if not prov else f"{o['republished']}/{o['triggered']}",
                     'n/a' if ctl else f"{o['read_after']}/{o['triggered']}",
                     'n/a' if ctl else f"{o['changed_since']} / {o['changed_since_read']} / {o['new_sig']}",
                     f"{o['dep_edited']}/{o['triggered']}", ', '.join(f'{k} {v}' for k, v in st.items()),
                     f"{o['dep_new']} / {o['prov_new']}", f"{o['unit']} / {o['accept']}",
                     '/'.join(str(x) for x in o['written']) + (' (calls ' + '/'.join(str(x) for x in o['calls']) + ')'),
                     f"${o['cost']}", o['wall']])
    md.append('\n## Contracts, corrected (readtime, 2 agents, Haiku)\n')
    md.append('Trigger: the provider\'s first contract on readtime/result.py (arms) or the provider\'s task finished '
              '(controls). Reader: the harness revises that contract and NOTEs the dependent agent. Provider: the docs '
              'change and the NOTE goes to the provider. Next read: the dependent agent\'s first contracts call after '
              'the revision (reader) or after the provider\'s republish (provider): changed_since non-empty / '
              'changed_since_read true / the text has the new constructor. Written: contract rows in the relay record '
              'by the agents per trial (calls: the tool calls, refused ones included).\n')
    md.append(table(['cell', 'n', 'triggered', 'trigger s', 'provider republished', 'dependent read after',
                     'next read: changed_since / _read / new line', 'dependent edited after', 'dependent state',
                     'dependent_new / provider_new', 'unit / acceptance', 'written', 'cost', 'wall s'], rows))
    b = []
    for cell in ('contracts', 'contracts-control'):
        ts = trials(BENCH, 'arm6', cell, 'readtime')
        if ts:
            st = {}
            for t in ts:
                st[t.get('dependent_state')] = st.get(t.get('dependent_state'), 0) + 1
            b.append([cell + ' (benchmark)', len(ts), sum(1 for t in ts if (t['contracts'].get('prd_change') or {}).get('at')),
                      sum(t['contracts'].get('changed_since_nonempty') or 0 for t in ts),
                      ', '.join(f'{k} {v}' for k, v in st.items()),
                      f"{mean([t.get('dependent_new_pass') for t in ts])} / {mean([t.get('provider_new_pass') for t in ts])}",
                      f"{mean([t['unit_pass'] for t in ts])} / {mean([t['accept_pass'] for t in ts])}",
                      '/'.join(str(t['contracts'].get('written')) for t in ts), f"${mean([t['cost_usd'] for t in ts])}"])
    if b:
        md.append('\nThe benchmark\'s arm 6 (the PRD changed, no contract revised; written counted calls):\n')
        md.append(table(['cell', 'n', 'changed', 'changed_since set', 'dependent state', 'dependent_new / provider_new',
                         'unit / acceptance', 'written (calls)', 'cost'], b))
    an['contracts'] = out


# ---------------------------------------------------------------- Sonnet pair-relay, v8 alone

def sonnet_section(md, an):
    ts = trials(OUT, 'sonnet', 'pair-relay', 'readtime', 'sonnet')
    bs = trials(BENCH, 'arm1', 'pair-relay', 'readtime', 'sonnet')
    bn = trials(BENCH, 'arm1', 'pair-none', 'readtime', 'sonnet')
    rows = []
    for label, xs in (('v8 pair-relay (this round)', ts), ('v7 pair-relay (benchmark)', bs), ('pair-none (benchmark)', bn)):
        if xs:
            rows.append([label, len(xs), mean([t['turns'] for t in xs], 1), f"${mean([t['cost_usd'] for t in xs])}",
                         rng([t['cost_usd'] for t in xs], 3), mean([t['wall_s'] for t in xs], 0),
                         sum(t.get('claims_with_wait') or 0 for t in xs), mean([t['accept_pass'] for t in xs]),
                         mean([t['duplicated_n'] for t in xs], 2)])
    md.append('\n## Sonnet pair-relay readtime: the wait_s sentence removed (v8)\n')
    md.append(table(['cell', 'n', 'turns', 'cost', 'cost range', 'wall s', 'claims with wait_s', 'acceptance',
                     'duplicated'], rows))
    an['sonnet'] = dict(v8=[dict(trial=t['trial'], turns=t['turns'], cost=t['cost_usd'], wall=t['wall_s'],
                                 claims_with_wait=t.get('claims_with_wait')) for t in ts])


# ---------------------------------------------------------------- cost and wall

def cost_section(md, an):
    rows, total, n = [], 0.0, 0
    for f in sorted(glob.glob(os.path.join(OUT, '*', '*', '*'))):
        if not os.path.isdir(f):
            continue
        xs = [json.load(open(p)) for p in sorted(glob.glob(os.path.join(f, '*-[0-9]*.json')))]
        if not xs:
            continue
        c = sum(t['cost_usd'] or 0 for t in xs)
        total, n = total + c, n + len(xs)
        rows.append([os.path.relpath(f, OUT), len(xs), f"${mean([t['cost_usd'] for t in xs])}",
                     rng([t['cost_usd'] for t in xs], 3), mean([t['wall_s'] for t in xs], 0), f'${round(c, 2)}'])
    md.append('\n## Cost and wall per cell\n')
    md.append(table(['cell', 'n', 'cost mean', 'range', 'wall mean s', 'cost sum'], rows))
    md.append(f'\nTotal: {n} trials, ${round(total, 2)}.')
    an['cost'] = dict(trials=n, total_usd=round(total, 2))


# ---------------------------------------------------------------- supplementary cells (Seeker 70074f5)

def supplementary_section(md, an):
    if not glob.glob(os.path.join(OUT, 'supp', '*', 'toy', '*.json')):
        return
    md.append('\n## Supplementary cells (Seeker 70074f5: a refused claim on a stalled hold says so, with the '
              'takeover hint; handoff and takeover wait out recent closing rows)\n')
    md.append('handoff1-control caught agent-1 holding a task in 1 of 5 trials, so handoff1-taskhold stops agent-1 '
              'while Seeker\'s tasks shows it holding a task (polled every second from 30 s). handoff1-registry runs '
              'again on the new build. Same settings as G1 (holdRequired, evidenceRequired, leaseSeconds 600, '
              'stallAfterSeconds 60).\n')
    takeover_section(md, an, sub='supp', arm='handoff1-taskhold', key='supp_taskhold', baseline=False,
                     title='\n### Takeover: handoff1-taskhold (agent-1 stopped while it holds a task)\n')
    path_section(md, an, sub='supp', key='supp_path_hold', baseline=False,
                 title='\n### Path hold with the stalled-refusal hint: handoff1-registry\n')


def main():
    md, an = ['# Round 5 tables (relay-stall-takeover-2026-11-09)\n', 'Generated by round5_analyse.py from the '
              'trial JSONs under results/relay-stall-takeover/.\n'], {}
    takeover_section(md, an)
    path_section(md, an)
    contracts_section(md, an)
    sonnet_section(md, an)
    supplementary_section(md, an)
    cost_section(md, an)
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, 'tables.md'), 'w') as fh:
        fh.write('\n'.join(md) + '\n')
    with open(os.path.join(OUT, 'analysis.json'), 'w') as fh:
        json.dump(an, fh, indent=1)
    print(os.path.join(OUT, 'tables.md'))


if __name__ == '__main__':
    main()
