"""What the Claude Code hook (plugin/hooks/hook.py) and the Codex hook (codex/hook.py) share: the injected rules and
the edit check of decision 7. Both harnesses take the same output shapes: hookSpecificOutput with additionalContext
(SessionStart, SubagentStart, PreToolUse) and, on PreToolUse, permissionDecision "deny" with a non-empty
permissionDecisionReason."""
import json
import time
import common
from paths import PATH, edit_paths as normalise_edit_paths, key as resource_key

CHECK_S = 2.0

RECALL = ("Your earlier turns in this session are not all in your context: compaction leaves details out, including "
          "instructions the user gave, files you read, commands that failed and decisions you made. The full record of "
          "this session (session_id {sid}) and of this user's earlier sessions is searchable with the ctrlb tools "
          "(search_sessions, find_command, next_actions, fetch_records). Before relying on anything from earlier work, "
          "search it. Do not say something was not said or not done until you have searched. When you start a "
          "subagent, put this session_id in its prompt and tell it to search the record before exploring.")

SUBAGENT = ("You are a subagent of session {sid}. That session's full record, including what it already read, ran and "
            "decided, is searchable with the ctrlb tools (search_sessions with session_id {sid}, fetch_records, "
            "next_actions). Search it before exploring files or rerunning commands.")

RELAY = ("CtrlB Relay: your run id is {run} and your agent id is {agent}. Other agents in this run may be working in "
         "the same files. Pass run_id=\"{run}\" and agent=\"{agent}\" to the ctrlb relay tools (peers, post, claim, "
         "release, handoff, ask, answer, brief, briefs, tasks, contract, contracts). Claim the task by the name your orchestrator registered (for example "
         "claim(resource=\"task 03 median\"); tasks lists them). Its files come with it. If held is false with "
         "finished_by, the task is done; if held is false with held_by, another agent has it: pick another. If reason "
         "is unknown_task, claim again with the suggestion. Work only while you hold it. When the task is done, release "
         "it with finished=true. A file that belongs to no task is claimed by its path. A shared thing that is not a "
         "file, such as the dev server or a port, is claimed as slot:<name> and released when you are done with it. Use "
         "the coordination tools before a prose note: handoff when you must leave a task you hold, so it moves to the "
         "agent you name in one step; ask when you are blocked on something only another agent or the orchestrator "
         "can decide; answer, with the question's id, when you can settle an open question; contract when you finish "
         "an interface others will build on (a type, a route, a config shape), with its name, the shape in a few "
         "lines and the files that define it; contracts before you build on another agent's interface. Post a note "
         "only for what none of them covers. If you were asked to do a finished task again, claim its name with "
         "reopen=true.")


def context(event, text):
    return {'hookSpecificOutput': {'hookEventName': event, 'additionalContext': text}}


def _resource(c):
    """What to name in the claim call: the conflict's path without the key prefix when claiming that names the same key
    ("src/app.py"), else the key itself ("path:Makefile", since a bare "Makefile" is keyed as a name)."""
    key = c.get('key') or ''
    if not key.startswith(PATH):
        return c.get('path') or key
    bare = key[len(PATH):]
    return bare if resource_key(bare)[0] == key else key


def _lease(c):
    try:
        mins = (float(c['lease_until']) / 1e6 - time.time()) / 60
        return f' (lease ends in about {max(1, round(mins))} min)' if mins > 0 else ''
    except Exception:
        return ''


def decide(cfg, run, agent, paths, cwd=''):
    """Decision 7: post the paths an edit is about to touch to Seeker's check and turn the answer into the hook
    output, or None for no output (allowed, mode off, or no conflict). Raises on any Seeker error; the caller fails
    open. The paths are sent as paths.edit_paths gives them (relative to cwd, once per key), so Seeker keys them as it
    keys claims and as it keys the paths column of the session rows. The tokens the agent's claims answered are sent
    with them (decision 21), so Seeker can tell a hold of an earlier round of the file's task."""
    paths = normalise_edit_paths(paths, cwd or '')
    if not paths or not run or not agent:
        return None
    keys = {p: resource_key(p, cwd or '')[0] for p in paths}
    try:
        held = common.TokenState(run, agent).get(sorted(set(keys.values())))
    except Exception:
        held = {}
    tokens = {p: held[k] for p, k in keys.items() if k in held}
    res = common.check(cfg, run, agent, paths, cwd or '', timeout=CHECK_S, tokens=tokens)
    mode = res.get('mode') or 'advisory'
    conflicts = [c for c in res.get('conflicts') or [] if isinstance(c, dict)
                 and (c.get('reason') in ('lapsed', 'unheld', 'stale_round') or c.get('held_by') and c['held_by'] != agent)]
    if mode == 'off' or not conflicts:
        return None
    first = _resource(conflicts[0])
    call = f'claim(resource="{first}", run_id="{run}", agent="{agent}")'
    reasons = {c.get('reason') or 'held' for c in conflicts}
    said = '; '.join(_said(c) for c in conflicts)
    if reasons == {'held'}:
        holder = conflicts[0]['held_by']
        if mode == 'enforce' and res.get('allowed') is False:
            reason = (f"CtrlB Relay (run {run}): {said}. Do not edit it. Call {call} and edit only if held is true; "
                      f"otherwise pick other work or call peers to see what {holder} is doing.")
            return _deny(reason)
        return context('PreToolUse', f"CtrlB Relay (run {run}): {said}. Another agent is working on this file. Before "
                                     f"editing it, call {call}; if held is false, leave the file to its holder and pick "
                                     f"other work.")
    # Fencing (holdRequired): the agent does not hold the file now.
    text = (f"CtrlB Relay (run {run}): {said}. Call {call} and edit only if held is true; if held is false, leave the "
            f"file to its holder and pick other work.")
    if mode == 'enforce' and res.get('allowed') is False:
        return _deny(text)
    return context('PreToolUse', text)


def _said(c):
    """One conflict in words: the holder (held), or Seeker's message for lapsed, unheld and stale_round."""
    reason = c.get('reason') or 'held'
    if reason == 'held':
        return f"{_resource(c)} is claimed by {c['held_by']}{_lease(c)}"
    msg = c.get('message') or {'lapsed': f'your lease on {_resource(c)} ran out',
                               'unheld': f'{_resource(c)} is not held; claim it before editing'}.get(reason, reason)
    return f"{msg} ({reason})"


def _deny(reason):
    return {'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'permissionDecision': 'deny',
                                   'permissionDecisionReason': reason}}


def _tool_answer(resp):
    """The JSON answer of an MCP tool from a PostToolUse tool_response: a dict, a list of content blocks, a dict with
    content, or text."""
    if isinstance(resp, dict) and ('held' in resp or 'handed' in resp or 'released' in resp):
        return resp
    if isinstance(resp, dict):
        resp = resp.get('content', resp.get('text', ''))
    if isinstance(resp, list):
        resp = ''.join(b.get('text', '') if isinstance(b, dict) else str(b) for b in resp)
    if not isinstance(resp, str):
        return {}
    try:
        out = json.loads(resp)
    except ValueError:
        return {}
    return out if isinstance(out, dict) else {}


def record_tokens(run, agent, tool, answer):
    """Keep the agent's hold tokens (decision 21): a held claim's token for its key and its bundle's keys; a handoff
    or a release drops them. Returns the keys changed."""
    if not run or not agent or not isinstance(answer, dict):
        return []
    keys = [k for k in [answer.get('key')] + list(answer.get('paths') or []) if isinstance(k, str) and k]
    if not keys:
        return []
    state = common.TokenState(run, agent)
    name = tool.rsplit('__', 1)[-1]
    if name == 'claim' and answer.get('held') is True and answer.get('token'):
        state.put(keys, answer['token'])
        return keys
    if name == 'handoff' and answer.get('handed') is True or name == 'release' and answer.get('released'):
        state.drop(keys)
        return keys
    return []