"""Replay finished runs through Seeker's run view for stalled tasks (Seeker decision 31): GET
/ai/relay/{dataset}/runs/{run}?at=<µs> stepped across each run, so the detector says how many tasks stalled and when.
Read only: it never writes a row or changes a setting.

Usage (Seeker must run a build with decision 31, feat/relay-done-stall or later):
  python3 -I research/relay/stall_replay.py [--step 30] [--after 900] [RUN_ID ...]
With no run id it replays the handoff-control trials (<RELAY_PROOF_OUT>/handoff-control/*/*.json). Writes
<STALL_REPLAY_OUT> and prints one line per run. Env: RELAY_SEEKER (http://127.0.0.1:8096), RELAY_SESSIONS_DATASET
(agent_sessions), SEEKER_MCP_KEY (read into the header only, never printed), RELAY_PROOF_OUT (default
results/relay-proof next to this file), STALL_REPLAY_OUT (default results/relay-done-stall/stall_replay.json).
"""
import argparse, glob, json, os, sys, urllib.error, urllib.parse, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
SEEKER = os.environ.get('RELAY_SEEKER', 'http://127.0.0.1:8096')
DS = os.environ.get('RELAY_SESSIONS_DATASET', 'agent_sessions')
PROOF_OUT = os.environ.get('RELAY_PROOF_OUT') or os.path.join(HERE, 'results', 'relay-proof')
OUT = os.environ.get('STALL_REPLAY_OUT') or os.path.join(HERE, 'results', 'relay-done-stall', 'stall_replay.json')


def detail(run_id, at=None, from_us=None):
    q = {}
    if at:
        q['at'] = int(at)
    if from_us:
        q['from'] = int(from_us)
    url = f'{SEEKER}/ai/relay/{DS}/runs/{urllib.parse.quote(run_id, safe="")}' + ('?' + urllib.parse.urlencode(q) if q else '')
    req = urllib.request.Request(url, headers={'Authorization': 'Bearer ' + os.environ['SEEKER_MCP_KEY'], 'X-CtrlB-Agent': 'stall-replay'})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read() or b'{}')
    except urllib.error.HTTPError as e:
        return e.code, {'error': (e.read() or b'')[:300].decode(errors='replace')}


def replay(run_id, step_s, after_s):
    st, now = detail(run_id)
    if st != 200:
        return dict(run_id=run_id, error=f'{st} {now}')
    run = now.get('run') or {}
    if 'stalled_tasks' not in run:
        return dict(run_id=run_id, error='this Seeker has no stall detector (stalled_tasks missing): run the feat/relay-done-stall build')
    start, end = int(run['started']), int(run['last_write'])
    first, timeline = {}, []
    at = start + step_s * 1_000_000
    while at <= end + after_s * 1_000_000:
        st, d = detail(run_id, at=at, from_us=start - 60_000_000)
        if st != 200:
            timeline.append(dict(at_s=round((at - start) / 1e6), error=st))
        else:
            stalled = (d.get('detectors') or {}).get('stalled') or []
            timeline.append(dict(at_s=round((at - start) / 1e6), stalled=len(stalled)))
            for s in stalled:
                first.setdefault(s['key'], dict(task=s['task'], holder=s['holder'], first_seen_s=round((at - start) / 1e6),
                                                stalled_since_s=round((s['last_activity_ts'] - start) / 1e6),
                                                last_activity=s['last_activity'], lease_lapsed=bool(s.get('lease_lapsed'))))
        at += step_s * 1_000_000
    return dict(run_id=run_id, duration_s=round((end - start) / 1e6), stalls=len(first), tasks=list(first.values()),
                stalled_tasks_now=run.get('stalled_tasks'), stalled_now=(now.get('detectors') or {}).get('stalled'),
                done=run.get('done'), done_with_evidence=run.get('done_with_evidence'), timeline=timeline)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('runs', nargs='*')
    ap.add_argument('--step', type=int, default=30, help='seconds between replay points (default 30)')
    ap.add_argument('--out', default=OUT, help='where to write the replay (default: OUT above)')
    ap.add_argument('--after', type=int, default=900, help='seconds past the run\'s last write to keep replaying (default 900)')
    a = ap.parse_args()
    if not os.environ.get('SEEKER_MCP_KEY'):
        sys.exit('SEEKER_MCP_KEY is not set')
    runs = a.runs or [json.load(open(f))['run_id'] for f in sorted(glob.glob(os.path.join(
        PROOF_OUT, 'handoff-control', '*', '*.json'))) if not f.endswith('summary.json')]
    out = [replay(r, a.step, a.after) for r in runs]
    for r in out:
        if 'error' in r:
            print(f"{r['run_id']}: {r['error']}")
            continue
        when = ', '.join(f"{t['task']} ({t['holder']}) stalled from {t['stalled_since_s']} s, seen at {t['first_seen_s']} s"
                         + (' lease lapsed' if t['lease_lapsed'] else '') for t in r['tasks']) or 'none'
        print(f"{r['run_id']}: {r['stalls']} stalled task(s) over {r['duration_s']} s: {when}")
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    with open(a.out, 'w') as fh:
        json.dump(out, fh, indent=1)
    print(f'wrote {a.out}')


if __name__ == '__main__':
    main()
