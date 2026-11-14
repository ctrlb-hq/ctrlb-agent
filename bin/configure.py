"""Run this yourself in a terminal, not through an agent:  python3 -I bin/configure.py
Asks for Seeker's URL, the sessions dataset, an optional run id, and your key (hidden input), reads the key's user
from the key (its "email" claim, the one Seeker reads), writes ~/.ctrlb/config.json (mode 0600, readable only by you)
and checks that Seeker answers. Environment variables CTRLB_* override the file. Flags (for scripts): --url,
--dataset, --run, --key-stdin (read the key from one line of stdin instead of a hidden prompt; the key is never taken
from the command line). --user is accepted and ignored: the name on the rows is the key's user."""
import argparse, datetime, getpass, json, os, shutil, sys, tempfile
sys.dont_write_bytecode = True
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'lib'))
import common  # noqa: E402


def ask(label, default='', given=None):
    if given is not None:
        return given.strip()
    v = input(f'{label}' + (f' [{default}]' if default else '') + ': ').strip()
    return v or default


def save(path, c):
    """Write config.json atomically: a new 0600 temporary file in the same directory, then a replace, so an interrupted
    run leaves the old file whole. The old file is copied to config.json.ctrlb-backup-<time> (0600) first when the
    content changes. When config.json is a link, the file it points to is written (said on screen) and the link kept.
    Returns (the file written, the backup path or '')."""
    target = path
    if os.path.islink(path):
        target = os.path.realpath(path)
        print(f'{path} is a link to {target}; writing that file and keeping the link')
    text = json.dumps(c, indent=1)
    backup = ''
    if os.path.exists(target):
        with open(target) as fh:
            same = fh.read() == text
        if same:
            os.chmod(target, 0o600)
            return target, ''
        stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
        backup, i = f'{target}.ctrlb-backup-{stamp}', 1
        while os.path.exists(backup):
            backup, i = f'{target}.ctrlb-backup-{stamp}-{i}', i + 1
        fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as out, open(target, 'rb') as src:
            shutil.copyfileobj(src, out)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(target), prefix='.config.json.', suffix='.ctrlb-tmp')
    try:
        with os.fdopen(fd, 'w') as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return target, backup


def main():
    ap = argparse.ArgumentParser()
    for f in ('url', 'dataset', 'user', 'run'):
        ap.add_argument('--' + f)
    ap.add_argument('--key-stdin', action='store_true')
    a = ap.parse_args()
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
    common.private_dir(common.HOME)
    written, backup = save(path, c)
    print(f'saved {written} (mode 0600)')
    if backup:
        print(f'backup of the previous file: {backup} (mode 0600)')
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
