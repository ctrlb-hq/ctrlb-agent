
import common
import datetime
import json
import os
import re
import shutil
import tempfile

BEGIN = '<!-- ctrlb:begin -->'
END = '<!-- ctrlb:end -->'
FILES = {'codex': 'AGENTS.md', 'claude': 'CLAUDE.md'}

RULE = common.RECALL_COMMON.format(session='')


class Refused(Exception):
    pass


def version():

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

    return re.findall(r'[^\n]*\n|[^\n]+$', text) if text else []


def _ending(line):
    return '\r\n' if line.endswith('\r\n') else '\n' if line.endswith('\n') else ''


def _markers(lines):

    begins, ends, fence, opened = [], [], None, None
    list_indents, fence_indent = [], 0
    for i, ln in enumerate(lines):
        body = ln[:len(ln) - len(_ending(ln))]

        expanded = body.expandtabs(4)
        indent = len(expanded) - len(expanded.lstrip(' '))
        if fence:
            if fence_indent and expanded.strip() and indent < fence_indent:

                fence = None
            else:
                m = re.match(r'^ {0,3}(`{3,}|~{3,})(.*)$', expanded[fence_indent:])
                if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence) and not m.group(2).strip():
                    fence = None
                continue
        if expanded.strip():
            while list_indents and indent < list_indents[-1]:
                list_indents.pop()
        offset = list_indents[-1] if list_indents else 0


        while True:
            item = re.match(r'^ {0,3}(?:[-+*]|[0-9]{1,9}[.)])( +)', expanded[offset:])
            if not item:
                break
            padding = len(item.group(1))
            offset += item.end() - (padding - 1 if padding > 4 else 0)
            list_indents.append(offset)
        m = re.match(r'^ {0,3}(`{3,}|~{3,})(.*)$', expanded[offset:])
        if m and not (m.group(1)[0] == '`' and '`' in m.group(2)):
            fence, opened, fence_indent = m.group(1), i, offset
            continue
        if body.strip() == BEGIN:
            begins.append(i)
        elif body.strip() == END:
            ends.append(i)
    return begins, ends, (opened if fence else None)


def render(old, remove=False):

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


            at_end = e == len(lines) - 1
            start = b - 1 if at_end and b > 0 and lines[b - 1] in ('\n', '\r\n') else b
            kept = lines[:start] + lines[e + 1:]
            if not _ending(lines[e]) and at_end and kept and start == b - 1:
                kept[-1] = kept[-1][:len(kept[-1]) - len(_ending(kept[-1]))]
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


def _reserve_backup(path):

    stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
    p, i = f'{path}.ctrlb-backup-{stamp}', 1
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, 'O_NOFOLLOW', 0)
    while True:
        try:
            return p, os.open(p, flags, 0o600)
        except FileExistsError:
            p, i = f'{path}.ctrlb-backup-{stamp}-{i}', i + 1


def write(path, text):

    path = os.path.realpath(path)
    backup, mode = '', 0o644
    if os.path.exists(path):
        mode = os.stat(path).st_mode & 0o777
        backup, backup_fd = _reserve_backup(path)
        try:
            with os.fdopen(backup_fd, 'wb') as dst, open(path, 'rb') as src:
                shutil.copyfileobj(src, dst)
        except BaseException:
            os.unlink(backup)
            raise
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
