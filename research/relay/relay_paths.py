"""CtrlB Relay resource keys, copied (below this note) from ctrlb-agent lib/paths.py so the research prototype
(swarm_mcp.py) keys resources byte for byte as Seeker does, except that the kinds table is read from the agent
package's lib/keys.json (RELAY_KEYS_JSON overrides). Tested against Seeker's tests/fixtures/keys.json by
test_swarm_mcp.py."""
import json
import os
import re

PATH, NAME, SLOT, CONTRACT = 'path:', 'name:', 'slot:', 'contract:'

# The kinds table: what each key prefix is (leasable, finishable, reopenable, checked_by_hook, default_lease_s).
# The prototype reads the agent package's copy (lib/keys.json, Seeker's tests/fixtures/keys.json byte for byte);
# RELAY_KEYS_JSON names another copy.
KEYS_JSON = os.environ.get('RELAY_KEYS_JSON') or os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'lib',
                                                               'keys.json')
with open(KEYS_JSON, encoding='utf-8') as _fh:
    KINDS = json.load(_fh)['kinds']

# Go's unicode.IsSpace: what strings.TrimSpace trims and strings.Fields splits on. Python's str.strip() also strips
# U+001C to U+001F, which Go keeps.
GO_SPACE = ('\t\n\v\f\r \x85\xa0            '
            '    　')
_FIELDS = re.compile('[' + re.escape(GO_SPACE) + ']+')
# Seeker's extensionPattern. RE2's \S is ASCII-only ([^\t\n\f\r ]) and its $ is the end of the text.
_EXT = re.compile(r'[^\t\n\f\r ]*\.[A-Za-z0-9_-]*[A-Za-z][A-Za-z0-9_-]*\Z')


def clean(p):
    """Go's path.Clean."""
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
    # Go's strings.ToLower maps rune by rune with no context: no final-sigma rule, U+0130 to 'i'.
    out = []
    for c in s:
        lc = c.lower()
        out.append(lc if len(lc) == 1 else ('i' if c == 'İ' else c))
    return ''.join(out)


def is_path(r):
    """Whether a trimmed resource is keyed as a path."""
    return '/' in r or _EXT.match(r) is not None


def normalise_path(p, cwd=''):
    """p cleaned, with a leading "<cwd>/" dropped when cwd is given. A p equal to cwd itself is kept whole."""
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


def kind_of(k):
    """The kinds-table row of a key, by its prefix (a key with no known prefix is a name)."""
    for row in KINDS:
        if k.startswith(row['prefix']):
            return row
    return next(row for row in KINDS if row['kind'] == 'name')


def lease_seconds_for(k, settings):
    """The lease of a hold on key k under the workspace's leaseSeconds: the kind's own, or the setting."""
    return kind_of(k)['default_lease_s'] or settings


def key(resource, cwd=''):
    """(key, kind) of a claimed resource, kind one of the table's ('path', 'name', 'slot', 'contract'); ('', '') when
    the resource is a prefix with nothing after it."""
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
    """The form of a file path an edit tool touches that the package sends: normalised as key() does, and keyed by
    key(edit_path(p, cwd), cwd) as PATH + normalise_path(p, cwd). '' for an empty path."""
    n = normalise_path((p or '').strip(GO_SPACE), cwd)
    if not n:
        return ''
    return n if key(n, cwd) == (PATH + n, 'path') else './' + n


def edit_paths(ps, cwd=''):
    """edit_path of each path, empty ones dropped, once per key, in order."""
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
