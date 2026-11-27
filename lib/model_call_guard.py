
import json
import os
import re
import shlex

SEPARATORS = ';&|()\n'
RESERVED = ('if', 'then', 'elif', 'else', 'do', 'while', 'until', '!', '{')
SHELLS = ('bash', 'sh', 'zsh', 'dash')
TIMEOUTS = ('timeout', 'gtimeout')
PASS_THROUGH = ('nohup', 'exec', 'command', 'time', 'setsid', 'builtin')
ASSIGN_RE = re.compile(r'^([A-Za-z_][A-Za-z0-9_]*)=(.*)$', re.S)
DURATION_RE = re.compile(r'^(\d+(?:\.\d*)?|\.\d+)([smhd]?)$')
REDIRECT_RE = re.compile(r'^\d*(?:<<?<?-?|>>?|<>|<&|>&|>\||&>>?)$')
CODEX_SUBCOMMANDS = ('exec', 'e', 'review')

CODEX_VALUE_OPTS = ('-c', '--config', '--enable', '--disable', '--remote', '--remote-auth-token-env', '-i', '--image',
                    '-m', '--model', '--local-provider', '-p', '--profile', '-s', '--sandbox', '-C', '--cd', '--add-dir',
                    '-a', '--ask-for-approval')



CLAUDE_VALUE_OPTS = ('--add-dir', '--agent', '--agents', '--allowed-tools', '--allowedTools',
                     '--append-subagent-system-prompt', '--append-subagent-system-prompt-file',
                     '--append-system-prompt', '--append-system-prompt-file', '--autocompact', '--betas',
                     '--debug-file', '--disallowed-tools', '--disallowedTools', '--effort', '--environment',
                     '--fallback-model', '--file', '--input-format', '--json-schema', '--max-budget-usd',
                     '--max-thinking-tokens', '--max-turns', '--mcp-config', '--model', '--name', '--output-format',
                     '--permission-mode', '--permission-prompt-tool', '--permission-prompts',
                     '--plan-mode-instructions', '--plugin-dir', '--plugin-url', '--remote-control-session-name-prefix',
                     '--resume-session-at', '--session-id', '--setting-sources', '--settings', '--system-prompt',
                     '--system-prompt-file', '--system-prompt-snapshot', '--tools', '-n')

CLAUDE_OPTIONAL_OPTS = ('--cloud', '--debug', '--from-pr', '--prompt-suggestions', '--remote-control', '--resume',
                        '--teleport', '--worktree', '-d', '-r', '-w')

CLAUDE_FLAGS = ('--allow-dangerously-skip-permissions', '--ax-screen-reader', '--background', '--bare', '--bg',
                '--brief', '--chrome', '--continue', '--dangerously-skip-permissions', '--desktop',
                '--disable-slash-commands', '--exclude-dynamic-system-prompt-sections', '--fork-session',
                '--forward-subagent-text', '--ide', '--include-hook-events', '--include-partial-messages', '--init',
                '--init-only', '--maintenance', '--no-chrome', '--no-session-persistence', '--replay-user-messages',
                '--restricted', '--safe-mode', '--strict-mcp-config', '--tmux', '--verbose', '-c')
CLAUDE_SHORT = 'cdhnprvw'
CLAUDE_SHORT_VALUE = 'ndrw'
CLAUDE_SHORT_REQUIRED = 'n'
CLAUDE_HELP = ('-h', '--help', '-v', '--version')
CLAUDE_MAYBE = 'claude'
TIME_VALUE_OPTS = ('-f', '--format', '-o', '--output')
TIMEOUT_VALUE_OPTS = ('-k', '--kill-after', '-s', '--signal')
HELP = ('-h', '--help', '-V', '--version')
MAX_DEPTH = 4


def enabled(cfg):
    return bool(cfg.get('guard_model_calls'))


def switch(var, file_value):

    v = var if var not in (None, '') else file_value
    if isinstance(v, bool):
        return v
    return str(v or '').strip().lower() in ('1', 'true', 'on', 'yes')


def off_before_load():

    f = {}
    try:
        with open(os.path.join(os.environ.get('CTRLB_HOME') or os.path.expanduser('~/.ctrlb'), 'config.json')) as fh:
            f = json.load(fh)
    except Exception:
        pass
    return not switch(os.environ.get('CTRLB_GUARD_MODEL_CALLS'), f.get('guardModelCalls') if isinstance(f, dict) else None)


def _heredoc_word(line, i):

    n, parts, quoted = len(line), [], False
    while i < n and line[i] not in ' \t;&|()<>':
        c = line[i]
        if c == '$' and line[i + 1:i + 2] in ("'", '"'):
            i += 1
            continue
        if c == "'":
            k = line.find("'", i + 1)
            k = n if k == -1 else k
            parts.append(line[i + 1:k])
            quoted, i = True, k + 1
        elif c == '"':
            i += 1
            while i < n and line[i] != '"':
                if line[i] == '\\' and i + 1 < n and line[i + 1] in '"\\$`':
                    i += 1
                parts.append(line[i])
                i += 1
            quoted, i = True, i + 1
        elif c == '\\':
            parts.append(line[i + 1:i + 2])
            quoted, i = True, i + 2
        else:
            parts.append(c)
            i += 1
    if not parts and not quoted:
        return None
    return ''.join(parts), quoted, i


def _heredoc_ops(line, stack):

    ops, i, n = [], 0, len(line)
    while i < n:
        top = stack[-1] if stack else None
        c = line[i]
        if top == "'":
            if c == "'":
                stack.pop()
            i += 1
        elif top == '((':
            k = line.find('))', i)
            if k == -1:
                i = n
            else:
                stack.pop()
                i = k + 2
        elif c == '\\':
            i += 2
        elif line.startswith('$((', i) or (top != '"' and line.startswith('((', i)):
            k = line.find('))', i + 2)
            if k == -1:
                stack.append('((')
                i = n
            else:
                i = k + 2
        elif line.startswith('$(', i):
            stack.append('$(')
            i += 2
        elif top == '"':
            if c == '"':
                stack.pop()
            i += 1
        elif c in '\'"':
            stack.append(c)
            i += 1
        elif c == '(':
            stack.append('(')
            i += 1
        elif c == ')':
            if top in ('$(', '('):
                stack.pop()
            i += 1
        elif c == '#' and (i == 0 or line[i - 1] in ' \t;&|()'):
            break
        elif line.startswith('<<<', i):
            i += 3
        elif line.startswith('<<', i):
            dash = line.startswith('<<-', i)
            j = i + (3 if dash else 2)
            while j < n and line[j] in ' \t':
                j += 1
            w = _heredoc_word(line, j)
            if w:
                ops.append((w[0], not w[1], dash))
                i = w[2]
            else:
                i = j
        else:
            i += 1
    return ops


def _substitutions(line):

    subs, i, n = [], 0, len(line)
    while i < n:
        if line[i] == '\\':
            i += 2
        elif line.startswith('$(', i) or line[i] == '`':
            try:
                j = _sub_end(line, i)
            except ValueError:
                break
            subs.append(line[i:j])
            i = j
        else:
            i += 1
    return subs


def _drop_heredocs(text):

    out, pending, body = [], [], []

    def close(expands):
        if expands:
            out.extend(_substitutions('\n'.join(body)))
        body.clear()

    waiting, joined, stack = [], False, []
    for line in text.split('\n'):
        if pending:
            end, expands, dash = pending[0]


            if not joined and (line.lstrip('\t') if dash else line) == end:
                close(expands)
                pending.pop(0)
            else:
                body.append(line)
            joined = expands and _continues(line)
            continue
        out.append(line)
        waiting += _heredoc_ops(line, stack)
        if not _continues(line):
            pending, waiting = waiting, []
    if pending:
        close(pending[0][1])
    return '\n'.join(out)


def _continues(line):

    return (len(line) - len(line.rstrip('\\'))) % 2 == 1


def _sub_end(text, i):

    n = len(text)
    if text[i] == '`':
        j = i + 1
        while j < n and text[j] != '`':
            j += 2 if text[j] == '\\' else 1
        if j >= n:
            raise ValueError('unclosed `')
        return j + 1
    depth, j = 1, i + 2
    while j < n:
        c = text[j]
        if c == '\\':
            j += 2
            continue
        if c == "'":
            k = text.find("'", j + 1)
            if k == -1:
                raise ValueError("unclosed '")
            j = k + 1
            continue
        if c == '"':
            j += 1
            while j < n and text[j] != '"':
                if text[j] == '\\':
                    j += 2
                elif text.startswith('$(', j) or text[j] == '`':
                    j = _sub_end(text, j)
                else:
                    j += 1
            if j >= n:
                raise ValueError('unclosed "')
            j += 1
            continue
        if c == '#' and text[j - 1] in ' \t\n;&|(':
            k = text.find('\n', j)
            j = n if k == -1 else k
            continue
        if c == '(':
            depth += 1
        elif c == ')':
            depth -= 1
            if depth == 0:
                return j + 1
        j += 1
    raise ValueError('unclosed $(')


def _sub_text(text, i, j):
    return text[i + 1:j - 1] if text[i] == '`' else text[i + 2:j - 1]


def _lex(text):

    out, n, i = [], len(text), 0
    word, subs, inword, quoted = [], [], False, False

    def end():
        nonlocal word, subs, inword, quoted
        if inword:
            out.append(('qword' if quoted else 'word', ''.join(word), subs))
        word, subs, inword, quoted = [], [], False, False

    while i < n:
        c = text[i]
        if c in ' \t\r':
            end()
            i += 1
        elif c == '#' and not inword:
            k = text.find('\n', i)
            i = n if k == -1 else k
        elif c in '<>' or (c == '&' and text.startswith('&>', i)):
            fd = ''
            if inword and not quoted and not subs and ''.join(word).isdigit():
                fd = ''.join(word)
                word, inword = [], False
            end()
            j = i + 1
            while j < n and text[j] in '<>&|':
                j += 1
            out.append(('word', fd + text[i:j], []))
            i = j
        elif c in SEPARATORS:
            end()
            j = i + 1
            while c not in '\n()' and j < n and text[j] in ';&|':
                j += 1
            out.append(('sep', text[i:j], []))
            i = j
        elif c == '\\':
            inword = quoted = True
            word.append(text[i + 1:i + 2])
            i += 2
        elif c == "'":
            inword = quoted = True
            k = text.find("'", i + 1)
            if k == -1:
                raise ValueError("unclosed '")
            word.append(text[i + 1:k])
            i = k + 1
        elif c == '"':
            inword = quoted = True
            i += 1
            while True:
                if i >= n:
                    raise ValueError('unclosed "')
                c = text[i]
                if c == '"':
                    i += 1
                    break
                if c == '\\':
                    word.append(text[i + 1:i + 2])
                    i += 2
                elif text.startswith('$(', i) or c == '`':
                    j = _sub_end(text, i)
                    subs.append(_sub_text(text, i, j))
                    word.append(text[i:j])
                    i = j
                else:
                    word.append(c)
                    i += 1
        elif text.startswith('$(', i) or c == '`':
            inword = True
            j = _sub_end(text, i)
            subs.append(_sub_text(text, i, j))
            word.append(text[i:j])
            i = j
        else:
            inword = True
            word.append(c)
            i += 1
    end()
    return out


def _is(item, kind, text):
    return item[0] == kind and item[1] == text


def _body(items, j):

    n = len(items)
    if _is(items[j], 'word', '{'):
        depth, at_cmd = 0, True
        for m in range(j, n):
            kind, text, _ = items[m]
            if kind == 'sep':
                at_cmd = True
            elif kind == 'word' and at_cmd and text == '{':
                depth += 1
            elif kind == 'word' and at_cmd and text == '}':
                depth -= 1
                if depth == 0:
                    return j + 1, m
            elif not (kind == 'word' and at_cmd and text in RESERVED):
                at_cmd = False
        return None
    if _is(items[j], 'sep', '('):
        depth = 0
        for m in range(j, n):
            if _is(items[m], 'sep', '('):
                depth += 1
            elif _is(items[m], 'sep', ')'):
                depth -= 1
                if depth == 0:
                    return j + 1, m
    return None


def _parts(items):

    i, n, seg = 0, len(items), []
    while i < n:
        kind, text, subs = items[i]
        if kind == 'word' and REDIRECT_RE.match(text):


            if i + 1 < n and items[i + 1][0] != 'sep':
                i += 1
                if items[i][2]:
                    seg.append((None, items[i][2]))
            i += 1
            continue
        name, j = None, i
        at_cmd = all(w in RESERVED for w, _ in seg)
        if at_cmd and kind == 'word' and text == 'function' and i + 1 < n and items[i + 1][0] != 'sep':
            name, j = items[i + 1][1], i + 2
            if j + 1 < n and _is(items[j], 'sep', '(') and _is(items[j + 1], 'sep', ')'):
                j += 2
        elif at_cmd and kind == 'word' and text not in RESERVED and i + 2 < n and _is(items[i + 1], 'sep', '(') \
                and _is(items[i + 2], 'sep', ')'):
            name, j = text, i + 3
        if name is not None:
            while j < n and _is(items[j], 'sep', '\n'):
                j += 1
            span = _body(items, j) if j < n else None
            if span:
                yield ('def', name, (items[span[0]:span[1]], _is(items[j], 'word', '{')))
                i = span[1] + 1
                continue
        if kind == 'sep':
            if seg:
                yield ('seg', seg)
            seg = []
        else:
            seg.append((text, subs))
        i += 1
    if seg:
        yield ('seg', seg)


def _run(items, timeout, found, depth, funcs, share=False):

    funcs = funcs if share and funcs is not None else dict(funcs or {})
    for part in _parts(items):
        if part[0] == 'def':
            funcs[part[1]] = part[2]
        else:
            _scan(part[1], timeout, found, depth, funcs)


def _model_call(words):

    if not words:
        return ''
    prog = os.path.basename(words[0])
    args = words[1:]
    if prog == 'codex':
        i = 0
        while i < len(args):
            a = args[i]
            if a == '--':
                return ''
            if a in HELP:
                return ''
            if a.startswith('-'):
                i += 2 if a in CODEX_VALUE_OPTS else 1
                continue
            opts = args[i + 1:]
            opts = opts[:opts.index('--')] if '--' in opts else opts
            if a not in CODEX_SUBCOMMANDS or any(h in opts for h in HELP):
                return ''
            return f'codex {"exec" if a == "e" else a}'
        return ''
    if prog == 'claude':
        return {'print': 'claude -p', 'maybe': CLAUDE_MAYBE}.get(_claude_prints(args), '')
    return ''


def _claude_prints(args):

    found = unknown = False
    after_unknown = False
    known = CLAUDE_VALUE_OPTS + CLAUDE_OPTIONAL_OPTS + CLAUDE_FLAGS + CLAUDE_HELP + ('-p', '--print')
    i = 0
    while i < len(args):
        a = args[i]
        i += 1
        maybe_value, after_unknown = after_unknown, False
        if a == '--':
            break
        if a in CLAUDE_HELP:
            if maybe_value:
                continue
            return False
        if a in CLAUDE_VALUE_OPTS:
            i += 1
        elif a in ('-p', '--print'):
            found = True
        elif a.startswith('--'):
            if a.split('=', 1)[0] not in known:
                unknown, after_unknown = True, '=' not in a
        elif len(a) > 1 and a[0] == '-' and a not in known:
            for k, c in enumerate(a[1:], 2):
                if c not in CLAUDE_SHORT:
                    unknown, after_unknown = True, True
                    break
                if c in 'hv':
                    if maybe_value:
                        break
                    return False
                found = found or c == 'p'
                if c in CLAUDE_SHORT_VALUE:
                    if k == len(a) and c in CLAUDE_SHORT_REQUIRED:
                        i += 1
                    break
    if unknown:
        maybe = any(a in ('-p', '--print') or (a[:1] == '-' and a[1:2] != '-' and 'p' in a[1:]) for a in args)
        return 'maybe' if maybe else ''
    return 'print' if found else ''


def _valid_duration(d):
    if d.startswith('$'):
        return True
    m = DURATION_RE.match(d)
    return bool(m) and float(m.group(1)) > 0


def _scan(seg, timeout, found, depth, funcs=None):

    funcs = funcs or {}
    for _, subs in seg:
        for inner in subs:
            _walk(inner, timeout, found, depth + 1, funcs)
    words = [w for w, _ in seg if w is not None]
    i = 0
    while i < len(words):
        w = words[i]
        if ASSIGN_RE.match(w) or w in RESERVED:
            i += 1
            continue
        prog = os.path.basename(w)
        if prog == 'env':
            i += 1
            while i < len(words) and words[i].startswith('-'):
                w = words[i]
                split = (words[i + 1] if i + 1 < len(words) else '') if w in ('-S', '--split-string') else \
                    w[len('--split-string='):] if w.startswith('--split-string=') else \
                    w[2:] if w.startswith('-S') and len(w) > 2 else None
                if split is not None:
                    rest = words[i + (2 if w in ('-S', '--split-string') else 1):]
                    _walk(split + ' ' + shlex.join(rest), timeout, found, depth + 1, funcs)
                    return
                i += 2 if w in ('-u', '--unset', '-C', '--chdir') else 1
            continue
        if prog in TIMEOUTS:
            i += 1
            while i < len(words) and words[i].startswith('-'):
                i += 2 if words[i] in TIMEOUT_VALUE_OPTS else 1
            if i < len(words):
                timeout = words[i] if _valid_duration(words[i]) else (timeout or '')
                i += 1
            continue
        if prog == 'command':
            k = i + 1
            while k < len(words) and words[k].startswith('-') and words[k] != '--':
                if 'v' in words[k][1:] or 'V' in words[k][1:]:
                    return
                k += 1
        if prog in PASS_THROUGH:
            i += 1
            while i < len(words) and words[i].startswith('-'):
                i += 2 if prog == 'time' and words[i] in TIME_VALUE_OPTS else 1
            continue
        if prog == 'nice':
            i += 1
            if i < len(words) and words[i] in ('-n', '--adjustment'):
                i += 2
            elif i < len(words) and words[i].startswith('-'):
                i += 1
            continue
        if prog == 'stdbuf':
            i += 1
            while i < len(words) and words[i].startswith('-'):
                i += 2 if words[i] in ('-i', '-o', '-e') else 1
            continue
        if prog in SHELLS:
            rest = words[i + 1:]
            for k, a in enumerate(rest):
                if a.startswith('-') and not a.startswith('--') and 'c' in a[1:] and k + 1 < len(rest):
                    _walk(rest[k + 1], timeout, found, depth + 1)
                    break
                if not a.startswith('-'):
                    break
            return
        break
    if i < len(words) and words[i] in funcs and depth < MAX_DEPTH:
        body, same_shell = funcs[words[i]]
        _run(body, timeout, found, depth + 1, funcs, share=same_shell)
        return
    call = _model_call(words[i:])
    if call:
        found.append((call, timeout))


def _walk(text, timeout, found, depth=0, funcs=None):
    if depth > MAX_DEPTH:
        return
    _run(_lex(_drop_heredocs(text).replace('\\\n', ' ')), timeout, found, depth, funcs)


def model_calls(command):

    if isinstance(command, list):
        command = shlex.join(str(c) for c in command)
    found = []
    _walk(str(command or ''), None, found)
    return found


def check(cfg, command):

    if not enabled(cfg):
        return None
    try:
        calls = model_calls(command)
    except ValueError:
        return None
    for call, timeout in calls:
        if not timeout:
            return refusal(call)
    return None


def refusal(call):
    if call == CLAUDE_MAYBE:
        return ('CtrlB Relay: this command may start `claude` in print mode without a time limit: it has a claude '
                'option the guard does not know, so a -p in it may be a flag or that option\'s value. guardModelCalls '
                'is on: a model CLI call needs a time limit, so a stuck call cannot hold this shell for hours. Put '
                '`timeout <seconds>` directly before the `claude` word, after any NAME=value words, and keep every '
                'other word as it is: `FOO=1 claude --opt x -p hi` becomes `FOO=1 timeout 600 claude --opt x -p hi`. '
                'The agent it starts finds this session on its own, and joins its Relay run when it has one.')
    return (f'CtrlB Relay: this command starts `{call}` without a time limit. guardModelCalls is on: a model CLI call '
            'needs one, so a stuck call cannot hold this shell for hours. Run it like this: timeout 1200 '
            f'{call} ... The agent it starts finds this session on its own, and joins its Relay run when it has one.')
