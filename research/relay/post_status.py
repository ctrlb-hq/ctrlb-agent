"""Post one status note to a Relay run on the local lake (stream swarm_board), so a watcher can read progress.

Usage: python3 -I post_status.py "STATUS: task 3 briefs in progress; Seeker routes and tools written, tests next"
Env:   SWARM_RUN   run id          (required)
       SWARM_AGENT agent name      (default orchestrator)
       CTRLB_URL   lake            (default http://127.0.0.1:5160); CTRLB_ORG, CTRLB_AUTH as lib_lake reads them

Read it back with:
  python3 -I post_status.py --tail            (last 20 notes of the run)
"""
import os, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import lib_lake as L  # noqa: E402

STREAM = 'swarm_board'
RUN = os.environ.get('SWARM_RUN', '')
AGENT = os.environ.get('SWARM_AGENT', 'orchestrator')


def post(text):
    L.ingest(STREAM, [dict(run_id=RUN, agent=AGENT, kind='NOTE', resource='', key='', text=text[:2000], nonce='')],
             timeout=10)
    print(f'posted to {RUN} as {AGENT}: {text[:80]}')


def tail(n=20):
    rows = L.search(STREAM, f"SELECT agent, kind, text, _timestamp FROM {STREAM} WHERE run_id = '{L.q(RUN)}' "
                            f"ORDER BY _timestamp DESC LIMIT {n}", size=n, timeout=10)
    for r in reversed(rows):
        t = time.strftime('%m-%d %H:%MZ', time.gmtime(int(r['_timestamp']) / 1e6))
        print(f"{t} {r['agent']:>14} {r['kind']:5} {r.get('text') or ''}")
    if not rows:
        print(f'no rows for run {RUN}')


if __name__ == '__main__':
    if len(sys.argv) < 2 or sys.argv[1] in ('-h', '--help'):
        sys.exit(__doc__)
    if not RUN:
        sys.exit('SWARM_RUN is not set')
    if sys.argv[1] == '--tail':
        tail()
    else:
        post(' '.join(sys.argv[1:]))
