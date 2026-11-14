"""Write the CtrlB block into a project's AGENTS.md (Codex) and CLAUDE.md (Claude Code).

  python3 -I install.py init --codex [--claude] [--dir DIR] [--dry-run] [--remove]
  python3 -I bin/init.py --codex [--claude] [--dir DIR] [--dry-run] [--remove]

The block asks the agent to search the CtrlB record before it greps, reads files or reruns a command for a question
about earlier work. It sits between <!-- ctrlb:begin --> and <!-- ctrlb:end -->; text outside the markers is never
changed. A file that does not exist is created. A run that would change nothing writes nothing. Before a change, the
old file is copied to <name>.ctrlb-backup-<time> beside it. --dry-run prints the diff and writes nothing. --remove
deletes the block. Every file is checked before any is written: one marker without its pair, or two blocks, is
refused (exit 2), and so is a file without a block that ends inside an open code block; a file that cannot be read or is not UTF-8 stops the run (exit 1). Markers inside fenced code blocks
are ignored."""
import argparse, difflib, os, sys
sys.dont_write_bytecode = True
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'lib'))
import project_rules as P  # noqa: E402


def diff(path, old, new):
    """A unified diff; a last line without a newline gets the usual "\\ No newline at end of file" line."""
    out = []
    for ln in difflib.unified_diff(P.split_lines(old or ''), P.split_lines(new),
                                   fromfile=path, tofile=path + ' (new)'):
        out.append(ln if ln.endswith('\n') else ln + '\n\\ No newline at end of file\n')
    return ''.join(out)


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
    plans, seen = [], set()
    for n in names:                     # plan every file before writing any
        p = os.path.join(a.dir, n)
        real = os.path.realpath(p)
        if real in seen:                # CLAUDE.md linked to AGENTS.md: one file, one block
            print(f'{p}: same file as {plans[-1][0]}, planned once')
            continue
        seen.add(real)
        try:
            old = P.read(p)
            plans.append((p, old, P.render(old, a.remove)))
        except P.Refused as e:
            print(f'init: {p}: {e}', file=sys.stderr)
            return 2
        except UnicodeDecodeError:
            print(f'init: {p}: not UTF-8 text; nothing written', file=sys.stderr)
            return 1
        except OSError as e:
            print(f'init: {p}: cannot read it ({e.strerror or e}); nothing written', file=sys.stderr)
            return 1
    for p, old, new in plans:
        if new is None or new == old:
            print(f'{p}: {"no CtrlB block, nothing to remove" if a.remove else "already up to date"}, nothing written')
        elif a.dry_run:
            print(diff(p, old, new), end='')
            print(f'{p}: dry run, nothing written')
        else:
            try:
                backup = P.write(p, new)
            except OSError as e:
                print(f'init: {p}: cannot write it ({e.strerror or e})', file=sys.stderr)
                return 1
            print(f'{p}: {"removed the CtrlB block" if a.remove else "wrote the CtrlB block"}'
                  + (f'; backup of the previous file: {backup}' if backup else '; created the file'))
    return 0


if __name__ == '__main__':
    sys.exit(main())
