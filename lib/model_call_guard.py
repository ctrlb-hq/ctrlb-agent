"""guardModelCalls: refuse a shell command that starts a model CLI (codex exec, codex review, claude -p) without a time
limit in front of it. Standard library only. Off unless CTRLB_GUARD_MODEL_CALLS or the config key guardModelCalls turns
it on. docs/relay-hooks.md describes it.

A model CLI started from a shell call with no time limit can block that call for hours. Joining the Relay run does not
need the command: the started agent finds its parent when it starts (lib/spawn.py). This check is only the time limit:
`timeout <duration>` (a duration above zero) in front of the model CLI.

The check reads only the command text. It sees through env assignments, env, timeout, nohup, exec, command, time,
setsid, nice, stdbuf, `bash -c` (sh, zsh, dash) and $( ) substitutions. A model CLI started from a script, or through
another program (xargs, npx, a Makefile), is not seen; spawnIdleMinutes (lib/spawn.py) covers those. A command the
tokenizer cannot read is let through."""
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
HEREDOC_RE = re.compile(r'<<-?\s*([\'"]?)([A-Za-z_][A-Za-z0-9_]*)\1')
CODEX_SUBCOMMANDS = ('exec', 'e', 'review')
# codex options before the subcommand that take a value (codex --help, 0.166)
CODEX_VALUE_OPTS = ('-c', '--config', '--enable', '--disable', '--remote', '--remote-auth-token-env', '-i', '--image',
                    '-m', '--model', '--local-provider', '-p', '--profile', '-s', '--sandbox', '-C', '--cd', '--add-dir',
                    '-a', '--ask-for-approval')
TIMEOUT_VALUE_OPTS = ('-k', '--kill-after', '-s', '--signal')
HELP = ('-h', '--help', '-V', '--version')
MAX_DEPTH = 4


def enabled(cfg):
    return bool(cfg.get('guard_model_calls'))


def switch(var, file_value):
    """The setting: the variable when it is set and not empty, else the file's value; true, 1, on and yes turn it on."""
    v = var if var not in (None, '') else file_value
    if isinstance(v, bool):
        return v
    return str(v or '').strip().lower() in ('1', 'true', 'on', 'yes')


def off_before_load():
    """Whether the setting is off, read straight from CTRLB_GUARD_MODEL_CALLS and <CTRLB_HOME>/config.json. The hook
    runs before every Bash call, so with the guard off it stops here, before loading the rest of the plugin."""
    f = {}
    try:
        with open(os.path.join(os.environ.get('CTRLB_HOME') or os.path.expanduser('~/.ctrlb'), 'config.json')) as fh:
            f = json.load(fh)
    except Exception:
        pass
    return not switch(os.environ.get('CTRLB_GUARD_MODEL_CALLS'), f.get('guardModelCalls') if isinstance(f, dict) else None)


def _drop_heredocs(text):
    """The command without heredoc bodies: their lines are data, not commands."""
    out, end = [], None
    for line in text.split('\n'):
        if end is not None:
            if line.strip() == end:
                end = None
            continue
        out.append(line)
        m = HEREDOC_RE.search(line)
        if m:
            end = m.group(2)
    return '\n'.join(out)


def _sub_end(text, i):
    """The index just past the command substitution that starts at i: $( ... ) or ` ... `. Quotes and nested
    parentheses inside it are followed. Raises ValueError when it is not closed."""
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
    """The command as the shell splits it: [('word', text, subs), ('qword', text, subs) for a word with quoting in it,
    or ('sep', text, [])]. Quotes are removed from a word;
    text in quotes never separates commands. subs: the text of each $( ) and ` ` in the word that the shell runs (not
    those in single quotes). A redirection operator is a word of its own. Raises ValueError for an unclosed quote."""
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
            end()
            j = i + 1
            while j < n and text[j] in '<>&|':
                j += 1
            out.append(('word', text[i:j], []))
            i = j
        elif c in SEPARATORS:
            end()
            j = i + 1
            while c not in '\n()' and j < n and text[j] in ';&|':      # && || ;; |& ; ( and ) stand alone
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
    """The function body that starts at items[j], a { ... } group or a ( ... ) subshell: (index of its first item,
    index of its closing item), or None. A brace counts only where a command starts, unquoted, as the shell reads it:
    `echo }` and `printf '{'` do not open or close the body."""
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
    """The command in the order the shell runs it: ('seg', [(text, subs)]) for each simple command, and
    ('def', name, (body items, runs in the same shell)) for each function definition (name() { ...; }, name() ( ... ),
    function name [()] { ...; })."""
    i, n, seg = 0, len(items), []
    while i < n:
        kind, text, subs = items[i]
        name, j = None, i
        at_cmd = all(w in RESERVED for w, _ in seg)      # nothing but if, then, do ... before it
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
            if span:      # a ( ) body runs in a subshell: what it defines does not stay
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
    """Scan the parts in order; a definition replaces the function's body for the calls after it. share: a function
    body run in the same shell, whose definitions stay for the rest of the command; otherwise (a new text: the command,
    a substitution, bash -c) the definitions are a copy."""
    funcs = funcs if share and funcs is not None else dict(funcs or {})
    for part in _parts(items):
        if part[0] == 'def':
            funcs[part[1]] = part[2]
        else:
            _scan(part[1], timeout, found, depth, funcs)


def _model_call(words):
    """'codex exec', 'codex review' or 'claude -p' when words (program first) start a model CLI, else ''."""
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
                return ''      # codex --help, codex --version
            if a.startswith('-'):
                i += 2 if a in CODEX_VALUE_OPTS else 1
                continue
            opts = args[i + 1:]
            opts = opts[:opts.index('--')] if '--' in opts else opts     # after --, --help is the prompt
            if a not in CODEX_SUBCOMMANDS or any(h in opts for h in HELP):
                return ''      # another subcommand, or its help
            return f'codex {"exec" if a == "e" else a}'
        return ''
    if prog == 'claude':
        found = False
        for a in args:
            if a == '--':
                break
            if a in HELP:
                return ''      # help or version: no model call
            found = found or a in ('-p', '--print')
        return 'claude -p' if found else ''
    return ''


def _valid_duration(d):
    if d.startswith('$'):
        return True            # a variable: the shell decides
    m = DURATION_RE.match(d)
    return bool(m) and float(m.group(1)) > 0


def _scan(seg, timeout, found, depth, funcs=None):
    """Walk one simple command's prefixes (assignments, wrappers, reserved words) and record any model CLI it starts,
    with the time limit in force for it: None, or the duration. A substitution in any of its words runs before the
    command does, so it gets only the time limit in force outside the command."""
    funcs = funcs or {}
    for _, subs in seg:
        for inner in subs:
            _walk(inner, timeout, found, depth + 1, funcs)
    words = [w for w, _ in seg]
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
                i += 2 if words[i] in ('-u', '--unset', '-C', '--chdir', '-S', '--split-string') else 1
            continue
        if prog in TIMEOUTS:
            i += 1
            while i < len(words) and words[i].startswith('-'):
                i += 2 if words[i] in TIMEOUT_VALUE_OPTS else 1
            if i < len(words):
                timeout = words[i] if _valid_duration(words[i]) else (timeout or '')
                i += 1
            continue
        if prog == 'command' and i + 1 < len(words) and words[i + 1] in ('-v', '-V'):
            return      # command -v codex: looks the name up, runs nothing
        if prog in PASS_THROUGH:
            i += 1
            while i < len(words) and words[i].startswith('-'):
                i += 1
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
    if i < len(words) and words[i] in funcs and depth < MAX_DEPTH:      # a call of a function defined in the command
        body, same_shell = funcs[words[i]]
        _run(body, timeout, found, depth + 1, funcs, share=same_shell)
        return
    call = _model_call(words[i:])
    if call:
        found.append((call, timeout))


def _walk(text, timeout, found, depth=0, funcs=None):
    if depth > MAX_DEPTH:
        return
    _run(_lex(_drop_heredocs(text.replace('\\\n', ' '))), timeout, found, depth, funcs)


def model_calls(command):
    """[(call, timeout)] for each model CLI the command starts. Raises ValueError when the command cannot be read."""
    if isinstance(command, list):
        command = shlex.join(str(c) for c in command)
    found = []
    _walk(str(command or ''), None, found)
    return found


def check(cfg, command):
    """The refusal text when the guard is on and the command starts a model CLI without a time limit; None otherwise
    (including when the command cannot be read)."""
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
    return (f'CtrlB Relay: this command starts `{call}` without a time limit. guardModelCalls is on: a model CLI call '
            'needs one, so a stuck call cannot hold this shell for hours. Run it like this: timeout 1200 '
            f'{call} ... The agent it starts finds this session on its own, and joins its Relay run when it has one.')
