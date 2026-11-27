
import base64, fcntl, hashlib, ipaddress, json, os, re, shutil, socket, subprocess, time, urllib.error, urllib.parse, urllib.request
import model_call_guard
from redact import redact

VERSION = '0.3.0'
HOME = os.environ.get('CTRLB_HOME') or os.path.expanduser('~/.ctrlb')
NAME_RE = re.compile(r'^[A-Za-z0-9_]{1,64}$')
DEFAULT_DATASET = 'agent_sessions'
USER_CLAIM = 'email'


def key_user(key):

    try:
        parts = (key or '').strip().split('.')
        if len(parts) != 3:
            return ''
        seg = parts[1]
        payload = json.loads(base64.urlsafe_b64decode(seg + '=' * (-len(seg) % 4)))
        user = payload.get(USER_CLAIM) if isinstance(payload, dict) else None
        return user.strip() if isinstance(user, str) else ''
    except Exception:
        return ''


def _file_config():
    try:
        with open(os.path.join(HOME, 'config.json')) as fh:
            f = json.load(fh)
        return f if isinstance(f, dict) else {}
    except Exception:
        return {}


def config():
    f = _file_config()
    env = os.environ.get

    def pick(var, key, default=''):
        return (env(var) or str(f.get(key) or '') or default).strip()

    c = dict(url=pick('CTRLB_URL', 'url').rstrip('/'),
             key=pick('CTRLB_KEY', 'key'),
             run=pick('CTRLB_RUN', 'run'),
             agent=(env('CTRLB_AGENT') or env('CTRLB_USER') or '').strip(),
             dataset=pick('CTRLB_DATASET', 'dataset', DEFAULT_DATASET),
             enforce=pick('CTRLB_ENFORCE', 'enforce').lower())
    mode = env('CTRLB_MODE') or f.get('mode')
    c['mode'] = 'local' if mode == 'local' else 'hosted'


    c['user'] = key_user(c['key'])
    c['mcp_url'] = pick('CTRLB_MCP_URL', 'mcp_url', c['url'] + '/mcp' if c['url'] else '')
    c['scratch_dir'] = pick('CTRLB_SCRATCH_DIR', 'scratchDir')
    c['auto_slots'] = _auto_slots(env('CTRLB_AUTO_SLOTS'), f.get('autoSlots'))
    c['guard_model_calls'] = model_call_guard.switch(env('CTRLB_GUARD_MODEL_CALLS'), f.get('guardModelCalls'))
    join = env('CTRLB_JOIN_SPAWNED')
    join = join if join not in (None, '') else f.get('joinSpawned')
    c['join_spawned'] = True if join in (None, '') else model_call_guard.switch(join, None)
    c['spawn_idle_minutes'] = _minutes(env('CTRLB_SPAWN_IDLE_MINUTES'), f.get('spawnIdleMinutes'))
    c['off'] = env('CTRLB_OFF') == '1' or not c['url'] or not c['key'] or not NAME_RE.match(c['dataset'])
    return c


def _minutes(var, file_value):

    v = var if var not in (None, '') else file_value
    try:
        m = float(v)
    except (TypeError, ValueError):
        return 0
    return m if m > 0 and m == m and m != float('inf') else 0


def local_mode(cfg):

    return cfg.get('mode') == 'local'


def relay_off(cfg):

    run = (os.environ.get('CTRLB_RUN') or '').strip()
    return local_mode(cfg) and (not run or re.fullmatch(r'\$\{[^}]*\}', run) is not None)


def ctrlb_binary():

    path = os.pathsep.join(p for p in os.environ.get('PATH', os.defpath).split(os.pathsep) if os.path.isabs(p))
    for candidate in (os.environ.get('CTRLB_BIN'), os.path.join(HOME, 'bin', 'ctrlb'), 'ctrlb'):
        if candidate:
            found = shutil.which(os.path.expanduser(candidate), path=path)
            if found:
                return os.path.abspath(found)
    return None


def loopback_url(cfg):
    try:
        url = urllib.parse.urlsplit(cfg.get('url') or '')
        return url.scheme in ('http', 'https') and url.hostname in ('127.0.0.1', 'localhost')
    except ValueError:
        return False


def _loopback_host(host):
    if host == 'localhost':
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def validate_url(url):

    try:
        parsed = urllib.parse.urlsplit(url)
        valid = parsed.scheme in ('http', 'https') and bool(parsed.hostname)
    except ValueError:
        valid = False
    if not valid:
        raise ValueError('the URL must start with http:// or https:// and include a host')
    if parsed.scheme == 'http' and not _loopback_host(parsed.hostname) and os.environ.get('CTRLB_ALLOW_HTTP') != '1':
        raise ValueError('HTTPS is required for non-loopback URLs; set CTRLB_ALLOW_HTTP=1 to allow remote plain HTTP')


RECALL_COMMON = ("Earlier work is in the CtrlB record, not in your context: this session's turns before compaction, and this "
                 "user's earlier sessions. Before your first grep, file read or shell command for a question about earlier "
                 "work (what was done, decided, run, read, or why something failed), call `search_sessions` without session_id "
                 "to search this user's sessions. Pass session_id{session} only to search this session's own turns before compaction. "
                 "After a command fails, call `find_command` with the command before changing it, then `next_actions` with a "
                 "session_id and seq it returns to see what followed. Do not say something was not said or not done until you "
                 "have searched.")


class _NoAPIRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None




def local_health(cfg, timeout=0.25):

    req = urllib.request.Request(cfg['url'] + '/healthz', headers={'Authorization': 'Bearer ' + cfg['key']})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoAPIRedirect())
    try:
        with opener.open(req, timeout=timeout) as resp:
            status = resp.status
    except urllib.error.HTTPError as resp:
        with resp:
            status = resp.code
    if not 200 <= status < 300:
        log(f'local server: health HTTP {status}')
    return status


def start_local_server(cfg):

    if not local_mode(cfg) or not loopback_url(cfg):
        return
    try:
        private_dir(HOME)
    except OSError:
        log('local server: could not prepare CTRLB_HOME')
        return
    try:
        with private_open(os.path.join(HOME, 'serve.lo' + 'ck'), 'a') as mutex:
            fcntl.flock(mutex, fcntl.LOCK_EX)
            try:
                local_health(cfg)
                return
            except Exception:
                pass
            binary = ctrlb_binary()
            if not binary:
                log('local server: ctrlb executable not found')
                return
            subprocess.Popen([binary, 'serve'], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True, cwd=HOME)

            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                try:
                    local_health(cfg, timeout=min(0.25, max(0.001, deadline - time.monotonic())))
                    return
                except Exception:
                    time.sleep(min(0.05, max(0, deadline - time.monotonic())))
            log('local server: health check did not answer in time')
    except Exception:
        log('local server: could not start ctrlb serve')


DEFAULT_AUTO_SLOTS = ('REGISTRY.md',)


def _auto_slots(var, file_value):

    if var is not None:
        items = var.split(',')
    elif isinstance(file_value, list):
        items = file_value
    elif isinstance(file_value, str):
        items = file_value.split(',')
    else:
        items = list(DEFAULT_AUTO_SLOTS)
    out = []
    for i in items:
        i = str(i).strip().replace('\\', '/')
        while i.startswith('./'):
            i = i[2:]
        if i and not i.startswith('/') and '..' not in i.split('/') and i not in out:
            out.append(i)
    return out


def safe_segment(s):

    s = re.sub(r'[^A-Za-z0-9._-]+', '_', str(s or ''))[:120].strip('.')
    return s or '_'


def scratch_dir(cfg, run):

    d = cfg.get('scratch_dir') or ''
    if d:
        return os.path.abspath(os.path.expanduser(d.replace('{run}', safe_segment(run))))
    return os.path.join(HOME, 'relay', safe_segment(run), 'scratch')


def run_id(cfg, session_id, parent=''):

    return cfg.get('run') or parent or session_id or ''


def agent_name(cfg, session_id, subagent=''):

    label = cfg.get('agent') or ''
    if subagent:
        sub = subagent if subagent.startswith('agent-') else 'agent-' + subagent
        return f'{label}:{sub}' if label else sub
    return label or session_id or ''


def _tighten(path, mode):

    try:
        st = os.stat(path)
        if st.st_mode & 0o077:
            os.chmod(path, st.st_mode & mode)
    except OSError:
        pass


def private_dir(d):

    os.makedirs(HOME, mode=0o700, exist_ok=True)
    _tighten(HOME, 0o700)
    if os.path.abspath(d) != os.path.abspath(HOME):
        os.makedirs(d, mode=0o700, exist_ok=True)
        _tighten(d, 0o700)
    return d


def private_open(path, mode='w'):

    flags = {'w': os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 'a': os.O_WRONLY | os.O_CREAT | os.O_APPEND}[mode]
    fd = os.open(path, flags, 0o600)
    try:
        if os.fstat(fd).st_mode & 0o077:
            os.fchmod(fd, 0o600)
    except OSError:
        pass
    return os.fdopen(fd, mode)


def log(msg):
    try:
        private_dir(HOME)
        with private_open(os.path.join(HOME, 'hooks.log'), 'a') as fh:
            fh.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {os.getpid()} {msg}\n")
    except Exception:
        pass


class SeekerError(RuntimeError):
    def __init__(self, status, text):
        super().__init__(f'HTTP {status}: {text}')
        self.status = status


def request(cfg, method, path, body=None, query=None, timeout=10, expected_status=None):

    url = cfg['url'] + path
    validate_url(url)
    if query:
        url += '?' + urllib.parse.urlencode({k: v for k, v in query.items() if v is not None and v != ''})
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        'Content-Type': 'application/json', 'Accept': 'application/json',
        'Authorization': 'Bearer ' + cfg['key'], 'User-Agent': 'ctrlb-agent/' + VERSION})
    try:
        handlers = [_NoAPIRedirect()]
        if local_mode(cfg) and _loopback_host(urllib.parse.urlsplit(url).hostname):
            handlers.append(urllib.request.ProxyHandler({}))
        open_request = urllib.request.build_opener(*handlers).open
        with open_request(req, timeout=timeout) as resp:
            if expected_status is not None and resp.status != expected_status:
                raise SeekerError(resp.status, f'expected HTTP {expected_status}; upload not acknowledged')
            raw = resp.read()
    except urllib.error.HTTPError as e:
        if 300 <= e.code < 400:
            e.close()
            raise SeekerError(e.code, 'API redirects are not allowed; configure the final server URL') from None
        raise SeekerError(e.code, e.read()[:300].decode(errors='replace')) from None
    try:
        return json.loads(raw or b'{}')
    except ValueError:
        return {'raw': raw[:300].decode(errors='replace')}


def _seg(s):
    return urllib.parse.quote(str(s), safe='')


def ingest_records(cfg, rows, timeout=20):

    answer = request(cfg, 'POST', f"/ai/sessions/{_seg(cfg['dataset'])}/records", rows, timeout=timeout,
                     expected_status=202)
    if not isinstance(answer, dict) or type(answer.get('accepted')) is not int or answer['accepted'] != len(rows):
        raise SeekerError(202, 'upload acknowledgment must accept every row; progress kept for retry')
    return answer


def check(cfg, run, agent, paths, cwd='', timeout=2, tokens=None):

    body = dict(agent=agent, paths=list(paths))
    if cwd:
        body['cwd'] = cwd
    if tokens:
        body['tokens'] = dict(tokens)
    return request(cfg, 'POST', f"/ai/relay/{_seg(cfg['dataset'])}/runs/{_seg(run)}/check", body, timeout=timeout)


def runs(cfg, status='all', limit=5, timeout=15):

    return request(cfg, 'GET', f"/ai/relay/{_seg(cfg['dataset'])}/runs", query=dict(status=status, limit=limit),
                   timeout=timeout)




class TokenState:


    def __init__(self, run, agent):
        d = private_dir(os.path.join(HOME, 'state'))
        name = hashlib.sha1(f'{run}\0{agent}'.encode()).hexdigest()
        self.file = os.path.join(d, f'tokens-{name}.json')

    def _locked(self, fn):
        with private_open(self.file + '.mutex') as mx:
            fcntl.flock(mx, fcntl.LOCK_EX)
            try:
                try:
                    with open(self.file) as fh:
                        data = json.load(fh)
                except Exception:
                    data = {}
                if not isinstance(data, dict):
                    data = {}
                out, changed = fn(data)
                if changed:
                    tmp = self.file + '.tmp'
                    with private_open(tmp) as fh:
                        json.dump(data, fh)
                    os.replace(tmp, self.file)
                return out
            finally:
                fcntl.flock(mx, fcntl.LOCK_UN)

    def put(self, keys, token):
        def fn(d):
            for k in keys:
                d[k] = token
            if len(d) > 2000:
                for k in list(d)[:-1000]:
                    del d[k]
            return None, True
        return self._locked(fn)

    def drop(self, keys):
        return self._locked(lambda d: (None, bool([d.pop(k) for k in keys if k in d])))

    def get(self, keys):
        return self._locked(lambda d: ({k: d[k] for k in keys if isinstance(d.get(k), str)}, False))


class LastTest(TokenState):


    def __init__(self, run, agent):
        super().__init__(run, agent)
        self.file = self.file.replace(os.sep + 'tokens-', os.sep + 'lasttest-')

    def put(self, rec):
        def fn(d):
            d.clear()
            d.update(rec)
            return None, True
        return self._locked(fn)

    def get(self):
        return self._locked(lambda d: (dict(d) if d.get('command') else None, False))


class SessionRecord(TokenState):

    BASH_MAX = 200
    EDITED_MAX = 200

    def __init__(self, run, agent):
        super().__init__(run, agent)
        self.file = self.file.replace(os.sep + 'tokens-', os.sep + 'session-')

    def update(self, sid, fn):

        def go(d):
            if d.get('sid') != sid:
                d.clear()
                d.update(sid=sid, bash=[], edited=[], note=None, relay=False)
            fn(d)
            d['bash'] = d.get('bash', [])[-self.BASH_MAX:]
            d['edited'] = d.get('edited', [])[-self.EDITED_MAX:]
            return None, True
        return self._locked(go)

    def get(self, sid):
        return self._locked(lambda d: (dict(d) if d.get('sid') == sid else
                                       dict(sid=sid, bash=[], edited=[], note=None, relay=False), False))


def handoff_all(cfg, run, agent, text, cwd='', timeout=0.8):

    body = dict(agent=agent, all=True, to='', text=redact(text)[0])
    if cwd:
        body['cwd'] = cwd
    return request(cfg, 'POST', f"/ai/relay/{_seg(cfg['dataset'])}/runs/{_seg(run)}/handoffs", body, timeout=timeout)




def _run_path(cfg, run, tail):
    return f"/ai/relay/{_seg(cfg['dataset'])}/runs/{_seg(run)}/{tail}"


def post_brief(cfg, run, agent, resource, text, paths=None, cwd='', test='', timeout=2):

    body = dict(agent=agent, resource=resource, text=text)
    if paths:
        body['paths'] = list(paths)
    if cwd:
        body['cwd'] = cwd
    if test:
        body['test'] = test
    return request(cfg, 'POST', _run_path(cfg, run, 'briefs'), body, timeout=timeout)


def claim(cfg, run, agent, resource, paths=None, cwd='', timeout=2):

    body = dict(agent=agent, resource=resource)
    if paths:
        body['paths'] = list(paths)
    if cwd:
        body['cwd'] = cwd
    return request(cfg, 'POST', _run_path(cfg, run, 'claims'), body, timeout=timeout)


def release(cfg, run, agent, resource, cwd='', timeout=2):

    body = dict(agent=agent, resource=resource)
    if cwd:
        body['cwd'] = cwd
    return request(cfg, 'DELETE', _run_path(cfg, run, 'claims'), body, timeout=timeout)


def post_note(cfg, run, agent, text, key='', hash_='', timeout=10):

    body = dict(agent=agent, text=text)
    if key:
        body['key'] = key
    if hash_:
        body['hash'] = hash_
    return request(cfg, 'POST', _run_path(cfg, run, 'posts'), body, timeout=timeout)


def get_run_part(cfg, run, tail, query=None, timeout=2):

    return request(cfg, 'GET', _run_path(cfg, run, tail), query=query, timeout=timeout)


class RunState(TokenState):


    def __init__(self, run, agent, name):
        super().__init__(run, agent)
        self.file = self.file.replace(os.sep + 'tokens-', os.sep + name + '-')

    def read(self):
        return self._locked(lambda d: (dict(d), False))


    def change(self, fn):

        return self._locked(lambda d: (fn(d), True))


class FileState:


    def __init__(self, path, deadline):
        d = private_dir(os.path.join(HOME, 'state'))
        key = hashlib.sha1(os.path.abspath(path).encode()).hexdigest()
        self.file = os.path.join(d, key + '.json')
        self.mutex_path = os.path.join(d, key + '.mutex')
        self.deadline = time.monotonic() + max(0, deadline - time.time())
        self.data = dict(path=os.path.abspath(path), offset=0, line=0, row=0, prev_ts=0, names={}, mids=[], cost=0.0)

    def __enter__(self):
        self.fh = private_open(self.mutex_path)
        while True:
            try:
                fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= self.deadline:
                    self.fh.close()
                    raise TimeoutError('state file busy')
                time.sleep(0.05)
        try:
            with open(self.file) as fh:
                self.data.update(json.load(fh))
        except Exception:
            pass
        return self

    def reset(self):
        self.data.update(offset=0, line=0, row=0, prev_ts=0, names={}, mids=[], cost=0.0)

    def save(self):
        names = self.data['names']
        if len(names) > 5000:
            self.data['names'] = dict(list(names.items())[-2000:])
        self.data['mids'] = self.data.get('mids', [])[-200:]
        tmp = self.file + '.tmp'
        with private_open(tmp) as fh:
            json.dump(self.data, fh)
        os.replace(tmp, self.file)

    def __exit__(self, *exc):
        fcntl.flock(self.fh, fcntl.LOCK_UN)
        self.fh.close()
        return False
