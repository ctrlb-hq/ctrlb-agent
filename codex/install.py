"""Connect Codex to CtrlB. Writes one block into $CODEX_HOME/config.toml (default ~/.codex/config.toml):

  [mcp_servers.ctrlb]   Seeker's /mcp URL; the key is read from the CTRLB_KEY environment variable
                        (bearer_token_env_var), never written into the file; X-CtrlB-Run and X-CtrlB-Agent are sent
                        from CTRLB_RUN and CTRLB_AGENT when they are set (env_http_headers)
  [[hooks.<Event>]]     SessionStart, SubagentStart, PreCompact, SessionEnd, Stop, SubagentStop and PreToolUse
                        (matcher apply_patch), each calling codex/hook.py from this repo
  [otel]                only with --otel: Codex's own OTLP log export, straight to the lake's OTLP endpoint

Usage:
  python3 -I codex/install.py [--mcp-url URL] [--codex-home DIR | --config FILE] [--dry-run]
  python3 -I codex/install.py --otel --otel-endpoint URL [--otel-header NAME=VALUE ...] ...
  python3 -I codex/install.py --uninstall [--dry-run]

The MCP URL defaults to CTRLB_MCP_URL, or CTRLB_URL + /mcp, or the url in ~/.ctrlb/config.json (bin/configure.py).
The block sits between two marker comments. A run replaces the block, so running it twice changes nothing; a run
that would change nothing writes nothing. Before writing, the old file is copied to config.toml.ctrlb-backup-<time>.
--dry-run prints the diff and writes nothing. Codex skips new or changed hooks until you trust them with /hooks."""
import argparse, datetime, difflib, json, os, re, shutil, sys
sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'lib'))
import common  # noqa: E402

BEGIN = '# >>> ctrlb-agent: written by codex/install.py; run it again to change this block, --uninstall to remove it'
END = '# <<< ctrlb-agent'
HOOK = os.path.realpath(os.path.join(HERE, 'hook.py'))
# (event, hook.py mode, timeout seconds, matcher, async)
HOOKS = [('SessionStart', 'session-start', 10, None, False),
         ('SubagentStart', 'subagent-start', 10, None, False),
         ('PreCompact', 'flush', 10, None, False),
         ('SessionEnd', 'session-end', 10, None, False),
         ('Stop', 'upload', 120, None, True),
         ('SubagentStop', 'upload', 120, None, True),
         ('PreToolUse', 'pre-tool', 5, 'apply_patch', False)]
EVENT_KEYS = {'hooks.' + h[0] for h in HOOKS}
HEADER_RE = re.compile(r'^\s*\[(\[?)\s*([^\[\]]+?)\s*\]\]?\s*(?:#.*)?$')
KEY_RE = re.compile(r'^\s*([A-Za-z0-9_\-."\' ]+?)\s*=')


class InstallError(Exception):
    pass


def q(s):
    """A TOML basic string (JSON string escaping is valid TOML)."""
    return json.dumps(s)


def _name(dotted):
    return '.'.join(part.strip().strip('"\'') for part in dotted.split('.'))


def block(mcp_url, otel_endpoint='', otel_headers=None):
    lines = [BEGIN,
             '[mcp_servers.ctrlb]',
             f'url = {q(mcp_url)}',
             'bearer_token_env_var = "CTRLB_KEY"',
             'env_http_headers = { "X-CtrlB-Run" = "CTRLB_RUN", "X-CtrlB-Agent" = "CTRLB_AGENT" }',
             '']
    for event, mode, timeout, matcher, run_async in HOOKS:
        lines.append(f'[[hooks.{event}]]')
        if matcher:
            lines.append(f'matcher = {q(matcher)}')
        lines += [f'[[hooks.{event}.hooks]]', 'type = "command"', f'command = {q(f"python3 -I {q(HOOK)} {mode}")}',
                  f'timeout = {timeout}']
        if run_async:
            lines.append('async = true')
        lines.append('')
    if otel_endpoint:
        lines += ['[otel]', 'log_user_prompt = true', 'log_agent_responses = true', '',
                  '[otel.exporter.otlp-http]', f'endpoint = {q(otel_endpoint)}', 'protocol = "json"']
        if otel_headers:
            lines.append('headers = { ' + ', '.join(f'{q(k)} = {q(v)}' for k, v in otel_headers) + ' }')
        lines.append('')
    lines.append(END)
    return '\n'.join(lines) + '\n'


def split(text):
    """(text before the block, the block, text after it). The block is '' when there is none."""
    lines = text.splitlines(keepends=True)
    starts = [i for i, ln in enumerate(lines) if ln.rstrip('\n') == BEGIN]
    ends = [i for i, ln in enumerate(lines) if ln.rstrip('\n') == END]
    if not starts and not ends:
        return text, '', ''
    if len(starts) != 1 or len(ends) != 1 or ends[0] < starts[0]:
        raise InstallError('the ctrlb-agent markers in the file are broken (missing, repeated or out of order); '
                           'fix or delete the block by hand and run again')
    s, e = starts[0], ends[0]
    return ''.join(lines[:s]), ''.join(lines[s:e + 1]), ''.join(lines[e + 1:])


def conflicts(text, otel):
    """Tables or keys outside the block that the block would redefine (TOML forbids defining a table twice)."""
    found, table = [], ''
    for n, ln in enumerate(text.splitlines(), 1):
        m = HEADER_RE.match(ln)
        if m:
            table = _name(m.group(2))
            names, array = [table], bool(m.group(1))
        else:
            k = KEY_RE.match(ln)
            if not k:
                continue
            names, array = [_name((table + '.' if table else '') + k.group(1))], False
        for full in names:
            if full == 'mcp_servers.ctrlb' or full.startswith('mcp_servers.ctrlb.'):
                found.append(f'line {n}: {ln.strip()} (an MCP server named ctrlb already exists; remove it with '
                             f'`codex mcp remove ctrlb` and run again)')
            elif otel and (full == 'otel' or full.startswith('otel.')):
                found.append(f'line {n}: {ln.strip()} (an [otel] table already exists; remove it or run without --otel)')
            elif full in EVENT_KEYS and not array:
                found.append(f'line {n}: {ln.strip()} (this event is defined as a table or a key here; the block adds '
                             f'[[hooks.<Event>]] entries, so write these in that form too and run again)')
    return found


def render(old, new_block):
    """The whole new file: old with the block replaced, removed (new_block '') or appended."""
    before, cur, after = split(old)
    if cur:
        if not new_block and before.endswith('\n\n'):
            before = before[:-1]                    # drop the blank line the install put before the block
        return before + new_block + after
    if not new_block:
        return old
    if before and not before.endswith('\n'):
        before += '\n'
    return before + ('\n' if before.strip() else '') + new_block


def validate(text):
    try:
        import tomllib                              # Python 3.11+
    except ImportError:
        return
    try:
        doc = tomllib.loads(text)
    except Exception as e:
        raise InstallError(f'the result would not be valid TOML ({e}); nothing written')
    if BEGIN in text and 'ctrlb' not in (doc.get('mcp_servers') or {}):
        raise InstallError('the result lacks [mcp_servers.ctrlb]; nothing written')


def backup_path(path):
    stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
    p, i = f'{path}.ctrlb-backup-{stamp}', 1
    while os.path.exists(p):
        p, i = f'{path}.ctrlb-backup-{stamp}-{i}', i + 1
    return p


def write(path, text):
    """Back up the old file, then replace it atomically, keeping its mode. Returns the backup path ('' when the file
    did not exist)."""
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    backup, mode = '', 0o600
    if os.path.exists(path):
        backup = backup_path(path)
        shutil.copy2(path, backup)
        mode = os.stat(path).st_mode & 0o777
    tmp = path + '.ctrlb-tmp'
    with open(tmp, 'w') as fh:
        fh.write(text)
    os.chmod(tmp, mode)
    os.replace(tmp, path)
    return backup


def parse_headers(items):
    out = []
    for h in items or []:
        name, sep, value = h.partition('=')
        if not sep or not name.strip():
            raise InstallError(f'--otel-header takes NAME=VALUE, got {h!r}')
        out.append((name.strip(), value.strip()))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description='Write the CtrlB MCP server, hooks and optional OTEL export into Codex config.toml.')
    ap.add_argument('--mcp-url', default='', help="Seeker's /mcp URL (default: CTRLB_MCP_URL, or CTRLB_URL + /mcp)")
    ap.add_argument('--codex-home', default='', help='Codex home (default: $CODEX_HOME, else ~/.codex)')
    ap.add_argument('--config', default='', help='config.toml to edit (default: <codex home>/config.toml)')
    ap.add_argument('--otel', action='store_true', help='also write [otel]: Codex log export to the lake OTLP endpoint')
    ap.add_argument('--otel-endpoint', default='', help='with --otel: <lake>/api/<org>/<stream>/_otel/v1/logs')
    ap.add_argument('--otel-header', action='append', default=[], metavar='NAME=VALUE',
                    help='with --otel: a header for the OTLP export (stored in config.toml as given)')
    ap.add_argument('--uninstall', action='store_true', help='remove the block')
    ap.add_argument('--dry-run', action='store_true', help='print the diff and write nothing')
    a = ap.parse_args(argv)
    try:
        home = os.path.expanduser(a.codex_home or os.environ.get('CODEX_HOME') or '~/.codex')
        path = os.path.abspath(os.path.expanduser(a.config)) if a.config else os.path.join(home, 'config.toml')
        cfg = common.config()
        if a.uninstall:
            new_block = ''
        else:
            mcp_url = (a.mcp_url or cfg['mcp_url']).strip()
            if not mcp_url:
                raise InstallError('no MCP URL: pass --mcp-url "<your CtrlB URL>/mcp", set CTRLB_MCP_URL, or run bin/configure.py')
            if not re.match(r'^https?://[^\s"]+$', mcp_url):
                raise InstallError(f'--mcp-url must be an http(s) URL, got {mcp_url!r}')
            headers = parse_headers(a.otel_header)
            if a.otel and not a.otel_endpoint:
                raise InstallError('--otel needs --otel-endpoint "<lake>/api/<org>/<stream>/_otel/v1/logs"')
            if (a.otel_endpoint or headers) and not a.otel:
                raise InstallError('--otel-endpoint and --otel-header need --otel')
            if cfg['key'] and any(cfg['key'] in s for s in [mcp_url, a.otel_endpoint] + [v for _, v in headers]):
                raise InstallError('the CtrlB key must not be written into config.toml: Codex reads it from CTRLB_KEY')
            new_block = block(mcp_url, a.otel_endpoint if a.otel else '', headers)
        old = ''
        if os.path.exists(path):
            with open(path) as fh:
                old = fh.read()
        before, _, after = split(old)
        if not a.uninstall:
            found = conflicts(before + after, a.otel)
            if found:
                raise InstallError('config.toml already defines what the block writes:\n  ' + '\n  '.join(found))
        new = render(old, new_block)
        validate(new)
    except InstallError as e:
        print(f'codex/install.py: {e}', file=sys.stderr)
        return 2
    if new == old:
        print(f'{path}: already up to date, nothing written')
        return 0
    diff = ''.join(difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True),
                                        fromfile=path, tofile=path + ' (new)'))
    if a.dry_run:
        print(diff, end='' if diff.endswith('\n') else '\n')
        print('dry run: nothing written')
        return 0
    backup = write(path, new)
    print(f'{"removed the ctrlb-agent block from" if a.uninstall else "wrote the ctrlb-agent block to"} {path}')
    print(f'backup of the previous file: {backup}' if backup else 'there was no config.toml before; created it')
    if not a.uninstall:
        if a.otel and headers:
            print('note: the --otel-header values are stored in config.toml as given')
        print('next:\n  export CTRLB_URL="<your CtrlB URL>"\n  export CTRLB_KEY="<your key>"\n'
              '  start Codex and run /hooks once to trust the new hooks (Codex skips new or changed hooks until then)\n'
              '  for several agents in one run, set the same CTRLB_RUN for each of them')
    return 0


if __name__ == '__main__':
    sys.exit(main())
