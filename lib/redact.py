"""Private tags and secret redaction, shared by every parser. clean() runs on each row's body and command before
upload, and on every other text the hooks send (notes, handoffs); private_paths() on each row's paths.

Private tags: text between <private> and </private> never leaves the machine; it is replaced with [PRIVATE]. The tags
match in any case and nest (the span ends at the close that balances the first open). The rules work on one text at
a time, which for a session row is one content block:
  - an open tag with no close strips to the end of that text;
  - a close tag with no open before it in the same text is left as it is, with the text before it. So a span opened
    in one block and closed in a later block is stripped only in the block that opened it: the later block's text
    up to the close is uploaded. Close the tag in the same message.

Secret redaction: pattern matching cannot catch every secret; it catches the common shapes (keys, tokens,
Authorization values, assignments to names ending in SECRET, TOKEN, PASSWORD or KEY, passwords in URLs)."""
import re

PRIVATE_MARK = '[PRIVATE]'
_TAG = re.compile(r'<(/?)private>', re.I)

REDACT = [
    re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----', re.S),
    re.compile(r'\b(?:AKIA|ASIA)[0-9A-Z]{16}\b'),
    re.compile(r'\b(?:sk-ant-|sk-proj-|sk-)[A-Za-z0-9_\-]{16,}'),
    re.compile(r'\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b'),
    re.compile(r'\bgithub_pat_[A-Za-z0-9_]{20,}\b'),
    re.compile(r'\bxox[abposr]-[A-Za-z0-9\-]{10,}'),
    re.compile(r'\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}'),  # JWT
    re.compile(r'(?i)(authorization\s*[:=]\s*["\']?(?:bearer|basic|token)?\s*)[A-Za-z0-9._~+/=\-]{8,}'),
    re.compile(r'(?i)(\bbearer\s+)[A-Za-z0-9._~+/=\-]{12,}'),
    # assignments; a value starting with < or $ is a placeholder or a variable reference, not a secret
    re.compile(r'(\b[A-Z][A-Z0-9_]*(?:SECRET|TOKEN|PASSWORD|PASSWD|API_KEY|APIKEY|PRIVATE_KEY|ACCESS_KEY|_KEY)[A-Z0-9_]*\s*=\s*["\']?)(?![<$])[^\s"\']{4,}'),
    # the same in any case, also under a prefix (aws_secret_access_key, db_password): round 8 posted an AWS-style
    # config line as an artefact because \b does not fire between "_" and "access"
    re.compile(r'(?i)(["\']?\b(?:[a-z0-9]+[_\-.])*(?:password|passwd|secret|client_secret|api_?key|access_?key|auth_?token|access_token|refresh_token)["\']?\s*[:=]\s*["\']?)(?![<$])[^\s"\',}]{4,}'),
    re.compile(r'(\b[a-z][a-z0-9+.\-]*://[^\s:/@]+:)[^\s@/]+(@)'),
]


def _sub(m):
    g = m.groups()
    if len(g) == 2:
        return g[0] + '[REDACTED]' + g[1]
    return (g[0] if g else '') + '[REDACTED]'


def private_spans(text):
    """The (start, end) character spans of text that are private, tags included: outermost spans only."""
    spans, depth, start = [], 0, 0
    for m in _TAG.finditer(text or ''):
        if not m.group(1):
            if depth == 0:
                start = m.start()
            depth += 1
        elif depth:
            depth -= 1
            if depth == 0:
                spans.append((start, m.end()))
    if depth:
        spans.append((start, len(text)))
    return spans


def strip_private(text):
    """(text with every private span replaced by [PRIVATE], number of spans)."""
    spans = private_spans(text)
    if not spans:
        return text or '', 0
    out, at = [], 0
    for a, b in spans:
        out += [text[at:a], PRIVATE_MARK]
        at = b
    out.append(text[at:])
    return ''.join(out), len(spans)


def private_paths(paths, raw_texts):
    """The paths (a list of strings) none of which is private: a path that holds a private tag is dropped, and so is a
    path that occurs in the row's raw texts (its body, its command) only inside private spans."""
    keep = []
    for p in paths:
        if not isinstance(p, str) or private_spans(p) or _TAG.search(p):
            continue
        hits = [(t, i) for t in raw_texts if t for i in _finds(t, p)]
        if hits and all(any(a <= i < b for a, b in private_spans(t)) for t, i in hits):
            continue
        keep.append(p)
    return keep


def _finds(text, needle):
    out, i = [], text.find(needle)
    while i >= 0:
        out.append(i)
        i = text.find(needle, i + 1)
    return out


def clean(text):
    """(text with private spans replaced by [PRIVATE], then secrets by [REDACTED], number of replacements): what every
    text the hooks send goes through."""
    text, n = strip_private(text)
    text, k = redact(text)
    return text, n + k


def redact(text):
    """(text with secrets replaced by [REDACTED], number of replacements). Private tags: clean."""
    if not text:
        return text or '', 0
    n = 0
    for rx in REDACT:
        text, k = rx.subn(_sub, text)
        n += k
    return text, n
