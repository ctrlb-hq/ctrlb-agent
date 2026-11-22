"""Started agents: a model CLI (claude -p, codex exec) that one agent starts from its shell finds that agent when it
starts, and, when that agent is in a Relay run, joins the run under its own agent name. Standard library only.
docs/relay-hooks.md describes it.

No command text is read. Every agent session writes where it can be found when it starts:
  - a process record, <CTRLB_HOME>/spawn/procs/<pid>.json, for its agent process (Claude Code and Codex);
  - for Claude Code, CTRLB_PARENT_* lines in CLAUDE_ENV_FILE, so every later Bash command, and every process those
    commands start, carries them, even when it leaves the process tree (nohup, setsid).
A session that starts looks for the nearest agent process above its own that left a record; failing that, for the
CTRLB_PARENT_* variables. That agent is its parent. The decision is kept in <CTRLB_HOME>/spawn/sessions/<sid>.json,
so every later hook of the session reads the same run and agent name.

The parent joins nothing on its own. The child joins the parent's run when the parent is in one (CTRLB_RUN set, or a
ctrlb relay tool called in that session), and posts one note saying who started it. A child started with its own
CTRLB_RUN and its own CTRLB_AGENT is left as it is. A child that inherited its parent's CTRLB_AGENT gets a name of its
own. joinSpawned (CTRLB_JOIN_SPAWNED) turns this off. spawnIdleMinutes (CTRLB_SPAWN_IDLE_MINUTES) stops a started agent
whose transcript has not changed for that many minutes (off by default)."""
import json, os, re, shlex, signal, subprocess, sys, time

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

AGENT_PROGRAMS = ('claude', 'codex')
SHELLS = ('sh', 'bash', 'zsh', 'dash')
PARENT_VARS = ('CTRLB_PARENT_SESSION', 'CTRLB_PARENT_RUN', 'CTRLB_PARENT_AGENT', 'CTRLB_PARENT_RELAY')
MAX_DEPTH = 32
KEEP_SESSIONS_S = 30 * 86400
NOTE_S = 1.5
WATCH_POLL_S = 30
KILL_GRACE_S = 20
UUID_RE = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.I)


def _dir(*tail):
    common.private_dir(os.path.join(common.HOME, 'spawn'))
    return common.private_dir(os.path.join(common.HOME, 'spawn', *tail))


# ---------------------------------------------------------------- processes

def _proc(pid):
    """(parent pid, program name, start time) of a process, or None. Linux reads /proc; elsewhere ps."""
    try:
        with open(f'/proc/{pid}/stat') as fh:
            s = fh.read()
        f = s[s.rindex(')') + 2:].split()
        name = s[s.index('(') + 1:s.rindex(')')]
        try:
            with open(f'/proc/{pid}/cmdline', 'rb') as fh:
                argv = [a.decode(errors='replace') for a in fh.read().split(b'\0') if a]
        except OSError:
            argv = []
        return int(f[1]), _program(name, argv), f[19]
    except FileNotFoundError:
        if os.path.isdir('/proc/self'):
            return None
    except (OSError, ValueError, IndexError):
        return None
    try:
        out = subprocess.run(['ps', '-o', 'ppid=', '-o', 'lstart=', '-o', 'command=', '-p', str(pid)],
                             capture_output=True, text=True, timeout=2).stdout.strip()
        if not out:
            return None
        ppid, rest = out.split(None, 1)
        start, command = rest[:24], rest[24:].strip()
        return int(ppid), _program('', command.split()), start
    except Exception:
        return None


def _program(name, argv):
    """'claude' or 'codex' when the process is that agent CLI (directly, or a node script of that name), else name."""
    for a in [name] + argv[:2]:
        base = os.path.basename(a or '')
        if base in AGENT_PROGRAMS:
            return base
    return name


def agent_process(pid=None):
    """(pid, program, start) of the agent CLI that runs this hook, or None: the hook's parent, or the parent of the
    shell that runs it. Nothing further up counts, so a hook run by some other program (a test, a script) under an
    agent's shell is not taken for that agent's own."""
    pid = pid or os.getpid()
    for _ in range(3):
        p = _proc(pid)
        if not p or p[0] <= 1:
            return None
        q = _proc(p[0])
        if not q:
            return None
        if q[1] in AGENT_PROGRAMS:
            return p[0], q[1], q[2]
        if q[1] not in SHELLS:
            return None
        pid = p[0]
    return None


def _record_path(pid):
    return os.path.join(_dir('procs'), f'{int(pid)}.json')


def write_record(me, sid, run, agent, relay):
    """The process record of this session's agent process me (pid, program, start), and the removal of records whose
    process has gone."""
    pid, prog, start = me
    d = _dir('procs')
    for f in os.listdir(d):
        if f.endswith('.json') and f[:-5].isdigit() and int(f[:-5]) != pid and not _proc(int(f[:-5])):
            try:
                os.remove(os.path.join(d, f))
            except OSError:
                pass
    _write_json(_record_path(pid), dict(session=sid, run=run, agent=agent, relay=bool(relay), program=prog, start=start))


def _write_json(path, data):
    tmp = f'{path}.{os.getpid()}.tmp'
    with common.private_open(tmp) as fh:
        json.dump(data, fh)
    os.replace(tmp, path)


def _read_json(path):
    try:
        with open(path) as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def find_parent(me, sid, env=None):
    """The agent that started this session: {session, run, agent, relay, how}, or None. The nearest agent process above
    this session's own (me) with a record wins; else the CTRLB_PARENT_* variables."""
    env = os.environ if env is None else env
    pid = me[0] if me else os.getpid()
    for _ in range(MAX_DEPTH):
        p = _proc(pid)
        if not p or p[0] <= 1:
            break
        pid = p[0]
        q = _proc(pid)
        if not q or q[1] not in AGENT_PROGRAMS:
            continue
        rec = _read_json(_record_path(pid))
        if rec.get('session') and rec.get('start') == q[2] and rec['session'] != sid:
            return dict(session=rec['session'], run=rec.get('run') or rec['session'], agent=rec.get('agent') or '',
                        relay=bool(rec.get('relay')), how='process')
    psid = (env.get('CTRLB_PARENT_SESSION') or '').strip()
    if psid and psid != sid:
        return dict(session=psid, run=(env.get('CTRLB_PARENT_RUN') or '').strip() or psid,
                    agent=(env.get('CTRLB_PARENT_AGENT') or '').strip(), relay=env.get('CTRLB_PARENT_RELAY') == '1',
                    how='environment')
    return None


# ---------------------------------------------------------------- the parent side

def mark_shell(sid, run, agent, relay, env_file=None):
    """Claude Code: CTRLB_PARENT_* for every later Bash command of this session, appended to CLAUDE_ENV_FILE (other
    hooks write to it too, so nothing is replaced). False when there is no such file."""
    env_file = env_file if env_file is not None else os.environ.get('CLAUDE_ENV_FILE') or ''
    if not env_file:
        return False
    vals = dict(CTRLB_PARENT_SESSION=sid, CTRLB_PARENT_RUN=run, CTRLB_PARENT_AGENT=agent,
                CTRLB_PARENT_RELAY='1' if relay else '0')
    text = ''.join(f'export {k}={shlex.quote(v or "")}\n' for k, v in vals.items())
    try:
        with open(env_file) as fh:
            if text in fh.read():
                return True
    except OSError:
        pass
    with open(env_file, 'a') as fh:
        fh.write(text)
    return True


def parent_in_relay(parent):
    """Whether the parent is in a relay run now: its run was set when it started, or it has called a relay tool."""
    if parent.get('relay'):
        return True
    try:
        rec = common.SessionRecord(parent['run'], parent['agent'] or parent['session']).get(parent['session'])
        return bool(rec.get('relay'))
    except Exception:
        return False


# ---------------------------------------------------------------- the child side

def _session_path(sid):
    return os.path.join(_dir('sessions'), re.sub(r'[^A-Za-z0-9_.-]', '_', sid)[:128] + '.json')


def joined(sid):
    """What session-start decided for this session: {parent, parent_agent, run, agent, joined, renamed, ...}, or {}."""
    return _read_json(_session_path(sid)) if sid else {}


def child_name(parent_agent, parent_session, program, sid):
    """<parent label>.<program>-<first 8 of the session id>, without the label when the parent has none (its agent
    name is its session id)."""
    own = f'{program or "agent"}-{sid[:8]}'
    if parent_agent and parent_agent != parent_session and not UUID_RE.match(parent_agent):
        return f'{parent_agent}.{own}'
    return own


def decide(cfg, sid, parent, program):
    """The child's run and agent name: {parent, parent_agent, run, agent, joined, renamed}. joined: the child takes the
    parent's run (it had none of its own, or the same). renamed: it had the parent's agent name."""
    own_run = cfg.get('run') or ''
    p_agent = parent['agent'] or parent['session']
    explicit_run = bool(own_run) and own_run != parent['run']
    join = not explicit_run and parent_in_relay(parent)
    agent = cfg.get('agent') or ''
    renamed = bool(agent) and agent == p_agent
    if renamed or (join and not agent):
        agent = child_name(p_agent, parent['session'], program, sid)
    return dict(parent=parent['session'], parent_agent=p_agent, parent_run=parent['run'], how=parent['how'],
                run=parent['run'] if join else own_run, agent=agent, joined=join, renamed=renamed,
                program=program, ts=time.time())


def apply(cfg, sid):
    """Put the session's decided run and agent name in cfg (every hook calls this after config()). Returns the
    decision, or {}."""
    if not sid or not cfg.get('join_spawned'):
        return {}
    j = joined(sid)
    if j.get('joined') and j.get('run'):
        cfg['run'] = j['run']
    if j.get('agent') and (j.get('joined') or j.get('renamed')):
        cfg['agent'] = j['agent']
    return j


def _prune_sessions():
    d = _dir('sessions')
    now = time.time()
    for f in os.listdir(d):
        p = os.path.join(d, f)
        try:
            if now - os.path.getmtime(p) > KEEP_SESSIONS_S:
                os.remove(p)
        except OSError:
            pass


def session_start(cfg, sid, source='', env_file=None, transcript=''):
    """At session start: find the parent, keep the decision, apply it to cfg, and leave this session's own marks for
    the agents it starts. Returns the decision ({} when the session was not started by an agent). Never raises."""
    if not sid or not cfg.get('join_spawned'):
        return {}
    decision = {}
    me = None
    try:
        me = agent_process()
        decision = joined(sid)
        if not decision and source not in ('compact', 'resume'):
            parent = find_parent(me, sid)
            if parent:
                decision = decide(cfg, sid, parent, me[1] if me else '')
                _write_json(_session_path(sid), decision)
                _prune_sessions()
                common.log(f"spawn {sid}: started by {decision['parent_agent']} ({decision['how']}); "
                           + (f"joined run {decision['run']} as {decision['agent']}" if decision['joined']
                              else 'renamed ' + decision['agent'] if decision['renamed'] else 'parent in no relay run'))
        apply(cfg, sid)
    except Exception as e:
        common.log(f"spawn {sid}: {type(e).__name__}: {str(e)[:200]}")
    run, agent = common.run_id(cfg, sid), common.agent_name(cfg, sid)
    relay = bool(cfg.get('run'))
    try:
        if me:
            write_record(me, sid, run, agent, relay)
    except Exception as e:
        common.log(f"spawn record {sid}: {type(e).__name__}: {str(e)[:200]}")
    try:
        mark_shell(sid, run, agent, relay, env_file)
    except Exception as e:
        common.log(f"spawn env file {sid}: {type(e).__name__}: {str(e)[:200]}")
    if decision and not decision.get('noted') and decision.get('joined') and not common.relay_off(cfg):
        try:
            common.post_note(cfg, decision['run'], decision['agent'],
                             f"Started by {decision['parent_agent']} (session {decision['parent']}) from its shell "
                             f"({decision.get('program') or 'agent CLI'}); joined this run.", timeout=NOTE_S)
            decision['noted'] = True
            _write_json(_session_path(sid), decision)
        except Exception as e:
            common.log(f"spawn note {decision['run']}/{decision['agent']}: {type(e).__name__}: {str(e)[:200]}")
    minutes = cfg.get('spawn_idle_minutes') or 0
    if decision and minutes > 0 and me and source not in ('compact', 'resume'):
        try:
            watch(dict(pid=me[0], start=me[2], transcript=transcript or '', minutes=minutes, run=decision.get('run') or '',
                       agent=decision.get('agent') or '', joined=bool(decision.get('joined')), sid=sid))
        except Exception as e:
            common.log(f"spawn watchdog {sid}: {type(e).__name__}: {str(e)[:200]}")
    return decision


# ---------------------------------------------------------------- spawnIdleMinutes

def watch(payload):
    """Start the watchdog as a detached process (its own session) and return at once."""
    p = subprocess.Popen([sys.executable, '-I', os.path.abspath(__file__), 'watch'], stdin=subprocess.PIPE,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
    try:
        p.stdin.write(json.dumps(payload).encode())
    finally:
        p.stdin.close()
    return p.pid


def _alive(pid, start):
    p = _proc(pid)
    return bool(p) and p[2] == start


def _last_change(path, since):
    try:
        return max(since, os.path.getmtime(path))
    except OSError:
        return since


def _watcher(payload, poll_s=WATCH_POLL_S, grace_s=KILL_GRACE_S):
    """Stop the agent process when its transcript has not changed for the set minutes: SIGTERM (its session-end hook
    hands its holds back), then SIGKILL after grace_s. Ends when the process ends."""
    pid, start, limit = int(payload['pid']), payload.get('start'), float(payload['minutes']) * 60
    began = time.time()
    while _alive(pid, start):
        idle = time.time() - _last_change(payload.get('transcript') or '', began)
        if idle >= limit:
            common.log(f"spawn watchdog {payload.get('sid')}: no change for {int(idle)}s, stopping pid {pid}")
            if payload.get('joined') and payload.get('run') and payload.get('agent'):
                try:
                    cfg = common.config()
                    if not cfg['off']:
                        common.post_note(cfg, payload['run'], payload['agent'],
                                         f"Stopped by spawnIdleMinutes: no progress for {int(idle // 60)} minutes.",
                                         timeout=5)
                except Exception as e:
                    common.log(f"spawn watchdog note: {type(e).__name__}: {str(e)[:200]}")
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                return
            end = time.time() + grace_s
            while time.time() < end and _alive(pid, start):
                time.sleep(1)
            if _alive(pid, start):
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
            return
        time.sleep(min(poll_s, max(1.0, limit - idle)))


if __name__ == '__main__':
    try:
        if sys.argv[1:2] == ['watch']:
            p = json.load(sys.stdin)
            _watcher(p, float(p.get('poll_s') or WATCH_POLL_S), float(p.get('grace_s') if p.get('grace_s') is not None
                                                                      else KILL_GRACE_S))
    except BaseException as e:
        if not isinstance(e, SystemExit):
            common.log(f"spawn {sys.argv[1:]}: {type(e).__name__}: {str(e)[:200]}")
    sys.exit(0)
