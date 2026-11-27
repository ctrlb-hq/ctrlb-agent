"""Upload past Claude Code sessions (and their subagents) from ~/.claude/projects, or past Codex sessions from
$CODEX_HOME/sessions (default ~/.codex/sessions), to CtrlB.
Usage: backfill.py [--codex] [--titles | --cost-ledger] [--project TEXT] [--count] [--yes]
  --codex         Codex rollouts instead of Claude Code transcripts
  --titles        only the session titles of transcripts already uploaded: the title rows (custom-title and
                  ai-title lines) of their uploaded lines that CtrlB has not accepted yet (uploaded before title rows
                  existed, or refused by an older CtrlB);
                  with --codex, each main thread's names from $CODEX_HOME/session_index.jsonl given since the last one
                  sent, oldest first, and the ones an older CtrlB refused, at their own keys. The upload offsets
                  stay as they are
  --cost-ledger   only put the cost records CtrlB already has, of the Claude Code transcripts, into this machine's
                  cost ledgers, in time order (sends nothing), so a session that continues one uploaded by plugin
                  0.3.0 or earlier is charged only its own spend. Run it once after upgrading. With --count, it
                  only counts them. A Claude Code upload does the same first, also for the records it is about to
                  upload, and uploads anyway (saying so) when the ledger file cannot be used
  --project TEXT  only project folders whose name contains TEXT (e.g. the repo name); with --codex, only sessions
                  whose working directory contains TEXT
  --count         print what would be uploaded and stop
  --yes           do not ask before uploading
Already-uploaded lines (with --titles, already-accepted titles) are skipped, so running it twice sends nothing new."""
import argparse, glob, os, sys, time
sys.dont_write_bytecode = True
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'lib'))
import common, rows_claude, rows_codex  # noqa: E402

ROOT = os.path.expanduser(os.environ.get('CLAUDE_PROJECTS_DIR', '~/.claude/projects'))


def claude_files(project):
    projects = sorted(d for d in glob.glob(os.path.join(ROOT, '*')) if os.path.isdir(d) and project in os.path.basename(d))
    files = []
    for d in projects:
        for p in sorted(glob.glob(os.path.join(d, '*.jsonl'))):
            files += rows_claude.session_files(p)
    return files, f'{len(projects)} project folders, {len(files)} transcript files (main sessions and subagents)'


def codex_files(project):
    files = [f for f in rows_codex.rollouts() if not project or project in str(rows_codex.read_meta(f).get('cwd') or '')]
    return files, f'{len(files)} Codex rollout files (sessions and subagents) under {os.path.join(rows_codex.codex_home(), "sessions")}'


def titles(cfg, files, a):
    upload_titles = rows_codex.upload_titles if a.codex else rows_claude.upload_titles
    found, unread = [], 0
    for f in files:
        try:
            n = upload_titles(cfg, f, time.time() + 600, send=False)
        except Exception as e:
            unread += 1
            print(f'  could not check {f}: {str(e)[:200]}')
            continue
        if n:
            found.append((f, n))
    print(f'{len(found)} uploaded transcript files with {sum(n for _, n in found)} session titles not sent yet'
          + (f'; {unread} files could not be checked' if unread else ''))
    if a.count or not found:
        return 1 if unread else 0
    if not a.yes and input('Send them? [y/N] ').strip().lower() != 'y':
        print('nothing sent')
        return 0
    sent, failed = 0, 0
    for f, _ in found:
        try:
            sent += upload_titles(cfg, f, time.time() + 600)
        except common.RecordsRefused as e:
            if e.refuses_titles():
                print(f'CtrlB refused the title rows ({str(e)[:200]}): it is older than session titles. '
                      f'{sent} session titles were sent before that. Upgrade it, then run this again.')
                return 1
            failed += 1
            print(f'  failed {f}: {str(e)[:200]}')
        except Exception as e:
            failed += 1
            print(f'  failed {f}: {str(e)[:200]}')
    common.log(f'backfill titles: {sent} sent')
    print(f'{sent} session titles sent, {failed}/{len(found)} files failed')
    return 1 if failed or unread else 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--codex', action='store_true')
    ap.add_argument('--titles', action='store_true')
    ap.add_argument('--cost-ledger', action='store_true')
    ap.add_argument('--project', default='')
    ap.add_argument('--count', action='store_true')
    ap.add_argument('--yes', action='store_true')
    a = ap.parse_args()
    if a.cost_ledger and (a.codex or a.titles):
        ap.error('--cost-ledger is for Claude Code transcripts and goes alone')
    cfg = common.config()
    if cfg['off']:
        sys.exit('ctrlb-agent is not configured: run bin/configure.py first')
    files, what = codex_files(a.project) if a.codex else claude_files(a.project)
    if a.cost_ledger:
        print(what)
        try:
            records, ledgers, unread = rows_claude.seed_ledgers(cfg, files, time.time() + 600, plan=False,
                                                                write=not a.count)
        except (OSError, TimeoutError) as e:
            print(f'the cost ledgers could not be filled: {str(e)[:200]}; nothing was sent')
            return 1
        for f, e in unread:
            print(f'  could not read {f}: {e[:200]}')
        print(f'{records} cost records CtrlB has would go into {ledgers} cost ledgers; nothing was written' if a.count
              else f'{records} cost records are in {ledgers} cost ledgers on this machine (those CtrlB has); nothing '
              'was sent')
        if unread:
            print(f'{len(unread)} transcript files could not be read; their cost records are left out')
        return 1 if unread else 0
    if a.titles:
        print(what)
        print(f"destination: {cfg['url']}  dataset {cfg['dataset']}  user {cfg['user'] or '(from the key)'}")
        return titles(cfg, files, a)
    upload = rows_codex.upload_file if a.codex else rows_claude.upload_file
    size = sum(os.path.getsize(f) for f in files)
    print(f'{what}, {size / 1e6:.1f} MB')
    print(f"destination: {cfg['url']}  dataset {cfg['dataset']}  user {cfg['user'] or '(from the key)'}")
    if a.count or not files:
        return
    if not a.yes and input('Upload them? Secrets matching the redaction patterns are replaced first. [y/N] ').strip().lower() != 'y':
        print('nothing sent')
        return
    seeded = True
    if not a.codex:
        # Cost records first, in time order across the files, so the files below can upload in any order.
        try:
            records, ledgers, unread = rows_claude.seed_ledgers(cfg, files, time.time() + 600)
            print(f'  {records} cost records in {ledgers} cost ledgers, in time order')
            for f, e in unread:
                seeded = False
                print(f'  could not read {f} for the cost ledgers: {e[:200]}')
        except (OSError, TimeoutError) as e:
            seeded = False
            print(f'  the cost ledgers could not be filled first ({str(e)[:200]}); uploading anyway, each cost record '
                  'counted as it is uploaded')
    total = dict(rows=0, lines=0, redacted=0)
    t0 = time.time()
    failed = 0
    for i, f in enumerate(files, 1):
        try:
            c = upload(cfg, f, time.time() + 600)
            for k in total:
                total[k] += c[k]
        except Exception as e:
            failed += 1
            print(f'  failed {f}: {str(e)[:200]}')
        if i % 25 == 0 or i == len(files):
            print(f'  {i}/{len(files)} files, {total["rows"]} rows, {total["redacted"]} redactions, {time.time() - t0:.0f}s')
    common.log(f'backfill {total}')
    print(f'{failed}/{len(files)} files failed')
    return 1 if failed or not seeded else 0


if __name__ == '__main__':
    sys.exit(main())
