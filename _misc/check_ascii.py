# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""
Verify that source files contain only ASCII characters.
"""

__all__ = (
    "main",
)

import os
import subprocess
import sys

# Directories to scan.
_SCAN_DIRS = (
    os.path.join("mcp"),
    os.path.join("addon"),
    os.path.join("chat_client"),
)

# Directories to skip (relative to the repository root).
_SKIP_DIRS = (
    os.path.join("mcp", "blmcp", "data", "api", "examples"),
)

# File extensions to check.
_EXTENSIONS = (
    ".py",
    ".toml",
)


def _source_files(repo_root: str) -> list[str]:
    """
    Return the files under ``_SCAN_DIRS`` that git knows about, relative to *repo_root*.

    Tracked files and untracked files that are not ignored are listed,
    so virtual environments, build directories and caches are skipped,
    while new files are checked before they are added.
    """
    output = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z", "--", *_SCAN_DIRS],
        cwd=repo_root,
        check=True,
        capture_output=True,
    ).stdout
    result = []
    # Unmerged files are listed once per stage, so remove duplicates.
    for path in sorted(set(output.decode("utf-8").split("\0"))):
        if not path:
            continue
        path = os.path.normpath(path)
        # Tracked files deleted from the working tree are still listed.
        if os.path.isfile(os.path.join(repo_root, path)):
            result.append(path)
    return result


def main() -> int:
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    fail = 0
    for filepath_rel in _source_files(repo_root):
        if any(filepath_rel.startswith(d + os.sep) for d in _SKIP_DIRS):
            continue
        if not filepath_rel.endswith(_EXTENSIONS):
            continue
        with open(os.path.join(repo_root, filepath_rel), "rb") as fh:
            for line_number, line in enumerate(fh, 1):
                try:
                    line.decode("ascii")
                except UnicodeDecodeError:
                    print("{:s}:{:d}:{:s}".format(
                        filepath_rel, line_number, line.decode("utf-8", errors="replace").rstrip(),
                    ))
                    fail = 1

    if fail:
        print("ERROR: non-ASCII characters found")
    return fail


if __name__ == "__main__":
    sys.exit(main())
