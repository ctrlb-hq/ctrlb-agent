"""Upload past Claude Code sessions (and their subagents) from ~/.claude/projects, or past Codex sessions from
$CODEX_HOME/sessions (default ~/.codex/sessions), to CtrlB through Seeker.
Usage: backfill.py [--codex] [--project TEXT] [--count] [--yes]
  --codex         Codex rollouts instead of Claude Code transcripts
  --project TEXT  only project folders whose name contains TEXT (e.g. the repo name); with --codex, only sessions
                  whose working directory contains TEXT
  --count         print what would be uploaded and stop
  --yes           do not ask before uploading
Already-uploaded lines are skipped, so running it twice sends nothing new."""
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--codex', action='store_true')
    ap.add_argument('--project', default='')
    ap.add_argument('--count', action='store_true')
    ap.add_argument('--yes', action='store_true')
    a = ap.parse_args()
    cfg = common.config()
    if cfg['off']:
        sys.exit('ctrlb-agent is not configured: run bin/configure.py first')
    files, what = codex_files(a.project) if a.codex else claude_files(a.project)
    upload = rows_codex.upload_file if a.codex else rows_claude.upload_file
    size = sum(os.path.getsize(f) for f in files)
    print(f'{what}, {size / 1e6:.1f} MB')
    print(f"destination: {cfg['url']}  dataset {cfg['dataset']}  user {cfg['user'] or '(from the key)'}")
    if a.count or not files:
        return
    if not a.yes and input('Upload them? Secrets matching the redaction patterns are replaced first. [y/N] ').strip().lower() != 'y':
        print('nothing sent')
        return
    total = dict(rows=0, lines=0, redacted=0)
    t0 = time.time()
    for i, f in enumerate(files, 1):
        try:
            c = upload(cfg, f, time.time() + 600)
            for k in total:
                total[k] += c[k]
        except Exception as e:
            print(f'  failed {f}: {str(e)[:200]}')
        if i % 25 == 0 or i == len(files):
            print(f'  {i}/{len(files)} files, {total["rows"]} rows, {total["redacted"]} redactions, {time.time() - t0:.0f}s')
    common.log(f'backfill {total}')


if __name__ == '__main__':
    main()
