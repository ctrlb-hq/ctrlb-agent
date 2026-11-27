
import json
import os
import re

PATH, NAME, SLOT, CONTRACT = 'path:', 'name:', 'slot:', 'contract:'



with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'keys.json'), encoding='utf-8') as _fh:
    KINDS = json.load(_fh)['kinds']



GO_SPACE = ('\t\n\v\f\r \x85\xa0            '
            '    　')
_FIELDS = re.compile('[' + re.escape(GO_SPACE) + ']+')

_EXT = re.compile(r'[^\t\n\f\r ]*\.[A-Za-z0-9_-]*[A-Za-z][A-Za-z0-9_-]*\Z')


def clean(p):

    if p == '':
        return '.'
    rooted = p.startswith('/')
    out = []
    for seg in p.split('/'):
        if seg in ('', '.'):
            continue
        if seg == '..':
            if out and out[-1] != '..':
                out.pop()
            elif not rooted:
                out.append('..')
            continue
        out.append(seg)
    s = '/'.join(out)
    if rooted:
        return '/' + s
    return s or '.'


def _lower(s):

    out = []
    for c in s:
        lc = c.lower()
        out.append(lc if len(lc) == 1 else ('i' if c == 'İ' else c))
    return ''.join(out)


def is_path(r):

    return '/' in r or _EXT.match(r) is not None


def normalise_path(p, cwd=''):

    if p == '':
        return ''
    p = clean(p)
    cwd = (cwd or '').strip(GO_SPACE)
    if cwd == '':
        return p
    prefix = clean(cwd)
    if prefix != '/':
        prefix += '/'
    if p.startswith(prefix) and len(p) > len(prefix):
        return p[len(prefix):]
    return p


def normalise_name(n):
    return ' '.join(x for x in _FIELDS.split(_lower(n)) if x)








def key(resource, cwd=''):

    r = (resource or '').strip(GO_SPACE)
    for row in KINDS:
        if r.startswith(row['prefix']):
            body = r[len(row['prefix']):]
            body = normalise_path(body.strip(GO_SPACE), cwd) if row['kind'] == 'path' else normalise_name(body)
            return (row['prefix'] + body, row['kind']) if body else ('', '')
    if is_path(r):
        return PATH + normalise_path(r, cwd), 'path'
    return NAME + normalise_name(r), 'name'


def edit_path(p, cwd=''):

    n = normalise_path((p or '').strip(GO_SPACE), cwd)
    if not n:
        return ''
    return n if key(n, cwd) == (PATH + n, 'path') else './' + n


def edit_paths(ps, cwd=''):

    out, seen = [], set()
    for p in ps or []:
        if not isinstance(p, str):
            continue
        e = edit_path(p, cwd)
        if not e:
            continue
        k = key(e, cwd)[0]
        if k not in seen:
            seen.add(k)
            out.append(e)
    return out
