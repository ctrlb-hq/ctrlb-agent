"""CtrlB Relay round 8 hooks (Seeker decisions 43 to 47), shared by plugin/hooks/hook.py and codex/hook.py. Standard
library only. docs/relay-hooks.md lists each hook's event, the fields it reads, what it writes and its time budget.

A  a sub-agent spawn (PreToolUse on Agent) registers a task: `task <n> <description>`, the prompt as its text, the
   workspace paths the prompt names as its files, and an interface block of the prompt as a contract. Synchronous,
   inside REGISTER_S, so the task is in the record before the sub-agent's first claim; nothing is written when Seeker
   does not answer, and the spawn always goes ahead.
B  a small text file written under the session's scratchpad or the run's scratch directory is posted as a NOTE keyed
   artifact:<relative path> with its hash, by a detached process; the same content is posted once.
C  a session of a relay run (CTRLB_RUN) gets its artefacts written back to the scratch directory. Under the hooks
   protocol (hooks_shared.Machine) the record block (open tasks, contracts, handoffs, notes) is no longer injected.
D  the scratch directory: common.scratch_dir.
E  autoSlots: an edit of a listed file is wrapped in a slot:<path> claim (pre-tool) and its release (post-tool).

Run as a script (python3 -I relay_hooks.py post-artifact, the payload on stdin) it is the detached artefact poster."""
import hashlib
import json
import os
import re
import subprocess
import sys
import time

if __name__ == '__main__':
    sys.dont_write_bytecode = True
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import common  # noqa: E402
import redact  # noqa: E402
from paths import edit_paths as normalise_edit_paths, is_path, key as resource_key  # noqa: E402

REGISTER_S = 2.5        # A: the task and contract writes together (the hook's alarm is REGISTER_S + 0.5)
SLOT_S = 1.5            # E: the slot claim, inside pre-tool's two-second alarm
RELEASE_S = 1.0         # E: the slot release, synchronous in post-tool
RECORD_S = 3.0          # C: the session-start read of the agent's artefacts
ARTIFACT_POST_S = 10    # B: the detached post
ARTIFACT_MAX = 32 * 1024
TEXT_MAX = 2000         # Seeker cuts a brief's text here
MAX_PATHS = 32          # Seeker takes at most 32 paths per task or contract
ARTIFACT = 'artifact:'


def in_relay(cfg, run, agent, sid):
    """Whether the session is in a relay run, as the session-end handoff decides it: CTRLB_RUN is set, or a ctrlb relay
    tool was called in this session."""
    if cfg.get('run'):
        return True
    if not run or not agent:
        return False
    try:
        return bool(common.SessionRecord(run, agent).get(sid).get('relay'))
    except Exception:
        return False


def _clean_text(text, limit=TEXT_MAX):
    """Text with the plugin's secret patterns redacted, cut at limit bytes (on a character boundary)."""
    out, _ = redact.redact(text or '')
    b = out.encode()
    return out if len(b) <= limit else b[:limit].decode(errors='ignore')


# ---------------------------------------------------------------- A: a spawn registers a task (decision 43)

_SPLIT = re.compile(r'[\s`\'"()\[\]{}<>,;|*=]+')
_LINE_REF = re.compile(r':\d+(?::\d+)?$')


def prompt_paths(prompt, cwd):
    """The repository paths a prompt names that resolve to a file or directory under the workspace (cwd), relative to
    it, in order, once each, at most MAX_PATHS. A token counts when it reads as a path (a '/' or a file extension);
    `src/app.py:12` names src/app.py; URLs and paths outside the workspace are left out."""
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
    """The first test command the prompt quotes (in backticks or a fenced block), or ''."""
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
    """(title, text) of the first interface the prompt carries, or None: a section under a heading that names an
    interface (API, contract, interface, shape, schema, fields, signature; a markdown heading or a short line ending in
    ':'), with its body up to the next heading; or a fenced block whose language is an interface language (json,
    typescript, proto, graphql, openapi) or whose language or preceding line names an interface."""
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
    """The next task number of the run on this machine (a counter under the state directory)."""
    def bump(d):
        d['n'] = int(d.get('n') or 0) + 1
        return d['n']
    return common.RunState(run, '*', 'counter').change(bump)


def _name_part(s, limit):
    """A name segment: no '/' or '\\' (which would key it as a path), white space collapsed, at most limit chars."""
    return ' '.join(str(s or '').replace('/', ' ').replace('\\', ' ').split())[:limit].strip()


def register_spawn(cfg, run, agent, tool_input, cwd, budget=REGISTER_S):
    """Decision 43. The task a sub-agent spawn registers, and the contract when the prompt carries an interface:
    {task, paths, test?, contract?}, or None when the prompt names no workspace path (a task needs one). Raises on a
    Seeker error or timeout; the caller logs it and lets the spawn go ahead."""
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


# ---------------------------------------------------------------- B: artefacts (decision 44)

SECRET_NAME = re.compile(r'(?:^|/)(?:\.env(?:\..*)?|.*\.(?:pem|key|p12|pfx|keystore)|id_(?:rsa|dsa|ecdsa|ed25519)(?:\.pub)?'
                         r'|auth\.json|credentials(?:\.json)?|\.netrc|\.npmrc|\.pypirc|\.git-credentials)$', re.I)


def artifact_roots(cfg, run, inp):
    """The directories whose files are artefacts: the session's scratchpad_dir and the run's scratch directory."""
    out = []
    for d in (inp.get('scratchpad_dir'), common.scratch_dir(cfg, run)):
        if isinstance(d, str) and d and os.path.isdir(d):
            r = os.path.realpath(d)
            if r not in out:
                out.append(r)
    return out


def artifact_of(path, roots):
    """(relative path, text, hash) of a file that is an artefact, or None. A file outside the roots is no artefact (no
    log line); a binary, one of ARTIFACT_MAX bytes or more, one with a secret-looking name, or one any secret pattern
    matches is skipped with a line in the hook log that names only its relative path."""
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
    """Where the plugin keeps a local copy of each artefact it posted (the session-start fallback)."""
    name = hashlib.sha1(f'{run}\0{agent}'.encode()).hexdigest()
    return os.path.join(common.HOME, 'state', 'artifacts-' + name)


def _safe_rel(rel):
    rel = str(rel or '').replace('\\', '/')
    parts = [p for p in rel.split('/') if p not in ('', '.')]
    if not parts or rel.startswith('/') or '..' in parts:
        return ''
    return '/'.join(parts)


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.ctrlb-tmp'
    with open(tmp, 'w') as fh:
        fh.write(text)
    os.replace(tmp, path)


def post_artifact(cfg, run, agent, path, inp):
    """Decision 44 for one written file: skipped (None), unchanged since its last post ('same'), or handed to a
    detached poster ('posted'). The hash cache is updated before the post; the poster takes the entry back when the
    post fails, so the next write tries again."""
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
        _write(os.path.join(_copy_dir(run, agent), rel), text)
    except OSError:
        pass
    detach('post-artifact', dict(run=run, agent=agent, rel=rel, text=text, hash=h))
    return 'posted'


def detach(mode, payload):
    """Run this module as a detached process (its own session, stdout and stderr to /dev/null) with payload on its
    stdin, and return at once: the hook's own output is printed by then or never."""
    p = subprocess.Popen([sys.executable, '-I', os.path.abspath(__file__), mode], stdin=subprocess.PIPE,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
    try:
        p.stdin.write(json.dumps(payload).encode())
    finally:
        p.stdin.close()
    return p.pid


def _poster(payload):
    cfg = common.config()
    if cfg['off']:
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


# ---------------------------------------------------------------- E: hook-managed slots (decision 47)

def slot_paths(cfg, paths, cwd):
    """The edited paths (as the edit check sends them) that autoSlots lists."""
    want = set(cfg.get('auto_slots') or [])
    if not want:
        return []
    return [p for p in normalise_edit_paths(paths, cwd or '') if (p[2:] if p.startswith('./') else p) in want]


def held_text(rel, holder):
    return f'CtrlB Relay: {rel} is held by {holder}, try again in a moment.'


def claim_slots(cfg, run, agent, rels, cwd, timeout=SLOT_S):
    """Claim slot:<path> with the file for each autoSlots path of an edit. Returns (denied reason or '', claimed
    paths). Only a definite answer that another agent holds it denies; any error raises (the caller fails open)."""
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
            return held_text(bare, holder), claimed
        common.log(f"auto slot {run}/{agent} {bare}: not held ({ans.get('reason') or ans.get('message') or '?'}); edit allowed")
    return '', claimed


def release_slots(cfg, run, agent, rels, cwd, timeout=RELEASE_S):
    """Release slot:<path> for each autoSlots path of an edit whose slot this agent holds (a token is kept for it)."""
    from hooks_shared import record_tokens
    deadline = time.time() + timeout
    done = []
    for rel in rels:
        bare = rel[2:] if rel.startswith('./') else rel
        k = resource_key('slot:' + bare)[0]
        if not common.TokenState(run, agent).get([k]):
            continue
        ans = common.release(cfg, run, agent, 'slot:' + bare, cwd=cwd, timeout=max(0.1, deadline - time.time()))
        # The hold is gone either way (released now, or already lapsed): forget its token.
        record_tokens(run, agent, 'release', dict(ans, key=ans.get('key') or k, released=1))
        done.append(bare)
    return done


# ---------------------------------------------------------------- C: a session starts with the record (decision 45)

def _get(cfg, run, tail, query, deadline, out, name):
    try:
        out[name] = common.get_run_part(cfg, run, tail, query, timeout=max(0.1, deadline - time.time()))
    except Exception as e:
        out[name + '_error'] = f'{type(e).__name__}: {str(e)[:120]}'


def restore_artifacts(parts, run, agent, scratch):
    """Write this agent's artefacts into the scratch directory, those it does not have yet: from Seeker's answer, else
    from the local copies of the artefacts this machine posted. Returns the relative paths now there."""
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
        dest = os.path.join(scratch, rel)
        try:
            if not os.path.exists(dest):
                if redact.redact(text)[1]:
                    common.log(f"artifact not restored: {rel}: matches a secret pattern")
                    continue
                _write(dest, text)
            have.append(rel)
        except OSError as e:
            common.log(f"artifact not restored: {rel}: {type(e).__name__}")
    return have


def make_scratch(cfg, run):
    """Create the run's scratch directory (decision D); returns its path ('' when it cannot be made)."""
    d = common.scratch_dir(cfg, run)
    try:
        os.makedirs(d, mode=0o700, exist_ok=True)
        return d
    except OSError as e:
        common.log(f"scratch dir {d}: {type(e).__name__}")
        return ''


def artefacts_line(cfg, run, agent, source='', budget=RECORD_S, machine=None):
    """Decision 45 under the hooks protocol: the session start no longer injects the record (open tasks, contracts,
    handoffs, notes): the hooks hold the agent's task and the injected text is who it is and how to talk to peers.
    This agent's artefacts are still written back to the scratch directory (one read), and one line says where. ''
    after a compaction, without a run id (CTRLB_RUN), or when there is nothing to write back."""
    if not cfg.get('run') or not run or not agent or source == 'compact':
        return ''
    scratch = make_scratch(cfg, run)
    if not scratch:
        return ''
    got = {}
    if machine is not None:
        machine.count('read')
    _get(cfg, run, 'artifacts', dict(agent=agent), time.time() + budget, got, 'artifacts')
    if 'artifacts_error' in got:
        common.log(f"session-start artefacts {run}/{agent}: {got['artifacts_error']}")
    parts = {'artifacts': got['artifacts']} if isinstance(got.get('artifacts'), dict) else {}
    have = restore_artifacts(parts, run, agent, scratch)
    return f'Your artefacts from the previous session are in {scratch}.' if have else ''


if __name__ == '__main__':
    try:
        if sys.argv[1:2] == ['post-artifact']:
            _poster(json.load(sys.stdin))
    except BaseException as e:
        if not isinstance(e, SystemExit):
            common.log(f"relay_hooks {sys.argv[1:]}: {type(e).__name__}: {str(e)[:200]}")
    sys.exit(0)
