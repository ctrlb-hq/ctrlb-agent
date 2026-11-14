"""Secret redaction, shared by every parser. Runs on each row's body and command before upload.
Pattern matching cannot catch every secret; it catches the common shapes (keys, tokens, Authorization values,
assignments to names ending in SECRET, TOKEN, PASSWORD or KEY, passwords in URLs, credential headers such as
X-API-Key and Cookie, and flags such as --token and --password)."""
import re

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
    # credential headers, in curl -H/--header, httpie Name:value and raw requests. A value already redacted by an
    # earlier pattern is left alone, so a secret is counted once.
    re.compile(r'(?i)(\b(?:x-api-key|api-key|x-auth-token|x-access-token|private-token|x-amz-security-token)\s*:\s*)'
               r'(?!\[REDACTED\])[^\s"\';,]{4,}'),
    re.compile(r'(?i)(\b(?:proxy-)?authorization\s*:\s*(?:[A-Za-z][A-Za-z0-9_\-]*\s+)?)(?!\[REDACTED\])[A-Za-z0-9._~+/=\-]{8,}'),
    re.compile(r'(?i)(\b(?:set-)?cookie\s*:\s*)(?!\[REDACTED\])[^\s=;"\']+=[^"\'\r\n]*'),
    # credential flags: --token <value>, --api-key=<value>, --password "<value>"; a value starting with -, < or $ is another
    # flag, a placeholder or a variable reference
    re.compile(r'(?i)((?<![\w-])--(?:[a-z0-9]+-)*(?:token|api-?key|password|passwd|secret|access-key|auth-token|access-token)'
               r'(?:=|\s+)["\']?)(?![-<$]|\[REDACTED\])[^\s"\']{4,}'),
]


def _sub(m):
    g = m.groups()
    if len(g) == 2:
        return g[0] + '[REDACTED]' + g[1]
    return (g[0] if g else '') + '[REDACTED]'


def redact(text):
    """(text with secrets replaced by [REDACTED], number of replacements)."""
    if not text:
        return text or '', 0
    n = 0
    for rx in REDACT:
        text, k = rx.subn(_sub, text)
        n += k
    return text, n
