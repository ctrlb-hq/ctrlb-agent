"""Config, the Seeker HTTP client, per-file upload state and logging for ctrlb-agent. Standard library only.

Config comes from environment variables, falling back to ~/.ctrlb/config.json (written by bin/configure.py, mode 0600):

  CTRLB_URL      Seeker's URL                                   file key "url"
  CTRLB_KEY      the user's MCP key, sent as Authorization: Bearer   file key "key"
  (no variable)  the key's user: the "email" claim of the key, as Seeker reads it; the only source of user_name
                                                                 file key "user" (written by bin/configure.py)
  CTRLB_RUN      run id shared by every agent of one run         file key "run"
  CTRLB_AGENT    optional agent label (default: the session id)  (environment only)
  CTRLB_USER     read for compatibility as the agent label when CTRLB_AGENT is unset; never sent as user_name
  CTRLB_DATASET  sessions dataset (default agent_sessions)        file key "dataset"
  CTRLB_ENFORCE  "off" skips the edit check without calling Seeker; anything else lets Seeker's mode decide
                                                                 file key "enforce"
  CTRLB_MCP_URL  Seeker's /mcp URL, read by plugin/.mcp.json     file key "mcp_url" (shown by status only)
  CTRLB_OFF=1    turns every hook off
  CTRLB_HOME     where config.json, hooks.log and upload state live (default ~/.ctrlb)

Everything is off when the URL or the key is missing or the dataset name is not letters, digits and _."""
import base64, fcntl, hashlib, json, os, re, socket, time, urllib.error, urllib.parse, urllib.request

VERSION = '0.2.0'
HOME = os.environ.get('CTRLB_HOME') or os.path.expanduser('~/.ctrlb')
NAME_RE = re.compile(r'^[A-Za-z0-9_]{1,64}$')
DEFAULT_DATASET = 'agent_sessions'
USER_CLAIM = 'email'   # the claim Seeker takes the caller's user_name from (middleware GetEmailFromJWT)


def key_user(key):
    """The user an MCP key belongs to: the USER_CLAIM of its JWT payload (base64url, no signature check; Seeker
    checks the signature). '' when the key is not a JWT or carries no such claim, so rows then omit user_name and
    Seeker fills it from the key."""
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
    # user_name only ever comes from the key in effect (CTRLB_KEY or the file's key), decoded now, so a key from the
    # environment and an old config.json "user" written as a label can never put another name on the rows.
    c['user'] = key_user(c['key'])
    c['mcp_url'] = pick('CTRLB_MCP_URL', 'mcp_url', c['url'] + '/mcp' if c['url'] else '')
    c['off'] = env('CTRLB_OFF') == '1' or not c['url'] or not c['key'] or not NAME_RE.match(c['dataset'])
    return c


def run_id(cfg, session_id, parent=''):
    """Decision 4: CTRLB_RUN when set; else a subagent joins its parent's run and a session is its own run."""
    return cfg.get('run') or parent or session_id or ''


def agent_name(cfg, session_id, subagent=''):
    """The agent id for relay calls and rows. A session: CTRLB_AGENT or the session id. A subagent: its own
    session id (agent-<id>), prefixed with the CTRLB_AGENT label and ':' when one is set."""
    label = cfg.get('agent') or ''
    if subagent:
        sub = subagent if subagent.startswith('agent-') else 'agent-' + subagent
        return f'{label}:{sub}' if label else sub
    return label or session_id or ''


def log(msg):
    try:
        os.makedirs(HOME, exist_ok=True)
        with open(os.path.join(HOME, 'hooks.log'), 'a') as fh:
            fh.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {os.getpid()} {msg}\n")
    except Exception:
        pass


class SeekerError(RuntimeError):
    def __init__(self, status, text):
        super().__init__(f'HTTP {status}: {text}')
        self.status = status


def request(cfg, method, path, body=None, query=None, timeout=10):
    """One call to Seeker. Returns the decoded JSON answer ({} when empty). Raises SeekerError on an HTTP error and
    OSError / TimeoutError when Seeker cannot be reached."""
    url = cfg['url'] + path
    if query:
        url += '?' + urllib.parse.urlencode({k: v for k, v in query.items() if v is not None and v != ''})
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        'Content-Type': 'application/json', 'Accept': 'application/json',
        'Authorization': 'Bearer ' + cfg['key'], 'User-Agent': 'ctrlb-agent/' + VERSION})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        raise SeekerError(e.code, e.read()[:300].decode(errors='replace')) from None
    try:
        return json.loads(raw or b'{}')
    except ValueError:
        return {'raw': raw[:300].decode(errors='replace')}


def _seg(s):
    return urllib.parse.quote(str(s), safe='')


def ingest_records(cfg, rows, timeout=20):
    """POST /ai/sessions/{dataset}/records: at most 500 rows or 4 MB per call; answers 202 {accepted: n}."""
    return request(cfg, 'POST', f"/ai/sessions/{_seg(cfg['dataset'])}/records", rows, timeout=timeout)


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
        d = os.path.join(HOME, 'state')
        os.makedirs(d, exist_ok=True)
        name = hashlib.sha1(f'{run}\0{agent}'.encode()).hexdigest()
        self.file = os.path.join(d, f'tokens-{name}.json')

    def _locked(self, fn):
        with open(self.file + '.mutex', 'w') as mx:
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
                    with open(tmp, 'w') as fh:
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
    PostToolUse hook (Seeker decision 30): {command, exit_code, summary, ts}. Quoted back to the agent when done is
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
    """What the hooks saw one agent of one run do in its current session (Seeker decisions 36 and 38): {sid, bash:
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
    """POST /ai/relay/{dataset}/runs/{run}/handoffs with all=true and no receiver (Seeker decision 36): every hold the
    agent has in the run is handed off openly (a task: RELEASE plus a NOTE with text) or released (a path, a slot), in
    one write."""
    body = dict(agent=agent, all=True, to='', text=text)
    if cwd:
        body['cwd'] = cwd
    return request(cfg, 'POST', f"/ai/relay/{_seg(cfg['dataset'])}/runs/{_seg(run)}/handoffs", body, timeout=timeout)


class FileState:
    """Upload progress for one transcript file: byte offset, line count, last timestamp, tool names by id, message
    ids already counted for tokens, and the last cumulative cost seen. Used as a context manager it holds the
    state file exclusively (fcntl), so two hooks never upload one transcript at once; it waits until the deadline
    (epoch seconds)."""

    def __init__(self, path, deadline):
        d = os.path.join(HOME, 'state')
        os.makedirs(d, exist_ok=True)
        key = hashlib.sha1(os.path.abspath(path).encode()).hexdigest()
        self.file = os.path.join(d, key + '.json')
        self.mutex_path = os.path.join(d, key + '.mutex')
        self.deadline = deadline
        self.data = dict(path=os.path.abspath(path), offset=0, line=0, prev_ts=0, names={}, mids=[], cost=0.0)

    def __enter__(self):
        self.fh = open(self.mutex_path, 'w')
        while True:
            try:
                fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.time() > self.deadline:
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
        self.data.update(offset=0, line=0, prev_ts=0, names={}, mids=[], cost=0.0)

    def save(self):
        names = self.data['names']
        if len(names) > 5000:
            self.data['names'] = dict(list(names.items())[-2000:])
        self.data['mids'] = self.data.get('mids', [])[-200:]
        tmp = self.file + '.tmp'
        with open(tmp, 'w') as fh:
            json.dump(self.data, fh)
        os.replace(tmp, self.file)

    def __exit__(self, *exc):
        fcntl.flock(self.fh, fcntl.LOCK_UN)
        self.fh.close()
        return False
