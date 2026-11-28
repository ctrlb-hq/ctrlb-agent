import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class CheckerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        self.git('init', '-q')
        self.git('config', 'user.email', 'dev@example.test')
        self.git('config', 'user.name', 'Dev')

    def git(self, *args):
        return subprocess.run(['git', *args], cwd=self.repo, check=True, capture_output=True)

    def run_checker(self, name, *args, terms=''):
        return subprocess.run(['python3', str(ROOT / 'scripts' / name), *args], cwd=self.repo,
                              env=dict(os.environ, PUBLIC_DENY_TERMS=terms), capture_output=True, text=True)

    def test_physical_lines(self):
        path = self.repo / 'sample.py'
        for separator in ('\u2028', '\u2029'):
            for newline in ('\n', '\r\n', '\r'):
                with self.subTest(separator=separator, newline=repr(newline)):
                    path.write_bytes(('x = "a' + separator + 'b"' + newline + '# unchanged' + newline).encode())
                    self.git('add', 'sample.py')
                    self.git('commit', '-qm', 'base', '--allow-empty')
                    path.write_bytes(('x = "a' + separator + 'b"' + newline + '# unchanged' + newline + '# added' + newline).encode())
                    result = self.run_checker('check_comments.py', 'HEAD')
                    self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                    self.assertIn('sample.py:3', result.stdout)
                    path.write_bytes(('x = "a' + separator + 'c"' + newline + '# unchanged' + newline).encode())
                    result = self.run_checker('check_comments.py', 'HEAD')
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_denied_filename(self):
        term = ''.join(('synthetic', '-marker'))
        (self.repo / (term + '.txt')).write_text('harmless')
        result = self.run_checker('check_public.py', terms=term.upper())
        self.assertEqual(result.returncode, 1)
        self.assertNotIn(term, (result.stdout + result.stderr).casefold())

    def test_home_paths(self):
        path = self.repo / 'sample.txt'
        for value in (f'/{root}/person{tail}' for root in ('home', 'Users') for tail in ('', '/file')):
            with self.subTest(value=value):
                path.write_text(value)
                self.assertEqual(self.run_checker('check_public.py').returncode, 1)
        for value in ('/home/dev', '/home/dev/file'):
            with self.subTest(value=value):
                path.write_text(value)
                self.assertEqual(self.run_checker('check_public.py').returncode, 0)


if __name__ == '__main__':
    unittest.main()
