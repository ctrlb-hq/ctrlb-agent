"""relay-redirect-2026-11-09 (round 6): tables for the report from the trial JSONs under results/relay-redirect/
(run_round6.py), beside the benchmark's (results/relay-benchmark) and round 5's (results/relay-stall-takeover).
Writes results/relay-redirect/tables.md, analysis.json and overhead.md. Read only on the trial files (and, for the
overhead accounting, the agents' stream logs left in each trial dir).

  python3 -I round6_analyse.py

Sections: A/E redirect and swarm sizing (turns, refused claims per hold, free_tasks followed, "no task" stops, idle
agents), B handoff by the hook (handoff1-stop, handoff1-kill), C stale contracts, D witnessed evidence, F REGISTRY.md
turns, the overhead accounting redone on the Sonnet cell, cost and wall.
"""
import glob, json, os, statistics, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import overhead as OH  # noqa: E402  (agent_log, trial, table, BUCKETS, GROUPS)

R = os.environ.get('ROUND6_RESULTS') or os.path.join(HERE, 'results')
OUT = os.path.join(R, 'relay-redirect')
BENCH = os.path.join(R, 'relay-benchmark')
R5 = os.path.join(R, 'relay-stall-takeover')
REG_BUCKETS = ['claim: REGISTRY.md', 'release: REGISTRY.md', 'edit refused by the hook: REGISTRY.md']


def files(root, sub, arm, proj, model='haiku'):
    return sorted(glob.glob(os.path.join(root, sub, arm, proj, f'{model}-[0-9]*.json')))


def trials(root, sub, arm, proj, model='haiku'):
    return [json.load(open(f)) for f in files(root, sub, arm, proj, model)]


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


def ratio(a, b):
    return f'{a}/{b} = {round(a / b, 2)}' if b else f'{a}/0'


# ---------------------------------------------------------------- A and E: redirect instead of wait, swarm sizing

A_CELLS = [('pair-relay hone', 'redirect', 'pair-relay', 'hone', 'haiku'),
           ('pair-relay readtime', 'redirect', 'pair-relay', 'readtime', 'haiku'),
           ('quad-relay hone', 'redirect', 'quad-relay', 'hone', 'haiku'),
           ('pair-relay readtime sonnet', 'sonnet', 'pair-relay', 'readtime', 'sonnet'),
           ('pair-relay-witness hone (G-D)', 'witness', 'pair-relay-witness', 'hone', 'haiku'),
           ('pair-relay-witness readtime (G-D)', 'witness', 'pair-relay-witness', 'readtime', 'haiku')]
A_BASE = [('pair-relay hone haiku (benchmark, v7)', BENCH, 'arm1', 'pair-relay', 'hone', 'haiku'),
          ('pair-relay readtime haiku (benchmark, v7)', BENCH, 'arm1', 'pair-relay', 'readtime', 'haiku'),
          ('quad-relay hone haiku (benchmark, v7)', BENCH, 'arm1', 'quad-relay', 'hone', 'haiku'),
          ('pair-relay readtime sonnet (benchmark, v7)', BENCH, 'arm1', 'pair-relay', 'readtime', 'sonnet'),
          ('pair-relay readtime sonnet (round 5, v8)', R5, 'sonnet', 'pair-relay', 'readtime', 'sonnet')]


def base_row(label, root, sub, arm, proj, model):
    ts = trials(root, sub, arm, proj, model)
    if not ts:
        return None, None
    held = sum(t.get('claims_held') or 0 for t in ts)
    ref = sum(t.get('claims_refused') or 0 for t in ts)
    o = dict(n=len(ts), turns=mean([t['turns'] for t in ts], 1), refused=ref, held=held,
             claims_with_wait=sum(t.get('claims_with_wait') or 0 for t in ts),
             accept=mean([t['accept_pass'] for t in ts]), dup=mean([t['duplicated_n'] for t in ts], 2),
             cost=mean([t['cost_usd'] for t in ts], 4), wall=mean([t['wall_s'] for t in ts], 0),
             source=os.path.relpath(os.path.join(root, sub, arm, proj), R) + f'/{model}-NN.json')
    return [label, o['n'], o['turns'], ratio(ref, held), 'n/a', 'n/a', 'n/a', 'n/a', o['claims_with_wait'], 'n/a',
            o['accept'], o['dup'], f"${o['cost']}", o['wall']], o


def redirect_section(md, an):
    rows, out = [], {}
    for label, sub, arm, proj, model in A_CELLS:
        ts = trials(OUT, sub, arm, proj, model)
        if not ts:
            continue
        rd = [t.get('redirect') or {} for t in ts]
        held = sum(t.get('claims_held') or 0 for t in ts)
        ref = sum(t.get('claims_refused') or 0 for t in ts)
        notes = [n for r in rd for n in r.get('no_task_notes') or []]
        ends = [t.get('tasks_end') or {} for t in ts]
        runs = [t.get('run_detail_summary') or {} for t in ts]
        o = dict(n=len(ts), turns=mean([t['turns'] for t in ts], 1), turns_range=rng([t['turns'] for t in ts], 0),
                 refused=ref, held=held, refused_rows=sum(r.get('refused') or 0 for r in rd),
                 hold_rows=sum(r.get('holds') or 0 for r in rd),
                 with_free=sum(r.get('refusals_with_free') or 0 for r in rd),
                 followed=sum(r.get('next_claim_free') or 0 for r in rd),
                 other=sum(r.get('next_claim_other') or 0 for r in rd),
                 empty=sum(r.get('refusals_empty') or 0 for r in rd),
                 no_task_notes=notes, wait_s_claims=sum(r.get('claims_wait_s') or 0 for r in rd),
                 idle_agents_end=[e.get('idle_agents') for e in ends], free_tasks_end=[e.get('free_tasks') for e in ends],
                 idle_agents_run=[x.get('idle_agents') for x in runs], free_tasks_run=[x.get('free_tasks') for x in runs],
                 accept=mean([t['accept_pass'] for t in ts]), unit=mean([t['unit_pass'] for t in ts]),
                 dup=mean([t['duplicated_n'] for t in ts], 2), cost=mean([t['cost_usd'] for t in ts], 4),
                 wall=mean([t['wall_s'] for t in ts], 0))
        out[label] = o
        stops = ', '.join(f"{n['agent']} {n['ts_s']} s (exit {n['exit_s']} s)" for n in notes) or '-'
        idle = sum(len(x) if isinstance(x, list) else (x or 0) for x in o['idle_agents_end'])
        rows.append([label + ' (round 6, v9)', o['n'], f"{o['turns']} ({o['turns_range']})", ratio(ref, held),
                     f"{o['with_free']}", f"{o['followed']}/{o['with_free']}", o['empty'], f"{len(notes)}: {stops}",
                     o['wait_s_claims'], idle, o['accept'], o['dup'], f"${o['cost']}", o['wall']])
    base = {}
    for label, root, sub, arm, proj, model in A_BASE:
        row, o = base_row(label, root, sub, arm, proj, model)
        if row:
            rows.append(row)
            base[label] = o
    md.append('\n## A and E: redirect instead of wait, swarm sizing\n')
    md.append('Turns: per trial, all agents (the result records\' num_turns). Refused per hold: the run view\'s '
              'claims_refused / claims_held summed over the cell. Free-list refusals: refused task claims whose answer '
              'listed free_tasks; followed: the agent\'s next claim named one of them. Empty: refusals with '
              'free_tasks []. "No task" stops: posts saying the agent has no task, with when and when the agent '
              'exited. wait_s claims: claims sent with wait_s (refused by Seeker in round 6; in v7 a wait). Idle '
              'agents: Seeker\'s tasks idle_agents at the end of each trial, summed.\n')
    md.append(table(['cell', 'n', 'turns', 'refused per hold', 'free-list refusals', 'followed', 'empty list',
                     '"no task" stops', 'wait_s claims', 'idle agents at end', 'acceptance', 'duplicated', 'cost',
                     'wall s'], rows))
    an['redirect'] = dict(round6=out, baselines=base)


# ---------------------------------------------------------------- B: handoff by the hook

def handoff_section(md, an):
    rows, per = [], {}
    for arm in ('handoff1-stop', 'handoff1-kill'):
        for t in trials(OUT, 'handoff', arm, 'toy'):
            oh, tk, w = t.get('open_handoff') or {}, t.get('takeover') or {}, t.get('hold_watch') or {}
            ws = oh.get('writes') or []
            a1 = (t.get('per_agent') or {}).get('agent-1') or {}
            first_edit = [x['first_edit_after_stop_s'] for x in ws if x.get('first_edit_after_stop_s') is not None]
            tos = tk.get('takeovers') or []
            stop = oh.get('stop_at_s') if oh.get('stop_at_s') is not None else w.get('stop_s')
            r = dict(arm=arm, trial=t['trial'], held_at_stop=w.get('held_at_stop'), task=w.get('task'), stop_s=stop,
                     signal=oh.get('stop_signal'), exit_after_stop_s=round(a1['exit_s'] - stop, 1)
                     if a1.get('exit_s') is not None and stop is not None else None,
                     hook_writes=len(ws), after_stop_s=[x['after_stop_s'] for x in ws],
                     handed=sorted({k for x in ws for k in x['keys']}),
                     text_present=all(x.get('text_present') for x in ws) if ws else None,
                     text=[x['text'] for x in ws], notes=len(oh.get('notes') or []),
                     next_holder=[x.get('next_holder') for x in ws],
                     next_hold_after_stop_s=[x.get('next_hold_after_stop_s') for x in ws],
                     first_edit_after_stop_s=min(first_edit) if first_edit else None,
                     takeovers=len(tos), takeover_after_stop_s=[round(x['at_s'] - stop, 1) for x in tos]
                     if stop is not None else [],
                     tasks=f"{tk.get('tasks_completed', oh.get('tasks_completed'))}/{tk.get('tasks', oh.get('tasks'))}",
                     dup_other=len(tk.get('duplicated_other') or []), giver_after=tk.get('giver_edits_after_takeover'),
                     wall=t['wall_s'], cost=t['cost_usd'])
            per.setdefault(arm, []).append(r)
            rows.append([arm, r['trial'], fmt(r['task']), fmt(r['stop_s']), fmt(r['signal']),
                         fmt(r['exit_after_stop_s']), r['hook_writes'],
                         ', '.join(str(x) for x in r['after_stop_s']) or '-',
                         ', '.join(k.split(':', 1)[1] for k in r['handed']) or '-', fmt(r['text_present']), r['notes'],
                         ', '.join(f"{a} {b}" for a, b in zip(r['next_holder'], r['next_hold_after_stop_s'])) or '-',
                         fmt(r['first_edit_after_stop_s']), r['takeovers'],
                         ', '.join(str(x) for x in r['takeover_after_stop_s']) or '-', r['tasks'], r['dup_other'],
                         fmt(r['giver_after']), r['wall'], f"${r['cost']}"])
    md.append('\n## B: handoff by the hook (toy, 2 agents, leaseSeconds 600, stallAfterSeconds 60)\n')
    md.append('agent-1 is told nothing about a shift and is stopped while Seeker\'s tasks shows it holding a task '
              '(polled every second from 30 s). handoff1-stop: SIGTERM to its process group, so Claude Code runs the '
              'SessionEnd hook (1.5 s budget) and the plugin hands its holds off openly. handoff1-kill: SIGKILL, so no '
              'hook runs and takeover must cover. Hook writes: open-handoff writes (RELEASE rows whose text starts '
              '"handed off") by agent-1; after stop: seconds from the signal to the write. Next hold: who claimed the '
              'handed task next, seconds after the stop. First edit: agent-2\'s first applied edit of a handed file, '
              'seconds after the stop. Takeovers: HANDOFF rows with the takeover text, seconds after the stop.\n')
    md.append(table(['arm', 'trial', 'task held at stop', 'stop s', 'signal', 'agent-1 exit after stop s', 'hook writes',
                     'write after stop s', 'handed keys', 'text present', 'NOTE rows', 'next hold (s after stop)',
                     'first edit of a handed file, s after stop', 'takeovers', 'takeover s after stop', 'tasks',
                     'dup other', 'giver edits after', 'wall s', 'cost'], rows))
    texts = [(r['arm'], r['trial'], x) for rs in per.values() for r in rs for x in r['text']]
    if texts:
        md.append('\nHandoff texts the hook wrote:\n')
        md += [f'- {a} #{k}: {x[:400]}' for a, k, x in texts]
    # round 5's stop-while-holding cell (handoff1-taskhold, v8, no hook handoff): from the stop to the takeover
    r5 = []
    for t in trials(R5, 'supp', 'handoff1-taskhold', 'toy'):
        w = t.get('hold_watch') or {}
        for x in (t.get('takeover') or {}).get('takeovers') or []:
            if w.get('stop_s') is not None and x.get('kind') != 'path':
                r5.append(round(x['at_s'] - w['stop_s'], 1))
    if r5:
        md.append(f'\nRound 5 handoff1-taskhold (v8, the hook wrote nothing; takeover only): stop to takeover of a '
                  f'task {rng(r5)} s, median {statistics.median(r5)} s over {len(r5)} takeovers '
                  f'(results/relay-stall-takeover/supp/handoff1-taskhold).')
    an['handoff'] = dict(trials=per, round5_stop_to_takeover_s=r5)


# ---------------------------------------------------------------- C: stale contracts

def contracts_section(md, an):
    rows, out = [], {}
    cells = [('contracts-provider (staleContracts on)', OUT, 'contracts', 'contracts-provider'),
             ('contracts-provider-staleoff (control)', OUT, 'contracts', 'contracts-provider-staleoff'),
             ('contracts-provider (round 5)', R5, 'contracts', 'contracts-provider'),
             ('contracts-provider-control (round 5: no contract tools)', R5, 'contracts', 'contracts-provider-control')]
    for label, root, sub, arm in cells:
        ts = trials(root, sub, arm, 'readtime')
        if not ts:
            continue
        cs = [t['contracts'] for t in ts]
        trig = [c for c in cs if c.get('triggered')]
        runs = [t.get('run_detail_summary') or {} for t in ts]
        st = {}
        for t in ts:
            st[t.get('dependent_state')] = st.get(t.get('dependent_state'), 0) + 1
        o = dict(n=len(ts), triggered=len(trig),
                 republished=sum(1 for c in trig if c.get('provider_republished')),
                 read_after=sum(1 for c in trig if c.get('dependent_reads_after')),
                 next_stale=sum(1 for c in trig if c.get('next_read_stale') is True),
                 stale_seen=sum(1 for c in trig if c.get('stale_seen_s') is not None),
                 stale_shown=sum(1 for c in cs if c.get('stale_shown_ever')),
                 changed_since=sum(1 for c in trig if c.get('changed_since_nonempty')),
                 changed_since_read=sum(1 for c in trig if c.get('changed_since_read') is True),
                 dep_edited=sum(1 for c in trig if c.get('dependent_edited_after_ref')),
                 stale_contracts_run=[x.get('stale_contracts') for x in runs], states=st,
                 dep_new=mean([t.get('dependent_new_pass') for t in ts]),
                 prov_new=mean([t.get('provider_new_pass') for t in ts]),
                 unit=mean([t['unit_pass'] for t in ts]), accept=mean([t['accept_pass'] for t in ts]),
                 cost=mean([t['cost_usd'] for t in ts], 4), wall=mean([t['wall_s'] for t in ts], 0))
        out[label] = o
        has_flag = any('stale_seen_s' in c for c in cs)
        runv = o['stale_contracts_run']
        rows.append([label, o['n'], o['triggered'], f"{o['republished']}/{o['triggered']}",
                     f"{o['read_after']}/{o['triggered']}",
                     f"{o['next_stale']} / {o['stale_seen']}" if has_flag else 'n/a (round 5)',
                     '/'.join(str(fmt(x)) for x in runv) if any(x is not None for x in runv) else 'n/a',
                     f"{o['changed_since']} / {o['changed_since_read']}", f"{o['dep_edited']}/{o['triggered']}",
                     ', '.join(f'{k} {v}' for k, v in st.items()), f"{o['dep_new']} / {o['prov_new']}",
                     f"{o['unit']} / {o['accept']}", f"${o['cost']}", o['wall']])
    md.append('\n## C: stale contracts (readtime, 2 agents, Haiku)\n')
    md.append('The round 5 provider shape: on the provider\'s first contract on readtime/result.py the PRD line changes '
              'and the NOTE goes to the provider, which may edit result.py and republish. Next read stale / stale '
              'seen: the dependent\'s first contracts read after the change (after the republish when there was one) '
              'shows the contract stale: true / any later read does. Run view: stale_contracts at the end of each '
              'trial. In the control the flag is not shown (staleContracts off). Round 5 rows: no stale flag, and its '
              'control had the contract tools disallowed.\n')
    md.append(table(['cell', 'n', 'triggered', 'provider republished', 'dependent read after', 'next read stale / '
                     'stale seen', 'run view stale_contracts', 'changed_since / _read', 'dependent edited after',
                     'dependent state', 'dependent_new / provider_new', 'unit / acceptance', 'cost', 'wall s'], rows))
    an['contracts'] = out


# ---------------------------------------------------------------- D: witnessed evidence

def witness_of(t):
    """A trial's witness measures, recomputed from its stored call flows with relay_proof's current classifier (only
    the witness hook's denials count as blocked; trials of the first cell were measured before that fix)."""
    w = t.get('witness') or {}
    pa = t.get('per_agent') or {}
    if pa and all('flow' in v for v in pa.values()):
        import relay_proof as rp
        run = dict(t.get('run_detail_summary') or {})
        for k in ('done_witnessed', 'done_with_evidence', 'done_with_task_test'):
            run.setdefault(k, w.get(k))
        run.setdefault('done', w.get('done_total'))
        return rp.witness_measures(pa, {'run': run})
    return w


def witness_section(md, an):
    rows, out, blocks = [], {}, []
    cells = [('pair-relay-witness hone', 'pair-relay-witness', 'hone'),
             ('pair-relay-witness readtime', 'pair-relay-witness', 'readtime'),
             ('false-evidence hone', 'false-evidence', 'hone'), ('false-evidence readtime', 'false-evidence', 'readtime'),
             ('pair-relay hone (G-A, witness off)', 'pair-relay', 'hone'),
             ('pair-relay readtime (G-A, witness off)', 'pair-relay', 'readtime')]
    for label, arm, proj in cells:
        ts = trials(OUT, 'witness' if 'witness off' not in label else 'redirect', arm, proj)
        if not ts:
            continue
        ws = [witness_of(t) for t in ts]
        dw = sum(w.get('done_witnessed') or 0 for w in ws)
        dt = sum(w.get('done_total') or 0 for w in ws)
        o = dict(n=len(ts), calls=sum(len(w.get('calls') or []) for w in ws), done=sum(w.get('done') or 0 for w in ws),
                 blocked=sum(w.get('blocked_by_hook') or 0 for w in ws),
                 tool_error=sum(w.get('tool_error') or 0 for w in ws),
                 witness_required=sum(w.get('witness_required') or 0 for w in ws),
                 evidence_required=sum(w.get('evidence_required') or 0 for w in ws),
                 done_witnessed=dw, done_run=dt, done_with_evidence=sum(w.get('done_with_evidence') or 0 for w in ws),
                 done_with_task_test=sum(w.get('done_with_task_test') or 0 for w in ws),
                 smoke=sorted({s for t in ts for s in t.get('smoke_tests') or []}),
                 accept=mean([t['accept_pass'] for t in ts]), unit=mean([t['unit_pass'] for t in ts]),
                 cost=mean([t['cost_usd'] for t in ts], 4))
        out[label] = o
        for t, w in zip(ts, ws):
            for b in w.get('blocked') or []:
                blocks.append((label, t['trial'], b))
        rows.append([label, o['n'], o['calls'], o['done'], o['blocked'], o['tool_error'], o['witness_required'], o['evidence_required'],
                     ratio(dw, dt), o['done_with_evidence'], o['done_with_task_test'], ', '.join(o['smoke']) or '-',
                     f"{o['unit']} / {o['accept']}", f"${o['cost']}"])
    md.append('\n## D: witnessed evidence (Haiku pairs; G-D has witnessRequired on)\n')
    md.append('Done calls: every done call in the agents\' transcripts. Blocked by the hook: the PreToolUse witness '
              'check denied it (the evidence names a command the hook did not see with that exit code). '
              'witness_required / evidence_required: Seeker refused it. done_witnessed: the run view\'s count of '
              'finished tasks whose DONE carries witnessed true, over the finished tasks. Smoke: tasks given a harness '
              'smoke test because no unit test exercises their module.\n')
    md.append(table(['cell', 'n', 'done calls', 'done', 'blocked by the hook', 'refused by the tool (bad arguments)', 'witness_required',
                     'evidence_required', 'done_witnessed', 'done_with_evidence', 'done_with_task_test',
                     'smoke-tested tasks', 'unit / acceptance', 'cost'], rows))
    if blocks:
        md.append('\nBlocked done calls (the hook\'s reason as the agent saw it):\n')
        md += [f"- {lab} #{k} {b['agent']} at {b['ts_s']} s, evidence {json.dumps(b.get('evidence'))[:160]}: "
               f"{b['text'][:300]}" for lab, k, b in blocks]
    an['witness'] = dict(cells=out, blocked=[dict(cell=l, trial=k, **b) for l, k, b in blocks])


# ---------------------------------------------------------------- F and the overhead accounting

def oh_cell(paths):
    ts = [OH.trial(p) for p in paths]
    m = lambda xs: sum(xs) / len(xs) if xs else 0.0  # noqa: E731
    return dict(n=len(ts), trials=ts, turns={k: m([t['turns'][k] for t in ts]) for k in OH.BUCKETS},
                cost={k: m([t['cost'][k] for t in ts]) for k in OH.BUCKETS},
                turns_total=m([t['num_turns'] for t in ts]), turns_reported=m([t['turns_reported'] for t in ts]),
                cost_total=m([t['cost_usd'] for t in ts]), hook_checks=m([t['hook_checks'] for t in ts]),
                hook_refusals=m([t['hook_refusals'] for t in ts]), api_calls=m([t['api_calls'] for t in ts]),
                tool_calls=m([t['tool_calls'] for t in ts]),
                outcomes={k: m([t['outcomes'][k] for t in ts]) for k in OH.OUTCOMES})


def usable(paths):
    """Trial files whose trial dir still has the agents' stream logs."""
    out = []
    for p in paths:
        try:
            d = json.load(open(p)).get('dir') or ''
        except (OSError, ValueError):
            continue
        if glob.glob(os.path.join(d, '.log-agent-*.jsonl')):
            out.append(p)
    return out


F_CELLS = [('pair-relay hone haiku', 'redirect', 'pair-relay', 'hone', 'haiku', 'hone', 'haiku'),
           ('pair-relay readtime haiku', 'redirect', 'pair-relay', 'readtime', 'haiku', 'readtime', 'haiku'),
           ('quad-relay hone haiku (against pair-none hone)', 'redirect', 'quad-relay', 'hone', 'haiku', 'hone', 'haiku'),
           ('pair-relay readtime sonnet', 'sonnet', 'pair-relay', 'readtime', 'sonnet', 'readtime', 'sonnet')]
F_BENCH = [('pair-relay hone haiku (benchmark)', 'pair-relay', 'hone', 'haiku'),
           ('pair-relay readtime haiku (benchmark)', 'pair-relay', 'readtime', 'haiku'),
           ('quad-relay hone haiku (benchmark, against pair-none hone)', 'quad-relay', 'hone', 'haiku'),
           ('pair-relay readtime sonnet (benchmark)', 'pair-relay', 'readtime', 'sonnet')]


def f_row(label, relay, none):
    reg = sum(relay['turns'][k] - none['turns'][k] for k in REG_BUCKETS)
    reg_abs = sum(relay['turns'][k] for k in REG_BUCKETS)
    extra = relay['turns_total'] - none['turns_total']
    share = f'{100 * reg / extra:.0f}%' if extra else 'n/a'
    return [label, relay['n'], f"{relay['turns_total']:.1f}", f"{none['turns_total']:.1f}", f'{extra:+.1f}',
            f'{reg_abs:.1f}', f'{reg:+.1f}', share,
            ', '.join(f"{k.split(': ')[0] if 'refused' not in k else 'refused edit'} {relay['turns'][k]:.1f}"
                      for k in REG_BUCKETS)], dict(reg=reg, reg_abs=reg_abs, extra=extra)


def registry_section(md, an):
    rows, out, nones = [], {}, {}
    for label, sub, arm, proj, model, nproj, nmodel in F_CELLS:
        paths = usable(files(OUT, sub, arm, proj, model))
        if not paths:
            continue
        key = (nproj, nmodel)
        if key not in nones:
            nones[key] = oh_cell(usable(files(BENCH, 'arm1', 'pair-none', nproj, nmodel)))
        if not nones[key]['n']:
            continue
        row, o = f_row(label + ' (round 6)', oh_cell(paths), nones[key])
        rows.append(row)
        out[label] = o
    for label, arm, proj, model in F_BENCH:
        paths = usable(files(BENCH, 'arm1', arm, proj, model))
        key = (proj, model)
        if key not in nones:
            nones[key] = oh_cell(usable(files(BENCH, 'arm1', 'pair-none', proj, model)))
        if paths and nones[key]['n']:
            row, o = f_row(label, oh_cell(paths), nones[key])
            rows.append(row)
            out[label] = o
    md.append('\n## F: REGISTRY.md (claimed as slot:REGISTRY.md with its file for the write, v9)\n')
    md.append('Turns by overhead.py\'s buckets (one tool call is one turn): claims and releases of REGISTRY.md (path or '
              'slot) and edits of it refused by the hook. Extra: against the benchmark\'s pair-none cell of the same '
              'project and model (the quad cells against pair-none too, so their extra turns include the two '
              'additional agents). Share: REGISTRY.md\'s extra turns over all extra turns; the benchmark measured '
              '22 to 35%.\n')
    md.append(table(['cell', 'n', 'turns', 'pair-none turns', 'extra turns', 'REGISTRY.md turns',
                     'REGISTRY.md extra', 'share of extra', 'by bucket'], rows))
    an['registry'] = out


def overhead_section(md, an):
    paths = usable(files(OUT, 'sonnet', 'pair-relay', 'readtime', 'sonnet'))
    none_paths = usable(files(BENCH, 'arm1', 'pair-none', 'readtime', 'sonnet'))
    if not paths or not none_paths:
        md.append('\n## Overhead accounting, Sonnet readtime (v9)\n\nNo Sonnet trials with stream logs yet.')
        return
    relay, none = oh_cell(paths), oh_cell(none_paths)
    lines, top, extra_t, extra_c = OH.table('readtime', 'sonnet', relay, none)
    bench = oh_cell(usable(files(BENCH, 'arm1', 'pair-relay', 'readtime', 'sonnet')))
    head = ['\n## Overhead accounting redone, Sonnet pair-relay readtime (v9, round 6) against the benchmark\'s '
            'pair-none\n',
            f'Round 6 pair-relay: {relay["turns_total"]:.1f} turns, ${relay["cost_total"]:.4f} per trial (n={relay["n"]}); '
            f'benchmark v7 pair-relay: {bench["turns_total"]:.1f} turns, ${bench["cost_total"]:.4f} (n={bench["n"]}); '
            f'pair-none: {none["turns_total"]:.1f} turns, ${none["cost_total"]:.4f} (n={none["n"]}). Extra over '
            f'pair-none: {extra_t:+.1f} turns, {extra_c:+.4f} USD.\n']
    md += head + lines
    with open(os.path.join(OUT, 'overhead.md'), 'w') as fh:
        fh.write('# Relay overhead accounting, round 6 (Sonnet pair-relay readtime, v9)\n' + '\n'.join(head + lines)
                 + '\n')
    an['overhead'] = dict(relay={k: relay[k] for k in ('n', 'turns', 'cost', 'turns_total', 'cost_total', 'outcomes')},
                          none={k: none[k] for k in ('n', 'turns_total', 'cost_total')},
                          benchmark_relay={k: bench[k] for k in ('n', 'turns_total', 'cost_total')},
                          extra_turns=extra_t, extra_cost=extra_c,
                          groups={g: sum(relay['turns'][k] - none['turns'][k] for k in ks) for g, ks in OH.GROUPS})


# ---------------------------------------------------------------- cost and wall

def cost_section(md, an):
    rows, total, n = [], 0.0, 0
    for f in sorted(glob.glob(os.path.join(OUT, '*', '*', '*'))):
        if not os.path.isdir(f) or os.path.relpath(f, OUT).startswith('venvs'):
            continue
        xs = [json.load(open(p)) for p in sorted(glob.glob(os.path.join(f, '*-[0-9]*.json')))]
        if not xs:
            continue
        for model in sorted({t['model'] for t in xs}):
            ys = [t for t in xs if t['model'] == model]
            c = sum(t['cost_usd'] or 0 for t in ys)
            total, n = total + c, n + len(ys)
            rows.append([os.path.relpath(f, OUT) + f' {model}', len(ys), f"${mean([t['cost_usd'] for t in ys], 4)}",
                         rng([t['cost_usd'] for t in ys], 3), mean([t['wall_s'] for t in ys], 0), f'${round(c, 2)}'])
    md.append('\n## Cost and wall per cell\n')
    md.append(table(['cell', 'n', 'cost mean', 'range', 'wall mean s', 'cost sum'], rows))
    md.append(f'\nTotal: {n} trials, ${round(total, 2)}.')
    an['cost'] = dict(trials=n, total_usd=round(total, 2))


def main():
    md, an = ['# Round 6 tables (relay-redirect-2026-11-09)\n', 'Generated by round6_analyse.py from the trial JSONs '
              'under results/relay-redirect/, beside results/relay-benchmark (v7) and results/relay-stall-takeover '
              '(round 5, v8).\n'], {}
    redirect_section(md, an)
    handoff_section(md, an)
    contracts_section(md, an)
    witness_section(md, an)
    registry_section(md, an)
    overhead_section(md, an)
    cost_section(md, an)
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, 'tables.md'), 'w') as fh:
        fh.write('\n'.join(md) + '\n')
    with open(os.path.join(OUT, 'analysis.json'), 'w') as fh:
        json.dump(an, fh, indent=1, default=str)
    print(os.path.join(OUT, 'tables.md'))


if __name__ == '__main__':
    main()
