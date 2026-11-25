"""guardModelCalls: refuse a shell command that starts a model CLI (codex exec, codex review, claude -p) without a time
limit in front of it. Standard library only. Off unless CTRLB_GUARD_MODEL_CALLS or the config key guardModelCalls turns
it on. docs/relay-hooks.md describes it.

A model CLI started from a shell call with no time limit can block that call for hours. Joining the Relay run does not
need the command: the started agent finds its parent when it starts (lib/spawn.py). This check is only the time limit:
`timeout <duration>` (a duration above zero) in front of the model CLI.

The check reads only the command text. It sees through env assignments, env, timeout, nohup, exec, command, time,
setsid, nice, stdbuf, redirections, `bash -c` (sh, zsh, dash) and $( ) substitutions, including those in a heredoc body
whose delimiter is not quoted. A model CLI started from a script, or through
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
REDIRECT_RE = re.compile(r'^\d*(?:<<?<?-?|>>?|<>|<&|>&|>\||&>>?)$')
CODEX_SUBCOMMANDS = ('exec', 'e', 'review')
# codex options before the subcommand that take a value (codex --help, 0.166)
CODEX_VALUE_OPTS = ('-c', '--config', '--enable', '--disable', '--remote', '--remote-auth-token-env', '-i', '--image',
                    '-m', '--model', '--local-provider', '-p', '--profile', '-s', '--sandbox', '-C', '--cd', '--add-dir',
                    '-a', '--ask-for-approval')
# claude's options (2.1.331: the options its --help lists, and a few it accepts but does not list). A newer claude
# can have more: an option not listed here makes the check fail closed (_claude_prints).
# Options that take a value: the next word is the value, even one that starts with '-'.
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
# Options whose value is optional ([value]): they never take a word that starts with '-'.
CLAUDE_OPTIONAL_OPTS = ('--cloud', '--debug', '--from-pr', '--prompt-suggestions', '--remote-control', '--resume',
                        '--teleport', '--worktree', '-d', '-r', '-w')
# Options that take no value (-p/--print and help/version are read on their own).
CLAUDE_FLAGS = ('--allow-dangerously-skip-permissions', '--ax-screen-reader', '--background', '--bare', '--bg',
                '--brief', '--chrome', '--continue', '--dangerously-skip-permissions', '--desktop',
                '--disable-slash-commands', '--exclude-dynamic-system-prompt-sections', '--fork-session',
                '--forward-subagent-text', '--ide', '--include-hook-events', '--include-partial-messages', '--init',
                '--init-only', '--maintenance', '--no-chrome', '--no-session-persistence', '--replay-user-messages',
                '--restricted', '--safe-mode', '--strict-mcp-config', '--tmux', '--verbose', '-c')
CLAUDE_SHORT = 'cdhnprvw'       # claude's short options
CLAUDE_SHORT_VALUE = 'ndrw'     # -n <name>, -d/-r/-w [value]: in -dp the rest of the word is the value of -d
CLAUDE_SHORT_REQUIRED = 'n'     # with nothing after it in the word, -n takes the next word, even one that starts with -
CLAUDE_HELP = ('-h', '--help', '-v', '--version')
CLAUDE_MAYBE = 'claude'         # the call when an unknown option leaves print mode unsure
TIME_VALUE_OPTS = ('-f', '--format', '-o', '--output')      # GNU time options that take a value
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


def _heredoc_word(line, i):
    """The heredoc delimiter word that starts at line[i]: (delimiter after quote removal, quoted, index past it), or
    None. The word runs to a blank or an operator character; any quoting in it (\\, '...', "...") makes it quoted."""
    n, parts, quoted = len(line), [], False
    while i < n and line[i] not in ' \t;&|()<>':
        c = line[i]
        if c == '$' and line[i + 1:i + 2] in ("'", '"'):      # $'EOF' and $"EOF": the $ goes with the quotes
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
    """[(delimiter, expands, strip_tabs)] for each heredoc operator in line, in order, outside quotes and comments. A
    body expands ($( ) and ` ` run) when its delimiter is not quoted. <<< (a here-string), and << in $(( )) or (( )),
    are not heredocs. stack holds the quotes, substitutions and arithmetic still open from earlier lines (', ", $(, ( and ((): a quote
    or a $( can span lines, as in codex exec "$(cat <<'PROMPT' ... )" <<'DIFF'. It is updated in place."""
    ops, i, n = [], 0, len(line)
    while i < n:
        top = stack[-1] if stack else None
        c = line[i]
        if top == "'":
            if c == "'":
                stack.pop()
            i += 1
        elif top == '((':      # arithmetic left open on an earlier line: << shifts until its ))
            k = line.find('))', i)
            if k == -1:
                i = n
            else:
                stack.pop()
                i = k + 2
        elif c == '\\':
            i += 2
        elif line.startswith('$((', i) or (top != '"' and line.startswith('((', i)):      # arithmetic: << shifts
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
    """The $( ) and ` ` substitutions in an expanding heredoc body (quotes outside them are plain text). One may span
    lines."""
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
    """The command without heredoc bodies: their lines are data, not commands. A body whose delimiter is not quoted
    keeps its $( ) and ` ` substitutions, each as a line of its own after the operator's line, because the shell runs
    them. A body ends at a line that is exactly its delimiter (<<- strips leading tabs first)."""
    out, pending, body = [], [], []

    def close(expands):
        if expands:
            out.extend(_substitutions('\n'.join(body)))
        body.clear()

    waiting, joined, stack = [], False, []      # operators on a logical line a backslash continues; open quotes
    for line in text.split('\n'):
        if pending:
            end, expands, dash = pending[0]
            # In an expanding body a backslash-newline joins lines, so a line it continues is never the delimiter;
            # in a quoted body it is plain text.
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
    if pending:      # a body with no end line runs to the end of the command
        close(pending[0][1])
    return '\n'.join(out)


def _continues(line):
    """Whether line ends with a backslash that escapes the newline (an odd number of them)."""
    return (len(line) - len(line.rstrip('\\'))) % 2 == 1


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
        if c == '#' and text[j - 1] in ' \t\n;&|(':      # a comment: an apostrophe in it is text
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
            fd = ''
            if inword and not quoted and not subs and ''.join(word).isdigit():      # 2>file: 2 is the fd, not a word
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
        if kind == 'word' and REDIRECT_RE.match(text):
            # A redirection and its target are not words of the command (>/dev/null codex exec runs codex). The
            # target's substitutions still run: they stay in the segment under a None word.
            if i + 1 < n and items[i + 1][0] != 'sep':
                i += 1
                if items[i][2]:
                    seg.append((None, items[i][2]))
            i += 1
            continue
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
        return {'print': 'claude -p', 'maybe': CLAUDE_MAYBE}.get(_claude_prints(args), '')
    return ''


def _claude_prints(args):
    """'print' when claude args turn on -p/--print with no help or version, 'maybe' when an unknown option leaves
    that unsure, else ''. Read as claude reads them. The value of an
    option is never a flag: in `claude --system-prompt -p hi` the -p is the system prompt. --opt=value is one word.
    Short options in one word are read flag by flag: -cp is -c -p; the first one that takes a value takes the rest of
    the word (-pndemo1: the name is demo1), or the next word when nothing is left (-pn --help: the name is --help).

    It fails closed. An option missing from the tables above (a newer claude) may take the next word as its value, so
    with one on the line a -p anywhere (alone or in short options) counts, and help or version right after it does
    not cancel the check. A wrong refusal costs a timeout; a missed call can hold the shell for hours."""
    found = unknown = False
    after_unknown = False          # this word may be the value of the unknown option before it
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
            return False           # help or version: no model call
        if a in CLAUDE_VALUE_OPTS:
            i += 1                 # its value
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
                if split is not None:      # env -S "codex exec" review: the string is split into the command
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
                    return      # command -v, -pv, -p -V codex: looks the name up, runs nothing
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
    _run(_lex(_drop_heredocs(text).replace('\\\n', ' ')), timeout, found, depth, funcs)


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
