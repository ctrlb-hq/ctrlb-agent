
import hashlib
import json
import os
import re
import secrets
import stat
import subprocess
import tempfile
import sys
import threading
import time

if __name__ == '__main__':
    sys.dont_write_bytecode = True
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import common
import redact
from paths import edit_paths as normalise_edit_paths, is_path, key as resource_key

REGISTER_S = 2.5
SLOT_S = 1.5
RELEASE_S = 1.0
RECORD_S = 3.0
ARTIFACT_POST_S = 10
ARTIFACT_MAX = 32 * 1024
BLOCK_MAX = 2000
TEXT_MAX = 2000
MAX_PATHS = 32
ARTIFACT = 'artifact:'


def in_relay(cfg, run, agent, sid):

    if cfg.get('run'):
        return True
    if not run or not agent:
        return False
    try:
        return bool(common.SessionRecord(run, agent).get(sid).get('relay'))
    except Exception:
        return False


def _clean_text(text, limit=TEXT_MAX):

    out, _ = redact.redact(text or '')
    b = out.encode()
    return out if len(b) <= limit else b[:limit].decode(errors='ignore')




_SPLIT = re.compile(r'[\s`\'"()\[\]{}<>,;|*=]+')
_LINE_REF = re.compile(r':\d+(?::\d+)?$')


def prompt_paths(prompt, cwd):

    if not cwd or not os.path.isdir(cwd):
        return []
    root = os.path.realpath(cwd)
    out = []
    for tok in _SPLIT.split(prompt or ''):
        tok = _LINE_REF.sub('', tok.strip().rstrip('.:!?'))
        if not tok or '://' in tok or not is_path(tok):
            continue
        p = os.path.normpath(os.path.join(root, os.path.expanduser(tok)))
        real = os.path.realpath(p)
        if real == root or not real.startswith(root + os.sep) or not os.path.exists(real):
            continue
        rel = os.path.relpath(p, root).replace(os.sep, '/')
        if rel.startswith('..') or rel in out:
            continue
        out.append(rel)
        if len(out) >= MAX_PATHS:
            break
    return out


def prompt_test(prompt):

    from hooks_shared import is_test_command
    spans = re.findall(r'`([^`\n]+)`', prompt or '')
    fenced = re.findall(r'^\s*(?:```|~~~)[^\n]*\n(.*?)^\s*(?:```|~~~)', prompt or '', re.S | re.M)
    for s in spans + [ln for block in fenced for ln in block.splitlines()]:
        s = s.strip()
        s = s[1:].strip() if s.startswith('$') else s
        if is_test_command(s) and len(s.encode()) <= 500 and redact.redact(s)[1] == 0:
            return s
    return ''


INTERFACE = re.compile(r'\b(?:api|apis|contracts?|interfaces?|shapes?|schemas?|fields|signatures?)\b', re.I)
INTERFACE_LANGS = {'json', 'jsonc', 'jsonschema', 'typescript', 'ts', 'proto', 'protobuf', 'graphql', 'gql', 'openapi'}
_HEADING = re.compile(r'^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$')
_LABEL = re.compile(r'^\s*(?:\*\*)?([A-Za-z][^:]{0,60}?)(?:\*\*)?:\s*$')
_FENCE = re.compile(r'^\s*(```+|~~~+)\s*([\w+.-]*)')


def prompt_contract(prompt):

    lines = (prompt or '').splitlines()
    title, body, i = None, [], 0

    def section():
        if title and INTERFACE.search(title) and any(b.strip() for b in body):
            return title, '\n'.join(body).strip('\n')
        return None

    while i < len(lines):
        ln = lines[i]
        m = _FENCE.match(ln)
        if m:
            j = i + 1
            while j < len(lines) and not lines[j].strip().startswith(m.group(1)):
                j += 1
            block = lines[i:j + 1]
            if not (title and INTERFACE.search(title)):
                lang = m.group(2).lower()
                prev = lines[i - 1] if i else ''
                named = INTERFACE.search(lang) or INTERFACE.search(prev)
                if (lang in INTERFACE_LANGS or named) and any(b.strip() for b in block[1:-1]):
                    return (named.group(0).lower() if named else lang), '\n'.join(block)
            body.extend(block)
            i = j + 1
            continue
        h = _HEADING.match(ln) or _LABEL.match(ln)
        if h:
            found = section()
            if found:
                return found
            title, body = h.group(1).strip().strip('*').strip(), []
        else:
            body.append(ln)
        i += 1
    return section()


def _task_number(run):

    def bump(d):
        d['n'] = int(d.get('n') or 0) + 1
        return d['n']
    return common.RunState(run, '*', 'counter').change(bump)


def _name_part(s, limit):

    return ' '.join(str(s or '').replace('/', ' ').replace('\\', ' ').split())[:limit].strip()


def register_spawn(cfg, run, agent, tool_input, cwd, budget=REGISTER_S):

    deadline = time.time() + budget
    args = tool_input if isinstance(tool_input, dict) else {}
    prompt = str(args.get('prompt') or '')
    paths = prompt_paths(prompt, cwd)
    if not prompt.strip() or not paths:
        common.log(f"spawn task {run}/{agent}: not registered (the prompt names no path under the workspace)")
        return None
    desc = _name_part(args.get('description'), 80) or _name_part(' '.join(prompt.split()[:6]), 80) or 'sub-agent'
    name = f'task {_task_number(run):02d} {desc}'
    test = prompt_test(prompt)
    common.post_brief(cfg, run, agent, name, _clean_text(prompt), paths, cwd, test,
                      timeout=max(0.1, deadline - time.time()))
    out = dict(task=name, paths=paths)
    if test:
        out['test'] = test
    found = prompt_contract(prompt)
    if found and deadline - time.time() > 0.1:
        cname = f'{name} {_name_part(found[0], 40)}'
        common.post_brief(cfg, run, agent, 'contract:' + cname, _clean_text(found[1]), paths, cwd,
                          timeout=max(0.1, deadline - time.time()))
        out['contract'] = cname
    common.log(f"spawn task {run}/{agent}: registered {name!r} with {len(paths)} path(s)"
               + (f", contract {out['contract']!r}" if 'contract' in out else ''))
    return out




SECRET_NAME = re.compile(r'(?:^|/)(?:\.env(?:\..*)?|.*\.(?:pem|key|p12|pfx|keystore)|id_(?:rsa|dsa|ecdsa|ed25519)(?:\.pub)?'
                         r'|auth\.json|credentials(?:\.json)?|\.netrc|\.npmrc|\.pypirc|\.git-credentials)$', re.I)


def artifact_roots(cfg, run, inp):

    out = []
    for d in (inp.get('scratchpad_dir'), common.scratch_dir(cfg, run)):
        if isinstance(d, str) and d and os.path.isdir(d):
            r = os.path.realpath(d)
            if r not in out:
                out.append(r)
    return out


def artifact_of(path, roots):

    if not isinstance(path, str) or not path:
        return None
    real = os.path.realpath(path)
    rel = next((os.path.relpath(real, r).replace(os.sep, '/') for r in roots if real.startswith(r + os.sep)), None)
    if not rel:
        return None
    why = ''
    try:
        size = os.path.getsize(real)
        if SECRET_NAME.search(rel):
            why = 'secret-looking file name'
        elif size >= ARTIFACT_MAX:
            why = f'{size} bytes, over the {ARTIFACT_MAX} byte limit'
        else:
            with open(real, 'rb') as fh:
                raw = fh.read(ARTIFACT_MAX)
            text = raw.decode('utf-8') if b'\0' not in raw else None
            if text is None:
                why = 'binary'
            elif redact.redact(text)[1]:
                why = 'matches a secret pattern'
    except UnicodeDecodeError:
        why = 'binary'
    except OSError as e:
        why = f'unreadable ({type(e).__name__})'
    if why:
        common.log(f"artifact skipped: {rel}: {why}")
        return None
    return rel, text, 'sha256:' + hashlib.sha256(raw).hexdigest()


def _copy_dir(run, agent):

    name = hashlib.sha1(f'{run}\0{agent}'.encode()).hexdigest()
    return os.path.join(common.HOME, 'state', 'artifacts-' + name)


def _safe_rel(rel):
    rel = str(rel or '').replace('\\', '/')
    parts = [p for p in rel.split('/') if p not in ('', '.')]
    if not parts or rel.startswith('/') or '..' in parts:
        return ''
    return '/'.join(parts)


def _write(path, text):

    d = os.path.dirname(path)
    os.makedirs(d, mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix='.' + os.path.basename(path) + '.', suffix='.ctrlb-tmp')
    try:
        with os.fdopen(fd, 'w') as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _confined(root, rel):

    parts = rel.split('/')
    cur = root
    for p in parts[:-1]:
        cur = os.path.join(cur, p)
        if os.path.islink(cur) or (os.path.exists(cur) and not os.path.isdir(cur)):
            return ''
    dest = os.path.join(root, *parts)
    base = os.path.realpath(root)
    parent = os.path.realpath(os.path.dirname(dest))
    if parent != base and not parent.startswith(base + os.sep):
        return ''
    return dest


def post_artifact(cfg, run, agent, path, inp):

    found = artifact_of(path, artifact_roots(cfg, run, inp))
    if not found:
        return None
    rel, text, h = found

    def put(d):
        if d.get(rel) == h:
            return False
        d[rel] = h
        return True
    if not common.RunState(run, agent, 'artifacts').change(put):
        return 'same'
    try:
        common.private_dir(os.path.join(common.HOME, 'state'))
        dest = _confined(_copy_dir(run, agent), rel)
        if dest:
            _write(dest, text)
    except OSError:
        pass
    detach('post-artifact', dict(run=run, agent=agent, rel=rel, text=text, hash=h))
    return 'posted'


def detach(mode, payload):

    p = subprocess.Popen([sys.executable, '-I', os.path.abspath(__file__), mode], stdin=subprocess.PIPE,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
    try:
        p.stdin.write(json.dumps(payload).encode())
    finally:
        p.stdin.close()
    return p.pid


def _poster(payload):
    cfg = common.config()
    if cfg['off'] or common.relay_off(cfg):
        return
    run, agent, rel, h = payload['run'], payload['agent'], payload['rel'], payload['hash']
    try:
        common.post_note(cfg, run, agent, payload['text'], key=ARTIFACT + rel, hash_=h, timeout=ARTIFACT_POST_S)
        common.log(f"artifact posted: {run}/{agent} {rel} ({len(payload['text'].encode())} bytes)")
    except Exception as e:
        common.log(f"artifact post {run}/{agent} {rel}: {type(e).__name__}: {str(e)[:200]}")

        def drop(d):
            if d.get(rel) == h:
                del d[rel]
        common.RunState(run, agent, 'artifacts').change(drop)




def slot_paths(cfg, paths, cwd):

    want = set(cfg.get('auto_slots') or [])
    if not want:
        return []
    return [p for p in normalise_edit_paths(paths, cwd or '') if (p[2:] if p.startswith('./') else p) in want]




def claim_slots(cfg, run, agent, rels, cwd, timeout=SLOT_S):

    from hooks_shared import record_tokens
    deadline = time.time() + timeout
    claimed = []
    for rel in rels:
        bare = rel[2:] if rel.startswith('./') else rel
        ans = common.claim(cfg, run, agent, 'slot:' + bare, paths=[rel], cwd=cwd, timeout=max(0.1, deadline - time.time()))
        if ans.get('held') is True:
            record_tokens(run, agent, 'claim', ans)
            claimed.append(bare)
            continue
        holder = ans.get('held_by')
        if holder and holder != agent:
            return f'CtrlB Relay: {bare} is held by {holder}, try again in a moment.', claimed
        common.log(f"auto slot {run}/{agent} {bare}: not held ({ans.get('reason') or ans.get('message') or '?'}); edit allowed")
    return '', claimed


def release_slots(cfg, run, agent, rels, cwd, timeout=RELEASE_S):

    from hooks_shared import record_tokens
    deadline = time.time() + timeout
    for rel in rels:
        bare = rel[2:] if rel.startswith('./') else rel
        k = resource_key('slot:' + bare)[0]
        if not common.TokenState(run, agent).get([k]):
            continue
        ans = common.release(cfg, run, agent, 'slot:' + bare, cwd=cwd, timeout=max(0.1, deadline - time.time()))

        record_tokens(run, agent, 'release', dict(ans, key=ans.get('key') or k, released=1))




def _get(cfg, run, tail, query, deadline, out, name):
    try:
        out[name] = common.get_run_part(cfg, run, tail, query, timeout=max(0.1, deadline - time.time()))
    except Exception as e:
        out[name + '_error'] = f'{type(e).__name__}: {str(e)[:120]}'


def read_record(cfg, run, agent, budget=RECORD_S):

    deadline = time.time() + budget
    got = {}
    reads = [('tasks', 'tasks', None), ('contracts', 'contracts', dict(agent=agent)),
             ('notes', 'peers', dict(kind='note', limit=100)), ('handoffs', 'peers', dict(kind='handoff', limit=50)),
             ('artifacts', 'artifacts', dict(agent=agent))]
    threads = [threading.Thread(target=_get, args=(cfg, run, tail, q, deadline, got, name), daemon=True)
               for name, tail, q in reads]
    for t in threads:
        t.start()
    for t in threads:
        t.join(max(0, deadline - time.time()))
    cache = common.RunState(run, agent, 'record')
    try:
        old = cache.read()
    except Exception:
        old = {}
    parts, stale = {}, []
    for name, _, _ in reads:
        if isinstance(got.get(name), dict):
            parts[name] = got[name]
        else:
            if name in got or name + '_error' in got:
                common.log(f"session-start record {run}/{agent}: {name}: {got.get(name + '_error') or 'not an object'}")
            else:
                common.log(f"session-start record {run}/{agent}: {name}: no answer inside {budget}s")
            if isinstance(old.get(name), dict):
                parts[name] = old[name]
            stale.append(name)
    fresh = {k: v for k, v in parts.items() if k not in stale}
    if fresh:
        try:
            cache.change(lambda d: d.update(fresh, ts=time.time()))
        except Exception:
            pass
    parts['cache_ts'] = old.get('ts')
    return parts, stale


def _restore(root, rel, text):

    os.makedirs(root, mode=0o700, exist_ok=True)
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in rel.split('/')[:-1]:
            try:
                os.mkdir(part, mode=0o700, dir_fd=fd)
            except FileExistsError:
                pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        name = rel.split('/')[-1]
        tmp = '.' + name + '.' + secrets.token_hex(8) + '.ctrlb-tmp'
        dest = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
        try:
            with os.fdopen(dest, 'w') as fh:
                fh.write(text)
            try:

                os.link(tmp, name, src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
            except FileExistsError:
                return stat.S_ISREG(os.stat(name, dir_fd=fd, follow_symlinks=False).st_mode)
        finally:
            os.unlink(tmp, dir_fd=fd)
        return True
    finally:
        os.close(fd)


def restore_artifacts(parts, run, agent, scratch):

    have = []
    items = (parts.get('artifacts') or {}).get('artifacts') if isinstance(parts.get('artifacts'), dict) else None
    texts = {}
    for a in items or []:
        if isinstance(a, dict) and isinstance(a.get('text'), str) and (a.get('agent') in (None, '', agent)):
            rel = _safe_rel(a.get('path') or str(a.get('key') or '')[len(ARTIFACT):])
            if rel and rel not in texts:
                texts[rel] = a['text']
    copies = _copy_dir(run, agent)
    if os.path.isdir(copies):
        for base, _, files in os.walk(copies):
            for f in files:
                rel = _safe_rel(os.path.relpath(os.path.join(base, f), copies).replace(os.sep, '/'))
                if rel and rel not in texts and not f.endswith('.ctrlb-tmp'):
                    try:
                        with open(os.path.join(base, f)) as fh:
                            texts[rel] = fh.read()
                    except (OSError, UnicodeDecodeError):
                        pass
    for rel, text in sorted(texts.items()):
        try:
            if redact.redact(text)[1]:
                common.log(f"artifact not restored: {rel}: matches a secret pattern")
                continue
            if _restore(scratch, rel, text):
                have.append(rel)
        except OSError as e:
            common.log(f"artifact not restored: {rel}: {type(e).__name__}")
    return have


def _cut(s, n):
    s = ' '.join(str(s or '').split())
    return s if len(s) <= n else s[:n - 3] + '...'


def record_block(parts, stale, run, agent, scratch, artefacts):

    tasks = [t for t in (parts.get('tasks') or {}).get('tasks') or [] if isinstance(t, dict) and t.get('state') != 'finished']
    lines = []
    if tasks:
        items = []
        for t in tasks[:12]:
            who = f"held by {t['held_by']}" if t.get('held_by') else 'free'
            flags = ', stalled' if t.get('stalled') else ''
            flags += ', handed off' if t.get('handed_off') else ''
            test = f"; test: {_cut(t['test'], 80)}" if t.get('test') else ''
            items.append(f"{t.get('name')} ({who}{flags}{test})")
        more = f' and {len(tasks) - 12} more' if len(tasks) > 12 else ''
        lines.append('Open tasks: ' + '; '.join(items) + more + '.')
    contracts = [c.get('name') for c in (parts.get('contracts') or {}).get('contracts') or [] if isinstance(c, dict) and c.get('name')]
    if contracts:
        lines.append('Contracts: ' + ', '.join(_cut(c, 60) for c in contracts[:12]) + '. Read them with `contracts` before building on them.')
    hand = []
    for t in tasks:
        if t.get('handed_off') and not t.get('held_by'):
            hand.append(f"{t.get('name')} handed off openly by {t.get('handoff_by') or '?'}: {_cut(t.get('handoff_text'), 200)}")
    for e in (parts.get('handoffs') or {}).get('events') or []:
        if isinstance(e, dict) and e.get('to') == agent:
            hand.append(f"{e.get('resource') or e.get('key')} handed to you by {e.get('agent')}: {_cut(e.get('text'), 160)}")
    if hand:
        lines.append('Handoffs: ' + ' | '.join(hand[:4]) + '.')
    notes = [e for e in (parts.get('notes') or {}).get('events') or [] if isinstance(e, dict) and e.get('agent') == agent
             and not str(e.get('key') or '').startswith(ARTIFACT)][:5]
    if notes:
        lines.append('Your last notes: ' + ' | '.join(_cut(e.get('text'), 120) for e in notes) + '.')
    if artefacts:
        lines.append(f'Your artefacts from the previous session are in {scratch}.')
    if not lines:
        return ''
    head = f'CtrlB Relay record of run {run} at session start'
    if stale and parts.get('cache_ts'):
        head += f" ({', '.join(stale)} from the local cache of {time.strftime('%H:%M:%S', time.localtime(parts['cache_ts']))}; CtrlB did not answer in time)"
    text = head + ':\n' + '\n'.join(lines)
    while len(text.encode()) > BLOCK_MAX and len(lines) > 1:

        for drop in ('Your last notes:', 'Handoffs:', 'Contracts:'):
            idx = next((i for i, ln in enumerate(lines) if ln.startswith(drop)), None)
            if idx is not None:
                del lines[idx]
                break
        else:
            break
        text = head + ':\n' + '\n'.join(lines)
    if len(text.encode()) > BLOCK_MAX:
        text = text.encode()[:BLOCK_MAX - 3].decode(errors='ignore') + '...'
    return text


def make_scratch(cfg, run):

    d = common.scratch_dir(cfg, run)
    try:
        common.private_dir(common.HOME)
    except OSError:
        pass
    try:
        os.makedirs(d, mode=0o700, exist_ok=True)
        return d
    except OSError as e:
        common.log(f"scratch dir {d}: {type(e).__name__}")
        return ''


def session_start_block(cfg, run, agent, source='', budget=RECORD_S):

    if not cfg.get('run') or not run or not agent or source == 'compact':
        return ''
    scratch = make_scratch(cfg, run)
    parts, stale = read_record(cfg, run, agent, budget)
    artefacts = restore_artifacts(parts, run, agent, scratch) if scratch else []
    return record_block(parts, stale, run, agent, scratch, artefacts)


if __name__ == '__main__':
    try:
        if sys.argv[1:2] == ['post-artifact']:
            _poster(json.load(sys.stdin))
    except BaseException as e:
        if not isinstance(e, SystemExit):
            common.log(f"relay_hooks {sys.argv[1:]}: {type(e).__name__}: {str(e)[:200]}")
    sys.exit(0)
