"""Config, the Seeker HTTP client, per-file upload state and logging for ctrlb-agent. Standard library only.

Config comes from environment variables, falling back to ~/.ctrlb/config.json (written by bin/configure.py, mode 0600):

  CTRLB_URL      Seeker's URL                                   file key "url"
  CTRLB_KEY      the user's MCP key, sent as Authorization: Bearer   file key "key"
  CTRLB_USER     optional; Seeker fills user_name from the key   file key "user"
  CTRLB_RUN      run id shared by every agent of one run         file key "run"
  CTRLB_AGENT    optional agent label (default: the session id)  (environment only)
  CTRLB_DATASET  sessions dataset (default agent_sessions)        file key "dataset"
  CTRLB_ENFORCE  "off" skips the edit check without calling Seeker; anything else lets Seeker's mode decide
                                                                 file key "enforce"
  CTRLB_MCP_URL  Seeker's /mcp URL, read by plugin/.mcp.json     file key "mcp_url" (shown by status only)
  CTRLB_OFF=1    turns every hook off
  CTRLB_HOME     where config.json, hooks.log and upload state live (default ~/.ctrlb)

Everything is off when the URL or the key is missing or the dataset name is not letters, digits and _."""
import fcntl, hashlib, json, os, re, socket, time, urllib.error, urllib.parse, urllib.request

VERSION = '0.2.0'
HOME = os.environ.get('CTRLB_HOME') or os.path.expanduser('~/.ctrlb')
NAME_RE = re.compile(r'^[A-Za-z0-9_]{1,64}$')
DEFAULT_DATASET = 'agent_sessions'


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
             user=pick('CTRLB_USER', 'user'),
             run=pick('CTRLB_RUN', 'run'),
             agent=(env('CTRLB_AGENT') or '').strip(),
             dataset=pick('CTRLB_DATASET', 'dataset', DEFAULT_DATASET),
             enforce=pick('CTRLB_ENFORCE', 'enforce').lower())
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


def check(cfg, run, agent, paths, cwd='', timeout=2):
    """POST /ai/relay/{dataset}/runs/{run}/check: {mode, allowed, conflicts: [{path, key, held_by, lease_until}]}."""
    body = dict(agent=agent, paths=list(paths))
    if cwd:
        body['cwd'] = cwd
    return request(cfg, 'POST', f"/ai/relay/{_seg(cfg['dataset'])}/runs/{_seg(run)}/check", body, timeout=timeout)


def runs(cfg, status='all', limit=5, timeout=15):
    """GET /ai/relay/{dataset}/runs: {runs: [...], next_cursor}."""
    return request(cfg, 'GET', f"/ai/relay/{_seg(cfg['dataset'])}/runs", query=dict(status=status, limit=limit),
                   timeout=timeout)


def host():
    return socket.gethostname()


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
