import os
import re
import subprocess
import sys
from pathlib import Path


def main():
    files = subprocess.check_output(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"]
    ).decode().split("\0")
    terms = [term.casefold() for term in os.environ.get("PUBLIC_DENY_TERMS", "").splitlines() if term]
    if not terms:
        print("PUBLIC_DENY_TERMS is empty; denied-term check skipped")
    home_path = re.compile(r"/(?:home|Users)/(?!dev(?:/|$))[^/\s]+/")
    hits = set()
    for name in sorted(set(files)):
        path = Path(name)
        if not name or not path.is_file():
            continue
        for number, line in enumerate(path.read_bytes().decode("utf-8", errors="replace").splitlines(), 1):
            if home_path.search(line):
                hits.add((name, number, "home path"))
            if any(term in line.casefold() for term in terms):
                hits.add((name, number, "denied term"))
    for name, number, reason in sorted(hits):
        print(f"{name}:{number}: {reason}")
    if not hits:
        print("Public repository check passed")
    return bool(hits)


if __name__ == "__main__":
    sys.exit(main())
