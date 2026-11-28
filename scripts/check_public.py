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
    home_path = re.compile(r"""/(?:home|Users)/(?!dev(?:/|$|\s|['"]))[^/\s]+""")
    hits = set()
    for name in sorted(set(files)):
        path = Path(name)
        if not name or not path.is_file():
            continue
        if any(term in name.casefold() for term in terms):
            hits.add((name, 0, "denied term"))
        for number, line in enumerate(path.read_bytes().decode("utf-8", errors="replace").splitlines(), 1):
            if home_path.search(line):
                hits.add((name, number, "home path"))
            if any(term in line.casefold() for term in terms):
                hits.add((name, number, "denied term"))
    for name, number, reason in sorted(hits):
        safe_name = "<redacted>" if any(term in name.casefold() for term in terms) else name
        message = f"{safe_name}:{number}: {reason}"
        print("" if any(term in message.casefold() for term in terms) else message)
    if not hits:
        message = "Public repository check passed"
        print("" if any(term in message.casefold() for term in terms) else message)
    return bool(hits)


if __name__ == "__main__":
    sys.exit(main())
