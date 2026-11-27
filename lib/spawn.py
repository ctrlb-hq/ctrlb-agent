
import errno, json, os, re, shlex, signal, subprocess, sys, time

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common

AGENT_PROGRAMS = ('claude', 'codex')
SHELLS = ('sh', 'bash', 'zsh', 'dash')
PARENT_VARS = ('CTRLB_PARENT_SESSION', 'CTRLB_PARENT_RUN', 'CTRLB_PARENT_AGENT', 'CTRLB_PARENT_RELAY',
               'CTRLB_PARENT_BASE_AGENT')
MAX_DEPTH = 32
KEEP_SESSIONS_S = 30 * 86400
NOTE_S = 1.5
WATCH_POLL_S = 30
KILL_GRACE_S = 20
UUID_RE = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.I)


def _dir(*tail):
    common.private_dir(os.path.join(common.HOME, 'spawn'))
    return common.private_dir(os.path.join(common.HOME, 'spawn', *tail))




def _proc(pid):

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
        argv = command.split()
        return int(ppid), _program(os.path.basename(argv[0]).lstrip('-') if argv else '', argv), start
    except Exception:
        return None


def _program(name, argv):

    for a in [name] + argv[:2]:
        base = os.path.basename(a or '')
        if base in AGENT_PROGRAMS:
            return base
    return name


def agent_process(pid=None):

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


def write_record(me, sid, run, agent, relay, base_agent='', transcript=''):

    pid, prog, start = me
    d = _dir('procs')
    for f in os.listdir(d):
        if f.endswith('.json') and f[:-5].isdigit() and int(f[:-5]) != pid and not _proc(int(f[:-5])):
            try:
                os.remove(os.path.join(d, f))
            except OSError:
                pass
    _write_json(_record_path(pid), dict(session=sid, run=run, agent=agent, relay=bool(relay), program=prog, start=start,
                                        base_agent=base_agent or '', transcript=transcript or ''))


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
            return _codex_relay(dict(session=rec['session'], run=rec.get('run') or rec['session'],
                                        agent=rec.get('agent') or '', relay=bool(rec.get('relay')),
                                        base_agent=rec.get('base_agent') or '', how='process'),
                                   rec.get('transcript') if rec.get('program') == 'codex' else '')
    psid = (env.get('CTRLB_PARENT_SESSION') or '').strip()
    if psid and psid != sid:
        return _codex_relay(dict(session=psid, run=(env.get('CTRLB_PARENT_RUN') or '').strip() or psid,
                                    agent=(env.get('CTRLB_PARENT_AGENT') or '').strip(),
                                    relay=env.get('CTRLB_PARENT_RELAY') == '1',
                                    base_agent=(env.get('CTRLB_PARENT_BASE_AGENT') or '').strip(), how='environment'))
    return None


def _codex_relay(parent, codex_transcript=''):

    if not parent['relay'] and codex_transcript:
        import rows_codex
        if rows_codex.relay_call(codex_transcript):
            parent['relay'] = True
    return parent




def mark_shell(sid, run, agent, relay, env_file=None, base_agent=''):

    env_file = env_file if env_file is not None else os.environ.get('CLAUDE_ENV_FILE') or ''
    if not env_file:
        return False
    vals = dict(CTRLB_PARENT_SESSION=sid, CTRLB_PARENT_RUN=run, CTRLB_PARENT_AGENT=agent,
                CTRLB_PARENT_RELAY='1' if relay else '0', CTRLB_PARENT_BASE_AGENT=base_agent or '')
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

    if parent.get('relay'):
        return True
    try:
        rec = common.SessionRecord(parent['run'], parent['agent'] or parent['session']).get(parent['session'])
        return bool(rec.get('relay'))
    except Exception:
        return False




def _session_path(sid, create=True):

    d = _dir('sessions') if create else os.path.join(common.HOME, 'spawn', 'sessions')
    return os.path.join(d, re.sub(r'[^A-Za-z0-9_.-]', '_', sid)[:128] + '.json')


def joined(sid):

    return _read_json(_session_path(sid, create=False)) if sid else {}


def short_id(sid):

    return sid.replace('-', '')[-8:]


def child_name(parent_agent, parent_session, program, sid):

    own = f'{program or "agent"}-{short_id(sid)}'
    if parent_agent and parent_agent != parent_session and not UUID_RE.match(parent_agent):
        return f'{parent_agent}.{own}'
    return own


def decide(cfg, sid, parent, program):

    own_run = cfg.get('run') or ''
    p_agent = parent['agent'] or parent['session']
    explicit_run = bool(own_run) and own_run != parent['run']
    join = not explicit_run and not common.relay_off(cfg) and parent_in_relay(parent)
    agent = cfg.get('agent') or ''
    renamed = bool(agent) and agent in (p_agent, parent.get('base_agent') or p_agent)
    if renamed or (join and not agent):
        agent = child_name(p_agent, parent['session'], program, sid)
    return dict(parent=parent['session'], parent_agent=p_agent, parent_run=parent['run'], how=parent['how'],
                run=parent['run'] if join else own_run, agent=agent, joined=join, renamed=renamed,
                program=program, ts=time.time())


def apply(cfg, sid):




    if not sid:
        return {}
    try:
        j = joined(sid)
    except Exception as e:
        common.log(f"spawn apply {sid}: {type(e).__name__}: {str(e)[:200]}")
        return {}
    if j:
        try:
            os.utime(_session_path(sid, create=False))
        except OSError:
            pass
    if j.get('joined') and j.get('run'):
        cfg['run'] = j['run']
    if j.get('agent') and (j.get('joined') or j.get('renamed')):
        cfg['agent'] = j['agent']
    return j


def link(sid, root):

    if not sid or not root or sid == root:
        return {}
    d = joined(root)
    if d and joined(sid) != dict(d, root=root):
        _write_json(_session_path(sid), dict(d, root=root))
    return d


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

    if not sid:
        return {}
    if not cfg.get('join_spawned'):
        return apply(cfg, sid)
    decision = {}
    me = None
    base_agent = cfg.get('agent') or ''
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
            write_record(me, sid, run, agent, relay, base_agent, transcript)
    except Exception as e:
        common.log(f"spawn record {sid}: {type(e).__name__}: {str(e)[:200]}")
    try:
        mark_shell(sid, run, agent, relay, env_file, base_agent)
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


    if decision and minutes > 0 and me and source != 'compact':
        try:
            watch(dict(pid=me[0], start=me[2], transcript=transcript or '', minutes=minutes, run=decision.get('run') or '',
                       agent=decision.get('agent') or '', joined=bool(decision.get('joined')), sid=sid))
        except Exception as e:
            common.log(f"spawn watchdog {sid}: {type(e).__name__}: {str(e)[:200]}")
    return decision




def watch(payload):

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


def _same_session(pid, sid):

    rec = _read_json(_record_path(pid))
    return not sid or not rec.get('session') or rec['session'] == sid


def _pidfd(pid):

    try:
        return os.pidfd_open(pid)
    except (AttributeError, OSError):
        return None


def _signal(pid, start, fd, sig):

    if fd is not None:
        signal.pidfd_send_signal(fd, sig)
    elif _alive(pid, start):
        os.kill(pid, sig)
    else:
        raise ProcessLookupError(pid)


class _Activity:


    def __init__(self, path):
        self.path = path
        self.seen = self.fail = self.fail_since = None
        self.changed = time.monotonic()
        self.look()

    def _stat(self):

        try:
            st = os.stat(self.path)
            return (st.st_mtime_ns, st.st_size, st.st_ino), None
        except OSError as e:
            return None, 'missing' if e.errno == errno.ENOENT else 'error'
        except ValueError:
            return None, 'error'

    def look(self):

        stamp, fail = self._stat()
        now = time.monotonic()
        if fail:
            if fail != self.fail:
                self.fail, self.fail_since = fail, now
            return fail, now - self.fail_since
        self.fail = self.fail_since = None
        if stamp != self.seen:
            self.seen, self.changed = stamp, now
        return 'ok', now - self.changed


def _due(look, limit):

    state, secs = look
    return state in ('ok', 'missing') and secs >= limit


def _watcher(payload, poll_s=WATCH_POLL_S, grace_s=KILL_GRACE_S):

    pid, start, limit = int(payload['pid']), payload.get('start'), float(payload['minutes']) * 60
    sid = payload.get('sid') or ''
    fd = _pidfd(pid)
    if not _alive(pid, start):
        return
    try:
        _watch_loop(payload, pid, start, sid, limit, _Activity(payload.get('transcript') or ''), fd, poll_s, grace_s)
    finally:
        if fd is not None:
            os.close(fd)


def _watch_loop(payload, pid, start, sid, limit, activity, fd, poll_s, grace_s):

    noted = logged = pending = False
    while _alive(pid, start) and _same_session(pid, sid):
        look = activity.look()
        state, secs = look
        due = _due(look, limit)
        if state == 'error':
            if secs >= limit and not logged:
                common.log(f"spawn watchdog {payload.get('sid')}: the transcript's stat has failed for {int(secs)}s; "
                           f"not stopping pid {pid}, still watching")
                logged = True
        else:
            logged = False
            if state == 'ok' and not due:
                if pending:
                    common.log(f"spawn watchdog {payload.get('sid')}: transcript changed, not stopping pid {pid}")
                noted = pending = False
        if pending and due:
            _stop(pid, start, sid, fd, grace_s)
            return
        if due:
            what = 'no change' if state == 'ok' else 'transcript missing'
            common.log(f"spawn watchdog {payload.get('sid')}: {what} for {int(secs)}s, stopping pid {pid}")
            if not noted and payload.get('joined') and payload.get('run') and payload.get('agent'):
                noted = True
                try:
                    cfg = common.config()
                    if not cfg['off']:
                        common.post_note(cfg, payload['run'], payload['agent'],
                                         f"Stopped by spawnIdleMinutes: no progress for {int(secs // 60)} minutes.",
                                         timeout=5)
                except Exception as e:
                    common.log(f"spawn watchdog note: {type(e).__name__}: {str(e)[:200]}")
            pending = True
        if state == 'error':
            time.sleep(poll_s)
        elif pending:
            time.sleep(min(poll_s, 1.0))
        else:
            time.sleep(min(poll_s, max(1.0, limit - secs)))


def _stop(pid, start, sid, fd, grace_s):

    try:
        _signal(pid, start, fd, signal.SIGTERM)
    except OSError:
        return
    end = time.monotonic() + grace_s
    while time.monotonic() < end and _alive(pid, start):
        time.sleep(1)
    if not _same_session(pid, sid):
        return
    try:
        _signal(pid, start, fd, signal.SIGKILL)
    except OSError:
        pass


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
