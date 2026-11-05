"""Claude Code transcript -> session rows (relay-plan section 5), one per content block, and incremental upload to
Seeker with redaction. seq is the line number in the transcript file, so a row can be traced back to its place.

Beyond the content blocks:
  run_id, agent   decision 4 (common.run_id, common.agent_name)
  user_name       only the key's own user (common.key_user); omitted when the key carries none, and Seeker fills it
  paths           JSON array text of the file paths an edit tool touched (Edit, Write, MultiEdit, NotebookEdit),
                  on tool_use rows, normalised against the row's cwd by paths.edit_paths so Seeker keys them as the
                  claims are keyed; '' on every other row
  input_tokens,   from message.usage, once per message id (Claude Code repeats the usage on every block line of one
  output_tokens   message); input_tokens includes cache reads and cache writes. Floats; 0.0 elsewhere
  cost_usd        on block_type "result" rows: the increase of the session's cumulative cost since the previous
                  cost record (a transcript "cost-state" record, or a stream-json "result" record), so the sum of
                  cost_usd over a session's rows is its cost. Float; 0.0 elsewhere"""
import datetime, glob, json, os, time
import common
from paths import edit_paths as normalise_edit_paths
from redact import redact

SUMMARY_PREFIX = 'This session is being continued from a previous conversation'
SKIP_TYPES = {'mode', 'permission-mode', 'bridge-session', 'custom-title', 'agent-name', 'queue-operation',
              'file-history-snapshot', 'file-history-delta', 'last-prompt', 'atis-latch', 'tools-used', 'attachment'}
EDIT_TOOLS = ('Edit', 'Write', 'MultiEdit', 'NotebookEdit')
BODY_CAP = 64 * 1024
BATCH_ROWS, BATCH_BYTES = 500, 3_500_000   # Seeker takes at most 500 rows or 4 MB per records call


def micros(ts):
    try:
        return int(datetime.datetime.fromisoformat(ts.replace('Z', '+00:00')).timestamp() * 1_000_000)
    except Exception:
        return 0


def edit_paths(tool_name, tool_input):
    """The file paths an edit tool call touches, as given in its input."""
    if tool_name not in EDIT_TOOLS or not isinstance(tool_input, dict):
        return []
    p = tool_input.get('notebook_path' if tool_name == 'NotebookEdit' else 'file_path')
    return [p] if isinstance(p, str) and p else []


def _blocks(rec):
    m = rec.get('message')
    if not isinstance(m, dict):
        return []
    c = m.get('content')
    if isinstance(c, str):
        return [{'type': 'text', 'text': c}]
    return [b for b in c if isinstance(b, dict)] if isinstance(c, list) else []


def _result_text(c):
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return '\n'.join(x.get('text', '') for x in c if isinstance(x, dict) and x.get('type') in ('text', 'input_text', 'output_text'))
    return ''


def _is_summary(rec):
    if rec.get('type') != 'user':
        return False
    if rec.get('isCompactSummary'):
        return True
    return any(b.get('type') == 'text' and (b.get('text') or '').startswith(SUMMARY_PREFIX) for b in _blocks(rec))


def _num(v):
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def ids_for(path):
    """(session_id, parent_session, agent_id) from a transcript path. Subagent files sit at
    <project>/<parent session id>/subagents/agent-<id>.jsonl."""
    base = os.path.basename(path)[:-6] if path.endswith('.jsonl') else os.path.basename(path)
    d = os.path.dirname(path)
    if os.path.basename(d) == 'subagents':
        return base, os.path.basename(os.path.dirname(d)), base[len('agent-'):] if base.startswith('agent-') else base
    return base, '', ''


def ids_for_file(cfg, path):
    sid, parent, agent_id = ids_for(path)
    return dict(sid=sid, parent=parent, agent_id=agent_id, run_id=common.run_id(cfg, sid, parent),
                agent=common.agent_name(cfg, sid, agent_id and sid))


def _base(cfg, ids, hostname, rec, ts, seq, parent, role, summ):
    r = dict(_timestamp=ts, host=hostname, harness='claude', session_id=ids['sid'], parent_session=parent,
             agent_id=ids['agent_id'], run_id=ids['run_id'], agent=ids['agent'], seq=seq, block_idx=0, role=role,
             block_type='', tool_name='', tool_use_id='', command='', paths='', body='', is_error=False,
             is_summary=summ, cwd=rec.get('cwd') or '', git_branch=rec.get('gitBranch') or '',
             cost_usd=0.0, input_tokens=0.0, output_tokens=0.0)
    if cfg.get('user'):   # the key's own user (common.key_user), never CTRLB_USER; without it Seeker fills user_name
        r['user_name'] = cfg['user']
    return r


def _cost_row(rec, seq, st, ids, cfg, hostname):
    t = rec.get('type')
    total = _num(rec.get('totalCostUSD') if t == 'cost-state' else rec.get('total_cost_usd'))
    ts = micros(rec['timestamp']) if rec.get('timestamp') else st['prev_ts'] + 1
    st['prev_ts'] = ts
    prev = _num(st.get('cost'))
    st['cost'] = total
    parent = ids['parent'] or (rec.get('sessionId') if ids['agent_id'] else '') or ''
    r = _base(cfg, ids, hostname, rec, ts, seq, parent, 'system', False)
    r['cwd'] = r['cwd'] or st.get('cwd', '')            # cost records carry no cwd or branch: use the last seen
    r['git_branch'] = r['git_branch'] or st.get('branch', '')
    r['block_type'] = 'result'
    r['cost_usd'] = round(total - prev if total >= prev else total, 8)
    if t == 'result':
        r['body'] = str(rec.get('result') or '')
        r['is_error'] = bool(rec.get('is_error'))
    else:
        r['body'] = json.dumps({k: v for k, v in rec.items() if k not in ('type', 'sessionId')})
    return [r]


def rows_from(rec, seq, st, ids, cfg, hostname):
    t = rec.get('type', '')
    if t in ('cost-state', 'result'):
        return _cost_row(rec, seq, st, ids, cfg, hostname)
    if t in SKIP_TYPES or t.startswith('api-request') or t not in ('user', 'assistant', 'system'):
        return []
    if not isinstance(rec.get('message'), dict):
        return []
    ts = micros(rec['timestamp']) if rec.get('timestamp') else st['prev_ts'] + 1
    st['prev_ts'] = ts
    if rec.get('cwd'):
        st['cwd'] = rec['cwd']
    if rec.get('gitBranch'):
        st['branch'] = rec['gitBranch']
    m, summ = rec['message'], _is_summary(rec)
    parent = ids['parent'] or (rec.get('sessionId') if ids['agent_id'] else '') or ''
    out = []
    for bi, b in enumerate(_blocks(rec)):
        bt = b.get('type', '')
        r = _base(cfg, ids, hostname, rec, ts, seq, parent, m.get('role') or t, summ)
        r['block_idx'], r['block_type'] = bi, bt
        if bt == 'text':
            r['body'] = b.get('text') or ''
        elif bt == 'thinking':
            r['body'] = b.get('thinking') or ''
            if not r['body']:
                continue
        elif bt == 'tool_use':
            inp = b.get('input') or {}
            r['tool_name'] = b.get('name') or ''
            r['tool_use_id'] = b.get('id') or ''
            st['names'][r['tool_use_id']] = r['tool_name']
            r['command'] = str(inp.get('command', '')) if r['tool_name'] == 'Bash' else ''
            p = normalise_edit_paths(edit_paths(r['tool_name'], inp), r['cwd'])
            r['paths'] = json.dumps(p) if p else ''
            r['body'] = r['command'] or json.dumps(inp)
        elif bt == 'tool_result':
            r['tool_use_id'] = b.get('tool_use_id') or ''
            r['tool_name'] = st['names'].get(r['tool_use_id'], '')
            r['body'] = _result_text(b.get('content'))
            r['is_error'] = bool(b.get('is_error'))
        else:
            continue
        out.append(r)
    u = m.get('usage')
    if out and t == 'assistant' and isinstance(u, dict):
        mid = m.get('id') or ''
        mids = st.setdefault('mids', [])
        if not mid or mid not in mids:
            out[0]['input_tokens'] = _num(u.get('input_tokens')) + _num(u.get('cache_read_input_tokens')) + \
                _num(u.get('cache_creation_input_tokens'))
            out[0]['output_tokens'] = _num(u.get('output_tokens'))
            if mid:
                mids.append(mid)
    return out


def _finish(r, counts):
    n = 0
    for k in ('body', 'command', 'paths'):
        r[k], k_n = redact(r[k])
        n += k_n
    counts['redacted'] += n
    raw = r['body'].encode()
    r['body_len'] = len(raw)
    r['truncated'] = len(raw) > BODY_CAP
    if r['truncated']:
        r['body'] = raw[:BODY_CAP].decode(errors='ignore')
    return r


def upload_file(cfg, path, deadline, ids=None, rows_fn=None):
    """Send the complete lines of path added since the last upload. Returns {rows, lines, redacted}. The offset only
    moves forward after Seeker accepts the rows, so a failed upload is retried next time. ids and rows_fn default to
    the Claude Code parser; rows_codex passes its own (rows_fn(rec, seq, state, ids, cfg, hostname) -> [row])."""
    counts = dict(rows=0, lines=0, redacted=0)
    if not path or not os.path.isfile(path):
        return counts
    ids = ids if ids is not None else ids_for_file(cfg, path)
    rows_fn = rows_fn or rows_from
    hostname = common.host()
    with common.FileState(path, deadline) as fs:
        st = fs.data
        size = os.path.getsize(path)
        if size < st['offset']:            # file replaced or truncated: start again
            fs.reset()
        if size == st['offset']:
            return counts
        with open(path, 'rb') as fh:
            fh.seek(st['offset'])
            chunk = fh.read()
        end = chunk.rfind(b'\n')
        if end < 0:
            return counts
        batch, nbytes = [], 0
        offset, line = st['offset'], st['line']
        first_line = line

        def send(rows):
            common.ingest_records(cfg, rows, timeout=max(2, deadline - time.time()))
            counts['rows'] += len(rows)

        for raw in chunk[:end + 1].split(b'\n')[:-1]:
            try:
                rec = json.loads(raw)
            except Exception:
                rec = None
            line_rows = [_finish(r, counts) for r in rows_fn(rec, line, st, ids, cfg, hostname)] if isinstance(rec, dict) else []
            size = sum(len(json.dumps(r)) + 1 for r in line_rows)
            if batch and (len(batch) + len(line_rows) > BATCH_ROWS or nbytes + size > BATCH_BYTES):
                send(batch)                    # everything before this line: the offset may move to its start
                batch, nbytes = [], 0
                st['offset'], st['line'] = offset, line
                fs.save()
            batch += line_rows
            nbytes += size
            while len(batch) > BATCH_ROWS:     # one line alone over the row cap
                send(batch[:BATCH_ROWS])
                batch = batch[BATCH_ROWS:]
                nbytes = sum(len(json.dumps(r)) + 1 for r in batch)
            line += 1
            offset += len(raw) + 1
        if batch:
            send(batch)
        counts['lines'] = line - first_line
        st['offset'], st['line'] = offset, line
        fs.save()
    return counts


def session_files(transcript_path):
    """The main transcript and its subagent transcripts."""
    if not transcript_path:
        return []
    out = [transcript_path]
    if transcript_path.endswith('.jsonl'):
        out += sorted(glob.glob(os.path.join(transcript_path[:-6], 'subagents', '*.jsonl')))
    return out
