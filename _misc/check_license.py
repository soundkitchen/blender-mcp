# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""
Verify that all Python files contain an SPDX license header on the first line.
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
    os.path.join("mcp", "blmcp", "data", "api"),
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
    """
    Entry point for the check-license script.
    """
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    fail = 0
    count = 0
    for filepath_rel in _source_files(repo_root):
        if any(filepath_rel.startswith(d + os.sep) for d in _SKIP_DIRS):
            continue
        if not filepath_rel.endswith(".py"):
            continue
        with open(os.path.join(repo_root, filepath_rel), "r", encoding="utf-8") as fh:
            first_line = fh.readline()
        if "SPDX" not in first_line:
            print("Missing SPDX in: {:s}".format(filepath_rel))
            fail = 1
        else:
            count += 1

    print("Found {:d} files with SPDX headers.".format(count))
    return fail


if __name__ == "__main__":
    sys.exit(main())
