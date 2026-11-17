"""Secret redaction, shared by every parser. Runs on each row's body and command before upload.
Pattern matching cannot catch every secret; it catches the common shapes (keys, tokens, Authorization values,
assignments to names ending in SECRET, TOKEN, PASSWORD or KEY, passwords in URLs, credential headers such as
X-API-Key and Cookie, and flags such as --token and --password)."""
import json, re

# Quoted values may contain spaces and escaped quotes. Keep placeholders and variable references intact.
ESCAPED_VALUE = (r'''\\"(?![<$]|\[REDACTED\](?:\\"|$))(?:(?!\\")[^\r\n])*(?:\\"|(?=\r?\n|$))''')
QUOTED_VALUE = (r'''(?:''' + ESCAPED_VALUE + r'''|"(?![<$]|\[REDACTED\](?:"|$))(?:\\[^\r\n]|[^"\\\r\n])*(?:"|(?=\r?\n|$))|'''
                r"""'(?![<$]|\[REDACTED\](?:'|$))(?:\\[^\r\n]|[^'\\\r\n])*(?:'|(?=\r?\n|$)))""")
VALUE = (r'(?:' + QUOTED_VALUE + '|' +
         r'''(?![<$]|\\["']|\[REDACTED\])[^\s"',}]+)''')
SHELL_VALUE = (r'(?:' + QUOTED_VALUE + '|' +
               r'''(?![<$]|\\["']|\[REDACTED\])[^\s"']+)''')
SECRET_SUFFIXES = ('password', 'passwd', 'secret', 'token', 'client_secret', 'api_key', 'apikey',
                   'private_key', 'privatekey', 'access_key', 'accesskey', 'auth_token', 'authtoken',
                   'access_token', 'refresh_token')
UPPER_PARTS = ('SECRET', 'TOKEN', 'PASSWORD', 'PASSWD', 'API_KEY', 'APIKEY', 'PRIVATE_KEY', 'ACCESS_KEY', '_KEY')
NAME = re.compile(r'[A-Z][A-Z0-9_]*$')
# Scan each complete candidate once. Starting another search at each hyphen or dot makes a long key quadratic.
ASSIGNMENT = re.compile(r'''(?<![\w.\-])((?:\\?["'])?([\w.\-]+)(?:\\?["'])?\s*(?::=?|=(?:>)?(?!=))\s*)''')
CASE_EQUIVALENTS = str.maketrans({'ſ': 's', 'İ': 'i', 'ı': 'i', 'K': 'k'})


def _upper_name(name, legacy=False):
    name = name.rsplit('.', 1)[-1].rsplit('-', 1)[-1]
    if not NAME.fullmatch(name):
        return False
    if any(name.find(part, 1) >= 1 for part in UPPER_PARTS):
        return True
    return not legacy and (name in ('SECRET', 'KEY') or name.startswith(UPPER_PARTS[1:-1]))


def _secret_name(name, legacy=False):
    name = name.translate(CASE_EQUIVALENTS).lower()
    for suffix in SECRET_SUFFIXES:
        if legacy and suffix in ('token', 'private_key', 'privatekey'):
            continue
        if name.endswith(suffix):
            prefix = name[:-len(suffix)]
            if not prefix or not prefix[-1].isalnum():
                # Any non-word character can introduce a name, including spaces in JSON keys. A leading
                # or repeated underscore stays part of the word and cannot introduce a suffix match.
                head = re.split(r'\W+', prefix)[-1]
                return not head or bool(re.fullmatch(r'(?:[a-z0-9]+_)+', head))
    return False


def _credential_name(name):
    if _secret_name(name):
        return True
    name = name.translate(CASE_EQUIVALENTS).lower()
    for suffix in ('x-api-key', 'api-key', 'x-auth-token', 'x-access-token', 'private-token',
                   'x-amz-security-token', 'authorization', 'cookie'):
        prefix = name.lower()[:-len(suffix)]
        if name.lower().endswith(suffix) and (not prefix or not (prefix[-1].isalnum() or prefix[-1] == '_')):
            return True
    return False


IDENT = r'[A-Za-z_][A-Za-z0-9_]*'
CODE_INDEX = '(?:' + IDENT + '|"' + IDENT + '"|' + "'" + IDENT + "')"
CODE_EXPR = re.compile(IDENT + r'(?:\.' + IDENT + r'|\((?:' + CODE_INDEX + r'(?:, *' + CODE_INDEX + r')*)?\)|'
                       r'\[' + CODE_INDEX + r'\])+')
HEADER_NAME = r'(?:x-api-key|api-key|x-auth-token|x-access-token|private-token|x-amz-security-token)'
HEADER_VALUE = (r'(?:' + QUOTED_VALUE + '|' + r'''(?!\\["']|\[REDACTED\])[^\s"';,]+)''')
AUTH_VALUE = (r'''(?!["'](?:bearer|basic|token)\s*(?:["']|[<$]))(?:''' + QUOTED_VALUE +
              r'|(?:[A-Za-z][A-Za-z0-9_\-]*\s+)?[A-Za-z0-9._~+/=\-]{8,})')
FLAG_PREFIX = (r'(?i)((?<![\w-])--(?:[a-z0-9]+-)*(?:token|api-?key|password|passwd|secret|access-key|auth-token|access-token)'
               r'(?:=|\s+))')

REDACT = [
    'private_keys',
    re.compile(r'\b(?:AKIA|ASIA)[0-9A-Z]{16}\b'),
    re.compile(r'\b(?:sk-ant-|sk-proj-|sk-)[A-Za-z0-9_\-]{16,}'),
    re.compile(r'\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b'),
    re.compile(r'\bgithub_pat_[A-Za-z0-9_]{20,}\b'),
    re.compile(r'\bxox[abposr]-[A-Za-z0-9\-]{10,}'),
    re.compile(r'\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}'),  # JWT
    re.compile(r'''(?i)((?:proxy-)?authorization(?:\\?["'])?\s*(?::|=(?!=))\s*(?:(?:bearer|basic|token)\s+)?)(?!\[REDACTED\])''' + AUTH_VALUE),
    re.compile(r'(?i)(\bbearer\s+)[A-Za-z0-9._~+/=\-]{12,}'),
    # Assignment passes run here, before URL passwords and headers.
    'assignments',
    re.compile(r'((?<![\w+.\-])[\w+.\-]+://[^\s:/@]+:)[^\s@/]+(@)'),
    # credential headers, in curl -H/--header, httpie Name:value and raw requests. A value already redacted by an
    # earlier pattern is left alone, so a secret is counted once.
    re.compile(r'''(?i)(\b''' + HEADER_NAME + r'''(?:\\?["'])?\s*:\s*)''' + HEADER_VALUE),
    re.compile(r'''(?i)(\b(?:set-)?cookie["']?\s*:\s*)''' + QUOTED_VALUE),
    'cookies',
    # credential flags: --token <value>, --api-key=<value>, --password "<value>"; a value starting with -, < or $ is another
    # flag, a placeholder or a variable reference
    re.compile(FLAG_PREFIX + r'(?![-<$]|\[REDACTED\])' + SHELL_VALUE),
    # Assignment matching can leave a comma/brace suffix on an equals-form flag. Trim it only after all
    # headers and later flags have been checked, so masking their names cannot hide their values.
    re.compile(FLAG_PREFIX + r'''\[REDACTED\][^\s"']+'''),
    # A brace can end an assignment value inside a header. Finish its suffix after later flags are checked.
    re.compile(r'''(?i)(\b''' + HEADER_NAME + r'''(?:\\?["'])?\s*:\s*)\[REDACTED\]}[^\s"';,]*'''),
    'operators',
]


def _replacement(prefix, value, expression=''):
    quote = '\\"' if value.startswith('\\"') else value[0] if value and value[0] in ('"', "'") else ''
    name = prefix.split('=', 1)[0].strip()
    if (_upper_name(name, True) or _secret_name(name, True)) and not _credential_name(name):
        literal = value[len(quote):]
        if quote and literal.endswith(quote):
            literal = literal[:-len(quote)]
        if len(literal) < 4:
            return prefix + value
    if prefix and not quote and (':' in prefix or '=' in prefix) and not prefix.lstrip().startswith('--') and not re.match(
            r'(?i)(?:' + HEADER_NAME + r'|(?:proxy-)?authorization|(?:set-)?cookie)\b', prefix):
        if (prefix.startswith(('"', "'", '\\"')) and re.search(r'''["']\s*:\s*$''', prefix)
                and re.fullmatch(r'(?i)null|true|false', value)):
            return prefix + value
        if re.fullmatch(r'(?:self\.)?' + IDENT + r'[ \t]+=[ \t]+', prefix):
            if not (_upper_name(name, True) or _secret_name(name, True)) and (CODE_EXPR.fullmatch(expression) or
                    ('.' in name and re.fullmatch(IDENT, expression))):
                return prefix + value
    return prefix + quote + '[REDACTED]' + quote


def _sub(m):
    g = m.groups()
    if len(g) == 2:
        return g[0] + '[REDACTED]' + g[1]
    prefix = g[0] if g else ''
    value = m.group()[len(prefix):]
    if prefix.lstrip().startswith('--') and value[:1] not in ('"', "'", '\\') and len(value) < 4:
        return m.group()
    return _replacement(prefix, value)


def _assignments(text, upper, operators=False):
    out, end, n = [], 0, 0
    value_rx = re.compile(SHELL_VALUE if upper else VALUE)
    for m in ASSIGNMENT.finditer(text):
        start = max(m.start(), end)
        if m.start(2) < end:
            continue
        prefix, name = m.groups()
        prefix = text[start:m.end()]
        operator = ':=' if ':=' in prefix else '=>' if '=>' in prefix else ''
        if bool(operator) != operators:
            if operators or not operator or (upper and operator != '=>'):
                continue
        if not (_upper_name(name) and '=' in prefix if upper else _secret_name(name)):
            continue
        if upper and not operators and ('"' in text[m.end(2):m.end()] or "'" in text[m.end(2):m.end()]):
            continue
        value = value_rx.match(text, m.end())
        if not value and ((operator == ':=' and text.startswith('<', m.end())) or
                          (operator == '=>' and text[m.end():m.end() + 1] in ('<', '$'))):
            # The old colon/equals passes covered these literals. Keep := dollar-variable placeholders exempt.
            value = re.compile(r'''[^\s"']+''' if upper else r'''[^\s"',}]+''').match(text, m.end())
        if not value:
            continue
        if bool(operator) != operators:
            # Check the name and actual value before scanning the legacy value, so repeated non-credentials
            # and placeholders stay linear. Spaced/quoted values wait until after other headers and flags.
            legacy = value_rx.match(text, start + prefix.index(operator) + 1)
            if not legacy or legacy.end() <= m.end():
                continue
        # Only a short, complete code expression can be exempt; do not copy the remaining transcript for
        # every assignment on a long line.
        expression = text[m.end():m.end() + 2048].split('\n', 1)[0].strip()
        if m.end() + 2048 < len(text) and '\n' not in text[m.end():m.end() + 2048]:
            expression = ''
        clean = _replacement(prefix, value.group(), expression)
        out.extend((text[end:start], clean))
        end = value.end()
        n += int(clean != text[start:end])
    out.append(text[end:])
    return ''.join(out), n


PRIVATE_START = re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----')
PRIVATE_END = re.compile(r'-----END [A-Z ]*PRIVATE KEY-----')
COOKIE_START = re.compile(r'''(?i)\b(?:set-)?cookie["']?\s*:\s*''')
COOKIE_PAIR_NAME = re.compile(r'''[^\s=;"']+''')
COOKIE_BODY = re.compile(r'''[^"'\r\n]*''')


def _private_keys(text):
    out, pos, n = [], 0, 0
    while True:
        start = PRIVATE_START.search(text, pos)
        if not start:
            break
        end = PRIVATE_END.search(text, start.end())
        if not end:
            break                         # later starts cannot have an end either
        out.extend((text[pos:start.start()], '[REDACTED]'))
        pos = end.end()
        n += 1
    out.append(text[pos:])
    return ''.join(out), n


def _cookies(text):
    out, pos, skip, key_end, n = [], 0, 0, 0, 0
    for start in COOKIE_START.finditer(text):
        if start.start() < skip or text.startswith('[REDACTED]', start.end()):
            continue
        if start.end() >= key_end:
            key = COOKIE_PAIR_NAME.match(text, start.end())
            if not key:
                continue
            key_end = key.end()
        if key_end == len(text) or text[key_end] != '=':
            # Another Cookie: may begin inside this run and end beyond it. Reuse the run's end so repeated
            # prefixes stay linear, but let the next header check its own key.
            continue
        end = COOKIE_BODY.match(text, key_end).end()
        out.extend((text[pos:start.end()], '[REDACTED]'))
        pos = skip = end
        n += 1
    out.append(text[pos:])
    return ''.join(out), n


def redact(text, _credential=False):
    """(text or JSON value with secrets replaced by [REDACTED], number of replacements)."""
    if isinstance(text, dict):
        out, n = {}, 0
        for key, value in text.items():
            credential = _credential or (isinstance(key, str) and (_credential_name(key) or
                                        (key != 'KEY' and _upper_name(key))))
            clean_key, k = redact(key) if isinstance(key, str) else (key, 0)
            n += k
            out[clean_key], k = redact(value, credential)
            n += k
        return out, n
    if isinstance(text, list):
        values = [redact(value, _credential) for value in text]
        return [v for v, n in values], sum(n for v, n in values)
    if not isinstance(text, str):
        if _credential and isinstance(text, (int, float)) and not isinstance(text, bool):
            return '[REDACTED]', 1
        return text, 0
    if _credential:
        if (text.startswith(('$', '<')) or text == '[REDACTED]' or
                re.match(r'(?i)^(?:bearer|basic|token)\s+[$<]', text)):
            return text, 0
        return '[REDACTED]', 1
    if not text:
        return text or '', 0
    # Tool results can themselves contain JSON strings. Decode before matching so escaped quotes and nested
    # credential dictionaries receive the same treatment as parsed tool inputs.
    if text.lstrip().startswith(('{', '[')):
        try:
            value = json.loads(text)
        except ValueError:
            value = None
        if isinstance(value, (dict, list)):
            value, n = redact(value)
            return (json.dumps(value, ensure_ascii=False) if n else text), n
    n = 0
    for rx in REDACT:
        if rx in ('private_keys', 'cookies'):
            text, k = (_private_keys if rx == 'private_keys' else _cookies)(text)
            n += k
            continue
        if rx in ('assignments', 'operators'):
            # New operator forms run after legacy headers/flags so their names cannot hide another value.
            text, k = _assignments(text, True, rx == 'operators')
            n += k
            text, k = _assignments(text, False, rx == 'operators')
            n += k
            continue
        def replace(m):
            nonlocal n
            value = _sub(m)
            n += int(value != m.group())
            return value
        text = rx.sub(replace, text)
    return text, n
