"""Write the CtrlB block into a project's AGENTS.md (Codex) and CLAUDE.md (Claude Code).

  python3 -I install.py init --codex [--claude] [--dir DIR] [--dry-run] [--remove]
  python3 -I bin/init.py --codex [--claude] [--dir DIR] [--dry-run] [--remove]

The block asks the agent to search the CtrlB record before it greps, reads files or reruns a command for a question
about earlier work. It sits between <!-- ctrlb:begin --> and <!-- ctrlb:end -->; text outside the markers is never
changed. A file that does not exist is created. A run that would change nothing writes nothing. Before a change, the
old file is copied to <name>.ctrlb-backup-<time> beside it. --dry-run prints the diff and writes nothing. --remove
deletes the block. One marker without its pair is refused (exit 2) and nothing is written."""
import argparse, difflib, os, sys
sys.dont_write_bytecode = True
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'lib'))
import project_rules as P  # noqa: E402


def one(path, remove, dry_run):
    old = P.read(path)
    new = P.render(old, remove)
    if new is None or new == old:
        print(f'{path}: {"no CtrlB block, nothing to remove" if remove else "already up to date"}, nothing written')
        return
    if dry_run:
        diff = ''.join(difflib.unified_diff((old or '').splitlines(keepends=True), new.splitlines(keepends=True),
                                            fromfile=path, tofile=path + ' (new)'))
        print(diff, end='' if diff.endswith('\n') else '\n')
        print(f'{path}: dry run, nothing written')
        return
    backup = P.write(path, new)
    print(f'{path}: {"removed the CtrlB block" if remove else "wrote the CtrlB block"}'
          + (f'; backup of the previous file: {backup}' if backup else '; created the file'))


def main(argv=None):
    ap = argparse.ArgumentParser(prog='init', description='Write the CtrlB block into AGENTS.md and/or CLAUDE.md.')
    ap.add_argument('--codex', action='store_true', help='AGENTS.md, which Codex reads')
    ap.add_argument('--claude', action='store_true', help='CLAUDE.md, which Claude Code reads')
    ap.add_argument('--dir', default='.', help='the project directory (default: the current directory)')
    ap.add_argument('--dry-run', action='store_true', help='print the diff and write nothing')
    ap.add_argument('--remove', action='store_true', help='remove the CtrlB block')
    a = ap.parse_args(argv)
    names = [P.FILES[h] for h in ('codex', 'claude') if getattr(a, h)]
    if not names:
        ap.error('give --codex, --claude or both')
    if not os.path.isdir(a.dir):
        ap.error(f'--dir {a.dir}: not a directory')
    paths = [os.path.join(a.dir, n) for n in names]
    for p in paths:                     # check every file before writing any
        try:
            P.render(P.read(p), a.remove)
        except P.Refused as e:
            print(f'init: {p}: {e}', file=sys.stderr)
            return 2
    seen = set()
    for p in paths:
        real = os.path.realpath(p)
        if real in seen:                # CLAUDE.md linked to AGENTS.md: written once
            print(f'{p}: same file as above, nothing more written')
            continue
        seen.add(real)
        one(p, a.remove, a.dry_run)
    return 0


if __name__ == '__main__':
    sys.exit(main())
