
import functools, glob, hashlib, json, os, re, shlex
import common, rows_claude
from paths import edit_paths as normalise_edit_paths
from rows_claude import micros, _num

HARNESS = 'codex'
UUID_RE = re.compile(r'([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$')

PATCH_RE = re.compile(r'\*\*\* (?:Update File|Add File|Delete File|Move to): (.+?)[ \t]*(?=\\r|\\n|\r|\n|"|$)')
CMD_RE = re.compile(r'''["']?\bcmd["']?\s*:\s*("(?:[^"\\]|\\.)*")''')
SHELL_TOOLS = ('shell', 'shell_command', 'exec_command', 'local_shell', 'container.exec', 'unified_exec')
TEXT_ITEMS = ('input_text', 'output_text', 'text')


def patch_paths(text):

    if not isinstance(text, str) or '*** Begin Patch' not in text:
        return []
    out = []
    for m in PATCH_RE.finditer(text):
        p = m.group(1).strip()
        if p and p not in out:
            out.append(p)
    return out


def strings_in(v):

    if isinstance(v, str):
        yield v
    elif isinstance(v, dict):
        for x in v.values():
            yield from strings_in(x)
    elif isinstance(v, list):
        for x in v:
            yield from strings_in(x)


def input_paths(tool_input):

    out = []
    for s in strings_in(tool_input):
        for p in patch_paths(s):
            if p not in out:
                out.append(p)
    return out


def exec_commands(code):

    out = []
    for m in CMD_RE.finditer(code or ''):
        try:
            out.append(str(json.loads(m.group(1))))
        except ValueError:
            out.append(m.group(1)[1:-1])
    return out


def shell_command(args):

    if not isinstance(args, dict):
        return ''
    c = args.get('cmd') if args.get('cmd') is not None else args.get('command')
    if isinstance(c, list):
        c = [str(x) for x in c]
        if len(c) == 3 and os.path.basename(c[0]) in ('bash', 'sh', 'zsh') and c[1] in ('-c', '-lc'):
            return c[2]
        return shlex.join(c)
    return c if isinstance(c, str) else ''


def read_meta(path):

    try:
        with open(path, 'rb') as fh:
            rec = json.loads(fh.readline(8_000_000))
        if rec.get('type') == 'session_meta' and isinstance(rec.get('payload'), dict):
            return rec['payload']
    except Exception:
        pass
    return {}


def agent_for(cfg, sid, parent=''):

    label = cfg.get('agent') or ''
    if parent:
        return f'{label}:{sid}' if label else sid
    return label or sid or ''


@functools.lru_cache(maxsize=4)
def _session_files(root):

    files = {}
    for file in rollouts(root):
        match = UUID_RE.search(os.path.basename(file))
        if match:
            files[match.group(1)] = file
    return files


def session_run(cfg, sid, parent='', path=''):

    if cfg.get('run') or not parent:
        return common.run_id(cfg, sid, parent)
    return root_session(sid, parent, path)


def root_session(sid, parent='', path=''):

    return root_rollout(sid, parent, path)[0]


def root_rollout(sid, parent='', path=''):

    file = path
    seen = {sid}
    while parent and parent not in seen and len(seen) < 64:
        seen.add(parent)
        sid, parent = parent, ''


        file = os.path.join(os.path.dirname(path), sid + '.jsonl') if path and re.fullmatch(r'[\w-]+', sid) else ''
        if not file or not os.path.isfile(file):
            file = _session_files(os.path.join(codex_home(), 'sessions')).get(sid) or ''
        if file:
            meta = read_meta(file)
            if str(meta.get('id') or meta.get('session_id') or '') == sid:
                parent = str(meta.get('parent_thread_id') or '')
    return sid, file


RELAY_WRITE_TOOLS = ('post', 'claim', 'handoff', 'release', 'done', 'task', 'brief', 'ask', 'answer', 'contract')


def relay_call(path):

    try:
        st = os.stat(path)
    except (OSError, TypeError, ValueError):
        return None
    keep, c, last = '', {}, None
    try:
        d = common.private_dir(os.path.join(common.HOME, 'spawn', 'rollouts'))
        keep = os.path.join(d, hashlib.sha256(os.path.abspath(path).encode()).hexdigest()[:32] + '.json')
        with open(keep) as fh:
            c = json.load(fh)
    except Exception:
        pass
    try:
        with open(path, 'rb') as fh:
            ok = (isinstance(c, dict) and c.get('ino') == st.st_ino and isinstance(c.get('offset'), int)
                  and 0 <= c['offset'] <= st.st_size)
            if ok and c['offset']:
                fh.seek(max(0, c['offset'] - 64))
                ok = fh.read(c['offset'] - max(0, c['offset'] - 64)).hex() == c.get('tail')
            if not ok:
                c = dict(ino=st.st_ino, offset=0, last=None, tail='')
            last = c.get('last')
            if c['offset'] < st.st_size:
                fh.seek(c['offset'])
                start = c['offset']
                while True:
                    line = fh.readline()
                    if not line.endswith(b'\n'):
                        break
                    c['offset'] += len(line)
                    if b'McpToolCall' in line:
                        last = _relay_item(line) or last
                if c['offset'] > start:
                    fh.seek(max(0, c['offset'] - 64))
                    c['tail'] = fh.read(c['offset'] - max(0, c['offset'] - 64)).hex()
                c['last'] = last
                if keep:
                    try:
                        tmp = f'{keep}.{os.getpid()}.tmp'
                        with common.private_open(tmp) as out:
                            json.dump(c, out)
                        os.replace(tmp, keep)
                    except OSError:
                        pass
    except TimeoutError:
        return tuple(last) if last else None
    except OSError:
        return None
    return tuple(last) if last else None


def _relay_item(line):
    try:
        item = (json.loads(line).get('payload') or {}).get('item') or {}
    except (ValueError, AttributeError):
        return None
    if (not isinstance(item, dict) or item.get('type') != 'McpToolCall' or item.get('server') != 'ctrlb'
            or item.get('tool') not in RELAY_WRITE_TOOLS):
        return None
    args = item.get('arguments')
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            args = {}
    args = args if isinstance(args, dict) else {}
    return [str(args.get('run_id') or ''), str(args.get('agent') or '')]


def _answer(item):

    if item.get('status') != 'completed' or item.get('error'):
        return None
    res = item.get('result')
    if not isinstance(res, dict) or res.get('isError') or res.get('is_error'):
        return None
    structured = res.get('structuredContent', res.get('structured_content'))
    if isinstance(structured, dict):
        return structured
    content = res.get('content')
    if not isinstance(content, list):
        return None
    text = ''.join(b.get('text') or '' for b in content if isinstance(b, dict) and b.get('type') == 'text')
    try:
        out = json.loads(text)
    except ValueError:
        return None
    return out if isinstance(out, dict) else None


def claim_calls(path, thread=''):

    calls = {}
    try:
        fh = open(path, 'rb')
    except (OSError, TypeError, ValueError):
        return []
    with fh:
        for n, line in enumerate(fh):
            if b'McpToolCall' not in line or b'claim' not in line:
                continue
            try:
                payload = json.loads(line).get('payload') or {}
                item = payload.get('item') or {}
            except (ValueError, AttributeError):
                continue
            if (not isinstance(item, dict) or item.get('type') != 'McpToolCall' or item.get('server') != 'ctrlb'
                    or item.get('tool') != 'claim'):
                continue
            if thread and payload.get('thread_id') and payload['thread_id'] != thread:
                continue
            args = item.get('arguments')
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    args = {}
            key = str(item.get('id') or f'line-{n}')
            first = calls[key][0] if key in calls else n
            calls[key] = (first, args if isinstance(args, dict) else {}, _answer(item))
    return [(a, ans) for _, a, ans in sorted(calls.values(), key=lambda c: c[0])]


def ids_for_file(cfg, path):
    meta = read_meta(path)
    m = UUID_RE.search(os.path.basename(path))
    sid = str(meta.get('id') or meta.get('session_id') or (m.group(1) if m else os.path.basename(path)))
    parent = str(meta.get('parent_thread_id') or '')
    return dict(sid=sid, parent=parent, agent_id=sid if parent else '', run_id=session_run(cfg, sid, parent, path),
                agent=agent_for(cfg, sid, parent))


def _text(o):
    if isinstance(o, str):
        return o
    if isinstance(o, list):
        return '\n'.join(x.get('text') or '' for x in o if isinstance(x, dict) and x.get('type') in TEXT_ITEMS)
    if isinstance(o, dict):
        return o.get('content') if isinstance(o.get('content'), str) else o
    return ''


def _failed(o, text):
    if isinstance(o, dict) and o.get('success') is False:
        return True
    if isinstance(text, dict):
        text = json.dumps(text)
    if isinstance(text, str) and text.startswith('Script failed'):
        return True
    if isinstance(text, str) and text.startswith('{'):
        try:
            meta = json.loads(text).get('metadata') or {}
            return bool(meta.get('exit_code'))
        except Exception:
            return False
    return False


def _row(cfg, ids, st, hostname, ts, seq, role):
    return rows_claude.row_defaults(cfg, ids, hostname, ts, seq, ids['parent'], role, HARNESS,
                                    st.get('cwd') or '', st.get('branch') or '', False)


def _tool_use(r, p, st):
    t = p.get('type')
    if t == 'custom_tool_call':
        name, raw = p.get('name') or '', p.get('input')
        scan = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
        r['command'] = '\n'.join(exec_commands(scan)) if name == 'exec' else ''
        r['body'] = raw
        paths = input_paths(raw)
    elif t == 'function_call':
        name, raw = p.get('name') or '', p.get('arguments') or ''
        try:
            args = json.loads(raw) if isinstance(raw, str) else raw
        except ValueError:
            args = raw
        r['command'] = shell_command(args) if name in SHELL_TOOLS or name.endswith('exec_command') else ''
        r['body'] = r['command'] or raw
        paths = input_paths(args) if isinstance(args, (dict, list)) else patch_paths(raw)
    else:
        name = 'local_shell'
        action = p.get('action') or {}
        r['command'] = shell_command(action)
        r['body'] = r['command'] or action
        paths = []
    r['block_type'], r['tool_name'] = 'tool_use', name
    r['tool_use_id'] = p.get('call_id') or p.get('id') or ''
    paths = normalise_edit_paths(paths, r['cwd'])
    r['paths'] = json.dumps(paths) if paths else ''
    if r['tool_use_id']:
        st['names'][r['tool_use_id']] = name


def rows_from(rec, seq, st, ids, cfg, hostname):
    t = rec.get('type') or ''
    p = rec.get('payload')
    if not isinstance(p, dict):
        return []
    ts = micros(rec['timestamp']) if rec.get('timestamp') else 0
    ts = ts or st['prev_ts'] + 1
    st['prev_ts'] = ts
    if t == 'session_meta':
        st['cwd'] = p.get('cwd') or st.get('cwd', '')
        st['branch'] = (p.get('git') or {}).get('branch') or st.get('branch', '')
        return []
    if t == 'turn_context':
        st['cwd'] = p.get('cwd') or st.get('cwd', '')
        return []
    if t == 'compacted':
        if not isinstance(p.get('message'), str) or not p['message'].strip():
            return []
        r = _row(cfg, ids, st, hostname, ts, seq, 'user')
        r.update(block_type='text', body=p['message'], is_summary=True)
        return [r]
    if t == 'token_usage_record':
        u, rid = p.get('usage'), p.get('response_id') or ''
        mids = st.setdefault('mids', [])
        if not isinstance(u, dict) or (rid and rid in mids):
            return []
        if rid:
            mids.append(rid)
        r = _row(cfg, ids, st, hostname, ts, seq, 'system')
        r.update(block_type='result', body=u, input_tokens=_num(u.get('input_tokens')),
                 output_tokens=_num(u.get('output_tokens')))
        return [r]
    if t != 'response_item':
        return []
    pt = p.get('type')
    out = []
    if pt == 'message':
        role = p.get('role') or ''
        if role not in ('user', 'assistant'):
            return []
        for i, c in enumerate(p.get('content') or []):
            if isinstance(c, dict) and c.get('type') in TEXT_ITEMS and (c.get('text') or '').strip():
                r = _row(cfg, ids, st, hostname, ts, seq, role)
                r.update(block_idx=i, block_type='text', body=c['text'])
                out.append(r)
    elif pt == 'reasoning':
        items = [x for x in (p.get('summary') or []) + (p.get('content') or []) if isinstance(x, dict)]
        for i, c in enumerate(items):
            if (c.get('text') or '').strip():
                r = _row(cfg, ids, st, hostname, ts, seq, 'assistant')
                r.update(block_idx=i, block_type='thinking', body=c['text'])
                out.append(r)
    elif pt in ('custom_tool_call', 'function_call', 'local_shell_call'):
        r = _row(cfg, ids, st, hostname, ts, seq, 'assistant')
        _tool_use(r, p, st)
        out.append(r)
    elif pt in ('custom_tool_call_output', 'function_call_output'):
        r = _row(cfg, ids, st, hostname, ts, seq, 'user')
        o = p.get('output')
        body = _text(o)
        r.update(block_type='tool_result', tool_use_id=p.get('call_id') or '', body=body, is_error=_failed(o, body))
        r['tool_name'] = st['names'].get(r['tool_use_id'], '')
        out.append(r)
    return out


def upload_file(cfg, path, deadline):

    if not path or not os.path.isfile(path):
        return dict(rows=0, lines=0, redacted=0)
    return rows_claude.upload_file(cfg, path, deadline, ids=ids_for_file(cfg, path), rows_fn=rows_from)


def codex_home():
    return os.path.expanduser(os.environ.get('CODEX_HOME') or '~/.codex')


def rollouts(root=None):

    root = root or os.path.join(codex_home(), 'sessions')
    return sorted(glob.glob(os.path.join(root, '**', 'rollout-*.jsonl'), recursive=True))
