
import json, os, sys, time
sys.dont_write_bytecode = True
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'lib'))
import common


def main():
    cfg = common.config()
    shown = {k: cfg[k] for k in ('url', 'mcp_url', 'dataset', 'user', 'run', 'agent', 'enforce', 'mode', 'off')}
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
    for field in ('url', 'mcp_url'):
        if cfg[field]:
            try:
                common.validate_url(cfg[field])
            except ValueError as e:
                print(f'Unsafe {field}: {e}')
                return
    if cfg['off']:
        print('ctrlb-agent is OFF (CTRLB_OFF=1, or the URL or key is missing, or a bad dataset name): run bin/configure.py')
        return
    if common.local_mode(cfg):
        print('local binary:', common.ctrlb_binary() or '(not found)')
        try:
            print(f'Local health answer: HTTP {common.local_health(cfg)}')
        except Exception:
            print('Local health check failed (server unavailable)')
    else:
        try:
            t0 = time.time()
            r = common.runs(cfg, limit=5, timeout=15)
            ms = (time.time() - t0) * 1000
            print(f"CtrlB answered: OK ({ms:.0f} ms); latest runs in {cfg['dataset']}:")
            for x in r.get('runs') or []:
                print(f"  {x.get('run_id')}  agents {x.get('agents')}  live {x.get('live')}  done {x.get('done')}")
        except Exception as e:
            print(f'CtrlB check failed: {str(e)[:300]}')
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
