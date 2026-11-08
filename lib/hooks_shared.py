"""What the Claude Code hook (plugin/hooks/hook.py) and the Codex hook (codex/hook.py) share: the injected rules and
the edit check of decision 7. Both harnesses take the same output shapes: hookSpecificOutput with additionalContext
(SessionStart, SubagentStart, PreToolUse) and, on PreToolUse, permissionDecision "deny" with a non-empty
permissionDecisionReason."""
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
         "release, brief, briefs). Before starting a task, call claim with the file path the task names (for example "
         "claim(resource=\"src/app.py\")), not a description of the task; work on it only if held is true, otherwise "
         "pick other work. Finish tasks, just release files: when you are done with a file, call release on it, and "
         "it is handed back for later rounds of work (a file path is never finished). Pass finished=true only when "
         "releasing a task name, to mark the task done. If a task name is refused as finished and you were asked to "
         "work on it again, claim it with reopen=true to start a new round.")


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
    keys claims and as it keys the paths column of the session rows."""
    paths = normalise_edit_paths(paths, cwd or '')
    if not paths or not run or not agent:
        return None
    res = common.check(cfg, run, agent, paths, cwd or '', timeout=CHECK_S)
    mode = res.get('mode') or 'advisory'
    conflicts = [c for c in res.get('conflicts') or [] if isinstance(c, dict) and c.get('held_by') and c['held_by'] != agent]
    if mode == 'off' or not conflicts:
        return None
    held = '; '.join(f"{_resource(c)} is claimed by {c['held_by']}{_lease(c)}" for c in conflicts)
    first = _resource(conflicts[0])
    if mode == 'enforce' and res.get('allowed') is False:
        reason = (f"CtrlB Relay (run {run}): {held}. Do not edit it. Call claim(resource=\"{first}\", run_id=\"{run}\", "
                  f"agent=\"{agent}\") and edit only if held is true; otherwise pick other work or call peers to see "
                  f"what {conflicts[0]['held_by']} is doing.")
        return {'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'permissionDecision': 'deny',
                                       'permissionDecisionReason': reason}}
    text = (f"CtrlB Relay (run {run}): {held}. Another agent is working on this file. Before editing it, call "
            f"claim(resource=\"{first}\", run_id=\"{run}\", agent=\"{agent}\"); if held is false, leave the file to "
            f"its holder and pick other work.")
    return context('PreToolUse', text)
