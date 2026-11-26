"""Config, the CtrlB HTTP client, per-file upload state and logging for ctrlb-agent. Standard library only.

Config comes from environment variables, falling back to ~/.ctrlb/config.json (written by bin/configure.py, mode 0600):

  CTRLB_URL      CtrlB's URL                                   file key "url"
  CTRLB_ALLOW_HTTP=1  allow plain HTTP to a non-loopback server (HTTPS is the default requirement)
  CTRLB_KEY      the user's MCP key, sent as Authorization: Bearer   file key "key"
  (no variable)  the key's user: the "email" claim of the key, as CtrlB reads it; the only source of user_name
                                                                 file key "user" (written by bin/configure.py)
  CTRLB_RUN      run id shared by every agent of one run         file key "run"
  CTRLB_AGENT    optional agent label (default: the session id)  (environment only)
  CTRLB_USER     read for compatibility as the agent label when CTRLB_AGENT is unset; never sent as user_name
  CTRLB_DATASET  sessions dataset (default agent_sessions)        file key "dataset"
  CTRLB_ENFORCE  "off" skips the edit check without calling CtrlB; anything else lets CtrlB's mode decide
                                                                 file key "enforce"
  CTRLB_MCP_URL  CtrlB's /mcp URL, read by plugin/.mcp.json     file key "mcp_url" (shown by status only)
  CTRLB_MODE     "local" runs Relay hooks only with CTRLB_RUN   file key "mode"
  CTRLB_BIN      optional path to the local ctrlb executable
  CTRLB_SCRATCH_DIR  the run's durable scratch directory; {run} is replaced by the run id
                 (default <CTRLB_HOME>/relay/<run id>/scratch)          file key "scratchDir"
  CTRLB_AUTO_SLOTS   files the hooks claim as slot:<path> around each edit (relative paths, comma-separated;
                 empty turns it off; default REGISTRY.md)               file key "autoSlots" (a list)
  CTRLB_JOIN_SPAWNED  0 (or false, off, no) stops an agent CLI started from another agent's shell from joining
                 that agent's Relay run (lib/spawn.py; default on)          file key "joinSpawned"
  CTRLB_SPAWN_IDLE_MINUTES  stop an agent CLI started from another agent's shell when its transcript has not changed
                 for this many minutes (default 0, off)                     file key "spawnIdleMinutes"
  CTRLB_GUARD_MODEL_CALLS  1 (or true, on, yes) refuses a shell command that starts codex exec, codex review or
                 claude -p without a time limit (timeout) in front (default off)  file key "guardModelCalls"
  CTRLB_OFF=1    turns every hook off
  CTRLB_HOME     where config.json, hooks.log and upload state live (default ~/.ctrlb)

Everything is off when the URL or the key is missing or the dataset name is not letters, digits and _."""
import base64, fcntl, hashlib, ipaddress, json, os, re, shutil, socket, subprocess, time, urllib.error, urllib.parse, urllib.request
import model_call_guard
from redact import redact

VERSION = '0.3.0'
HOME = os.environ.get('CTRLB_HOME') or os.path.expanduser('~/.ctrlb')
NAME_RE = re.compile(r'^[A-Za-z0-9_]{1,64}$')
DEFAULT_DATASET = 'agent_sessions'
USER_CLAIM = 'email'   # the claim CtrlB takes the caller's user_name from


def key_user(key):
    """The user an MCP key belongs to: the USER_CLAIM of its JWT payload (base64url, no signature check; CtrlB
    checks the signature). '' when the key is not a JWT or carries no such claim, so rows then omit user_name and
    CtrlB fills it from the key."""
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
    # user_name only ever comes from the key in effect (CTRLB_KEY or the file's key), decoded now, so a key from the
    # environment and an old config.json "user" written as a label can never put another name on the rows.
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
    """A number of minutes from the variable when set and not empty, else the file; 0 when missing, not a number or
    not above zero."""
    v = var if var not in (None, '') else file_value
    try:
        m = float(v)
    except (TypeError, ValueError):
        return 0
    return m if m > 0 and m == m and m != float('inf') else 0


def local_mode(cfg):
    """Whether the session uses the local server."""
    return cfg.get('mode') == 'local'


def relay_off(cfg):
    """Local Relay needs a run in the environment, matching the MCP connection's run header."""
    run = (os.environ.get('CTRLB_RUN') or '').strip()
    return local_mode(cfg) and (not run or re.fullmatch(r'\$\{[^}]*\}', run) is not None)


def ctrlb_binary():
    """First executable in CTRLB_BIN, <CTRLB_HOME>/bin/ctrlb, then absolute PATH entries."""
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
    """API traffic requires HTTPS, loopback HTTP, or an explicit environment opt-in for remote HTTP."""
    try:
        parsed = urllib.parse.urlsplit(url)
        valid = parsed.scheme in ('http', 'https') and bool(parsed.hostname)
    except ValueError:
        valid = False
    if not valid:
        raise ValueError('the URL must start with http:// or https:// and include a host')
    if parsed.scheme == 'http' and not _loopback_host(parsed.hostname) and os.environ.get('CTRLB_ALLOW_HTTP') != '1':
        raise ValueError('HTTPS is required for non-loopback URLs; set CTRLB_ALLOW_HTTP=1 to allow remote plain HTTP')


class _NoAPIRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class _NoHealthRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def local_health(cfg, timeout=0.25):
    """The health HTTP status; never follow redirects, use proxies, or read response text."""
    req = urllib.request.Request(cfg['url'] + '/healthz', headers={'Authorization': 'Bearer ' + cfg['key']})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoHealthRedirect())
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
    """Best effort SessionStart: short probe, detached serve, then at most three seconds of polling."""
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
            # Keep exclusive access until health answers so the next session re-probes before spawning.
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
    """autoSlots: CTRLB_AUTO_SLOTS when set (comma-separated; empty is none), else the file's list, else REGISTRY.md."""
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
    """A run id as one file-name segment."""
    s = re.sub(r'[^A-Za-z0-9._-]+', '_', str(s or ''))[:120].strip('.')
    return s or '_'


def scratch_dir(cfg, run):
    """The run's durable scratch directory: scratchDir, with {run} replaced, or
    <CTRLB_HOME>/relay/<run id>/scratch."""
    d = cfg.get('scratch_dir') or ''
    if d:
        return os.path.abspath(os.path.expanduser(d.replace('{run}', safe_segment(run))))
    return os.path.join(HOME, 'relay', safe_segment(run), 'scratch')


def run_id(cfg, session_id, parent=''):
    """CTRLB_RUN when set; else a subagent joins its parent's run and a session is its own run."""
    return cfg.get('run') or parent or session_id or ''


def agent_name(cfg, session_id, subagent=''):
    """The agent id for relay calls and rows. A session: CTRLB_AGENT or the session id. A subagent: its own
    session id (agent-<id>), prefixed with the CTRLB_AGENT label and ':' when one is set."""
    label = cfg.get('agent') or ''
    if subagent:
        sub = subagent if subagent.startswith('agent-') else 'agent-' + subagent
        return f'{label}:{sub}' if label else sub
    return label or session_id or ''


def _tighten(path, mode):
    """Clear the group and other bits of an existing file or directory. Never raises."""
    try:
        st = os.stat(path)
        if st.st_mode & 0o077:
            os.chmod(path, st.st_mode & mode)
    except OSError:
        pass


def private_dir(d):
    """Make d (mode 0700), and make sure CTRLB_HOME and d are readable only by this user: the state files hold shell
    commands and output summaries, the log holds error text. Raises OSError only when d cannot be made."""
    os.makedirs(HOME, mode=0o700, exist_ok=True)
    _tighten(HOME, 0o700)
    if os.path.abspath(d) != os.path.abspath(HOME):
        os.makedirs(d, mode=0o700, exist_ok=True)
        _tighten(d, 0o700)
    return d


def private_open(path, mode='w'):
    """open() for a file under CTRLB_HOME: created with mode 0600, an existing one tightened to 0600."""
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
        self.text = text


class RecordsRefused(SeekerError):
    """CtrlB answered a records call with 400: it validated the rows and refused them, so sending the same rows again
    gets the same answer. A SeekerError subclass, so a caller that catches SeekerError catches it too."""

    def names(self, field):
        """Whether the refusal names field, e.g. block_type when an older CtrlB does not know a block type."""
        return field in self.text


def request(cfg, method, path, body=None, query=None, timeout=10, expected_status=None):
    """One call to CtrlB. Returns the decoded JSON answer ({} when empty). Raises SeekerError on an HTTP error and
    OSError / TimeoutError when CtrlB cannot be reached."""
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
    """POST /ai/sessions/{dataset}/records: at most 500 rows or 4 MB per call; answers 202 {accepted: n}. A 400 answer
    raises RecordsRefused; any other failure raises SeekerError, OSError or TimeoutError as request does."""
    try:
        answer = request(cfg, 'POST', f"/ai/sessions/{_seg(cfg['dataset'])}/records", rows, timeout=timeout,
                         expected_status=202)
    except SeekerError as e:
        if e.status == 400:
            raise RecordsRefused(e.status, e.text) from None
        raise
    if not isinstance(answer, dict) or type(answer.get('accepted')) is not int or answer['accepted'] != len(rows):
        raise SeekerError(202, 'upload acknowledgment must accept every row; progress kept for retry')
    return answer


def check(cfg, run, agent, paths, cwd='', timeout=2, tokens=None):
    """POST /ai/relay/{dataset}/runs/{run}/check: {mode, allowed, conflicts: [{path, key, held_by, lease_until, reason,
    lapsed_at?, message?}], renewed?}. tokens maps a path (as sent) to the token its claim answered."""
    body = dict(agent=agent, paths=list(paths))
    if cwd:
        body['cwd'] = cwd
    if tokens:
        body['tokens'] = dict(tokens)
    return request(cfg, 'POST', f"/ai/relay/{_seg(cfg['dataset'])}/runs/{_seg(run)}/check", body, timeout=timeout)


def runs(cfg, status='all', limit=5, timeout=15):
    """GET /ai/relay/{dataset}/runs: {runs: [...], next_cursor}."""
    return request(cfg, 'GET', f"/ai/relay/{_seg(cfg['dataset'])}/runs", query=dict(status=status, limit=limit),
                   timeout=timeout)


def host():
    return socket.gethostname()


class TokenState:
    """The hold tokens of one agent of one run: {key: token}, from the claim and handoff answers the PostToolUse hook
    sees, sent with the pre-tool check. A JSON file under the plugin's state directory, held exclusively (fcntl) while
    it is read and written. Tokens are hold nonces, not secrets."""

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
    """The last test-looking Bash command of one agent of one run, its exit code and last output line, from the
    PostToolUse hook: {command, exit_code, summary, ts}. Quoted back to the agent when done is
    refused for evidence; never sent as evidence by the hook."""

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
    """What the hooks saw one agent of one run do in its current session: {sid, bash:
    [{command, exit_code, summary, ts}] (newest last, at most BASH_MAX), edited: [path], note: {text, ts}, relay: bool
    (a ctrlb relay tool was called)}. A new session id starts a new record. Read by the session-end handoff (its text)
    and by the done witness (was this command run, with this exit code, in this session)."""
    BASH_MAX = 200
    EDITED_MAX = 200

    def __init__(self, run, agent):
        super().__init__(run, agent)
        self.file = self.file.replace(os.sep + 'tokens-', os.sep + 'session-')

    def update(self, sid, fn):
        """fn(record) changes the record in place; a record of another session is dropped first."""
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
    """POST /ai/relay/{dataset}/runs/{run}/handoffs with all=true and no receiver: every hold the
    agent has in the run is handed off openly (a task: RELEASE plus a NOTE with text) or released (a path, a slot), in
    one write."""
    body = dict(agent=agent, all=True, to='', text=redact(text)[0])
    if cwd:
        body['cwd'] = cwd
    return request(cfg, 'POST', f"/ai/relay/{_seg(cfg['dataset'])}/runs/{_seg(run)}/handoffs", body, timeout=timeout)


# ---------------------------------------------------------------- relay calls

def _run_path(cfg, run, tail):
    return f"/ai/relay/{_seg(cfg['dataset'])}/runs/{_seg(run)}/{tail}"


def post_brief(cfg, run, agent, resource, text, paths=None, cwd='', test='', timeout=2):
    """POST .../briefs: a brief on a task name with paths registers the task; on contract:<name> with paths it is a
    contract. Answers {ok, key, resource, paths?, test?}."""
    body = dict(agent=agent, resource=resource, text=text)
    if paths:
        body['paths'] = list(paths)
    if cwd:
        body['cwd'] = cwd
    if test:
        body['test'] = test
    return request(cfg, 'POST', _run_path(cfg, run, 'briefs'), body, timeout=timeout)


def claim(cfg, run, agent, resource, paths=None, cwd='', timeout=2):
    """POST .../claims: {held, resource, key, paths?, held_by?, token?, ...}."""
    body = dict(agent=agent, resource=resource)
    if paths:
        body['paths'] = list(paths)
    if cwd:
        body['cwd'] = cwd
    return request(cfg, 'POST', _run_path(cfg, run, 'claims'), body, timeout=timeout)


def release(cfg, run, agent, resource, cwd='', timeout=2):
    """DELETE .../claims: {released, finished, resource, key, paths?}. A slot or a path is always handed back."""
    body = dict(agent=agent, resource=resource)
    if cwd:
        body['cwd'] = cwd
    return request(cfg, 'DELETE', _run_path(cfg, run, 'claims'), body, timeout=timeout)


def post_note(cfg, run, agent, text, key='', hash_='', timeout=10):
    """POST .../posts: a NOTE. key (artifact:<path>) and hash identify the artefact path and content."""
    body = dict(agent=agent, text=text)
    if key:
        body['key'] = key
    if hash_:
        body['hash'] = hash_
    return request(cfg, 'POST', _run_path(cfg, run, 'posts'), body, timeout=timeout)


def get_run_part(cfg, run, tail, query=None, timeout=2):
    """GET .../tasks, .../contracts, .../peers or .../artifacts of one run."""
    return request(cfg, 'GET', _run_path(cfg, run, tail), query=query, timeout=timeout)


class RunState(TokenState):
    """A small JSON document per (run, agent) under the state directory; name picks the
    document: 'artifacts' (path -> hash of the last posted content), 'record' (the session-start cache), 'counter'.
    Held exclusively while it is read and written, as TokenState is."""

    def __init__(self, run, agent, name):
        super().__init__(run, agent)
        self.file = self.file.replace(os.sep + 'tokens-', os.sep + name + '-')

    def read(self):
        return self._locked(lambda d: (dict(d), False))

    def write(self, data):
        def fn(d):
            d.clear()
            d.update(data)
            return None, True
        return self._locked(fn)

    def change(self, fn):
        """fn(d) changes d in place and returns a value, which change returns."""
        return self._locked(lambda d: (fn(d), True))


class FileState:
    """Upload progress for one transcript file (see fresh() for its keys). Used as a context manager it holds the
    state file exclusively (fcntl), so two hooks never upload one transcript at once; it waits until the deadline
    (epoch seconds)."""
    TITLE_KEEP = 500

    def __init__(self, path, deadline):
        d = private_dir(os.path.join(HOME, 'state'))
        key = hashlib.sha1(os.path.abspath(path).encode()).hexdigest()
        self.file = os.path.join(d, key + '.json')
        self.mutex_path = os.path.join(d, key + '.mutex')
        self.deadline = time.monotonic() + max(0, deadline - time.time())
        self.data = dict(path=os.path.abspath(path), **self.fresh())

    @staticmethod
    def fresh():
        """The state of a file nothing has been read from:
          offset, line, row   bytes and lines uploaded, and the row cursor inside a line that spans calls
          prev_ts             the time of the last line read (µs)
          names, mids, cost   tool names by id, message ids already counted for tokens, the last cumulative cost
          title               the last title written (or, Claude Code, waiting) for the file; a refused one counts
          title_wait          Claude Code: [seq, title] pairs waiting for the first line with a time
          title_custom        Claude Code: a custom-title was seen, so later ai-title lines are ignored
          title_seqs          Claude Code: the seqs of the title rows CtrlB accepted
          title_at            Codex: the updated_at (µs) of the name written last
          title_rows          Codex: the title rows written, so the next gets block_idx 1000 + title_rows
          title_refused       Codex: [seq, block_idx, time, name] of title rows an older CtrlB refused"""
        return dict(offset=0, line=0, row=0, prev_ts=0, names={}, mids=[], cost=0.0, title='', title_wait=[],
                    title_custom=False, title_seqs=[], title_at=None, title_rows=0, title_refused=[])

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
        self.data.update(self.fresh())

    def save(self):
        names = self.data['names']
        if len(names) > 5000:
            self.data['names'] = dict(list(names.items())[-2000:])
        self.data['mids'] = self.data.get('mids', [])[-200:]
        for k in ('title_seqs', 'title_refused'):
            self.data[k] = (self.data.get(k) or [])[-self.TITLE_KEEP:]
        tmp = self.file + '.tmp'
        with private_open(tmp) as fh:
            json.dump(self.data, fh)
        os.replace(tmp, self.file)

    def __exit__(self, *exc):
        fcntl.flock(self.fh, fcntl.LOCK_UN)
        self.fh.close()
        return False
