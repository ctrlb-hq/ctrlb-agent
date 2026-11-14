"""The CtrlB block for a project's AGENTS.md (Codex) and CLAUDE.md (Claude Code), and how bin/init.py writes it.

The block is the session-start rule (hooks_shared.RECALL) without the session id, under a `## CtrlB` heading, between
two marker comments. A marker is the whole line, trimmed: `<!-- ctrlb:begin -->` or `<!-- ctrlb:end -->`; markers
inside ``` or ~~~ fenced code blocks are examples and are ignored. Writing it never touches text outside the markers:

- no file: the file is created with the block;
- a file without markers: the block is appended at the end, after one blank line;
- a file with both markers: only the text between them is replaced;
- one marker, or more than one block: refused, naming the line, and nothing is written;
- no block and a ``` or ~~~ code block left open at the end: refused, naming the line, since an appended block
  would land inside it.

A write that changes an existing file first copies it to <name>.ctrlb-backup-<time>. A write that would change
nothing writes nothing. Removal deletes the block, plus the one blank line before it only when the block ends the
file (where init puts it), and gives back a file without a final newline as it was. Line endings outside the block
are kept; the block takes the file's most common ending, so a CRLF file stays CRLF. Files are read and written as
UTF-8. A symlinked file is written through the link."""
import datetime
import json
import os
import re
import shutil
import tempfile

BEGIN = '<!-- ctrlb:begin -->'
END = '<!-- ctrlb:end -->'
FILES = {'codex': 'AGENTS.md', 'claude': 'CLAUDE.md'}

RULE = ("Earlier work is in the CtrlB record, not in your context: this session's turns before compaction, and this "
        "user's earlier sessions. Before your first grep, file read or shell command for a question about earlier "
        "work (what was done, decided, run, read, or why something failed), call `search_sessions`. After a command "
        "fails, call `find_command` with the command before changing it, then `next_actions` with a session_id and "
        "seq it returns to see what followed. Do not say something was not said or not done until you have searched.")


class Refused(Exception):
    pass


def version():
    """The plugin version from plugin.json, looked up beside this file in a clone and in an installed plugin copy."""
    here = os.path.dirname(os.path.abspath(__file__))
    for d in (os.path.join(here, '..', 'plugin'), os.path.join(here, '..'),
              os.path.join(os.path.dirname(os.path.realpath(__file__)), '..', 'plugin')):
        try:
            with open(os.path.join(d, '.claude-plugin', 'plugin.json')) as fh:
                return str(json.load(fh).get('version') or 'unknown')
        except (OSError, ValueError):
            continue
    return 'unknown'


def block_lines():
    return [BEGIN, f'<!-- written by ctrlb-agent {version()}; run init again to update it, init --remove to remove it -->',
            '## CtrlB', '', RULE, END]


def split_lines(text):
    """Each line with its ending; only \\n ends a line, and ''.join(split_lines(t)) == t."""
    return re.findall(r'[^\n]*\n|[^\n]+$', text) if text else []


def _ending(line):
    return '\r\n' if line.endswith('\r\n') else '\n' if line.endswith('\n') else ''


def _markers(lines):
    """(begin marker indexes, end marker indexes, index of a fence left open at the end or None), skipping lines inside
    ``` or ~~~ fenced code blocks. A marker is the whole line, trimmed: <!-- ctrlb:begin --> or <!-- ctrlb:end -->."""
    begins, ends, fence, opened = [], [], None, None
    for i, ln in enumerate(lines):
        body = ln[:len(ln) - len(_ending(ln))]
        m = re.match(r'^ {0,3}(`{3,}|~{3,})(.*)$', body)
        if fence:
            if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence) and not m.group(2).strip():
                fence = None
            continue
        if m and not (m.group(1)[0] == '`' and '`' in m.group(2)):
            fence, opened = m.group(1), i
            continue
        if body.strip() == BEGIN:
            begins.append(i)
        elif body.strip() == END:
            ends.append(i)
    return begins, ends, (opened if fence else None)


def render(old, remove=False):
    """The file's text after the change. old is None when the file does not exist. Raises Refused on broken markers.
    The block's lines take the file's most common line ending. A file without a newline at its end gets the block
    without one too, so --remove gives back the same bytes."""
    lines = split_lines(old)
    crlf = sum(1 for ln in lines if _ending(ln) == '\r\n')
    lf = sum(1 for ln in lines if _ending(ln) == '\n')
    nl = '\r\n' if crlf > lf else '\n'
    begins, ends, open_fence = _markers(lines)
    if begins or ends:
        if len(begins) != 1 or len(ends) != 1 or ends[0] < begins[0]:
            first = min(begins + ends)
            which = 'begin' if first in begins else 'end'
            raise Refused(f'line {first + 1} has a ctrlb:{which} marker without its pair, or the file has more than '
                          f'one CtrlB block; fix the markers by hand and run again')
        b, e = begins[0], ends[0]
        if remove:
            # init appends the block at the end of the file after one blank line. Only that blank line goes with it;
            # a block anywhere else leaves every line around it as it is.
            at_end = e == len(lines) - 1
            start = b - 1 if at_end and b > 0 and lines[b - 1] in ('\n', '\r\n') else b
            kept = lines[:start] + lines[e + 1:]
            if not _ending(lines[e]) and at_end and kept and start == b - 1:
                kept[-1] = kept[-1][:len(kept[-1]) - len(_ending(kept[-1]))]   # the file had no newline at its end
            return ''.join(kept)
        blk = [ln + nl for ln in block_lines()]
        blk[-1] = blk[-1][:-len(nl)] + _ending(lines[e])
        return ''.join(lines[:b] + blk + lines[e + 1:])
    if remove:
        return old
    if open_fence is not None:
        raise Refused(f'line {open_fence + 1} opens a code block (``` or ~~~) that is never closed, so the block would '
                      f'land inside it; close the code block and run again')
    if not old:
        return nl.join(block_lines()) + nl
    last = _ending(lines[-1])
    body = old if last else old + nl
    return body + nl + nl.join(block_lines()) + last


def read(path):
    if not os.path.exists(path):
        return None
    with open(path, encoding='utf-8', newline='') as fh:
        return fh.read()


def backup_path(path):
    stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
    p, i = f'{path}.ctrlb-backup-{stamp}', 1
    while os.path.exists(p):
        p, i = f'{path}.ctrlb-backup-{stamp}-{i}', i + 1
    return p


def write(path, text):
    """Back up an existing file, then replace it atomically with its mode kept. The new text goes to a temporary file
    created exclusively, under a fresh name, beside the target, which is removed if anything fails. Returns the backup
    path or ''."""
    path = os.path.realpath(path)
    backup, mode = '', 0o644
    if os.path.exists(path):
        backup = backup_path(path)
        shutil.copy2(path, backup)
        mode = os.stat(path).st_mode & 0o777
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix='.' + os.path.basename(path) + '.ctrlb-', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='') as fh:
            fh.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return backup
