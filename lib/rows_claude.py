"""Claude Code transcript -> session rows, one per content block, and incremental upload to
CtrlB with redaction. seq is the line number in the transcript file, so a row can be traced back to its place.

Beyond the content blocks:
  run_id, agent   resolved by common.run_id and common.agent_name
  user_name       only the key's own user (common.key_user); omitted when the key carries none, and CtrlB fills it
  paths           JSON array text of the file paths an edit tool touched (Edit, Write, MultiEdit, NotebookEdit),
                  on tool_use rows, normalised against the row's cwd by paths.edit_paths so CtrlB keys them as the
                  claims are keyed; '' on every other row
  input_tokens,   from message.usage, once per message id (Claude Code repeats the usage on every block line of one
  output_tokens   message); input_tokens includes cache reads and cache writes. Floats; 0.0 elsewhere
  cost_usd        on block_type "result" rows: the increase of the session's cumulative cost since the previous
                  cost record (a transcript "cost-state" record, or a stream-json "result" record), so the sum of
                  cost_usd over a session's rows is its cost. Float; 0.0 elsewhere

Session title: Claude Code writes the title the user sets as {"type": "custom-title", "customTitle": ...} lines and
the one it generates as {"type": "ai-title", "aiTitle": ...} lines, with no timestamp and repeated many times. A
custom title wins: once a file has had one, later ai-title lines are ignored. The first title of a file, and each
change, is one row with role "system", block_type "title", block_idx 0 and the title as body; a repeat writes nothing.
Its seq is the title line's own and its time that of the last message or cost record read + 1 µs (other lines with a
timestamp do not count); a title before any such line with a time waits, keeping its seq, for the first one and
takes its time. Title rows go to CtrlB in a call of their own, so an older CtrlB that refuses block_type "title"
refuses only them."""
import copy, datetime, glob, itertools, json, os, time
import common
from paths import edit_paths as normalise_edit_paths
from redact import redact

SUMMARY_PREFIX = 'This session is being continued from a previous conversation'
TITLE_TYPES = {'custom-title': 'customTitle', 'ai-title': 'aiTitle'}     # line type -> the key holding the title
SKIP_TYPES = {'mode', 'permission-mode', 'bridge-session', 'agent-name', 'queue-operation',
              'file-history-snapshot', 'file-history-delta', 'last-prompt', 'atis-latch', 'tools-used', 'attachment'}
EDIT_TOOLS = ('Edit', 'Write', 'MultiEdit', 'NotebookEdit')
BODY_CAP = 64 * 1024
BATCH_ROWS, BATCH_BYTES = 500, 3_500_000   # CtrlB takes at most 500 rows or 4 MB per records call
# 2020-01-01 in µs, where CtrlB's session reads start. A prev_ts below it was never read from a line (a line with no
# time at the start of a file counts on from 0), so a title waits for a real time instead of taking it.
TIME_MIN = 1_577_836_800_000_000


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
    if cfg.get('user'):   # the key's own user (common.key_user), never CTRLB_USER; without it CtrlB fills user_name
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
        r['body'] = {k: v for k, v in rec.items() if k not in ('type', 'sessionId')}
    return _titles_due(rec, st, ids, cfg, hostname) + [r]


def _title_row(rec, seq, title, ts, st, ids, cfg, hostname):
    parent = ids['parent'] or (rec.get('sessionId') if ids['agent_id'] else '') or ''
    r = _base(cfg, ids, hostname, rec, ts, seq, parent, 'system', False)
    r['cwd'] = r['cwd'] or st.get('cwd', '')            # title records carry no cwd or branch: use the last seen
    r['git_branch'] = r['git_branch'] or st.get('branch', '')
    r['block_type'] = 'title'
    r['body'] = title
    return r


def _title(rec, seq, st, ids, cfg, hostname):
    """A custom-title or ai-title line: a row when the title is new for the file or changed, nothing for a repeat or
    for an ai-title after a custom title. Before any line with a time, the title waits in st['title_wait'] (see
    _titles_due)."""
    t = rec.get('type')
    title = rec.get(TITLE_TYPES[t])
    title = title.strip() if isinstance(title, str) else ''
    if not title or (t == 'ai-title' and st.get('title_custom')):
        return []
    if t == 'custom-title':
        st['title_custom'] = True
    if title == st.get('title'):
        return []
    st['title'] = title
    if st['prev_ts'] < TIME_MIN:              # titles wait only while no time is known, so they stay in order
        st['title_wait'] = (st.get('title_wait') or []) + [[seq, title]]
        return []
    ts = st['prev_ts'] + 1
    st['prev_ts'] = ts
    return [_title_row(rec, seq, title, ts, st, ids, cfg, hostname)]


def _titles_due(rec, st, ids, cfg, hostname):
    """The waiting titles, once st['prev_ts'] holds a time read from a line: each keeps its own seq and takes that
    line's time and cwd."""
    wait = st.get('title_wait')
    if not wait or st['prev_ts'] < TIME_MIN:
        return []
    st['title_wait'] = []
    return [_title_row(rec, seq, title, st['prev_ts'], st, ids, cfg, hostname) for seq, title in wait]


def rows_from(rec, seq, st, ids, cfg, hostname):
    t = rec.get('type', '')
    if t in TITLE_TYPES:
        return _title(rec, seq, st, ids, cfg, hostname)
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
    due, out = _titles_due(rec, st, ids, cfg, hostname), []
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
            r['body'] = r['command'] or inp
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
    return due + out


def _finish(r, counts):
    n = 0
    for k in ('body', 'command', 'paths'):
        r[k], k_n = redact(r[k])
        if not isinstance(r[k], str):
            r[k] = json.dumps(r[k], ensure_ascii=False)
        # A lone surrogate (from a \ud83d escape in a line) cannot be encoded: it becomes '?', so it cannot stop
        # the upload.
        r[k] = r[k].encode('utf-8', 'replace').decode('utf-8')
        n += k_n
    counts['redacted'] += n
    raw = r['body'].encode()
    r['body_len'] = len(raw)
    r['truncated'] = len(raw) > BODY_CAP
    if r['truncated']:
        r['body'] = raw[:BODY_CAP].decode(errors='ignore')
    return r


def _merged(seqs, more):
    return list(dict.fromkeys(list(seqs or []) + list(more)))


def _size(r):
    """A row's bytes in a records call, with its separator."""
    return len(json.dumps(r).encode()) + 2


def log_title_refusal(path, rerun):
    common.log(f'upload {os.path.basename(path)}: CtrlB refused block_type "title" (HTTP 400), so it is older than '
               f'session titles; title rows skipped. Run {rerun} after upgrading CtrlB to send them.')


def _fits(sized):
    """How many of the leading (row, size) pairs fit in one records call; at least one."""
    n, total = 0, 2
    while n < len(sized) and n < BATCH_ROWS and total + sized[n][1] <= BATCH_BYTES:
        total += sized[n][1]
        n += 1
    return max(n, 1)


def upload_file(cfg, path, deadline, ids=None, rows_fn=None):
    """Send the complete lines of path added since the last upload. Returns {rows, lines, redacted}. The offset only
    moves forward after CtrlB accepts the rows, so a failed upload is retried next time. ids and rows_fn default to
    the Claude Code parser; rows_codex passes its own (rows_fn(rec, seq, state, ids, cfg, hostname) -> [row]).

    Title rows (block_type "title") wait until their line is complete and go in calls of their own, sent before the
    transcript rows of every checkpoint. The seqs CtrlB accepts are saved at once (title_seqs), and a title row whose
    seq is there is not sent again, so a retry after a later failure does not duplicate it. When CtrlB refuses title
    rows with a 400 naming block_type (a CtrlB older than title rows), they and the rest of this upload's title rows
    are skipped with one line in the hook log and the transcript rows go on; their title is in the parse state, which
    the next checkpoint saves, so later uploads do not send them again (backfill.py --titles does). Any other failure
    stops the upload without moving the offset."""
    counts = dict(rows=0, lines=0, redacted=0)
    until = time.monotonic() + max(0, deadline - time.time())
    if not path or not os.path.isfile(path) or time.monotonic() >= until:
        return counts
    ids = ids if ids is not None else ids_for_file(cfg, path)
    rows_fn = rows_fn or rows_from
    hostname = common.host()
    with common.FileState(path, deadline) as fs:
        if os.path.getsize(path) < fs.data['offset']:
            fs.reset()
        pending = copy.deepcopy(fs.data)
        first_line = pending['line']
        batch, nbytes = [], 2             # JSON array brackets; each row includes its separator
        titles, accepted = [], []         # (title row, size) of completed lines; seqs of the title rows CtrlB accepted
        refused = False                   # CtrlB refused title rows in this upload: skip the rest

        def checkpoint(state):
            fs.data = copy.deepcopy(state)
            if accepted:
                fs.data['title_seqs'] = _merged(fs.data.get('title_seqs'), accepted)
            fs.save()
            counts['lines'] = state['line'] - first_line

        def send(rows):
            remaining = until - time.monotonic()
            if remaining <= 0:
                return False
            common.ingest_records(cfg, rows, timeout=remaining)
            counts['rows'] += len(rows)
            return True

        def send_titles():
            nonlocal refused
            while titles and not refused:
                n = _fits(titles)
                rows = [r for r, _ in titles[:n]]
                try:
                    if not send(rows):
                        return False
                except common.RecordsRefused as e:
                    if not e.names('block_type'):
                        raise
                    refused = True
                    log_title_refusal(path, 'backfill.py --titles')
                    break
                accepted.extend(r['seq'] for r in rows)
                # Saved now, with the offset of the last checkpoint: should a later call fail, the retry parses these
                # lines again and must not send these rows twice.
                fs.data['title_seqs'] = _merged(fs.data.get('title_seqs'), accepted)
                fs.save()
                del titles[:n]
            titles.clear()
            return True

        with open(path, 'rb') as fh:
            fh.seek(pending['offset'])
            while time.monotonic() < until:
                # Read bounded chunks, checking the budget even within a very long line. An incomplete last
                # line stays at its old offset until a later hook sees its terminating newline.
                pieces = []
                while time.monotonic() < until:
                    raw = fh.readline(1024 * 1024)
                    if not raw:
                        break
                    pieces.append(raw)
                    if raw.endswith(b'\n'):
                        break
                if not pieces or not pieces[-1].endswith(b'\n') or time.monotonic() >= until:
                    break
                raw = b''.join(pieces)
                candidate = copy.deepcopy(pending)
                try:
                    rec = json.loads(raw)
                except ValueError:
                    rec = None
                line_rows = [_finish(r, counts) for r in rows_fn(rec, pending['line'], candidate, ids, cfg, hostname)] if isinstance(rec, dict) else []
                sizes = []
                for r in line_rows:
                    size = _size(r)
                    if size + 2 > BATCH_BYTES:
                        # Body truncation normally suffices; a shell command or metadata can also be huge.
                        for k, v in r.items():
                            if isinstance(v, str):
                                r[k] = v.encode()[:BODY_CAP].decode(errors='ignore')
                        r['truncated'] = True
                        size = _size(r)
                        while size + 2 > BATCH_BYTES:
                            k = max((k for k, v in r.items() if isinstance(v, str)),
                                    key=lambda k: len(json.dumps(r[k])))
                            r[k] = r[k][:len(r[k]) // 2]
                            size = _size(r)
                    sizes.append(size)
                line_titles = [(r, size) for r, size in zip(line_rows, sizes) if r['block_type'] == 'title']
                if line_titles:
                    kept = [(r, size) for r, size in zip(line_rows, sizes) if r['block_type'] != 'title']
                    line_rows, sizes = [r for r, _ in kept], [size for _, size in kept]
                start = pending.get('row', 0)
                line_rows, sizes = line_rows[start:], sizes[start:]
                if batch and (len(batch) + len(line_rows) > BATCH_ROWS or nbytes + sum(sizes) > BATCH_BYTES):
                    if not send_titles() or not send(batch):
                        return counts
                    checkpoint(pending)   # candidate has not entered this acknowledged batch
                    batch, nbytes = [], 2
                candidate.update(offset=fh.tell(), line=pending['line'] + 1, row=0)
                for i, (r, size) in enumerate(zip(line_rows, sizes)):
                    if batch and (len(batch) == BATCH_ROWS or nbytes + size > BATCH_BYTES):
                        if not send_titles() or not send(batch):
                            return counts
                        # A line can span batches. Keep its pre-parse state and a row cursor until every row
                        # has been accepted, so retries neither lose usage state nor resend accepted blocks.
                        pending['row'] = start + i
                        checkpoint(pending)
                        batch, nbytes = [], 2
                    batch.append(r)
                    nbytes += size
                pending = candidate
                # A line's title rows wait for the line's end: a checkpoint inside the line keeps the state from
                # before it, so a retry parses them again, and they must not have been sent.
                done = set(fs.data.get('title_seqs') or [])
                titles.extend((r, size) for r, size in line_titles if r['seq'] not in done)
            if not send_titles() or (batch and not send(batch)):
                return counts
            checkpoint(pending)
    return counts


def _title_rows(cfg, path, upto):
    """The title rows a parse from the first line gives for the first upto lines of path (what upload_file writes
    for a file it uploaded from its first line), and the parse state after them; ([], None) when none of those lines
    holds a title."""
    marks = tuple(t.encode() for t in TITLE_TYPES)
    with open(path, 'rb') as fh:
        if not any(m in raw for raw in itertools.islice(fh, upto) for m in marks):
            return [], None
        fh.seek(0)
        ids, hostname = ids_for_file(cfg, path), common.host()
        st = common.FileState.fresh()
        rows = []
        for seq, raw in enumerate(itertools.islice(fh, upto)):
            try:
                rec = json.loads(raw)
            except ValueError:
                continue
            if isinstance(rec, dict):
                rows += [r for r in rows_from(rec, seq, st, ids, cfg, hostname) if r['block_type'] == 'title']
                if len(st['mids']) > 400:        # token counts are not needed here; keep the lookups short
                    del st['mids'][:-200]
    return rows, st


def upload_titles(cfg, path, deadline, send=True):
    """backfill.py --titles: the title rows of the lines already uploaded (before the file's offset) that CtrlB has
    not accepted (title_seqs in the file state), as a parse from the first line gives them. Files uploaded before
    title rows existed, and titles an older CtrlB refused, have them. With send, they go to CtrlB in calls of their
    own and their seqs are added to title_seqs; the offset and the rest of the upload state stay as they are, except
    that a state that tracks no title yet takes the title its lines end with, so the next upload sends only a change,
    and one that saw no custom title learns of one. Returns the number of title rows found (send false) or sent.
    Raises common.RecordsRefused when CtrlB refuses them, TimeoutError when the deadline passes (or another upload
    keeps the state file busy), and what ingest_records raises on other failures."""
    if not path or not os.path.isfile(path):
        return 0
    fs = common.FileState(path, deadline)
    if not os.path.exists(fs.file):          # never uploaded: its next upload writes its titles
        return 0
    until = time.monotonic() + max(0, deadline - time.time())
    with fs:
        upto = fs.data['line']
        if not upto or os.path.getsize(path) < fs.data['offset']:
            return 0
        rows, st = _title_rows(cfg, path, upto)
        done = set(fs.data.get('title_seqs') or [])
        counts = dict(redacted=0)
        sized = [(r, _size(r)) for r in (_finish(r, counts) for r in rows if r['seq'] not in done)]
        if not send:
            return len(sized)
        sent = 0
        while sent < len(sized):
            n = _fits(sized[sent:])
            remaining = until - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('title backfill ran out of time')
            chunk = [r for r, _ in sized[sent:sent + n]]
            common.ingest_records(cfg, chunk, timeout=remaining)
            fs.data['title_seqs'] = _merged(fs.data.get('title_seqs'), [r['seq'] for r in chunk])
            fs.save()
            sent += n
        if st and st['title'] and not fs.data.get('title'):
            fs.data.update(title=st['title'], title_wait=st['title_wait'])
        if st and st['title_custom'] and not fs.data.get('title_custom'):
            fs.data['title_custom'] = True
        fs.save()
        return sent


def session_files(transcript_path):
    """The main transcript and its subagent transcripts."""
    if not transcript_path:
        return []
    out = [transcript_path]
    if transcript_path.endswith('.jsonl'):
        out += sorted(glob.glob(os.path.join(transcript_path[:-6], 'subagents', '*.jsonl')))
    return out
