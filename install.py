"""Install ctrlb-agent for a harness from a clone of this repo.

  python3 -I install.py --codex [options]   writes the CtrlB block into Codex's config.toml (codex/install.py; every
                                            option after --codex is passed through, --help lists them)
  python3 -I install.py init --codex|--claude [--dir DIR] [--dry-run] [--remove]
                                            writes the CtrlB block into the project's AGENTS.md (Codex) or
                                            CLAUDE.md (Claude Code) (bin/init.py; text outside the block is kept)

Claude Code installs from the marketplace instead:
  claude plugin marketplace add ctrlb-hq/ctrlb-agent
  claude plugin install ctrlb-agent@ctrlb-agent"""
import os, sys
sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))


def main(argv):
    if argv[:1] == ['init']:
        sys.path.insert(0, os.path.join(HERE, 'bin'))
        import init as project_init   # bin/init.py
        return project_init.main(argv[1:])
    if argv[:1] != ['--codex']:
        print(__doc__)
        return 0 if argv[:1] in (['-h'], ['--help']) else 2
    sys.path.insert(0, os.path.join(HERE, 'codex'))
    import install as codex_install   # codex/install.py
    return codex_install.main(argv[1:])


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
