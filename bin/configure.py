"""Run this yourself in a terminal, not through an agent:  python3 -I bin/configure.py
Asks for Seeker's URL, the sessions dataset, an optional run id, and your key (hidden input), reads the key's user
from the key (its "email" claim, the one Seeker reads), writes ~/.ctrlb/config.json (mode 0600, readable only by you)
and checks that Seeker answers. Environment variables CTRLB_* override the file. Flags (for scripts): --url,
--dataset, --run, --key-stdin (read the key from one line of stdin instead of a hidden prompt; the key is never taken
from the command line). --user is accepted and ignored: the name on the rows is the key's user.
--local [--port N] runs ctrlb init without prompts and prints exports that read the key from config.json."""
import argparse, getpass, json, os, shlex, subprocess, sys
sys.dont_write_bytecode = True
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'lib'))
import common  # noqa: E402


def ask(label, default='', given=None):
    if given is not None:
        return given.strip()
    v = input(f'{label}' + (f' [{default}]' if default else '') + ': ').strip()
    return v or default


def configure_local(port):
    binary = common.ctrlb_binary()
    if not binary:
        sys.exit('ctrlb executable not found; install ctrlb or set CTRLB_BIN')
    args = [binary, 'init']
    if port is not None:
        args += ['--port', str(port)]
    try:
        # init may display the local key. Neither its output nor an error is echoed here.
        p = subprocess.run(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        sys.exit('ctrlb init failed; check the binary and CTRLB_HOME')
    if p.returncode:
        sys.exit('ctrlb init failed; check the binary and CTRLB_HOME')
    path = os.path.join(common.HOME, 'config.json')
    c = common._file_config()
    if c.get('mode') != 'local' or not c.get('mcp_url') or not c.get('key'):
        sys.exit('ctrlb init did not write a local config.json')
    print('Local mode configured. Add these to your shell profile for Claude Code:')
    print('  export CTRLB_MCP_URL=' + shlex.quote(c['mcp_url']))
    code = 'import json; print(json.load(open(' + repr(os.path.abspath(path)) + '))["key"])'
    print('  export CTRLB_KEY="$(python3 -I -c ' + shlex.quote(code) + ')"')


def main():
    ap = argparse.ArgumentParser()
    for f in ('url', 'dataset', 'user', 'run'):
        ap.add_argument('--' + f)
    ap.add_argument('--key-stdin', action='store_true')
    ap.add_argument('--local', action='store_true')
    ap.add_argument('--port', type=int)
    a = ap.parse_args()
    if a.port is not None and (not a.local or not 1 <= a.port <= 65535):
        ap.error('--port requires --local and a number from 1 to 65535')
    if a.local:
        if any(getattr(a, f) is not None for f in ('url', 'dataset', 'user', 'run')) or a.key_stdin:
            ap.error('--local accepts only --port')
        configure_local(a.port)
        return
    path = os.path.join(common.HOME, 'config.json')
    try:
        with open(path) as fh:
            old = json.load(fh)
    except Exception:
        old = {}
    c = dict(url=ask('Seeker URL (the CtrlB URL the Connect page shows)', old.get('url', ''), a.url).rstrip('/'),
             dataset=ask('Sessions dataset', old.get('dataset', common.DEFAULT_DATASET), a.dataset),
             run=ask('Run id for every session (Enter: each session is its own run, which is usual)', old.get('run', ''), a.run))
    if a.key_stdin:
        key = sys.stdin.readline()
    else:
        key = getpass.getpass('Key (hidden; Enter keeps the current one): ')
    c['key'] = key.strip() or old.get('key', '')
    c['mcp_url'] = c['url'] + '/mcp' if c['url'] else ''
    if not c['url'].startswith(('http://', 'https://')):
        sys.exit('the URL must start with http:// or https://')
    if not common.NAME_RE.match(c['dataset']):
        sys.exit('the dataset name must be letters, digits or _')
    if not c['key']:
        sys.exit('a key is required: mint one on the Connect page')
    if a.user:
        print('--user is ignored: the name on the rows is the key\'s user. Set CTRLB_AGENT for an agent label.')
    user = common.key_user(c['key'])
    if user:
        c['user'] = user
    print(f"the key's user (written as user_name): {user or '(none in the key; CtrlB fills it from the key)'}")
    os.makedirs(common.HOME, mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as fh:
        json.dump(c, fh, indent=1)
    os.chmod(path, 0o600)
    print(f'saved {path} (mode 0600)')
    cfg = common.config()
    try:
        common.runs(cfg, limit=1, timeout=15)
        print('Seeker answered: OK')
    except Exception as e:
        print(f'Seeker check failed: {str(e)[:300]}')
    print('The ctrlb tools in Claude Code read two environment variables. Add these to your shell profile:')
    print(f'  export CTRLB_MCP_URL="{c["mcp_url"]}"')
    print('  export CTRLB_KEY="<your key>"')


if __name__ == '__main__':
    main()
