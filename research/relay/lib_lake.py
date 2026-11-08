"""A small lake client for the Relay research code: write rows to a stream and query it with SQL. Standard library only.

Env: CTRLB_URL (the lake, default http://127.0.0.1:5160), CTRLB_ORG (default), CTRLB_AUTH (the whole Authorization
header value; empty for an unauthenticated local lake)."""
import json, os, time, urllib.error, urllib.request

URL = os.environ.get('CTRLB_URL', 'http://127.0.0.1:5160').rstrip('/')
ORG = os.environ.get('CTRLB_ORG', 'default')
AUTH = os.environ.get('CTRLB_AUTH', '')
WINDOW_START = 1577836800000000  # 2020-01-01, in microseconds


def _post(path, body, timeout):
    req = urllib.request.Request(URL + path, data=json.dumps(body).encode(), headers={'Content-Type': 'application/json'})
    if AUTH:
        req.add_header('Authorization', AUTH)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f'HTTP {e.code}: {e.read()[:300].decode(errors="replace")}') from None
    try:
        return json.loads(raw or b'{}')
    except ValueError:
        return {'raw': raw[:300].decode(errors='replace')}


def ingest(stream, rows, timeout=60):
    return _post(f'/api/{ORG}/{stream}/_json_evolving', rows, timeout)


def search(stream, sql, size=100, timeout=60):
    body = dict(sql=sql, start_time=WINDOW_START, end_time=int((time.time() + 86400) * 1e6), size=size)
    body['from'] = 0
    return _post(f'/api/{ORG}/_search', body, timeout).get('hits', [])


def timed(fn, *a, **k):
    t0 = time.perf_counter()
    out = fn(*a, **k)
    return out, (time.perf_counter() - t0) * 1000


def q(s):
    """A value for a single-quoted SQL string."""
    return str(s).replace("'", "''")
