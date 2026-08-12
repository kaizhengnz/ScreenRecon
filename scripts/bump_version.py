"""Bump __version__ in src/screenrecon/__init__.py.

Usage: python scripts/bump_version.py {patch|minor|major}

Writes new_version=X.Y.Z to $GITHUB_OUTPUT when run inside GitHub Actions.
"""

from __future__ import annotations

import os
import pathlib
import re
import sys

VERSION_FILE = pathlib.Path("src/screenrecon/__init__.py")
VERSION_RE = re.compile(r'(__version__\s*=\s*")(\d+)\.(\d+)\.(\d+)(")')


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in {"patch", "minor", "major"}:
        print("usage: bump_version.py {patch|minor|major}", file=sys.stderr)
        return 2

    segment = sys.argv[1]
    text = VERSION_FILE.read_text(encoding="utf-8")
    match = VERSION_RE.search(text)
    if not match:
        print(f"could not find __version__ in {VERSION_FILE}", file=sys.stderr)
        return 1

    prefix, major_s, minor_s, patch_s, suffix = match.groups()
    major, minor, patch = int(major_s), int(minor_s), int(patch_s)

    if segment == "patch":
        patch += 1
    elif segment == "minor":
        minor += 1
        patch = 0
    else:
        major += 1
        minor = 0
        patch = 0

    new_version = f"{major}.{minor}.{patch}"
    new_text = VERSION_RE.sub(f"{prefix}{new_version}{suffix}", text, count=1)
    VERSION_FILE.write_text(new_text, encoding="utf-8")

    gh_output = os.environ.get("GITHUB_OUTPUT")
    if gh_output:
        with open(gh_output, "a", encoding="utf-8") as handle:
            handle.write(f"new_version={new_version}\n")

    print(f"bumped {major_s}.{minor_s}.{patch_s} -> {new_version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
