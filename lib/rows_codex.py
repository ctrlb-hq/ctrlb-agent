"""Codex rollout -> session rows, the same columns as rows_claude with harness = "codex".

A rollout is ~/.codex/sessions/<yyyy>/<mm>/<dd>/rollout-<time>-<thread id>.jsonl, one JSON object per line:
{timestamp, type, payload}. What becomes a row (seq is the line number, block_idx the item index):

  response_item message              user and assistant text items -> block_type "text" (developer messages are
                                     harness instructions and are skipped, as Claude Code's system prompt is)
  response_item reasoning            summary and content text -> "thinking" (encrypted-only reasoning is skipped)
  response_item custom_tool_call,    -> "tool_use"; tool_use_id is call_id. command: the shell command(s) of the call
                function_call,         (exec_command cmd values inside a code-mode "exec" call; command / cmd of a
                local_shell_call       shell function call). paths: the files an apply_patch touches, from the
                                       patch's *** Update File / *** Add File / *** Delete File / *** Move to lines,
                                       normalised against the row's cwd by paths.edit_paths
  response_item *_output             -> "tool_result" with the tool name of its call; is_error when an exec script
                                       failed or a shell call exited non-zero
  token_usage_record                 -> one "result" row, role system, input_tokens and output_tokens from usage
                                       (input includes cached input, as OpenAI counts it), once per response_id
  compacted with a message           -> "text" row, role user, is_summary true

session_meta gives the ids, cwd and git branch; turn_context updates cwd. A rollout whose session_meta names a
parent_thread_id is a subagent: parent_session is that id, agent_id its own thread id, and it joins its parent's
run. Upload is incremental by byte offset with FileState, through rows_claude.upload_file.

Thread name: Codex never writes it into the rollout. It appends {id, thread_name, updated_at} to
$CODEX_HOME/session_index.jsonl each time a thread is named (by the user or the Codex app). After an upload that sent
lines of a main thread (never a subagent), a current name that differs from the last title written for the file is one
row: role "system", block_type "title", the stripped name as body, seq the last uploaded line's, block_idx
TITLE_IDX + the title rows already written for the file (so names after one line keep distinct, increasing keys) and
the time of the last uploaded line + 1 µs, never updated_at (a rename days later must not make an old session look
active). Without a line with a time yet it waits for a later upload. It goes in a call of its own, refused as
rows_claude.upload_file describes."""
import functools, glob, hashlib, json, os, re, shlex, time
import common, rows_claude
from paths import edit_paths as normalise_edit_paths
from rows_claude import micros

HARNESS = 'codex'
UUID_RE = re.compile(r'([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$')
# A patch header line ends at a newline, real or escaped inside a code-mode string literal, or at the literal's end.
PATCH_RE = re.compile(r'\*\*\* (?:Update File|Add File|Delete File|Move to): (.+?)[ \t]*(?=\\r|\\n|\r|\n|"|$)')
CMD_RE = re.compile(r'''["']?\bcmd["']?\s*:\s*("(?:[^"\\]|\\.)*")''')
SHELL_TOOLS = ('shell', 'shell_command', 'exec_command', 'local_shell', 'container.exec', 'unified_exec')
TEXT_ITEMS = ('input_text', 'output_text', 'text')
INDEX = 'session_index.jsonl'
TITLE_IDX = 1000
WHEN_RE = re.compile(r'(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?(Z|[+-]\d\d:\d\d)?')
_names = {}       # index path -> ((mtime_ns, size), {thread id: name})


def patch_paths(text):
    """The file paths an apply_patch text touches, in order, once each. Empty unless the text holds a patch."""
    if not isinstance(text, str) or '*** Begin Patch' not in text:
        return []
    out = []
    for m in PATCH_RE.finditer(text):
        p = m.group(1).strip()
        if p and p not in out:
            out.append(p)
    return out


def strings_in(v):
    """Every string inside a JSON value."""
    if isinstance(v, str):
        yield v
    elif isinstance(v, dict):
        for x in v.values():
            yield from strings_in(x)
    elif isinstance(v, list):
        for x in v:
            yield from strings_in(x)


def input_paths(tool_input):
    """Patch paths from a hook's tool_input (apply_patch, or a shell command that pipes a patch to apply_patch)."""
    out = []
    for s in strings_in(tool_input):
        for p in patch_paths(s):
            if p not in out:
                out.append(p)
    return out


def exec_commands(code):
    """The cmd values of tools.exec_command({cmd: "..."}) calls in a code-mode exec input."""
    out = []
    for m in CMD_RE.finditer(code or ''):
        try:
            out.append(str(json.loads(m.group(1))))
        except ValueError:
            out.append(m.group(1)[1:-1])
    return out


def shell_command(args):
    """The command of a shell function call's arguments: cmd or command, a string or an argv list."""
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
    """The session_meta payload on the first line of a rollout, or {}."""
    try:
        with open(path, 'rb') as fh:
            rec = json.loads(fh.readline(8_000_000))
        if rec.get('type') == 'session_meta' and isinstance(rec.get('payload'), dict):
            return rec['payload']
    except Exception:
        pass
    return {}


def agent_for(cfg, sid, parent=''):
    """The agent id of a Codex thread. A session: CTRLB_AGENT or its session id. A subagent thread: its own thread id,
    prefixed with the CTRLB_AGENT label and ':' when one is set."""
    label = cfg.get('agent') or ''
    if parent:
        return f'{label}:{sid}' if label else sid
    return label or sid or ''


@functools.lru_cache(maxsize=4)
def _session_files(root):
    """Index filenames once per process; backfill must not reopen every session for each child."""
    files = {}
    for file in rollouts(root):
        match = UUID_RE.search(os.path.basename(file))
        if match:
            files[match.group(1)] = file
    return files


def session_run(cfg, sid, parent='', path=''):
    """Resolve the ancestor chain once for both uploads and hooks. A missing ancestor falls back to the oldest
    known thread; cycles stop without looping. An explicit run always wins."""
    if cfg.get('run') or not parent:
        return common.run_id(cfg, sid, parent)
    return root_session(sid, parent, path)


def root_session(sid, parent='', path=''):
    """The session id at the top of a thread's ancestor chain (sid itself when it has no parent)."""
    return root_rollout(sid, parent, path)[0]


def root_rollout(sid, parent='', path=''):
    """(session id, rollout file) at the top of a thread's ancestor chain; the file is path for a thread with no
    parent, and '' when the root's file is not found."""
    file = path
    seen = {sid}
    while parent and parent not in seen and len(seen) < 64:
        seen.add(parent)
        sid, parent = parent, ''
        # A sibling named directly by id is also useful for exported rollouts. Only safe filename components
        # may be used here; normal Codex filenames are found by their UUID suffix, without reading other files.
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
    """(run_id, agent) of the last ctrlb relay tool call that writes to a run, from a Codex rollout's McpToolCall
    items (server ctrlb), or None. Codex has no hook after a tool call, so this is how a Codex session is known to be in
    a run. A rollout only grows: the part read is kept under <CTRLB_HOME>/spawn/rollouts (its offset, its inode and
    the 64 bytes before the offset, so a replaced or rewritten file is read again from the start), and each call reads
    only what was added since the last one."""
    try:
        st = os.stat(path)
    except (OSError, TypeError, ValueError):
        return None
    keep, c, last = '', {}, None
    try:      # without the kept part (a folder that cannot be made, a bad file) the whole rollout is read
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
            if ok and c['offset']:       # the bytes just before the offset are still the ones read (not a rewritten file)
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
                        break           # a line still being written is read next time
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
    except TimeoutError:      # the hook's time ran out: what was found so far, the kept part included
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
    """The JSON object a completed McpToolCall answered, or None: a failed call (status failed, an error), an error
    answer (isError) and text that is not a JSON object are no answer."""
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
    """Every ctrlb claim call of a Codex rollout with its answer, in order: [(arguments, answer)]. A McpToolCall item
    holds the call (arguments) and, once completed, its answer (result) under one item id; an item seen again (started,
    then completed) is matched by that id and keeps its last state. answer is the JSON object the claim answered, or
    None (still running, failed, an error answer, or text that does not parse). thread keeps only the items of that
    thread when an event names one."""
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
    r = dict(_timestamp=ts, host=hostname, harness=HARNESS, session_id=ids['sid'], parent_session=ids['parent'],
             agent_id=ids['agent_id'], run_id=ids['run_id'], agent=ids['agent'], seq=seq, block_idx=0, role=role,
             block_type='', tool_name='', tool_use_id='', command='', paths='', body='', is_error=False,
             is_summary=False, cwd=st.get('cwd') or '', git_branch=st.get('branch') or '', cost_usd=0.0,
             input_tokens=0.0, output_tokens=0.0)
    if cfg.get('user'):   # the key's own user (common.key_user), never CTRLB_USER; without it CtrlB fills user_name
        r['user_name'] = cfg['user']
    return r


def _num(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


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
    else:                                   # local_shell_call
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


def _when(s):
    """An updated_at value as µs since the epoch (any number of fraction digits); 0 when it is not such a time."""
    m = WHEN_RE.fullmatch(s.strip()) if isinstance(s, str) else None
    if not m:
        return 0
    return micros(f"{m.group(1)}.{(m.group(2) or '')[:6].ljust(6, '0')}{m.group(3) or 'Z'}")


def thread_names(home=None):
    """{thread id: name} from <CODEX_HOME>/session_index.jsonl: for each id the line with the newest updated_at, the
    later line on a tie (a line without a readable updated_at counts as oldest). Names are stripped; '' when the newest
    is empty. Lines that are not {id, thread_name} objects are skipped; no file is {}. Read again only when the file's
    mtime or size changes."""
    path = os.path.join(home or codex_home(), INDEX)
    try:
        st = os.stat(path)
    except OSError:
        return {}
    key = (st.st_mtime_ns, st.st_size)
    if path in _names and _names[path][0] == key:
        return _names[path][1]
    best = {}
    try:
        with open(path, 'rb') as fh:
            for raw in fh:
                try:
                    r = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(r, dict) or not isinstance(r.get('id'), str) or not isinstance(r.get('thread_name'), str):
                    continue
                when = _when(r.get('updated_at'))
                if r['id'] and (r['id'] not in best or when >= best[r['id']][0]):
                    best[r['id']] = (when, r['thread_name'].strip())
    except OSError:
        return {}
    _names[path] = (key, {k: name for k, (_, name) in best.items()})
    return _names[path][1]


def _write_title(cfg, path, deadline, ids, backfill, send=True):
    """Write the thread's current name as a title row when it differs from the file state's title (an upload; a
    refused name counts as written) or title_ok (backfill: the last name CtrlB accepted). Only a main thread whose
    rollout was uploaded before and has a line with a time. Returns 1 when a row was sent (or, without send, would
    be), else 0. A refusal naming block_type is logged and marked written in an upload, raised in a backfill; other
    failures are raised and leave the state as it was."""
    name = '' if ids['parent'] else thread_names().get(ids['sid']) or ''
    if not name:
        return 0
    fs = common.FileState(path, deadline)
    if not os.path.exists(fs.file):
        return 0
    with fs:
        d = fs.data
        if (not d['line'] or os.path.getsize(path) < d['offset'] or d['prev_ts'] < rows_claude.TIME_MIN
                or name == d.get('title_ok' if backfill else 'title')):
            return 0
        if not send:
            return 1
        remaining = deadline - time.time()
        if remaining <= 0:
            if backfill:
                raise TimeoutError('title backfill ran out of time')
            return 0
        n = int(d.get('title_rows') or 0)
        r = _row(cfg, ids, d, common.host(), d['prev_ts'] + 1, d['line'] - 1, 'system')
        r.update(block_idx=TITLE_IDX + n, block_type='title', body=name)
        rows_claude._finish(r, dict(redacted=0))
        try:
            common.ingest_records(cfg, [r], timeout=remaining)
        except common.RecordsRefused as e:
            if backfill or not e.names('block_type'):
                raise
            common.log(f'upload {os.path.basename(path)}: CtrlB refused block_type "title" (HTTP 400), so it is older '
                       'than session titles; title row skipped, backfill.py --titles --codex sends it later')
            d.update(title=name, title_rows=n + 1)
            fs.save()
            return 0
        d.update(title=name, title_ok=name, title_rows=n + 1)
        fs.save()
        return 1


def upload_file(cfg, path, deadline):
    """Send the complete lines of a rollout added since the last upload, then, when lines were sent, the thread's
    name if it changed (see the module text). Returns {rows, lines, redacted}."""
    if not path or not os.path.isfile(path):
        return dict(rows=0, lines=0, redacted=0)
    ids = ids_for_file(cfg, path)
    c = rows_claude.upload_file(cfg, path, deadline, ids=ids, rows_fn=rows_from)
    if c['lines'] and not ids['parent']:
        c['rows'] += _write_title(cfg, path, deadline, ids, backfill=False)
    return c


def upload_titles(cfg, path, deadline, send=True):
    """backfill.py --titles --codex: the thread's current name for a rollout already uploaded, when CtrlB has not
    accepted it yet (named, renamed or refused since the last upload, or uploaded before title rows), at the same seq,
    block_idx and time an upload would give it. The offset stays as it is. Returns 1 when a row was sent (or, without
    send, would be), else 0. Raises common.RecordsRefused when CtrlB refuses it."""
    if not path or not os.path.isfile(path) or read_meta(path).get('parent_thread_id'):
        return 0
    return _write_title(cfg, path, deadline, ids_for_file(cfg, path), backfill=True, send=send)


def codex_home():
    return os.path.expanduser(os.environ.get('CODEX_HOME') or '~/.codex')


def rollouts(root=None):
    """Every rollout file under $CODEX_HOME/sessions, oldest first."""
    root = root or os.path.join(codex_home(), 'sessions')
    return sorted(glob.glob(os.path.join(root, '**', 'rollout-*.jsonl'), recursive=True))
