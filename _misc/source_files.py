# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""
List the source files that the check scripts look at.

Shared by ``check_license.py`` and ``check_ascii.py``.
"""

__all__ = (
    "list_source_files",
)

import os
import subprocess


def list_source_files(repo_root: str, scan_dirs: tuple[str, ...]) -> list[str]:
    """
    Return the files under *scan_dirs* that git knows about, relative to *repo_root*.

    Tracked files and untracked files that are not ignored are listed,
    so virtual environments, build directories and caches are skipped,
    while new files are checked before they are added.
    Ignore rules include ``.git/info/exclude`` and the user's global excludes file,
    so a new file matching personal ignore rules is only checked once it is added.

    Raise ``RuntimeError`` when git cannot list the files
    (git is not installed, or *repo_root* is not a git work tree).
    """
    cmd = ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z", "--", *scan_dirs]
    try:
        output = subprocess.run(cmd, cwd=repo_root, check=True, capture_output=True).stdout
    except FileNotFoundError as ex:
        raise RuntimeError("git is required to list the files to check: {:s}".format(str(ex))) from ex
    except subprocess.CalledProcessError as ex:
        stderr = os.fsdecode(ex.stderr).strip()
        raise RuntimeError(
            "git could not list the files to check (exit status {:d}): {:s}".format(ex.returncode, stderr)
        ) from ex

    result = []
    # Unmerged files are listed once per stage, so remove duplicates.
    # `os.fsdecode` keeps file names that are not valid UTF-8 (as surrogate escapes).
    for path_bytes in sorted(set(output.split(b"\0"))):
        if not path_bytes:
            continue
        path = os.path.normpath(os.fsdecode(path_bytes))
        # Tracked files deleted from the working tree are still listed.
        if os.path.isfile(os.path.join(repo_root, path)):
            result.append(path)
    return result
