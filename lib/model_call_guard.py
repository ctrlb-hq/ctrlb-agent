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

SEPARATORS = set(';&|()\n')
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


def _tokens(text):
    lex = shlex.shlex(text, posix=True, punctuation_chars=';&|()<>\n')
    lex.whitespace = ' \t\r'
    lex.commenters = ''
    return list(lex)


def _segments(tokens):
    """Simple commands: runs of tokens between separators (; & | && || ( ) newline). Redirections stay in."""
    seg = []
    for t in tokens:
        if t and all(c in ';&|()<>\n' for c in t) and any(c in SEPARATORS for c in t):
            if seg:
                yield seg
            seg = []
        else:
            seg.append(t)
    if seg:
        yield seg


def _substitutions(token):
    """The text of each $( ... ) in a token, by paren depth."""
    out, i = [], token.find('$(')
    while i != -1:
        depth, j = 0, i + 1
        while j < len(token):
            if token[j] == '(':
                depth += 1
            elif token[j] == ')':
                depth -= 1
                if depth == 0:
                    break
            j += 1
        out.append(token[i + 2:j])
        i = token.find('$(', j)
    return out


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
            if a.startswith('-'):
                i += 2 if a in CODEX_VALUE_OPTS else 1
                continue
            return f'codex {"exec" if a == "e" else a}' if a in CODEX_SUBCOMMANDS else ''
        return ''
    if prog == 'claude':
        for a in args:
            if a == '--':
                break
            if a in ('-p', '--print'):
                return 'claude -p'
        return ''
    return ''


def _valid_duration(d):
    if d.startswith('$'):
        return True            # a variable: the shell decides
    m = DURATION_RE.match(d)
    return bool(m) and float(m.group(1)) > 0


def _scan(words, timeout, found, depth):
    """Walk one simple command's prefixes (assignments and wrappers) and record any model CLI it starts, with the time
    limit in force for it: None, or the duration."""
    i = 0
    while i < len(words):
        w = words[i]
        if ASSIGN_RE.match(w):
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
    rest = words[i:]
    call = _model_call(rest)
    if call:
        found.append((call, timeout))
    for w in rest:
        for inner in _substitutions(w):
            _walk(inner, timeout, found, depth + 1)


def _walk(text, timeout, found, depth=0):
    if depth > MAX_DEPTH:
        return
    for seg in _segments(_tokens(_drop_heredocs(text.replace('\\\n', ' ')))):
        if os.path.basename(seg[0]) != 'export':
            _scan(seg, timeout, found, depth)


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
