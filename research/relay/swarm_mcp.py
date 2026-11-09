"""swarm: an MCP stdio server giving one agent of a swarm a shared board on the lake (stream swarm_board).
The research prototype of CtrlB Relay, kept as the reference the product's rules were ported to and from; it is not the
plugin. Env: SWARM_RUN (shared by every agent of one run), SWARM_AGENT (this agent's id), SWARM_TOOLS (comma list of
tools to expose, default peers,post,claim,release), SWARM_WAIT_MS (20), SWARM_LEASE_S (600), SWARM_STALL_S (300), and
lib_lake's CTRLB_URL, CTRLB_ORG, CTRLB_AUTH. Standard library only.

  peers()                    what every agent in this run has posted, claimed or released, newest first
  post(text)                 append a note (what you are about to do or have done)
  claim(resource, reopen?, paths?, wait_s?)
                             write-first claim: write HOLD (or REOPEN) on the resource and on each of paths in one
                             write, wait, read each key's rows; held iff the first live hold of the current round is
                             ours on every deciding key. wait_s (at most 55) waits in line instead of refusing
  claim(resource, takeover=true)
                             take over a hold whose holder made no progress for SWARM_STALL_S (300): a HANDOFF with the
                             stalled holder as giver (Seeker decision 33); refused not_stalled while it works
  release(resource, finished?)  hand back (RELEASE) or, for a task name, finish (DONE); a bundle's files go with it;
                             finished is the alias of done
  done(resource, evidence?)  finish a task name (DONE) with the evidence of its check {command, exit_code, summary} as
                             the DONE row's text (Seeker decision 30); SWARM_EVIDENCE_REQUIRED=1 refuses one that
                             names no command, and a finished release, with reason evidence_required
  task(name, text, paths)    register a task with its files (a BRIEF on the task name with paths)
  tasks()                    the run's registered tasks with their state
  brief(resource, text)      a note about a file or document read, keyed as claims key it (a BRIEF row)
  briefs(resources?, limit?) the newest brief of each key, newest first (40 by default, at most 200)

The rules are CtrlB Relay's (Seeker docs/relay.md, Claim protocol, State machine, Waiting, Registered tasks and bundled
claims), ported from Seeker's internal/pkg/relay: resources are keyed as Seeker keys them (relay_paths.key, the table
tests/fixtures/keys.json); file paths are leases and never finished; task names are worked on in rounds (DONE finishes
a round, the first REOPEN after it starts the next); read_key is the same fold as Seeker's ReadKey (both pass
tests/fixtures/key_states.json); a claim whose second read does not show its own row decides nothing (reason
not_visible); a finished release after the lease ran out finishes the task when nobody else holds it.

Waiting (decision 25): a claim with wait_s keeps its losing HOLDs live as its place in line and reads the keys again
every 5 x SWARM_WAIT_MS until it is first (held, waited_ms), a DONE finishes the round (finished_by), or wait_s runs
out (a RELEASE with text timed_out, a REFUSED row, reason timed_out with held_by and position).

Registered tasks (decisions 18 and 27): a file registered to one task is decided on the task's key when that task is
claimed (its HOLD is still written); a claim naming a file registered to a task the caller does not hold is refused
before anything is written (reason belongs_to_task, task, held_by). Not ported: unknown_task and its suggestion.
"""
import json, os, random, sys, time, uuid
sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lib_lake as L  # noqa: E402
import relay_paths  # noqa: E402

STREAM = 'swarm_board'
RUN, AGENT = os.environ.get('SWARM_RUN', ''), os.environ.get('SWARM_AGENT', '')
WAIT_MS = int(os.environ.get('SWARM_WAIT_MS', '20'))
LEASE_S = int(os.environ.get('SWARM_LEASE_S', '600'))
EXPOSE = [t for t in os.environ.get('SWARM_TOOLS', 'peers,post,claim,release').split(',') if t]
EVIDENCE_REQUIRED = os.environ.get('SWARM_EVIDENCE_REQUIRED', '') == '1'
EVIDENCE_REQUIRED_MESSAGE = 'say which command you ran and its exit code'

HOLDS = ('HOLD', 'REOPEN')
KIND_RANK = {'HOLD': 0, 'REOPEN': 1, 'RELEASE': 2, 'DONE': 3, 'HANDOFF': 4}
NOT_VISIBLE, TIMED_OUT, RETRY, BELONGS_TO_TASK = 'not_visible', 'timed_out', 'retry', 'belongs_to_task'
# Waiting (Seeker decision 25): wait_s is capped at MAX_WAIT_S; a waiting claim reads its keys every WAIT_READ_EVERY x
# WAIT_MS; a bundle with paths decided by the AND rule adds a random BACKOFF_S to each read after a loss, and sleeps one
# before it claims again when it found itself crossed with another bundle.
MAX_WAIT_S = 55
WAIT_READ_EVERY = 5
BACKOFF_S = (0.05, 0.5)
MAX_PATHS = 32
# Takeover (Seeker decision 33): a hold whose holder made no progress for STALL_S (the workspace's stallAfterSeconds)
# can be taken over with claim(takeover=true): a HANDOFF with the stalled holder as giver and this text.
STALL_S = int(os.environ.get('SWARM_STALL_S', '300'))
NOT_STALLED, OTHER_USER = 'not_stalled', 'other_user'
TAKEOVER_TEXT = 'stalled: taken over by '


# ---------------------------------------------------------------------------------------------------------------------
# The state machine (Seeker internal/pkg/relay ReadKey)
# ---------------------------------------------------------------------------------------------------------------------

def _order(r):
    """Rows fold in the order (timestamp, agent, nonce, kind), a total order on the fields read_key reads. Python
    compares str by code point, which is Go's byte order of the same UTF-8 strings."""
    return (int(r['ts']), r['agent'], r['nonce'], KIND_RANK.get(r['kind'], 5), r.get('token') or '', r.get('to') or '')


def read_key(rows, now_us, lease_s, key=None):
    """The state of one key from its rows (any order; kinds other than HOLD, REOPEN, RELEASE and DONE are ignored).
    Each row: ts (µs), agent, kind, nonce, and key (or pass key). Returns dict(live, open, finished, reopened):
    live and open holds of the current round oldest first (open: not closed by a RELEASE or DONE of their nonce; live:
    also younger than lease_s), the DONE that finished the current round (a name key only) and the REOPEN that
    started it. A HANDOFF row (to, token) whose nonce is the first live hold at its time, by its agent, closes that hold
    and opens the receiver's (agent to, nonce token, ts the HANDOFF's) in its place in the order.

    A name key is worked on in rounds: a DONE whose nonce is a hold of the current round finishes it (the first such
    DONE only); the first REOPEN after that starts the next round, and a REOPEN in an unfinished round is an ordinary
    hold. A path key has one round and is never finished. What a key's prefix allows comes from the kinds table
    (relay_paths.kind_of: finishable, reopenable), and a kind with a lease of its own (a slot) uses it instead of
    lease_s."""
    if key is None:
        key = next((r.get('key') for r in rows if r.get('key')), '')
    rows = sorted(rows, key=_order)
    closed = {r['nonce'] for r in rows if r['kind'] in ('RELEASE', 'DONE')}
    kind = relay_paths.kind_of(key)
    is_name, reopenable = kind['finishable'], kind['reopenable']
    lease_s = relay_paths.lease_seconds_for(key, lease_s)
    lease_us = lease_s * 1_000_000
    finished = reopened = None
    holds, round_, closed_so_far = [], set(), set()
    for r in rows:
        if r['kind'] == 'REOPEN' and reopenable and finished is not None:
            finished, reopened, round_, holds = None, r, {r['nonce']}, [r]
        elif r['kind'] in HOLDS:
            round_.add(r['nonce'])
            holds.append(r)
        elif r['kind'] in ('RELEASE', 'DONE'):
            closed_so_far.add(r['nonce'])
            if r['kind'] == 'DONE' and is_name and finished is None and r['nonce'] in round_:
                finished = r
        elif r['kind'] == 'HANDOFF':
            # Taken only when its nonce is the first live hold at the HANDOFF, of its agent, the round not finished.
            at = -1
            if finished is None and r.get('to') and r.get('token') and r['nonce']:
                for i, g in enumerate(holds):
                    if g['nonce'] in closed_so_far or int(r['ts']) - int(g['ts']) >= lease_us:
                        continue
                    if g['nonce'] == r['nonce'] and g['agent'] == r['agent'] and g.get('user', '') == r.get('user', ''):
                        at = i
                    break
            if at < 0:
                continue
            closed_so_far.add(r['nonce'])
            closed.add(r['nonce'])
            round_.add(r['token'])
            # The receiver takes the giver's place in the order.
            holds.insert(at + 1, dict(ts=int(r['ts']), agent=r['to'], kind='HOLD', nonce=r['token'], key=r.get('key', key),
                                      paths=r.get('paths', ''), user=r.get('user', '')))
    open_ = [r for r in holds if r['nonce'] not in closed]
    live = [r for r in open_ if now_us - int(r['ts']) < lease_us]
    return dict(live=live, open=open_, finished=finished, reopened=reopened)


def _key_takeover_rows(st, holder, agent, token, resource, key, main):
    """The rows a takeover writes on one key (Seeker decision 33), given that key's state: where the stalled holder is
    first live, a RELEASE of its other live holds of the key and a HANDOFF of its first live hold to agent (token);
    where it holds the key live but not first (a file decided on its task's key), a RELEASE of its open holds and
    agent's HOLD (token) behind; where it holds nothing, agent's HOLD (token). The holder's rows carry its name: the
    takeover is a HANDOFF the holder did not write, so the fold does not change."""
    rel = lambda h: dict(agent=holder, kind='RELEASE', nonce=h['nonce'], to='', key=key, resource=resource)
    hold = dict(agent=agent, kind='HOLD', nonce=token, to='', key=key, resource=resource, text=TAKEOVER_TEXT + agent)
    live = st['live']
    if live and live[0]['agent'] == holder:
        out = [rel(h) for h in live[1:] if h['agent'] == holder]
        out.append(dict(agent=holder, kind='HANDOFF', nonce=live[0]['nonce'], to=agent, token=token, key=key,
                        resource=resource, text=TAKEOVER_TEXT + agent))
        return out
    if not main and any(h['agent'] == holder for h in live):
        return [rel(h) for h in st['open'] if h['agent'] == holder] + [hold]
    return [hold]


def stall_reading(st, now_us, stall_after_s, activity_us=None, rows=()):
    """The stall detector on a key's first live hold (Seeker decision 31, every hold since decision 34): its holder,
    its newest progress (the hold's own stamp, a HANDOFF's for a handed hold, the holder's newest row of the key in
    rows, or activity_us: its newest other progress, such as rows on the task's files), and whether it is stalled
    (no progress for stall_after_s)."""
    h = st['live'][0]
    last, kind = int(h['ts']), 'hold'
    for r in rows:
        if r['agent'] == h['agent'] and int(r['ts']) > last:
            last, kind = int(r['ts']), r['kind'].lower()
    if activity_us is not None and int(activity_us) > last:
        last, kind = int(activity_us), 'activity'
    idle_s = (now_us - last) // 1_000_000
    return dict(holder=h['agent'], last_activity=kind, last_activity_ts=last, idle_s=idle_s,
                stall_after_s=stall_after_s, stalled=now_us - last >= stall_after_s * 1_000_000)


def takeover_plan(rows, now_us, lease_s, key, agent, stall_after_s, activity_us=None, token=None, user=''):
    """The takeover decision on one key (Seeker decision 33), a pure function of the key's rows: reason '' with the
    rows to write when the first live hold is another agent's and stalled; reason not_stalled (nothing written) when it
    is not; when there is nothing to take over (the key is free, its holder's lease ran out, or the caller holds it) the
    ordinary claim's HOLD, and nothing on a finished key (the claim is refused finished_by). held: whether the taker
    holds the key once the rows are written (decided by the fold, as the read after the wait decides). other_user
    when another user's agent holds it (the receiver's hold belongs to the giver's user, so agents take over within
    one user). holder and idle_s are the detector's reading. The taker's rows carry token as nonce (None in a plan:
    tokens are random)."""
    st = read_key(rows, now_us, lease_s, key)
    if st['finished']:
        return dict(held=False, reason='', holder='', idle_s=0, rows=[])
    if not st['live'] or st['live'][0]['agent'] == agent:
        # Nothing to take over: the ordinary claim's HOLD (a renew on the caller's own hold).
        return dict(held=True, reason='', holder='', idle_s=0,
                    rows=[dict(agent=agent, kind='HOLD', nonce=token, to='', key=key, resource='', text='')])
    rd = stall_reading(st, now_us, stall_after_s, activity_us, rows)
    if st['live'][0].get('user', '') != user:
        return dict(held=False, reason=OTHER_USER, holder=rd['holder'], idle_s=rd['idle_s'], rows=[], stall=rd)
    if not rd['stalled']:
        return dict(held=False, reason=NOT_STALLED, holder=rd['holder'], idle_s=rd['idle_s'], rows=[], stall=rd)
    return dict(held=True, reason='', holder=rd['holder'], idle_s=rd['idle_s'], stall=rd,
                rows=_key_takeover_rows(st, rd['holder'], agent, token, '', key, True))


def check_state(rows, now_us, lease_s, key, agent):
    """Seeker's edit check of one file with holdRequired: reason held (held_by, another agent is first live), unheld
    (nobody holds it live; lapsed when the agent's own hold ran out), or '' (the agent is first live)."""
    st = read_key(rows, now_us, lease_s, key)
    if st['live']:
        h = st['live'][0]
        return dict(reason='', held_by='') if h['agent'] == agent else dict(reason='held', held_by=h['agent'])
    if any(h['agent'] == agent for h in st['open']):
        return dict(reason='lapsed', held_by='')
    return dict(reason='unheld', held_by='')


# ---------------------------------------------------------------------------------------------------------------------
# Registered tasks and bundles (Seeker internal/pkg/relay tasks.go, ClaimBundle; decisions 18 and 27)
# ---------------------------------------------------------------------------------------------------------------------

def path_key(p, cwd=''):
    """Seeker's PathKey: one entry of a paths list keeps a prefix of the kinds table, any other is keyed as a file
    path (so a file with no extension is not taken for a task name)."""
    r = str(p or '').strip(relay_paths.GO_SPACE)
    if not r.startswith(tuple(row['prefix'] for row in relay_paths.KINDS)):
        r = relay_paths.PATH + r
    k = relay_paths.key(r, cwd)[0]
    if not k:
        raise ValueError(f'paths: {p!r} names nothing after its prefix')
    return k


def bundle_keys(resource_key, paths, cwd=''):
    """Seeker's bundleKeys: the keys of a bundle's paths, sorted, the resource key and repeats dropped; each a file
    or a slot (leasable, not finishable); at most MAX_PATHS."""
    paths = list(paths or [])
    if len(paths) > MAX_PATHS:
        raise ValueError(f'at most {MAX_PATHS} paths per claim, got {len(paths)}')
    out = set()
    for p in paths:
        k = path_key(p, cwd)
        kind = relay_paths.kind_of(k)
        if not kind['leasable'] or kind['finishable']:
            raise ValueError(f"paths: {p} is a {kind['kind']}; a claim takes files and slots with its resource, not "
                             f"another {kind['kind']}")
        if k != resource_key:
            out.add(k)
    return sorted(out)


def _path_keys(r):
    try:
        v = json.loads(r.get('paths') or '[]')
    except ValueError:
        return []
    return [k for k in v if isinstance(k, str) and k] if isinstance(v, list) else []


def registered_tasks(rows):
    """Seeker's RegisteredTasks: the newest BRIEF with paths of each task name (by timestamp, then agent) is its
    registration; a newer brief of the name without paths does not unregister it. Each row: ts, agent, kind, key,
    resource, text, paths (a JSON array of keys). Sorted by key."""
    newest = {}
    for r in rows:
        if r['kind'] != 'BRIEF' or not relay_paths.kind_of(r['key'])['finishable'] or not _path_keys(r):
            continue
        cur = newest.get(r['key'])
        if cur is None or (int(r['ts']), r['agent']) > (int(cur['ts']), cur['agent']):
            newest[r['key']] = r
    return [dict(key=k, resource=r.get('resource') or k[len(relay_paths.NAME):], text=r.get('text') or '',
                 agent=r['agent'], ts=int(r['ts']), paths=_path_keys(r)) for k, r in sorted(newest.items())]


def owners_of(tasks, key):
    """The registered tasks whose files include key."""
    return [t for t in tasks if key in t['paths']]


def deciding_keys(keys, task, tasks):
    """Seeker's decidingKeys: the resource key always decides; a path key does unless it is a file (a kind checked by
    the edit hook) registered to the claimed task and to no other, which the task's key alone decides (decision 27)."""
    out = [True] * len(keys)
    for i in range(1, len(keys)):
        if task is None or not relay_paths.kind_of(keys[i])['checked_by_hook']:
            continue
        owners = owners_of(tasks, keys[i])
        if len(owners) == 1 and owners[0]['key'] == task['key']:
            out[i] = False
    return out


def belongs_to_task(keys, task, tasks, state_of, agent):
    """Seeker's belongsToTask: the first key of a claim (keys[0] the resource, then the bundle's paths) that is a file
    registered to a task other than the claimed one, when the agent holds none of the tasks it is registered to, as
    dict(index, key, task (the first owner's resource), held_by (that task's first live holder, '' when none)); None
    when the claim may go on. state_of(key) is read_key of a task key now."""
    if not tasks:
        return None
    for i, k in enumerate(keys):
        if not relay_paths.kind_of(k)['checked_by_hook']:
            continue
        owners = owners_of(tasks, k)
        if not owners or task is not None and any(t['key'] == task['key'] for t in owners):
            continue
        holder, mine = '', False
        for o in owners:
            live = state_of(o['key'])['live']
            if live and live[0]['agent'] == agent:
                mine = True
                break
            if not holder and live:
                holder = live[0]['agent']
        if mine:
            continue
        return dict(index=i, key=k, task=owners[0]['resource'], held_by=holder)
    return None


# ---------------------------------------------------------------------------------------------------------------------
# The lake
# ---------------------------------------------------------------------------------------------------------------------

def row(kind, resource='', key='', text='', nonce='', **extra):
    r = dict(run_id=RUN, agent=AGENT, kind=kind, resource=resource, key=key, text=str(text)[:2000], nonce=nonce)
    if key:
        r['resource_kind'] = relay_paths.kind_of(key)['kind']
    r.update(extra)
    return r


def write(kind, resource='', key='', text='', nonce='', **extra):
    L.ingest(STREAM, [row(kind, resource, key, text, nonce, **extra)], timeout=10)


def write_rows(rows):
    """Rows in one lake write (the lake stamps them with one timestamp, observed, not guaranteed)."""
    if rows:
        L.ingest(STREAM, rows, timeout=10)


def run_tasks():
    """The run's registered tasks (registered_tasks over its BRIEF rows on task names)."""
    try:
        rows = L.search(STREAM, f"SELECT * FROM {STREAM} WHERE run_id = '{L.q(RUN)}' AND kind = 'BRIEF' AND key LIKE "
                                f"'{L.q(relay_paths.NAME)}%'", size=5000, timeout=10)
    except RuntimeError as e:
        if 'key' in str(e) and ('Schema' in str(e) or 'No field' in str(e) or 'not found' in str(e)):
            return []
        raise
    return registered_tasks([dict(ts=int(r['_timestamp']), agent=r['agent'], kind=r['kind'], key=r.get('key') or '',
                                  resource=r.get('resource') or '', text=r.get('text') or '', paths=r.get('paths') or '')
                             for r in rows])


def hold_rows(key):
    """Every HOLD, REOPEN, RELEASE and DONE row of key in this run."""
    try:
        rows = L.search(STREAM, f"SELECT * FROM {STREAM} WHERE run_id = '{L.q(RUN)}' AND key = '{L.q(key)}' AND kind "
                                f"IN ('HOLD', 'REOPEN', 'RELEASE', 'DONE', 'HANDOFF')", size=10000, timeout=10)
    except RuntimeError as e:
        if 'key' in str(e) and ('Schema' in str(e) or 'No field' in str(e) or 'not found' in str(e)):
            return []  # the stream has no key column yet: no row of any key has been written
        raise
    if len(rows) >= 10000:
        raise RuntimeError(f'more than 10000 claim rows on {key}; refusing to decide on a capped read')
    return [dict(ts=int(r['_timestamp']), agent=r['agent'], kind=r['kind'], nonce=r.get('nonce') or '',
                 key=r.get('key') or key, to=r.get('to') or '', token=r.get('token') or '', paths=r.get('paths') or '')
            for r in rows]


def now_us():
    return int(time.time() * 1e6)


# ---------------------------------------------------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------------------------------------------------

def peers(limit=40):
    rows = L.search(STREAM, f"SELECT agent, kind, resource, key, text, _timestamp FROM {STREAM} WHERE run_id = "
                            f"'{L.q(RUN)}' ORDER BY _timestamp DESC LIMIT {int(limit)}", size=int(limit), timeout=10)
    return json.dumps(dict(you=AGENT, events=[dict(agent=r['agent'], kind=r['kind'], resource=r.get('resource') or '',
                                                   key=r.get('key') or '', text=(r.get('text') or '')[:300],
                                                   age_s=round((now_us() - int(r['_timestamp'])) / 1e6))
                                              for r in rows]))


def post(text):
    write('NOTE', text=text)
    return json.dumps(dict(ok=True))


def brief(resource, text):
    key, kind = relay_paths.key(resource)
    if not str(text).strip():
        raise ValueError('text is required')
    L.ingest(STREAM, [dict(run_id=RUN, agent=AGENT, kind='BRIEF', resource=resource, key=key, resource_kind=kind,
                           text=str(text)[:2000], nonce='')], timeout=10)
    return json.dumps(dict(ok=True, run_id=RUN, agent=AGENT, key=key, resource=resource))


def briefs(resources=None, limit=40):
    """The newest BRIEF of each key of the run (only the keys of resources when given), newest first."""
    if isinstance(resources, str):
        resources = resources.split(',')
    keys = sorted({relay_paths.key(r)[0] for r in resources or [] if str(r).strip()})
    limit = max(1, min(int(limit or 40), 200))
    where = f"run_id = '{L.q(RUN)}' AND kind = 'BRIEF'"
    if keys:
        where += ' AND key IN (' + ', '.join(f"'{L.q(k)}'" for k in keys) + ')'
    try:
        rows = L.search(STREAM, f"SELECT agent, key, resource, text, _timestamp FROM {STREAM} WHERE {where} "
                                f"ORDER BY _timestamp DESC", size=5000, timeout=10)
    except RuntimeError as e:
        if 'key' in str(e) and ('Schema' in str(e) or 'No field' in str(e) or 'not found' in str(e)):
            rows = []
        else:
            raise
    out, seen = [], set()
    for r in rows:
        if r['key'] in seen:
            continue
        seen.add(r['key'])
        out.append(dict(key=r['key'], resource=r.get('resource') or '', agent=r['agent'], text=r.get('text') or '',
                        ts=int(r['_timestamp']), age_s=round((now_us() - int(r['_timestamp'])) / 1e6)))
        if len(out) == limit:
            break
    return json.dumps(dict(briefs=out))


def _finished_message(resource, done):
    at = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(int(done['ts']) / 1e6))
    return (f"{resource} was already finished by {done['agent']} at {at}. If you were asked to work on it again, claim "
            f"it with reopen=true to start a new round of work; otherwise pick something else")


def _holdable(resource, key):
    if not key:
        raise ValueError(f'{resource!r} names nothing after its prefix')
    kind = relay_paths.kind_of(key)
    if not kind['leasable']:
        raise ValueError(f"{resource} is a {kind['kind']}: it is never held, so it cannot be claimed or released; write "
                         f"it with the {kind['kind']} tool and read it with {kind['kind']}s")


def claim(resource, reopen=False, paths=None, wait_s=0, takeover=False):
    """Seeker's ClaimBundle: the claim protocol on the resource's key and, for a bundle, on each of paths at once.
    A registered task claimed without paths takes its registered paths; a file registered to it alone is decided on the
    task's key. wait_s (at most MAX_WAIT_S) waits in line instead of refusing at once. takeover=true takes over a
    stalled hold (Seeker decision 33, _takeover)."""
    key, rkind = relay_paths.key(resource)
    _holdable(resource, key)
    wait_s = min(float(wait_s or 0), MAX_WAIT_S)
    if wait_s < 0:
        raise ValueError('wait_s must not be negative')
    if isinstance(paths, str):
        paths = [p for p in paths.split(',') if p.strip()]
    out = dict(held=False, resource=resource, key=key)
    # Seeker reads the run's registrations for every claim of a task name or a file, or with paths.
    tasks = run_tasks() if rkind == 'name' or paths or relay_paths.kind_of(key)['checked_by_hook'] else []
    task = next((t for t in tasks if t['key'] == key), None)
    if task is not None and not paths:
        paths = task['paths']
    others = bundle_keys(key, paths)
    if others:
        out['paths'] = others
    keys = [key] + others
    b = belongs_to_task(keys, task, tasks, lambda k: read_key(hold_rows(k), now_us(), LEASE_S, k), AGENT)
    if b:
        file = resource if b['index'] == 0 else b['key']
        state = f"{b['held_by']} holds it" if b['held_by'] else 'nobody holds it now'
        msg = (f"{file} belongs to the task {json.dumps(b['task'])} ({state}): its files are held through the task. "
               f"Claim {json.dumps(b['task'])} instead, and its files come with it")
        if b['index']:
            msg = f'{resource} was not claimed: {msg}'
            out['conflict_key'] = b['key']
        out.update(reason=BELONGS_TO_TASK, task=b['task'], message=msg)
        if b['held_by']:
            out['held_by'] = b['held_by']
        write('REFUSED', resource if b['index'] == 0 else b['key'], b['key'], text=msg, held_by=b['held_by'])
        return json.dumps(out)
    if takeover and not reopen:
        return json.dumps(_takeover(resource, keys, deciding_keys(keys, task, tasks), out))
    return json.dumps(_Claim(resource, keys, deciding_keys(keys, task, tasks), reopen, wait_s).run(out))


def _activity(agent, keys):
    """The newest row (any kind) agent wrote on any of keys in this run, in µs, or None: the prototype's stand-in for
    Seeker's progress reading (relay rows on the task's key and files; the prototype has no edit stream)."""
    if not keys:
        return None
    ks = ', '.join(f"'{L.q(k)}'" for k in keys)
    try:
        rows = L.search(STREAM, f"SELECT max(_timestamp) AS t FROM {STREAM} WHERE run_id = '{L.q(RUN)}' AND agent = "
                                f"'{L.q(agent)}' AND key IN ({ks})", size=1, timeout=10)
    except RuntimeError:
        return None
    t = rows[0].get('t') if rows else None
    return int(t) if t else None


def _takeover(resource, keys, decides, out):
    """Seeker's claim with takeover (decision 33). When the first live hold of the resource key is another agent's and
    its holder made no progress for STALL_S, write in one lake write, per key of the bundle, the rows of
    _key_takeover_rows (a HANDOFF with the stalled holder as giver where it is first), then wait, read every key and
    decide like a claim: held when first live on every deciding key (the read-your-write guard accepts the HANDOFF that
    carries our token); a loser releases the token on every key, which closes a received hold by its nonce. Not
    stalled: refused not_stalled with the reading, nothing written. Nothing to take over (free, finished, lapsed, our
    own): an ordinary claim."""
    key = keys[0]
    rows, now = hold_rows(key), now_us()
    st = read_key(rows, now, LEASE_S, key)
    if st['finished'] or not st['live'] or st['live'][0]['agent'] == AGENT:
        return _Claim(resource, keys, decides, False, 0).run(out)
    holder, token = st['live'][0]['agent'], uuid.uuid4().hex
    plan = takeover_plan(rows, now, LEASE_S, key, AGENT, STALL_S, _activity(holder, keys), token=token)
    reading = {k: v for k, v in plan['stall'].items() if k != 'stalled'}
    if plan['reason']:
        msg = (f"{resource} is held by {holder}, who last worked on it {reading['idle_s']}s ago (stalled after "
               f"{STALL_S}s); it was not taken over. Pick something else") if plan['reason'] == NOT_STALLED else (
               f'{resource} is held by {holder} of another user; only that user\'s agents can take it over')
        out.update(reason=plan['reason'], held_by=holder, stall=reading, message=msg)
        return out
    batch = [row(r['kind'], resource, key, text='' if r['kind'] == 'RELEASE' else r['text'], nonce=r['nonce'],
                 agent=r['agent'], **({'to': r['to'], 'token': r['token']} if r['kind'] == 'HANDOFF' else {}))
             for r in plan['rows']]
    if len(keys) > 1:
        for r in batch:
            if r['kind'] == 'HANDOFF':
                r['paths'] = json.dumps(keys[1:])
    for i, k in enumerate(keys[1:], 1):
        ks = read_key(hold_rows(k), now, LEASE_S, k)
        first = ks['live'][0]['agent'] if ks['live'] else ''
        if decides[i] and first not in ('', holder, AGENT):
            msg = (f'{resource} was not taken over: {k}, which comes with it, is held by {first}; nothing was taken. '
                   f'Pick something else')
            out.update(held_by=first, conflict_key=k, message=msg)
            write('REFUSED', k, k, text=msg, held_by=first)
            return out
        batch += [row(r['kind'], k, k, text='' if r['kind'] == 'RELEASE' else r['text'], nonce=r['nonce'],
                      agent=r['agent'], **({'to': r['to'], 'token': r['token']} if r['kind'] == 'HANDOFF' else {}))
                  for r in _key_takeover_rows(ks, holder, AGENT, token, k, k, False)]
    c = _Claim(resource, keys, decides, False, 0)
    try:
        write_rows(batch)
    except Exception:
        c._abandon(token)
        raise
    time.sleep(WAIT_MS / 1000)
    states = c._read(token)
    if states is None:
        return c._not_visible(out, token)
    lost = c._lost(states)
    if lost >= 0:
        return c._refuse(out, states, lost, token, '')
    c._held(out, states, token)
    out.update(taken_over_from=holder, stall=reading)
    return out


class _Claim:
    """One claim after its arguments were checked (Seeker's claimRun)."""

    def __init__(self, resource, keys, decides, reopen, wait_s):
        self.resource, self.keys, self.decides, self.reopen, self.wait_s = resource, keys, decides, reopen, wait_s
        self.and_paths = any(decides[1:])
        self.start = time.time()
        self.waited = False

    def _resource_of(self, i):
        return self.resource if i == 0 else self.keys[i]

    def _releases(self, nonce, text=''):
        write_rows([row('RELEASE', self._resource_of(i), k, text=text, nonce=nonce) for i, k in enumerate(self.keys)])

    def _set_waited(self, out):
        if self.waited:
            out['waited_ms'] = max(1, int((time.time() - self.start) * 1000))

    def _backoff(self):
        return random.uniform(*BACKOFF_S)

    def run(self, out):
        key, deadline = self.keys[0], self.start + self.wait_s
        while True:
            # Step 0: a finished task name is refused unless the claim reopens it.
            hold_kind, text = 'HOLD', ''
            done = read_key(hold_rows(key), now_us(), LEASE_S, key)['finished']
            if done:
                if not self.reopen:
                    out.update(finished_by=done['agent'], finished_at=done['ts'],
                               message=_finished_message(self.resource, done))
                    self._set_waited(out)
                    write('REFUSED', self.resource, key, text=out['message'], held_by=done['agent'])
                    return out
                hold_kind, text = 'REOPEN', f"reopened after {done['agent']} finished it"
            # Step 1: one HOLD per key, one nonce, one write; the resource key's row carries the others in paths.
            nonce = uuid.uuid4().hex
            holds = [row('HOLD', self._resource_of(i), k, nonce=nonce) for i, k in enumerate(self.keys)]
            holds[0].update(kind=hold_kind, text=text)
            if len(self.keys) > 1:
                holds[0]['paths'] = json.dumps(self.keys[1:])
            try:
                write_rows(holds)
            except Exception:
                self._abandon(nonce)
                raise
            # Step 2.
            time.sleep(WAIT_MS / 1000)
            states, lost, retry = None, -1, False
            while not retry:
                # Step 3, with the read-your-write guard on every key.
                got = self._read(nonce)
                if got is not None:
                    states = got
                    # Step 4.
                    lost = self._lost(states)
                    if lost < 0:
                        return self._held(out, states, nonce)
                    if states[lost]['finished'] or not self.wait_s:
                        return self._refuse(out, states, lost, nonce, '')
                    if self._crossed(states):
                        # Two bundles crossed: neither leaves the line by waiting, so this one steps out and claims
                        # again with a fresh nonce.
                        self._releases(nonce, RETRY)
                        self.waited = True
                        time.sleep(self._backoff())
                        if time.time() >= deadline:
                            return self._refuse(out, states, lost, nonce, TIMED_OUT)
                        retry = True
                        continue
                elif not self.wait_s:
                    return self._not_visible(out, nonce)
                now = time.time()
                if now >= deadline:
                    if states is None:
                        return self._not_visible(out, nonce)   # never saw its own row: nothing was decided
                    return self._refuse(out, states, lost, nonce, TIMED_OUT)
                d = WAIT_READ_EVERY * WAIT_MS / 1000
                if self.and_paths and got is not None:
                    d += self._backoff()
                self.waited = True
                time.sleep(min(d, deadline - now))

    def _abandon(self, nonce):
        try:
            self._releases(nonce)
        except Exception:
            pass

    def _read(self, nonce):
        """Every key's state, or None when a read does not show the caller's own row."""
        states = []
        for k in self.keys:
            rows = hold_rows(k)
            # A takeover's own row on a key the holder was first on is the HANDOFF that carries its token.
            if not any(r['nonce'] == nonce and r['kind'] in HOLDS or r['kind'] == 'HANDOFF' and r.get('token') == nonce
                       for r in rows):
                return None
            states.append(read_key(rows, now_us(), LEASE_S, k))
        return states

    def _lost(self, states):
        for i, st in enumerate(states):
            if self.decides[i] and (not st['live'] or st['live'][0]['agent'] != AGENT or st['finished']):
                return i
        return -1

    def _crossed(self, states):
        """The caller is first live on some key while the first live holder of a deciding key it lost holds a live
        hold behind it there."""
        for j, lost in enumerate(states):
            if not self.decides[j] or not lost['live'] or lost['live'][0]['agent'] == AGENT:
                continue
            h = lost['live'][0]['agent']
            for i, st in enumerate(states):
                if i != j and st['live'] and st['live'][0]['agent'] == AGENT and any(o['agent'] == h for o in st['live'][1:]):
                    return True
        return False

    def _held(self, out, states, nonce):
        now = now_us()
        out.update(held=True, token=nonce)
        until = None
        for i, k in enumerate(self.keys):
            own = [int(r['ts']) for r in states[i]['live'] if r['agent'] == AGENT]
            u = (max(own) if own else now) + relay_paths.lease_seconds_for(k, LEASE_S) * 1_000_000
            until = u if until is None else min(until, u)
        out['lease_until'] = until
        if states[0]['live'][0]['nonce'] != nonce:
            out['renewed'] = True
        if states[0]['reopened'] and states[0]['reopened']['nonce'] == nonce:
            out['reopened'] = True
        self._set_waited(out)
        return out

    def _not_visible(self, out, nonce):
        # The read-your-write guard: a read that does not show our own row decides nothing.
        self._releases(nonce, NOT_VISIBLE)
        out.update(reason=NOT_VISIBLE, message=f'{self.resource} was not claimed: the claim record was not readable '
                                               f'yet, so nothing was decided. Claim it again')
        self._set_waited(out)
        return out

    def _refuse(self, out, states, lost, nonce, reason):
        self._releases(nonce, reason)
        lost_key, st = self.keys[lost], states[lost]
        if st['finished']:
            holder = st['finished']['agent']
            out.update(finished_by=holder, finished_at=st['finished']['ts'],
                       message=_finished_message(self.resource, st['finished']))
        else:
            holder = st['live'][0]['agent'] if st['live'] else '?'
            out['held_by'] = holder
            if lost:
                out['conflict_key'] = lost_key
            if reason == TIMED_OUT:
                what = self.resource if not lost else f'{self.resource} (with {lost_key})'
                out.update(reason=reason, message=f'{what} is still held by {holder} after waiting '
                                                  f'{round(time.time() - self.start)}s; nothing was taken. Pick '
                                                  f'something else',
                           position=next((n + 1 for n, r in enumerate(st['live']) if r['agent'] == AGENT), 0))
            elif not lost:
                out['message'] = f'{self.resource} is already held by {holder}; pick something else'
            else:
                out['message'] = (f'{self.resource} was not claimed: {lost_key}, which comes with it, is already held '
                                  f'by {holder}; nothing was taken. Pick something else')
        self._set_waited(out)
        write('REFUSED', self._resource_of(lost), lost_key, text=out['message'], nonce=nonce, held_by=holder)
        return out


def handoff(resource, to):
    """Hand our live hold of resource to agent `to` in one HANDOFF row (Seeker relay.Handoff, one key)."""
    key, kind = relay_paths.key(resource)
    _holdable(resource, key)
    out = dict(handed=False, resource=resource, key=key, to=to)
    st = read_key(hold_rows(key), now_us(), LEASE_S, key)
    if not st['live'] or st['live'][0]['agent'] != AGENT or st['finished']:
        holder = st['live'][0]['agent'] if st['live'] else ''
        out.update(held_by=holder or None, message=f'{resource} was not handed off: '
                   + (f'{holder} holds it, not you' if holder else 'you do not hold it'))
        return json.dumps({k: v for k, v in out.items() if v is not None})
    if to == AGENT:
        return claim(resource)
    giver, token = st['live'][0], uuid.uuid4().hex
    rows = [dict(run_id=RUN, agent=AGENT, kind='RELEASE', resource=resource, key=key, text='', nonce=h['nonce'])
            for h in st['live'][1:] if h['agent'] == AGENT]
    rows.append(dict(run_id=RUN, agent=AGENT, kind='HANDOFF', resource=resource, key=key, text=f'to {to}',
                     nonce=giver['nonce'], to=to, token=token))
    L.ingest(STREAM, rows, timeout=10)
    out.update(handed=True, token=token, lease_until=now_us() + relay_paths.lease_seconds_for(key, LEASE_S) * 1_000_000)
    return json.dumps(out)


def ask(text, to=''):
    """A QUESTION row with a fresh id (Seeker relay Ask): open until an ANSWER carries the id."""
    if not str(text).strip():
        raise ValueError('text is required')
    qid = uuid.uuid4().hex
    L.ingest(STREAM, [dict(run_id=RUN, agent=AGENT, kind='QUESTION', resource='', key='', text=str(text)[:2000],
                           nonce=qid, to=str(to or '').strip())], timeout=10)
    return json.dumps(dict(id=qid, run_id=RUN, agent=AGENT))


def answer(id, text):
    """An ANSWER row for question id of this run (Seeker relay Answer); an unknown id is an error."""
    if not str(text).strip():
        raise ValueError('text is required')
    rows = L.search(STREAM, f"SELECT nonce FROM {STREAM} WHERE run_id = '{L.q(RUN)}' AND kind = 'QUESTION' AND nonce = "
                            f"'{L.q(str(id))}'", size=1, timeout=10)
    if not rows:
        raise ValueError(f'no question {id} in run {RUN}')
    L.ingest(STREAM, [dict(run_id=RUN, agent=AGENT, kind='ANSWER', resource='', key='', text=str(text)[:2000],
                           nonce=str(id))], timeout=10)
    return json.dumps(dict(ok=True, id=str(id), run_id=RUN, agent=AGENT))


def contract(name, text, paths):
    """A BRIEF on contract:<name> with the files that define it (Seeker relay Contract)."""
    if isinstance(paths, str):
        paths = paths.split(',')
    keys = []
    for p in paths or []:
        if str(p).strip():
            k = path_key(p)
            if k not in keys:
                keys.append(k)
    if not keys or not str(text).strip():
        raise ValueError('a contract needs text and at least one file in paths')
    n = str(name).strip()
    key = relay_paths.key(n if n.startswith(relay_paths.CONTRACT) else relay_paths.CONTRACT + n)[0]
    if not key:
        raise ValueError('name is required')
    L.ingest(STREAM, [dict(run_id=RUN, agent=AGENT, kind='BRIEF', resource=key, key=key, resource_kind='contract',
                           text=str(text)[:2000], nonce='', paths=json.dumps(keys))], timeout=10)
    return json.dumps(dict(ok=True, run_id=RUN, agent=AGENT, key=key, paths=keys))


def contracts(names=None):
    """The newest contract of each name, newest first, with changed_since: its files with a HOLD, REOPEN or HANDOFF
    stamped after it (the prototype has no session rows, so edits are not counted)."""
    if isinstance(names, str):
        names = names.split(',')
    want = {relay_paths.key(n if n.strip().startswith(relay_paths.CONTRACT) else relay_paths.CONTRACT + n.strip())[0]
            for n in names or [] if str(n).strip()}
    rows = L.search(STREAM, f"SELECT * FROM {STREAM} WHERE run_id = '{L.q(RUN)}' AND kind = 'BRIEF' AND key LIKE "
                            f"'contract:%' ORDER BY _timestamp DESC", size=5000, timeout=10)
    out, seen = [], set()
    for r in rows:
        if r['key'] in seen or want and r['key'] not in want:
            continue
        seen.add(r['key'])
        paths = json.loads(r.get('paths') or '[]')
        changed = []
        for k in paths:
            holds = [h for h in hold_rows(k) if h['kind'] in ('HOLD', 'REOPEN', 'HANDOFF')]
            if any(int(h['ts']) > int(r['_timestamp']) for h in holds):
                changed.append(k)
        out.append(dict(name=r['key'][len(relay_paths.CONTRACT):], key=r['key'], agent=r['agent'], ts=int(r['_timestamp']),
                        age_s=round((now_us() - int(r['_timestamp'])) / 1e6), text=r.get('text') or '', paths=paths,
                        changed_since=sorted(changed)))
    return json.dumps(dict(contracts=out))


def task(name, text, paths, ask_about=None):
    """Register a task with its files (Seeker's task tool): a BRIEF on the task name with paths (keys) required."""
    key, kind = relay_paths.key(name)
    if kind != 'name' or not key:
        raise ValueError(f'{name!r} is not a task name')
    if isinstance(paths, str):
        paths = [p for p in paths.split(',') if p.strip()]
    keys = bundle_keys(key, paths)
    if not keys or not str(text).strip():
        raise ValueError('a task needs text and at least one file in paths')
    extra = dict(paths=json.dumps(keys))
    if ask_about:
        items = [str(a).strip().encode()[:300].decode(errors='ignore') for a in ask_about if str(a).strip()][:16]
        if items:
            extra['ask_about'] = json.dumps(items)
    write('BRIEF', str(name).strip(), key, text=text, **extra)
    return json.dumps(dict(ok=True, run_id=RUN, agent=AGENT, key=key, paths=keys))


def tasks():
    """The run's registered tasks with their state: open, held (held_by) or finished (finished_by)."""
    out = []
    for t in run_tasks():
        st = read_key(hold_rows(t['key']), now_us(), LEASE_S, t['key'])
        d = dict(name=t['resource'], key=t['key'], paths=t['paths'], text=t['text'], state='open')
        if st['finished']:
            d.update(state='finished', finished_by=st['finished']['agent'])
        elif st['live']:
            d.update(state='held', held_by=st['live'][0]['agent'])
            rd = stall_reading(st, now_us(), STALL_S, _activity(st['live'][0]['agent'], [t['key']] + t['paths']))
            d['stalled'] = rd['stalled']
        out.append(d)
    return json.dumps(dict(tasks=out))


def release(resource, finished=True):
    """Close our holds on resource: DONE on a task name when finished, else RELEASE. A bundle's other keys (the paths
    of our hold on the resource key) are handed back first. finished=true on a task name is the alias of done (no
    evidence); under SWARM_EVIDENCE_REQUIRED it is refused."""
    key, kind = relay_paths.key(resource)
    _holdable(resource, key)
    if finished and EVIDENCE_REQUIRED and relay_paths.kind_of(key)['finishable']:
        return json.dumps(dict(released=0, finished=False, resource=resource, key=key, reason='evidence_required',
                               message=EVIDENCE_REQUIRED_MESSAGE + ': call done(resource, evidence) with the command '
                                                                   'you ran; nothing was written',
                               deprecated='finished is replaced by done(resource)'))
    return json.dumps(_release(resource, finished))


def _evidence(evidence):
    """Seeker's RelayEvidence: an object {command, exit_code, summary} or free text (the summary), trimmed and cut
    (command 500 bytes, summary 300), as the compact JSON Go writes (fields in that order, no spaces)."""
    if evidence is None:
        return {}
    if isinstance(evidence, str):
        evidence = {'summary': evidence}
    if not isinstance(evidence, dict):
        raise ValueError('evidence must be an object {command, exit_code, summary} or text')
    out = {}
    if str(evidence.get('command') or '').strip():
        out['command'] = str(evidence['command']).strip().encode()[:500].decode(errors='ignore')
    if isinstance(evidence.get('exit_code'), (int, float)) and not isinstance(evidence.get('exit_code'), bool):
        out['exit_code'] = int(evidence['exit_code'])
    if str(evidence.get('summary') or '').strip():
        out['summary'] = str(evidence['summary']).strip().encode()[:300].decode(errors='ignore')
    return out


def done(resource, evidence=None):
    """Finish a task name with the evidence of its check (Seeker relay.Done): the finished release, with the evidence
    as the DONE rows' text; read_key never reads it. A file path is never done."""
    key, kind = relay_paths.key(resource)
    _holdable(resource, key)
    if not relay_paths.kind_of(key)['finishable']:
        raise ValueError(f"{'a file path' if kind == 'path' else 'a ' + kind} is never done: release it")
    ev = _evidence(evidence)
    out = dict(done=False, resource=resource, key=key)
    if EVIDENCE_REQUIRED and not ev.get('command'):
        out.update(reason='evidence_required', message=EVIDENCE_REQUIRED_MESSAGE)
        return json.dumps(out)
    rel = _release(resource, True, json.dumps(ev, separators=(',', ':')) if ev else '')
    out['done'] = rel['finished']
    for k in ('finished_after_lease', 'message', 'paths'):
        if k in rel:
            out[k] = rel[k]
    if out['done'] and ev:
        out['evidence'] = ev
    if not out['done'] and 'message' not in out:
        out['message'] = f'{resource} was not marked finished: you hold no claim on it in its current round, or it is finished'
    return json.dumps(out)


def _release_bundle(h):
    """Seeker's releaseBundle: on each key in hold h's paths where we still have an open hold of h's nonce, a RELEASE
    of every open hold of ours there (one write). The keys released."""
    out = []
    for k in _path_keys(h):
        st = read_key(hold_rows(k), now_us(), LEASE_S, k)
        if not any(r['nonce'] == h['nonce'] and r['agent'] == AGENT for r in st['open']):
            continue
        write_rows([row('RELEASE', k, k, nonce=o['nonce']) for o in st['open'] if o['agent'] == AGENT])
        out.append(k)
    return out


def _release(resource, finished, done_text=''):
    key, kind = relay_paths.key(resource)
    st = read_key(hold_rows(key), now_us(), LEASE_S, key)
    is_path = not relay_paths.kind_of(key)['finishable']
    row_kind = 'DONE' if finished and not is_path else 'RELEASE'
    mine = [r for r in st['live'] if r['agent'] == AGENT]
    others = []
    for r in mine:
        others += _release_bundle(r)
        write(row_kind, resource, key, text=done_text if row_kind == 'DONE' else '', nonce=r['nonce'])
    out = dict(released=len(mine), finished=row_kind == 'DONE' and bool(mine), resource=resource, key=key)
    if finished and is_path and mine:
        what = 'file path' if kind == 'path' else kind
        out['message'] = (f'{resource} is a {what}, so it was handed back for later work, not marked finished; only '
                          f'task names are finished')
    if not mine and row_kind == 'DONE' and st['finished'] is None:
        # Finishing after the lease: our newest open hold of the round, when nobody else holds the key live.
        own = [r for r in st['open'] if r['agent'] == AGENT]
        other = next((r for r in st['live'] if r['agent'] != AGENT), None)
        if own and other:
            out['message'] = (f"{resource} was not marked finished: your lease ran out and {other['agent']} holds it "
                              f"now; tell them your work is done")
        elif own:
            newest = max(own, key=lambda r: int(r['ts']))
            others += _release_bundle(newest)
            write('DONE', resource, key, text=done_text, nonce=newest['nonce'])
            out.update(released=1, finished=True, finished_after_lease=True,
                       message=f'{resource} is marked finished; your lease had run out, but nobody else claimed it in '
                               f'the meantime')
    if others:
        out['paths'] = sorted(set(others))
    return out


S = {'type': 'string'}
B = {'type': 'boolean'}
A = {'type': 'array', 'items': S}
ALL = dict(
    peers=dict(fn=peers, required=[], properties={},
               description='What the other agents in this run have posted, claimed and released, newest first. '
                           'Call it before choosing a task.'),
    post=dict(fn=post, required=['text'], properties=dict(text=S),
              description='Post a short note to the shared board: what you are taking, or what you finished.'),
    claim=dict(fn=claim, required=['resource'],
               properties=dict(resource=S, reopen=B, paths=A, wait_s={'type': 'number'}, takeover=B),
               description='Take an exclusive claim on a resource: a task name such as "slugify" or a file path such '
                           'as "src/slugify.py". Returns held=true if it is yours; held=false with held_by if someone '
                           'else has it, or with finished_by if the task name is already finished, in which case pick '
                           'another. reopen=true claims a finished task name anyway and starts a new round of work on '
                           'it; use it only when you were asked to redo that task. paths: files (or slot:<name>) taken '
                           'with the resource in one step; a registered task takes its registered files. wait_s (at '
                           'most 55): wait in line for the holder to let go instead of being refused at once; it ends '
                           'held, finished_by, or reason timed_out with held_by and position. reason belongs_to_task: '
                           'the file is held through its task; claim the task instead. takeover=true: take over a '
                           'hold whose holder has stopped (stalled in tasks); refused not_stalled while it works.'),
    handoff=dict(fn=handoff, required=['resource', 'to'], properties=dict(resource=S, to=S),
                 description='Hand a resource you hold to another agent of the run in one step: from then on it '
                             'holds it and you do not, and no third agent can take it in between.'),
    ask=dict(fn=ask, required=['text'], properties=dict(text=S, to=S),
             description='Ask a question you are blocked on and only another agent or the orchestrator can decide. '
                         'Answers {id}; it stays open until someone calls answer with that id.'),
    answer=dict(fn=answer, required=['id', 'text'], properties=dict(id=S, text=S),
                description='Answer an open question of this run by its id. The first answer closes it.'),
    contract=dict(fn=contract, required=['name', 'text', 'paths'], properties=dict(name=S, text=S, paths=A),
                  description='When you finish an interface others will build on, write its contract: its name, the '
                              'shape in a few lines, and the files that define it.'),
    contracts=dict(fn=contracts, required=[], properties=dict(names=A),
                   description='The newest contract of each name, with changed_since: its files claimed after it.'),
    task=dict(fn=task, required=['name', 'text', 'paths'], properties=dict(name=S, text=S, paths=A, ask_about=A),
              description='Register a task of this run with the files it owns (for an orchestrator). A claim of the '
                          'task takes its files with it; a bare claim of one of its files is refused (belongs_to_task).'),
    tasks=dict(fn=tasks, required=[], properties={},
               description="The run's registered tasks with their files and state (open, held_by, finished_by)."),
    release=dict(fn=release, required=['resource'], properties=dict(resource=S, finished=B),
                 description='Release a claim you hold. On a task name, finished=true (the default) marks the task '
                             'done so nobody claims it again; finished=false hands it back. A file path is always '
                             'handed back, never finished: files are edited again in later rounds of work. The files '
                             'claimed with a task are handed back with it. To finish a task, call done instead.'),
    done=dict(fn=done, required=['resource'],
              properties=dict(resource=S, evidence={'type': 'object', 'properties': dict(
                  command=S, exit_code={'type': 'integer'}, summary=S)}),
              description='Finish a task you hold, when its test passes: evidence {command, exit_code, summary} is the '
                          'test or check command you ran, its exit code and one line of its output. Later claims of '
                          'the task are refused unless they pass reopen=true. done=false with reason '
                          'evidence_required: call done again with the command you ran. A file path is never done: '
                          'release it.'),
)
ALL.update(
    brief=dict(fn=brief, required=['resource', 'text'], properties=dict(resource=S, text=S),
               description='Leave a brief about a file or document you read, keyed by that resource, so the other '
                           'agents of the run can read the brief instead of the whole file: what it is for, what it '
                           'exports or decides, what not to touch (cut at 2,000 characters). Not a claim; it reserves '
                           'nothing. The newest brief of a resource is the one briefs returns.'),
    briefs=dict(fn=briefs, required=[], properties=dict(resources=S, limit={'type': 'number'}),
                description='The newest brief of each resource in this run, newest first. resources: comma-separated '
                            'file paths or names to look up (all when left out); limit: 40 by default, at most 200. '
                            'Call it before reading a file another agent may already have read.'),
)
TOOLS = {k: v for k, v in ALL.items() if k in EXPOSE}


def handle(msg):
    method, mid = msg.get('method'), msg.get('id')
    if method == 'initialize':
        res = dict(protocolVersion=(msg.get('params') or {}).get('protocolVersion') or '2025-06-18',
                   capabilities={'tools': {}}, serverInfo={'name': 'swarm', 'version': '0.2.0'},
                   instructions=f'You are {AGENT} in swarm run {RUN}. The board is shared with the other agents.')
    elif method == 'tools/list':
        res = {'tools': [dict(name=n, description=t['description'],
                              inputSchema=dict(type='object', properties=t['properties'], required=t['required']))
                         for n, t in TOOLS.items()]}
    elif method == 'tools/call':
        p = msg.get('params') or {}
        t = TOOLS.get(p.get('name'))
        try:
            text, err = (t['fn'](**{k: v for k, v in (p.get('arguments') or {}).items() if k in t['properties']}),
                         False) if t else (json.dumps({'error': 'unknown tool'}), True)
        except Exception as e:
            text, err = json.dumps({'error': f'{type(e).__name__}: {str(e)[:300]}'}), True
        res = {'content': [{'type': 'text', 'text': text}], 'isError': err}
    elif method == 'ping':
        res = {}
    elif mid is None:
        return None
    else:
        return {'jsonrpc': '2.0', 'id': mid, 'error': {'code': -32601, 'message': f'method not found: {method}'}}
    return None if mid is None else {'jsonrpc': '2.0', 'id': mid, 'result': res}


def main():
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            msg = json.loads(line)
        except Exception:
            out = {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32700, 'message': 'parse error'}}
        else:
            out = handle(msg) if isinstance(msg, dict) else None
        if out is not None:
            sys.stdout.write(json.dumps(out) + '\n')
            sys.stdout.flush()


if __name__ == '__main__':
    main()
