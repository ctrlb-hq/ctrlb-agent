"""Codex rollout -> session rows (relay-plan section 5), the same columns as rows_claude with harness = "codex".

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
run (decision 4). Upload is incremental by byte offset with FileState, through rows_claude.upload_file."""
import glob, json, os, re, shlex
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


def ids_for_file(cfg, path):
    meta = read_meta(path)
    m = UUID_RE.search(os.path.basename(path))
    sid = str(meta.get('id') or meta.get('session_id') or (m.group(1) if m else os.path.basename(path)))
    parent = str(meta.get('parent_thread_id') or '')
    return dict(sid=sid, parent=parent, agent_id=sid if parent else '', run_id=common.run_id(cfg, sid, parent),
                agent=agent_for(cfg, sid, parent))


def _text(o):
    if isinstance(o, str):
        return o
    if isinstance(o, list):
        return '\n'.join(x.get('text') or '' for x in o if isinstance(x, dict) and x.get('type') in TEXT_ITEMS)
    if isinstance(o, dict):
        return o.get('content') if isinstance(o.get('content'), str) else json.dumps(o)
    return ''


def _failed(o, text):
    if isinstance(o, dict) and o.get('success') is False:
        return True
    if text.startswith('Script failed'):
        return True
    if text.startswith('{'):
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
    if cfg.get('user'):
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
        name, raw = p.get('name') or '', p.get('input') if isinstance(p.get('input'), str) else json.dumps(p.get('input'))
        r['command'] = '\n'.join(exec_commands(raw)) if name == 'exec' else ''
        r['body'] = raw
        paths = patch_paths(raw)
    elif t == 'function_call':
        name, raw = p.get('name') or '', p.get('arguments') or ''
        try:
            args = json.loads(raw) if isinstance(raw, str) else raw
        except ValueError:
            args = {}
        r['command'] = shell_command(args) if name in SHELL_TOOLS or name.endswith('exec_command') else ''
        r['body'] = r['command'] or (raw if isinstance(raw, str) else json.dumps(raw))
        paths = input_paths(args) if isinstance(args, (dict, list)) else patch_paths(raw)
    else:                                   # local_shell_call
        name = 'local_shell'
        action = p.get('action') or {}
        r['command'] = shell_command(action)
        r['body'] = r['command'] or json.dumps(action)
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
        r.update(block_type='result', body=json.dumps(u), input_tokens=_num(u.get('input_tokens')),
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
    """Send the complete lines of a rollout added since the last upload. Returns {rows, lines, redacted}."""
    if not path or not os.path.isfile(path):
        return dict(rows=0, lines=0, redacted=0)
    return rows_claude.upload_file(cfg, path, deadline, ids=ids_for_file(cfg, path), rows_fn=rows_from)


def codex_home():
    return os.path.expanduser(os.environ.get('CODEX_HOME') or '~/.codex')


def rollouts(root=None):
    """Every rollout file under $CODEX_HOME/sessions, oldest first."""
    root = root or os.path.join(codex_home(), 'sessions')
    return sorted(glob.glob(os.path.join(root, '**', 'rollout-*.jsonl'), recursive=True))
