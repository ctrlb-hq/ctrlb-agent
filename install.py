"""Install ctrlb-agent for a harness from a clone of this repo.

  python3 -I install.py --codex [options]   writes the CtrlB block into Codex's config.toml (codex/install.py; every
                                            option after --codex is passed through, --help lists them)

Claude Code installs from the marketplace instead:
  claude plugin marketplace add ctrlb-hq/ctrlb-agent
  claude plugin install ctrlb-agent@ctrlb-agent"""
import os, sys
sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))


def main(argv):
    if argv[:1] != ['--codex']:
        print(__doc__)
        return 0 if argv[:1] in (['-h'], ['--help']) else 2
    sys.path.insert(0, os.path.join(HERE, 'codex'))
    import install as codex_install   # codex/install.py
    return codex_install.main(argv[1:])


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
