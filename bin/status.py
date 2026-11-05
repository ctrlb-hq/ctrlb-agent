"""Print ctrlb-agent's configuration (the key masked), the user the key carries (the name rows are written under),
whether the environment the ctrlb tools need is set, whether
Seeker answers and the latest runs, how many transcript files are tracked, and the last lines of the hook log."""
import json, os, sys, time
sys.dont_write_bytecode = True
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'lib'))
import common  # noqa: E402


def main():
    cfg = common.config()
    shown = {k: cfg[k] for k in ('url', 'mcp_url', 'dataset', 'user', 'run', 'agent', 'enforce', 'off')}
    shown['key'] = f"set ({len(cfg['key'])} characters)" if cfg['key'] else '(none)'
    print('config:', json.dumps(shown))
    print(f"key's user (written as user_name): {cfg['user'] or '(none in the key; CtrlB fills user_name from the key)'}")
    try:
        with open(os.path.join(common.HOME, 'config.json')) as fh:
            stored = (json.load(fh).get('user') or '').strip()
    except Exception:
        stored = ''
    if stored and stored != cfg['user']:
        print(f"config.json names user {stored!r}, which is not the key's user: it is ignored; run bin/configure.py again")
    if os.environ.get('CTRLB_USER'):
        print(f"CTRLB_USER={os.environ['CTRLB_USER']!r} is read as the agent label only"
              + (' (CTRLB_AGENT is set and wins)' if os.environ.get('CTRLB_AGENT') else '') + ', never as user_name')
    for var in ('CTRLB_MCP_URL', 'CTRLB_KEY'):
        print(f"{var} in the environment: {'yes' if os.environ.get(var) else 'NO (the ctrlb tools need it)'}")
    if cfg['off']:
        print('ctrlb-agent is OFF (CTRLB_OFF=1, or the URL or key is missing, or a bad dataset name): run bin/configure.py')
        return
    try:
        t0 = time.time()
        r = common.runs(cfg, limit=5, timeout=15)
        ms = (time.time() - t0) * 1000
        print(f"Seeker answered: OK ({ms:.0f} ms); latest runs in {cfg['dataset']}:")
        for x in r.get('runs') or []:
            print(f"  {x.get('run_id')}  agents {x.get('agents')}  live {x.get('live')}  done {x.get('done')}")
    except Exception as e:
        print(f'Seeker check failed: {str(e)[:300]}')
    states = os.path.join(common.HOME, 'state')
    n = len([f for f in os.listdir(states) if f.endswith('.json')]) if os.path.isdir(states) else 0
    print(f'transcript files tracked: {n}')
    logf = os.path.join(common.HOME, 'hooks.log')
    if os.path.isfile(logf):
        with open(logf) as fh:
            tail = fh.readlines()[-8:]
        print('hook log (last lines):')
        print(''.join('  ' + l for l in tail), end='')


if __name__ == '__main__':
    main()
