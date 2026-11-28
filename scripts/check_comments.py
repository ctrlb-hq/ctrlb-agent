import ast
import difflib
import io
import re
import subprocess
import sys
import tokenize
from pathlib import Path


def git(*args):
    return subprocess.check_output(["git", *args])


def main():
    base = sys.argv[1] if len(sys.argv) > 1 else "origin/main"
    base_files = set(git("ls-tree", "-r", "--name-only", "-z", base).decode().split("\0"))
    files = git("ls-files", "-z", "--cached", "--others", "--exclude-standard").decode().split("\0")
    hits = set()
    for name in sorted(set(files)):
        path = Path(name)
        if not name.endswith(".py") or not path.is_file():
            continue
        with tokenize.open(path) as source:
            text = source.read()
        old = re.split(r"\r\n|\r|\n", git("show", f"{base}:{name}").decode("utf-8")) if name in base_files else []
        new = re.split(r"\r\n|\r|\n", text)
        changed = set()
        for tag, _, _, start, end in difflib.SequenceMatcher(None, old, new, autojunk=False).get_opcodes():
            if tag in ("replace", "insert"):
                changed.update(range(start + 1, end + 1))
        for token in tokenize.generate_tokens(io.StringIO(text).readline):
            if token.type == tokenize.COMMENT and token.start[0] in changed:
                hits.add((name, token.start[0]))
        tree = ast.parse(text, filename=name)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
                first = node.body[0]
                if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
                    for line in range(first.lineno, first.end_lineno + 1):
                        if line in changed:
                            hits.add((name, line))
    for name, line in sorted(hits):
        print(f"{name}:{line}")
    if not hits:
        print("Comment check passed")
    return bool(hits)


if __name__ == "__main__":
    sys.exit(main())
