"""Time GET history for one resource, index and scan predicates interleaved.
Usage: python3 -I hist_time.py RESOURCE [N] [OUT.json]. Prints wall and server ms (min/median/max) and the answer's counts."""
import sys, json, time, statistics
sys.path.insert(0, '/home/adarsh/benchmarks/swarm')
import relay_proof as r
res, n = sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 7
out = {'index': [], 'scan': []}
last = {}
for i in range(n):
    for pred in ['index', 'scan']:
        t = time.time()
        st, d = r.http('GET', f'/ai/relay/relaycoord_542c9dfe/history?resource={res}&since=30d&limit=50&predicate={pred}', cred='SEEKER_JWT', timeout=60)
        el = (time.time() - t) * 1000
        assert st == 200, (st, d)
        out[pred].append(dict(wall_ms=round(el, 1), server_ms=d['read']['elapsed_ms']))
        last[pred] = {k: d[k] for k in ['runs', 'counts']}
summary = {}
for p, v in out.items():
    w = [x['wall_ms'] for x in v]; s = [x['server_ms'] for x in v]
    summary[p] = dict(wall=[min(w), statistics.median(w), max(w)], server=[min(s), statistics.median(s), max(s)], answer=last[p])
    print(p, 'wall ms min/med/max', round(min(w)), round(statistics.median(w)), round(max(w)), '| server ms min/med/max', round(min(s)), round(statistics.median(s)), round(max(s)), json.dumps(last[p]))
if len(sys.argv) > 3:
    json.dump(dict(resource=res, n=n, runs=out, summary=summary, at=time.time()), open(sys.argv[3], 'w'), indent=1)
