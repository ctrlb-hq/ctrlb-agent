"""A session started for one named Relay task (by `ctrlb watch`). Standard library only.

The starter sets, in the agent's environment:
  CTRLB_RUN, CTRLB_AGENT  the run and the worker's agent name
  CTRLB_TASK              the task's name (at most 1,024 bytes)
  CTRLB_TASK_MODE         claim, or takeover for a stalled task
  CTRLB_LAUNCH            the launch id, 16 hex digits
  CTRLB_WATCH_DIR         optional; the starter's folder (default ~/.ctrlb/watch)

The first session that starts with a given launch id is its worker: it creates <watch dir>/used/<launch> (create
exclusive) with its session id inside, and the same session id keeps the role at every later hook (compaction, resume,
a repeated start). A subagent never takes it. Every other session with the same launch id (a subagent, an agent the
worker starts from its shell) is a descendant. Without CTRLB_LAUNCH nothing here applies and nothing is written.

The worker's claim of its task, as the claim answered it, goes to <watch dir>/result/<launch>.json:
{"claim": "held"} or {"claim": "refused"}. Only the worker's own claim of the named task in the launch's run counts;
another claim, a failed call and an answer that does not parse write nothing. held is never replaced by refused. The
file is written under a temporary name and renamed."""
import fcntl, json, os, re
import common

LAUNCH_RE = re.compile(r'[0-9a-f]{16}')
TASK_MAX = 1024
WORKER, DESCENDANT = 'worker', 'descendant'


def current(env=None):
    """The launch this session was started for: {launch, task, mode, run}, or None (no CTRLB_LAUNCH, or a launch
    without a valid id, a task name or a run: the session then works as any other)."""
    env = os.environ if env is None else env
    launch = (env.get('CTRLB_LAUNCH') or '').strip()
    if not launch:
        return None
    task = (env.get('CTRLB_TASK') or '').strip()
    run = (env.get('CTRLB_RUN') or '').strip()
    if not LAUNCH_RE.fullmatch(launch) or not task or len(task.encode()) > TASK_MAX or not run:
        common.log(f"launch: CTRLB_LAUNCH ignored (an id of 16 hex digits, CTRLB_TASK of 1 to {TASK_MAX} bytes and "
                   f"CTRLB_RUN are needed)")
        return None
    mode = 'takeover' if (env.get('CTRLB_TASK_MODE') or '').strip().lower() == 'takeover' else 'claim'
    return dict(launch=launch, task=task, mode=mode, run=run)


def watch_dir(env=None):
    env = os.environ if env is None else env
    return os.path.abspath(os.path.expanduser((env.get('CTRLB_WATCH_DIR') or '').strip() or '~/.ctrlb/watch'))


def _sub(name):
    """<watch dir>/<name>, made (mode 0700, the watch dir too) when missing."""
    top = watch_dir()
    os.makedirs(top, mode=0o700, exist_ok=True)
    d = os.path.join(top, name)
    os.makedirs(d, mode=0o700, exist_ok=True)
    return d


def marker_path(info):
    return os.path.join(_sub('used'), info['launch'])


def _owner(path):
    try:
        with open(path) as fh:
            return fh.read(512).strip()
    except OSError:
        return ''


def role(info, sid, subagent=False):
    """WORKER or DESCENDANT. A subagent is a descendant before anything is read or written. A session takes the
    marker when nobody has it (a hard link of a file that already holds its session id, so the marker is never seen
    empty); a session whose id is in the marker is the worker; every other session is a descendant."""
    if subagent or not sid:
        return DESCENDANT
    try:
        path = marker_path(info)
        if os.path.exists(path):
            return WORKER if _owner(path) == sid else DESCENDANT
        tmp = f'{path}.{os.getpid()}.tmp'
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, sid.encode())
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            os.link(tmp, path)
            common.log(f"launch {info['launch']}: session {sid} is the worker for task {info['task']!r}")
        except FileExistsError:
            pass
        finally:
            os.unlink(tmp)
        return WORKER if _owner(path) == sid else DESCENDANT
    except OSError as e:
        common.log(f"launch {info['launch']}: marker: {type(e).__name__}: {str(e)[:200]}")
        return DESCENDANT


def is_worker(info, sid):
    """Whether sid holds the launch's marker. Reads only: a later hook never takes the marker."""
    if not info or not sid:
        return False
    try:
        return _owner(marker_path(info)) == sid
    except OSError:
        return False


def claim_outcome(info, args, answer, own_run, own_agent):
    """'held' or 'refused' for a claim of the launch's task in the launch's run by the worker's agent, as its answer
    says; None for any other claim (another resource or run, a claim with next) and for an answer without a held
    true or false."""
    if not info or not isinstance(args, dict) or not isinstance(answer, dict):
        return None
    resource = args.get('resource')
    if not isinstance(resource, str) or resource.strip() != info['task'] or args.get('next'):
        return None
    if str(args.get('run_id') or own_run or '') != info['run']:
        return None
    if own_agent and str(args.get('agent') or own_agent) != own_agent:
        return None
    held = answer.get('held')
    if held is True:
        return 'held'
    if held is False:
        return 'refused'
    return None


def write_result(info, claim):
    """Write {"claim": claim} to result/<launch>.json under a temporary name, then rename. A held result is never
    replaced by refused. Returns what the file says afterwards."""
    if claim not in ('held', 'refused'):
        return None
    path = os.path.join(_sub('result'), info['launch'] + '.json')
    with open(marker_path(info)) as mark:       # the worker's marker serialises the worker's own hooks
        fcntl.flock(mark, fcntl.LOCK_EX)
        try:
            try:
                with open(path) as fh:
                    had = json.load(fh).get('claim')
            except Exception:
                had = None
            if had == claim or (had == 'held' and claim == 'refused'):
                return had
            tmp = f'{path}.{os.getpid()}.tmp'
            with common.private_open(tmp) as fh:
                json.dump({'claim': claim}, fh, separators=(',', ':'))
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
            common.log(f"launch {info['launch']}: claim of {info['task']!r} {claim}")
            return claim
        finally:
            fcntl.flock(mark, fcntl.LOCK_UN)


def record_claim(info, sid, args, answer, own_run, own_agent):
    """The worker's claim of its task as a result file; nothing for any other session or claim. Returns the claim
    written ('held' or 'refused'), or None."""
    claim = claim_outcome(info, args, answer, own_run, own_agent)
    if not claim or not is_worker(info, sid):
        return None
    return write_result(info, claim)
