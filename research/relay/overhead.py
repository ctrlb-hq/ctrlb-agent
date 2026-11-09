"""Relay overhead accounting (round 5, section D): where the extra turns of the Relay pair arms go.

Reads the benchmark's arm-1 trial JSONs (results/relay-benchmark/arm1/<arm>/<project>/<model>-NN.json) and, for each
trial, the agents' stream-json logs the harness left in the trial directory (<dir>/.log-agent-N.jsonl). No new runs.

Definitions
  turn      the harness's `turns` is the sum over agents of the `result` record's num_turns. In these logs num_turns is
            the number of tool calls plus one (the closing answer), whether or not calls share one API response. So
            every tool call is one turn, put in a bucket by tool; the rest (num_turns minus tool calls, the closing
            answer) is the bucket `final answer`. The buckets add up to the harness's turns exactly.
  hook      the plugin's PreToolUse hook checks every Edit, Write, MultiEdit and NotebookEdit in a Relay arm. A check
            is not a turn: it is reported as checks and refusals beside the turns. A refused edit is still a turn (the
            agent spent a call on it) and is counted in the bucket `edit refused by the hook`.
  cost      each API response (message id) carries its usage. Its tokens are weighted as Anthropic prices them
            relative to uncached input (input 1, 1-hour cache write 2, cache read 0.1, output 5; the same ratios for
            Haiku and Sonnet), split evenly over the tool calls of that response (a text-only response goes to
            `final answer`), and each agent's weighted total is scaled to its result record's total_cost_usd, so the
            buckets add up to the trial's cost.

Usage: python3 -I overhead.py   (writes results/relay-stall-takeover/overhead.md and overhead.json)
"""
import glob, json, os, re

HERE = os.path.dirname(os.path.abspath(__file__))
ARM1 = os.path.join(HERE, 'results', 'relay-benchmark', 'arm1')
OUT = os.path.join(HERE, 'results', 'relay-stall-takeover')
CELLS = [('readtime', 'sonnet'), ('hone', 'haiku'), ('readtime', 'haiku')]
WEIGHT = dict(input_tokens=1.0, cache_creation_input_tokens=2.0, cache_read_input_tokens=0.1, output_tokens=5.0)
DENY = re.compile(r'\((lapsed|unheld|stale_round)\)|PreToolUse:\w+ hook|hook.*(denied|blocked)', re.I)
EDIT_TOOLS = ('Edit', 'Write', 'MultiEdit', 'NotebookEdit')
TEST = re.compile(r'\bpytest\b|-m\s+unittest\b|\bunittest\b')
PY = re.compile(r'\bpython3?\b')
INSPECT = re.compile(r'^\s*(cd [^;&]+(&&|;)\s*)?(cat|ls|head|tail|grep|find|sed -n|wc|tree|pwd|echo)\b')

BUCKETS = ['claim: task name', 'claim: REGISTRY.md', 'claim: other file or slot', 'release: task name',
           'release: REGISTRY.md', 'release: other file or slot', 'done', 'peers', 'tasks', 'contract/contracts', 'handoff', 'ask/answer', 'post',
           'ToolSearch (loads the Relay tools)', 'test run (pytest, unittest, task test)', 'own check (python -c, script)',
           'REGISTRY.md read', 'REGISTRY.md edit', 'edit refused by the hook: REGISTRY.md',
           'edit refused by the hook: task file', 'Edit/Write (applied)', 'Read',
           'Bash inspect (cat, ls, grep)', 'Bash other', 'other tool', 'final answer',
           'first response (prompt and tool definitions; cost only)']
OUTCOMES = ['held', 'held, renewed', 'refused: held by another', 'refused: finished', 'refused: timed out in line',
            'refused: belongs_to_task', 'claim errors', 'claims with wait_s']
GROUPS = [
    ('task protocol: claim, release, done, tasks', ['claim: task name', 'release: task name', 'done', 'tasks']),
    ('REGISTRY.md under Relay: claim, release, refused edits', ['claim: REGISTRY.md', 'release: REGISTRY.md',
                                                               'edit refused by the hook: REGISTRY.md']),
    ('file claims outside a task, refused task-file edits', ['claim: other file or slot', 'release: other file or slot',
                                                             'edit refused by the hook: task file']),
    ('tool loading and talk: ToolSearch, peers, post, contract, handoff, ask', [
        'ToolSearch (loads the Relay tools)', 'peers', 'post', 'contract/contracts', 'handoff', 'ask/answer']),
    ('checks: test runs and own checks', ['test run (pytest, unittest, task test)', 'own check (python -c, script)']),
    ('the work itself: Read, Edit/Write, REGISTRY.md read and edit, Bash', [
        'Read', 'Edit/Write (applied)', 'REGISTRY.md read', 'REGISTRY.md edit', 'Bash inspect (cat, ls, grep)',
        'Bash other', 'other tool']),
    ('first response and final answer', ['first response (prompt and tool definitions; cost only)', 'final answer']),
]
FIRST = 'first response (prompt and tool definitions; cost only)'


def bucket(name, inp, refused):
    short = name.split('__')[-1] if name.startswith('mcp__') else name
    inp = inp or {}
    if name.startswith('mcp__') and short in ('claim', 'release'):
        res = str(inp.get('resource') or '')
        if res.endswith('REGISTRY.md'):
            return f'{short}: REGISTRY.md'
        what = 'other file or slot' if ('/' in res or res.startswith(('path:', 'slot:')) or re.search(r'\.\w{1,4}$', res)) \
            else 'task name'
        return f'{short}: {what}'
    if name.startswith('mcp__'):
        return {'done': 'done', 'peers': 'peers', 'tasks': 'tasks',
                'task': 'tasks', 'contract': 'contract/contracts', 'contracts': 'contract/contracts',
                'handoff': 'handoff', 'ask': 'ask/answer', 'answer': 'ask/answer', 'post': 'post'}.get(short, 'other tool')
    if name == 'ToolSearch':
        return 'ToolSearch (loads the Relay tools)'
    if name in EDIT_TOOLS:
        if refused:
            reg = str(inp.get('file_path') or '').endswith('REGISTRY.md')
            return 'edit refused by the hook: ' + ('REGISTRY.md' if reg else 'task file')
        if str(inp.get('file_path') or inp.get('notebook_path') or '').endswith('REGISTRY.md'):
            return 'REGISTRY.md edit'
        return 'Edit/Write (applied)'
    if name == 'Read':
        return 'REGISTRY.md read' if str(inp.get('file_path') or '').endswith('REGISTRY.md') else 'Read'
    if name == 'Bash':
        cmd = str(inp.get('command') or '')
        if 'REGISTRY.md' in cmd:
            return 'REGISTRY.md edit' if re.search(r'>>?\s*\S*REGISTRY\.md|sed -i|tee', cmd) else 'REGISTRY.md read'
        if TEST.search(cmd):
            return 'test run (pytest, unittest, task test)'
        if PY.search(cmd):
            return 'own check (python -c, script)'
        if INSPECT.search(cmd):
            return 'Bash inspect (cat, ls, grep)'
        return 'Bash other'
    return 'other tool'


def text_of(c):
    if isinstance(c, list):
        return ''.join(x.get('text', '') for x in c if isinstance(x, dict))
    return str(c or '')


def agent_log(path):
    """Turns and cost by bucket, hook checks and refusals, from one agent's stream log."""
    calls, order, results, usage, msg_calls = {}, [], {}, {}, {}
    num_turns, total_cost = 0, 0.0
    with open(path, errors='replace') as fh:
        for line in fh:
            try:
                m = json.loads(line)
            except ValueError:
                continue
            t = m.get('type')
            if t == 'assistant':
                msg = m.get('message') or {}
                mid = msg.get('id') or str(len(usage))
                u = msg.get('usage') or {}
                prev = usage.get(mid)
                if prev is None or (u.get('output_tokens') or 0) >= (prev.get('output_tokens') or 0):
                    usage[mid] = u
                msg_calls.setdefault(mid, [])
                for b in msg.get('content') or []:
                    if isinstance(b, dict) and b.get('type') == 'tool_use':
                        calls[b['id']] = (b['name'], b.get('input'))
                        order.append(b['id'])
                        msg_calls[mid].append(b['id'])
            elif t == 'user':
                for b in (m.get('message') or {}).get('content') or []:
                    if isinstance(b, dict) and b.get('type') == 'tool_result':
                        results[b.get('tool_use_id')] = (bool(b.get('is_error')), text_of(b.get('content')))
            elif t == 'result':
                num_turns = m.get('num_turns') or 0
                total_cost = m.get('total_cost_usd') or 0.0
    turns = {k: 0 for k in BUCKETS}
    cost = {k: 0.0 for k in BUCKETS}
    which = {}
    checks = refusals = 0
    for cid in order:
        name, inp = calls[cid]
        err, text = results.get(cid, (False, ''))
        refused = name in EDIT_TOOLS and err and bool(DENY.search(text))
        if name in EDIT_TOOLS:
            checks += 1
            refusals += refused
        b = bucket(name, inp, refused)
        which[cid] = b
        turns[b] += 1
    turns['final answer'] += max(0, num_turns - len(order))
    outcomes = {k: 0 for k in OUTCOMES}
    for cid in order:
        name, inp = calls[cid]
        if not name.endswith('__claim'):
            continue
        outcomes['claims with wait_s'] += bool((inp or {}).get('wait_s'))
        try:
            ans = json.loads(results.get(cid, (False, ''))[1])
        except ValueError:
            outcomes['claim errors'] += 1
            continue
        if ans.get('held'):
            outcomes['held, renewed' if ans.get('renewed') else 'held']+= 1
        elif ans.get('finished_by'):
            outcomes['refused: finished'] += 1
        elif ans.get('reason') == 'timed_out':
            outcomes['refused: timed out in line'] += 1
        elif ans.get('reason') == 'belongs_to_task':
            outcomes['refused: belongs_to_task'] += 1
        else:
            outcomes['refused: held by another'] += 1
    weighted = {}
    for mid, u in usage.items():
        weighted[mid] = sum(WEIGHT[k] * (u.get(k) or 0) for k in WEIGHT)
    total_w = sum(weighted.values()) or 1.0
    first = next(iter(usage), None)
    for mid, w in weighted.items():
        ids = msg_calls.get(mid) or []
        share = total_cost * w / total_w
        if mid == first:
            cost[FIRST] += share
        elif not ids:
            cost['final answer'] += share
        else:
            for cid in ids:
                cost[which[cid]] += share / len(ids)
    return dict(turns=turns, cost=cost, outcomes=outcomes, num_turns=num_turns, tool_calls=len(order), hook_checks=checks,
                hook_refusals=refusals, cost_usd=total_cost, api_calls=len(usage))


def trial(path):
    t = json.load(open(path))
    out = dict(file=os.path.relpath(path, ARM1), turns_reported=t['turns'], cost_reported=t.get('cost_usd'),
               turns={k: 0 for k in BUCKETS}, cost={k: 0.0 for k in BUCKETS}, num_turns=0, tool_calls=0,
               hook_checks=0, hook_refusals=0, cost_usd=0.0, api_calls=0, outcomes={k: 0 for k in OUTCOMES})
    for log in sorted(glob.glob(os.path.join(t['dir'], '.log-agent-*.jsonl'))):
        a = agent_log(log)
        for k in BUCKETS:
            out['turns'][k] += a['turns'][k]
            out['cost'][k] += a['cost'][k]
        for k in OUTCOMES:
            out['outcomes'][k] += a['outcomes'][k]
        for k in ('num_turns', 'tool_calls', 'hook_checks', 'hook_refusals', 'cost_usd', 'api_calls'):
            out[k] += a[k]
    return out


def mean(xs):
    return sum(xs) / len(xs) if xs else 0.0


def cell(arm, project, model):
    ts = [trial(p) for p in sorted(glob.glob(os.path.join(ARM1, arm, project, f'{model}-*.json')))]
    return dict(n=len(ts), trials=ts,
                turns={k: mean([t['turns'][k] for t in ts]) for k in BUCKETS},
                cost={k: mean([t['cost'][k] for t in ts]) for k in BUCKETS},
                turns_total=mean([t['num_turns'] for t in ts]), turns_reported=mean([t['turns_reported'] for t in ts]),
                cost_total=mean([t['cost_usd'] for t in ts]), hook_checks=mean([t['hook_checks'] for t in ts]),
                hook_refusals=mean([t['hook_refusals'] for t in ts]), api_calls=mean([t['api_calls'] for t in ts]),
                tool_calls=mean([t['tool_calls'] for t in ts]),
                outcomes={k: mean([t['outcomes'][k] for t in ts]) for k in OUTCOMES})


def table(project, model, relay, none):
    extra_t = relay['turns_total'] - none['turns_total']
    extra_c = relay['cost_total'] - none['cost_total']
    lines = [f'**{project}, {model.capitalize()}** (pair-relay n={relay["n"]}, pair-none n={none["n"]}; turns and cost are '
             f'means per trial, both agents)', '',
             '| bucket | pair-relay turns | pair-none turns | extra turns | share of extra turns | pair-relay cost | '
             'pair-none cost | extra cost |', '|---|---|---|---|---|---|---|---|']
    rows = []
    for k in BUCKETS:
        r, n = relay['turns'][k], none['turns'][k]
        if r == 0 and n == 0 and relay['cost'][k] == 0 and none['cost'][k] == 0:
            continue
        rows.append((r - n, k, r, n, relay['cost'][k], none['cost'][k]))
    rows.sort(key=lambda x: (-x[0], -(x[4] - x[5])))
    for d, k, r, n, rc, nc in rows:
        share = f'{100 * d / extra_t:.0f}%' if extra_t else 'n/a'
        lines.append(f'| {k} | {r:.1f} | {n:.1f} | {d:+.1f} | {share} | ${rc:.4f} | ${nc:.4f} | {rc - nc:+.4f} |')
    lines.append(f'| **total** | **{relay["turns_total"]:.1f}** | **{none["turns_total"]:.1f}** | **{extra_t:+.1f}** | '
                 f'100% | **${relay["cost_total"]:.4f}** | **${none["cost_total"]:.4f}** | **{extra_c:+.4f}** |')
    lines += ['', 'Grouped:', '', '| group | extra turns | share | extra cost |', '|---|---|---|---|']
    for g, ks in GROUPS:
        d = sum(relay['turns'][k] - none['turns'][k] for k in ks)
        dc = sum(relay['cost'][k] - none['cost'][k] for k in ks)
        share = f'{100 * d / extra_t:.0f}%' if extra_t else 'n/a'
        lines.append(f'| {g} | {d:+.1f} | {share} | {dc:+.4f} |')
    o = relay['outcomes']
    lines += ['', 'Claim answers per pair-relay trial: ' + ', '.join(f'{k} {o[k]:.1f}' for k in OUTCOMES if o[k]) + '.']
    lines += [f'Hook (not turns): pair-relay {relay["hook_checks"]:.1f} edits checked per trial, '
              f'{relay["hook_refusals"]:.1f} refused; pair-none has no hook ({none["hook_checks"]:.1f} edits, unchecked). '
              f'API responses per trial: {relay["api_calls"]:.1f} against {none["api_calls"]:.1f} '
              f'(several tool calls can share one response).', '']
    return lines, sorted(rows, key=lambda x: -x[0])[:3], extra_t, extra_c


def main():
    os.makedirs(OUT, exist_ok=True)
    data, md = {}, ['# Relay overhead accounting (no new runs)', '',
                    'From the benchmark\'s arm-1 trials (`results/relay-benchmark/arm1`, v7 text, holdRequired and '
                    'evidenceRequired on in pair-relay, task tests named in every arm) and the agents\' stream logs. '
                    'A turn is one tool call; the closing answer is its own bucket, so the buckets add up to the '
                    'harness\'s `turns` (num_turns). Cost is each API response\'s usage, weighted at Anthropic\'s '
                    'relative prices, split over its tool calls and scaled to the agent\'s reported cost. Rows are '
                    'sorted by extra turns. Produced by `overhead.py`.', '',
                    'The biggest lines. (1) Task-name claims are the largest single bucket in every cell (5.6 to 6.6 '
                    'claims per trial for 3 tasks, 26 to 32% of the extra turns), and the task protocol as a whole '
                    '(claim, release, done, tasks) is half to two thirds of them; in the Haiku cells about 2 claims per '
                    'trial are refused because the task is already finished, and 7 per trial carry wait_s. (2) '
                    'REGISTRY.md costs 4.8 to 6.7 extra turns per trial (22 to 35%): it belongs to no task, so every '
                    'agent claims it, has its edit refused unheld first (42 of the 59 hook refusals in the Relay pair '
                    'trials are REGISTRY.md), and releases it. (3) With Sonnet the extra $0.125 per trial is mostly '
                    'claims (task names $0.045, file and REGISTRY.md claims $0.038), tasks ($0.020) and refused edits '
                    '($0.025); with Haiku the Relay pair is no dearer, because the agents read and inspect less. '
                    'ToolSearch (loading the deferred Relay tools) is about 2 turns per trial in every cell but costs '
                    'almost nothing beyond the first response, which is the same size in both arms.', '']
    checks, tops = [], []
    for project, model in CELLS:
        relay, none = cell('pair-relay', project, model), cell('pair-none', project, model)
        key = f'{project}-{model}'
        data[key] = {'pair-relay': relay, 'pair-none': none}
        lines, top, extra_t, extra_c = table(project, model, relay, none)
        md += lines
        tops.append((key, top, extra_t, extra_c, relay, none))
        for arm, c in (('pair-relay', relay), ('pair-none', none)):
            checks.append(f'{key} {arm}: bucket sum {sum(c["turns"].values()):.1f}, num_turns {c["turns_total"]:.1f}, '
                          f'reported {c["turns_reported"]:.1f}')
    data['_check'] = checks
    with open(os.path.join(OUT, 'overhead.json'), 'w') as fh:
        json.dump(data, fh, indent=1)
    md += ['Check: the buckets add up to the harness\'s turns in every cell:', ''] + [f'- {c}' for c in checks] + ['']
    with open(os.path.join(OUT, 'overhead.md'), 'w') as fh:
        fh.write('\n'.join(md) + '\n')
    for key, top, extra_t, extra_c, relay, none in tops:
        print(key, f'extra turns {extra_t:+.1f}, extra cost {extra_c:+.4f}')
        for d, k, r, n, rc, nc in top:
            print(f'   {k}: {d:+.1f} turns ({100 * d / extra_t:.0f}%), {rc - nc:+.4f} USD')
    print('\n'.join(checks))


if __name__ == '__main__':
    main()
