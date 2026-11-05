"""Resource keys of CtrlB Relay (relay-plan section 3, decision 6), identical to Seeker's internal/pkg/relay Key. Both
sides pass the same table, tests/fixtures/keys.json (copied from Seeker's tests/fixtures/keys.json).

key(resource, cwd) -> (key, kind). The resource is trimmed of surrounding whitespace. One that already carries a key
prefix ("path:" or "name:", as claim echoes it) is keyed as that kind again, so key(key(r)) is key(r). One containing a
'/' or ending in a file extension is a path: cleaned as Go's path.Clean does ("./" stripped, "//" and "/./" collapsed,
".." resolved, a trailing '/' dropped; a leading "//" collapses to "/", unlike posixpath.normpath), and when cwd is given
a leading "<cwd>/" is dropped. The case of a path is kept. Anything else is a name: lower-cased, runs of whitespace
collapsed to one space.

edit_paths(paths, cwd) is what the package sends for the files an edit tool touches, in the check call of the pre-tool
hooks and in the paths column of session rows: each path cleaned and made relative to cwd as key() does, once per key,
and written so that Seeker's Key(p, cwd) keys it as a path even when the file name has no extension ("Makefile" is sent
as "./Makefile", so an edit of it is keyed path:Makefile from both harnesses)."""
import re

PATH, NAME = 'path:', 'name:'

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


def key(resource, cwd=''):
    """(key, kind) of a claimed resource, kind 'path' or 'name'."""
    r = (resource or '').strip(GO_SPACE)
    if r.startswith(PATH):
        return PATH + normalise_path(r[len(PATH):].strip(GO_SPACE), cwd), 'path'
    if r.startswith(NAME):
        return NAME + normalise_name(r[len(NAME):]), 'name'
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
