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
import sys

from source_files import list_source_files

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


def main() -> int:
    """
    Entry point for the check-license script.
    """
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    try:
        filepaths_rel = list_source_files(repo_root, _SCAN_DIRS)
    except RuntimeError as ex:
        print("ERROR: {:s}".format(str(ex)), file=sys.stderr)
        return 1

    fail = 0
    count = 0
    for filepath_rel in filepaths_rel:
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
