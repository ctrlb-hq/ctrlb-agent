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
SECRET_NAME = (r'(?:[a-z0-9]+[_\-.])*(?:password|passwd|secret|token|client_secret|api_?key|private_?key|'
               r'access_?key|auth_?token|access_token|refresh_token)')
UPPER_NAME = (r'(?:[A-Z][A-Z0-9_]*(?:SECRET|TOKEN|PASSWORD|PASSWD|API_KEY|APIKEY|PRIVATE_KEY|ACCESS_KEY|_KEY)'
              r'[A-Z0-9_]*|SECRET|(?:TOKEN|PASSWORD|PASSWD|API_KEY|APIKEY|PRIVATE_KEY|ACCESS_KEY)[A-Z0-9_]*|KEY)')
# Names covered before the bare token/private-key additions must never use the code exception.
LEGACY_NAME = re.compile(r'\b(?:[A-Z][A-Z0-9_]*(?:SECRET|TOKEN|PASSWORD|PASSWD|API_KEY|APIKEY|PRIVATE_KEY|ACCESS_KEY|_KEY)'
                         r'[A-Z0-9_]*|(?i:(?:[a-z0-9]+[_\-.])*(?:password|passwd|secret|client_secret|api_?key|'
                         r'access_?key|auth_?token|access_token|refresh_token)))$')
IDENT = r'[A-Za-z_][A-Za-z0-9_]*'
CODE_INDEX = '(?:' + IDENT + '|"' + IDENT + '"|' + "'" + IDENT + "')"
CODE_EXPR = re.compile(IDENT + r'(?:\.' + IDENT + r'|\((?:' + CODE_INDEX + r'(?:, *' + CODE_INDEX + r')*)?\)|'
                       r'\[' + CODE_INDEX + r'\])+')
HEADER_NAME = r'(?:x-api-key|api-key|x-auth-token|x-access-token|private-token|x-amz-security-token)'
CREDENTIAL_NAME = re.compile(r'(?i)\b(?:' + SECRET_NAME + '|' + HEADER_NAME + r'|(?:proxy-)?authorization|(?:set-)?cookie)$')
HEADER_VALUE = (r'(?:' + QUOTED_VALUE + '|' + r'''(?!\\["']|\[REDACTED\])[^\s"';,]+)''')
AUTH_VALUE = (r'''(?!["'](?:bearer|basic|token)\s*(?:["']|[<$]))(?:''' + QUOTED_VALUE +
              r'|(?:[A-Za-z][A-Za-z0-9_\-]*\s+)?[A-Za-z0-9._~+/=\-]{8,})')

REDACT = [
    re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----', re.S),
    re.compile(r'\b(?:AKIA|ASIA)[0-9A-Z]{16}\b'),
    re.compile(r'\b(?:sk-ant-|sk-proj-|sk-)[A-Za-z0-9_\-]{16,}'),
    re.compile(r'\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b'),
    re.compile(r'\bgithub_pat_[A-Za-z0-9_]{20,}\b'),
    re.compile(r'\bxox[abposr]-[A-Za-z0-9\-]{10,}'),
    re.compile(r'\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}'),  # JWT
    re.compile(r'''(?i)((?:proxy-)?authorization(?:\\?["'])?\s*(?::|=(?!=))\s*(?:(?:bearer|basic|token)\s+)?)(?!\[REDACTED\])''' + AUTH_VALUE),
    re.compile(r'(?i)(\bbearer\s+)[A-Za-z0-9._~+/=\-]{12,}'),
    # assignments; a value starting with < or $ is a placeholder or a variable reference, not a secret
    re.compile(r'(\b' + UPPER_NAME + r'\s*=(?!=)\s*)' + SHELL_VALUE),
    # the same in any case, also under a prefix (aws_secret_access_key, db_password): round 8 posted an AWS-style
    # config line as an artefact because \b does not fire between "_" and "access"
    re.compile(r'''(?i)((?:\\?["'])?\b''' + SECRET_NAME + r'''(?:\\?["'])?\s*(?::|=(?!=))\s*)''' + VALUE),
    re.compile(r'(\b[a-z][a-z0-9+.\-]*://[^\s:/@]+:)[^\s@/]+(@)'),
    # credential headers, in curl -H/--header, httpie Name:value and raw requests. A value already redacted by an
    # earlier pattern is left alone, so a secret is counted once.
    re.compile(r'''(?i)(\b''' + HEADER_NAME + r'''(?:\\?["'])?\s*:\s*)''' + HEADER_VALUE),
    re.compile(r'''(?i)(\b(?:set-)?cookie["']?\s*:\s*)''' + QUOTED_VALUE),
    re.compile(r'''(?i)(\b(?:set-)?cookie["']?\s*:\s*)(?!\[REDACTED\])[^\s=;"']+=[^"'\r\n]*'''),
    # credential flags: --token <value>, --api-key=<value>, --password "<value>"; a value starting with -, < or $ is another
    # flag, a placeholder or a variable reference
    re.compile(r'(?i)((?<![\w-])--(?:[a-z0-9]+-)*(?:token|api-?key|password|passwd|secret|access-key|auth-token|access-token)'
               r'(?:=|\s+))(?![-<$]|\[REDACTED\])' + SHELL_VALUE),
]


def _sub(m):
    g = m.groups()
    if len(g) == 2:
        return g[0] + '[REDACTED]' + g[1]
    prefix = g[0] if g else ''
    value = m.group()[len(prefix):]
    quote = '\\"' if value.startswith('\\"') else value[0] if value and value[0] in ('"', "'") else ''
    if prefix.lstrip().startswith('--') and not quote and len(value) < 4:
        return m.group()
    name = prefix.split('=', 1)[0].strip()
    # Generic uppercase *_KEY names retain their original four-character minimum (e.g. PRIMARY_KEY = "id").
    if LEGACY_NAME.fullmatch(name) and not CREDENTIAL_NAME.fullmatch(name):
        literal = value[len(quote):]
        if quote and literal.endswith(quote):
            literal = literal[:-len(quote)]
        if len(literal) < 4:
            return m.group()
    if prefix and not quote and (':' in prefix or '=' in prefix) and not prefix.lstrip().startswith('--') and not re.match(
            r'(?i)(?:' + HEADER_NAME + r'|(?:proxy-)?authorization|(?:set-)?cookie)\b', prefix):
        if prefix.startswith(('"', "'", '\\"')) and re.fullmatch(r'(?i)null|true|false', value):
            return m.group()
        # Only new names in clearly spaced code assignments can be exempt. Check the whole expression, since
        # the value match may end at a comma in a call. Punctuation in a literal must never turn into an exemption.
        if re.fullmatch(r'(?:self\.)?' + IDENT + r'[ \t]+=[ \t]+', prefix):
            expression = m.string[m.end(1):].split('\n', 1)[0].strip()
            if not LEGACY_NAME.search(name) and (CODE_EXPR.fullmatch(expression) or
                    ('.' in name and re.fullmatch(IDENT, expression))):
                return m.group()
    return prefix + quote + '[REDACTED]' + quote


def redact(text, _credential=False):
    """(text or JSON value with secrets replaced by [REDACTED], number of replacements)."""
    if isinstance(text, dict):
        out, n = {}, 0
        for key, value in text.items():
            credential = _credential or (isinstance(key, str) and (CREDENTIAL_NAME.search(key) or
                                        (key != 'KEY' and re.fullmatch(UPPER_NAME, key))))
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
        def replace(m):
            nonlocal n
            value = _sub(m)
            n += int(value != m.group())
            return value
        text = rx.sub(replace, text)
    return text, n
