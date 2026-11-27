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
    os.path.join("mcp", "blmcp", "data", "api", "examples"),
)

# File extensions to check.
_EXTENSIONS = (
    ".py",
    ".toml",
)


def main() -> int:
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    try:
        filepaths_rel = list_source_files(repo_root, _SCAN_DIRS)
    except RuntimeError as ex:
        print("ERROR: {:s}".format(str(ex)), file=sys.stderr)
        return 1

    fail = 0
    for filepath_rel in filepaths_rel:
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
