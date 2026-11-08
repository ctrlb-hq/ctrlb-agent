"""Recount duplicated work for finished relay-proof trials after the bash_writes fix.

Before the fix, a Bash command counted as an edit of every task file it named when it contained `python3 -c`
(relay_proof.parse_transcript used run_swarm.WRITE_RE), so a command that cat'ed files and checked imports made the
agent an editor of them. Now relay_proof.parse_transcript counts a Bash command only when run_swarm.bash_writes says it
writes the file. Hook-denied and failed edit attempts (is_error tool results) were already excluded there.

For every trial JSON under results/relay-proof/<arm>/<project>/ (superseded-* dirs included), this rebuilds the
edit timeline from the agents' saved transcripts and recomputes editors, duplicated, duplicated_n, completions,
redo_completions, redone_share, src_written, src_files_written, per-agent files_written and abandonment, with the same
code as relay_proof.analyse. The old values go under "before_fix" (written once; a rerun keeps the first). A trial
with a missing transcript is listed and left unchanged. Then every arm's summary.json is rebuilt
(relay_proof.summarise), and results/relay-proof/dup-fix.md gets a before/after table per arm.

Usage: python3 -I dup_fix.py   (RELAY_PROOF_OUT, as relay_proof.py reads it, is the results tree it rewrites)
"""
import glob, json, os, statistics, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import relay_proof as rp  # noqa: E402

ROOT = rp.OUT_ROOT
KEYS = ('editors', 'duplicated', 'duplicated_n', 'completions', 'redo_completions', 'redone_share', 'src_written',
        'src_files_written', 'abandoned', 'abandoned_per_agent')


def recount(d):
    spec = 'toy' if d['project'] == 'toy' else f"devbench:{d['project']}"
    project = rp.load_project(spec)
    files = [p for _, p, _ in project['tasks']]
    task_of = set(files)
    agents = list(d['per_agent'])
    timeline, missing = {}, []
    for a in agents:
        sid = d['per_agent'][a].get('session_id') or ''
        tpath = rp.transcript_path(sid) if sid else ''
        if not tpath:
            missing.append(f'{a} (session {sid or "none"})')
            continue
        timeline[a] = rp.parse_transcript(tpath, d['dir'], files)
    if missing:
        return None, missing
    applied = {a: [e for e in timeline[a]['edits'] if e['applied'] and e['path'] in task_of] for a in agents}
    written = {a: sorted({e['path'] for e in applied[a]}) for a in agents}
    editors = {f: sorted(a for a in agents if f in written[a]) for f in files}
    duplicated = sorted(f for f, s in editors.items() if len(s) > 1)
    comps = []
    for a in agents:
        for f in written[a]:
            ts = [e['ts'] for e in applied[a] if e['path'] == f and e['ts']]
            if ts:
                comps.append(dict(agent=a, file=f, first=min(ts), last=max(ts)))
    redo = [c for c in comps if any(o['file'] == c['file'] and o['agent'] != c['agent'] and o['last'] < c['first']
                                    for o in comps)]
    src_written = sorted({f for a in agents for f in written[a]})
    new = dict(editors=editors, duplicated=duplicated, duplicated_n=len(duplicated), completions=len(comps),
               redo_completions=[dict(agent=c['agent'], file=c['file']) for c in redo],
               redone_share=round(len(redo) / len(comps), 4) if comps else 0.0,
               src_written=src_written, src_files_written=len(src_written))
    return dict(new=new, written=written), []


def mean(xs):
    xs = [x for x in xs if isinstance(x, (int, float)) and not isinstance(x, bool)]
    return round(statistics.mean(xs), 4) if xs else None


def main():
    if len(sys.argv) > 1:
        sys.exit(__doc__)
    if not os.path.isdir(ROOT):
        sys.exit(f'no results tree at {ROOT}: set RELAY_PROOF_OUT')
    files = sorted(f for f in glob.glob(os.path.join(ROOT, '**', '*.json'), recursive=True)
                   if os.path.basename(f) != 'summary.json' and os.path.dirname(os.path.dirname(f)) != ROOT
                   and os.path.dirname(f) != ROOT)
    arms, missing_all = {}, []
    for f in files:
        with open(f) as fh:
            d = json.load(fh)
        if not all(k in d for k in ('arm', 'per_agent', 'project', 'run_id')):
            continue
        arm = os.path.relpath(os.path.dirname(os.path.dirname(f)), ROOT)
        out, missing = recount(d)
        rec = arms.setdefault(arm, dict(before=[], after=[], changed=0, n=0, missing=[]))
        rec['n'] += 1
        before = d.get('before_fix') or {k: d.get(k) for k in KEYS}
        before['files_written'] = (d.get('before_fix') or {}).get(
            'files_written', {a: v.get('files_written') for a, v in d['per_agent'].items()})
        if out is None:
            rec['missing'].append(f"{os.path.relpath(f, ROOT)}: {', '.join(missing)}")
            missing_all.append(f)
            rec['before'].append(before)
            rec['after'].append(before)
            continue
        new = out['new']
        for a, w in out['written'].items():
            d['per_agent'][a]['files_written'] = w
        d.update(new)
        d['abandoned'], d['abandoned_per_agent'] = rp.abandonment(d)
        after = {k: d.get(k) for k in KEYS}
        changed = any(before.get(k) != after.get(k) for k in ('editors', 'duplicated', 'redone_share', 'src_written'))
        rec['changed'] += changed
        d['before_fix'] = before
        d['dup_fix'] = 'bash_writes: python -c counts only when its program writes the file; denied edits excluded'
        tmp = f + '.tmp'
        with open(tmp, 'w') as fh:
            json.dump(d, fh, indent=1)
        os.replace(tmp, f)
        rec['before'].append(before)
        rec['after'].append(after)
    for arm in sorted(arms):
        rp.summarise(arm)
    lines = ['# Duplicated-work recount after the bash_writes fix', '',
             'Fix: a Bash command counts as an edit of a task file only when it writes it (redirect, tee, sed -i, '
             'cp/mv onto it, or a python -c / python heredoc program that opens it for writing, write_text/'
             'write_bytes, shutil copy/move or os.replace/rename onto it). Before, any command naming the file and '
             'containing `python3 -c` counted (relay_proof.parse_transcript used run_swarm.WRITE_RE), e.g. readtime '
             'pair-relay trial 2, agent-1: `cat readtime/utils.py readtime/api.py && python3 -c "import bs4, ..."`. '
             'Hook-denied and failed Edit/Write attempts (is_error results) do not count (relay_proof already '
             'excluded them; run_swarm.analyse now does too). Recomputed from the saved transcripts with '
             '`python3 -I dup_fix.py`; old values are in each trial JSON under `before_fix`. A write after a `cd` to '
             'a directory outside the trial dir (an agent testing in its own scratch copy, e.g. fence-off readtime '
             'trial 1) does not count unless it names the trial dir absolutely. Redirect/heredoc writes to non-src/ '
             'paths, which the old WRITE_RE missed, now count. Dropping a false edit can also raise redone_share: the '
             'agent\'s first real edit of the file then comes after the other agent finished it.', '',
             'Unit tests of the rule: `python3 -I test_bash_writes.py`.', '',
             '| Arm | Trials | Changed | mean duplicated_n before | after | mean redone_share before | after | '
             'mean src_files_written before | after | abandoned before | after |',
             '|---|---|---|---|---|---|---|---|---|---|---|']
    for arm in sorted(arms):
        r = arms[arm]
        b, a = r['before'], r['after']
        lines.append(f"| {arm} | {r['n']} | {r['changed']} | {mean([x.get('duplicated_n') for x in b])} | "
                     f"{mean([x.get('duplicated_n') for x in a])} | {mean([x.get('redone_share') for x in b])} | "
                     f"{mean([x.get('redone_share') for x in a])} | {mean([x.get('src_files_written') for x in b])} | "
                     f"{mean([x.get('src_files_written') for x in a])} | {sum(bool(x.get('abandoned')) for x in b)} | "
                     f"{sum(bool(x.get('abandoned')) for x in a)} |")
    miss = [m for arm in sorted(arms) for m in arms[arm]['missing']]
    lines += ['', 'Trials with a missing transcript (left unchanged): ' + ('none' if not miss else ''), '']
    lines += [f'- {m}' for m in miss]
    lines += ['', 'Trials written after this recount by a relay_proof.py process started before the fix still carry '
                  'the old count; rerun `python3 -I dup_fix.py` once those finish (it is idempotent: before_fix keeps '
                  'the first values).', '']
    with open(os.path.join(ROOT, 'dup-fix.md'), 'w') as fh:
        fh.write('\n'.join(lines))
    print('\n'.join(lines))


if __name__ == '__main__':
    main()
