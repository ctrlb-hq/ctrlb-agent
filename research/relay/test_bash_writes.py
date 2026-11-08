"""Tests of run_swarm.bash_writes: which Bash commands count as writing a task file (duplicated / editors / redone).

Usage: python3 -I test_bash_writes.py   (RUN_SWARM=<path of a run_swarm.py to test>, default the one next to this file)
"""
import importlib.util, os, sys, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PATH = os.environ.get('RUN_SWARM', os.path.join(HERE, 'run_swarm.py'))
sys.path.insert(0, HERE)
_spec = importlib.util.spec_from_file_location('run_swarm_under_test', PATH)
rs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rs)

F = 'readtime/utils.py'
WRITES = [
    "cat > readtime/utils.py << 'EOF'\nimport re\nEOF",
    'cat <<EOF > /tmp/relay-proof-x/readtime/utils.py\nx = 1\nEOF',
    'echo "x = 1" >> readtime/utils.py',
    'printf "x" > ./readtime/utils.py',
    "echo x | tee readtime/utils.py",
    "sed -i 's/265/240/' readtime/utils.py",
    "sed -i.bak -e 's/a/b/' readtime/utils.py",
    'cp /tmp/u.py readtime/utils.py',
    'mv /tmp/u.py readtime/utils.py && ls',
    """python3 -c "open('readtime/utils.py', 'w').write('x = 1')" """,
    """python3 -c "f = open('readtime/utils.py', mode='a'); f.write('x')" """,
    """python -c 'open("readtime/utils.py","r+").write("x")'""",
    """python3 -c "from pathlib import Path; Path('readtime/utils.py').write_text('x')" """,
    """python3 -c "import pathlib; p = pathlib.Path('readtime/utils.py'); p.write_text('x')" """,
    """python3 -c "p = 'readtime/utils.py'; open(p, 'w').write('x')" """,
    """python3 -c "import shutil; shutil.copy('/tmp/u.py', 'readtime/utils.py')" """,
    """python3 -c "import shutil; shutil.move('/tmp/u.py', 'readtime/utils.py')" """,
    """python3 -c "import os; os.replace('/tmp/u.py', 'readtime/utils.py')" """,
    """python3 -c "import os; os.rename(os.path.join('/tmp', 'u.py'), 'readtime/utils.py')" """,
    "python3 - <<'PY'\ntext = open('readtime/utils.py').read()\nopen('readtime/utils.py', 'w').write(text + '\\n')\nPY",
]
READS = [
    # readtime pair-relay trial 2, agent-1: read both files, then check imports
    'cat readtime/utils.py readtime/api.py && python3 -c "import bs4, lxml, markdown2, pyquery"',
    'cat readtime/utils.py',
    'cat readtime/utils.py 2>/dev/null; echo done > /tmp/log.txt',
    'grep -n "def " readtime/utils.py',
    'head -50 readtime/utils.py',
    "sed -n '1,40p' readtime/utils.py",
    'python -m pytest readtime/utils.py -q',
    'python3 -m pytest unit_tests -q 2>&1 | tail -5',
    'python3 -c "import readtime.utils as u; print(u.read_time(\'x\'))" && cat readtime/utils.py',
    """python3 -c "print(open('readtime/utils.py').read())" """,
    """python3 -c "print(open('readtime/utils.py').read()); open('/tmp/x.txt', 'w').write('1')" """,
    """python3 -c "import ast; ast.parse(open('readtime/utils.py').read())" """,
    'cp readtime/utils.py /tmp/backup.py',
    'wc -l readtime/utils.py > /tmp/count.txt',
    'diff readtime/utils.py /tmp/u.py',
    'ls readtime/ && cat readtime/api.py',  # another file named only
]


class BashWrites(unittest.TestCase):
    def test_writes(self):
        for cmd in WRITES:
            with self.subTest(cmd=cmd):
                self.assertTrue(rs.bash_writes(cmd, F), cmd)

    def test_reads_never_count(self):
        for cmd in READS:
            with self.subTest(cmd=cmd):
                self.assertFalse(rs.bash_writes(cmd, F), cmd)

    def test_other_file(self):
        self.assertFalse(rs.bash_writes("cat > readtime/api.py <<'EOF'\nx\nEOF", F))
        self.assertTrue(rs.bash_writes("cat > readtime/api.py <<'EOF'\nx\nEOF", 'readtime/api.py'))
        self.assertFalse(rs.bash_writes("""python3 -c "open('readtime/api.py', 'w').write(open('readtime/utils.py').read())" """, F))

    def test_cd_elsewhere(self):
        cwd = '/tmp/relay-proof-fence-off-01-abc'
        # fence-off readtime trial 1, agent-2: wrote a scratch copy in /tmp/agent2-check, not the trial's file
        away = "mkdir -p /tmp/agent2-check/readtime && cd /tmp/agent2-check && cat > readtime/utils.py <<'EOF'\nx\nEOF"
        self.assertFalse(rs.bash_writes(away, F, cwd))
        self.assertTrue(rs.bash_writes(away, F))  # without the trial dir the cd cannot be judged
        self.assertTrue(rs.bash_writes(f"cd /tmp/agent2-check && cat > {cwd}/readtime/utils.py <<'EOF'\nx\nEOF", F, cwd))
        self.assertTrue(rs.bash_writes(f"cd {cwd} && cat > readtime/utils.py <<'EOF'\nx\nEOF", F, cwd))
        self.assertTrue(rs.bash_writes("cat > readtime/utils.py <<'EOF'\nx\nEOF", F, cwd))


if __name__ == '__main__':
    unittest.main()
