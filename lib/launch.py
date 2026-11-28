
import fcntl, json, os, re
import common

LAUNCH_RE = re.compile(r'[0-9a-f]{16}')
TASK_MAX = 1024
WORKER, DESCENDANT = 'worker', 'descendant'


def current(env=None):

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
    given = env.get('CTRLB_WATCH_DIR') or ''
    return os.path.abspath(given if given else os.path.expanduser('~/.ctrlb/watch'))


def _sub(name):

    top = watch_dir()
    d = os.path.join(top, name)
    if os.path.isdir(d):
        return d
    made = not os.path.isdir(top)
    os.makedirs(top, mode=0o700, exist_ok=True)
    if made:
        _sync_dir(os.path.dirname(top))
    os.makedirs(d, mode=0o700, exist_ok=True)
    _sync_dir(top)
    return d


def _sync_dir(path):

    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def marker_path(info):
    return os.path.join(_sub('used'), info['launch'])


def _owner(path):
    try:
        with open(path) as fh:
            return fh.read(512).strip()
    except OSError:
        return ''


def role(info, sid, subagent=False):

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

    if not info or not sid:
        return False
    try:
        return _owner(marker_path(info)) == sid
    except OSError:
        return False


def claim_outcome(info, args, answer, own_run, own_agent):

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

    if claim not in ('held', 'refused'):
        return None
    path = os.path.join(_sub('result'), info['launch'] + '.json')
    with open(marker_path(info)) as mark:
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
            _sync_dir(os.path.dirname(path))
            common.log(f"launch {info['launch']}: claim of {info['task']!r} {claim}")
            return claim
        finally:
            fcntl.flock(mark, fcntl.LOCK_UN)


def record_claim(info, sid, args, answer, own_run, own_agent):

    claim = claim_outcome(info, args, answer, own_run, own_agent)
    if not claim or not is_worker(info, sid):
        return None
    return write_result(info, claim)
