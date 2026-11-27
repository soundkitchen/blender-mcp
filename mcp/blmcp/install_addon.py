# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""
Install the Blender add-on: ``blender-mcp install-addon``.

Downloads the add-on zip from the newest GitHub Release of this fork
(tagged ``v<version>-fork.<N>``), or takes a local zip,
then installs and enables it with Blender's ``extension install-file`` command.
"""

__all__ = (
    "add_arguments",
    "find_blender",
    "run",
    "select_release",
)

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request

_REPO = "soundkitchen/blender-mcp"
_RELEASES_URL = "https://api.github.com/repos/{:s}/releases?per_page=100".format(_REPO)
_TIMEOUT = 60.0

# Fork release tags, e.g. `v1.0.3-fork.2`.
_TAG_RE = re.compile(r"^v(\d+(?:\.\d+)*)-fork\.(\d+)$")
# The add-on zip written by `make build_addon`, e.g. `mcp-1.0.3.zip`.
_ASSET_RE = re.compile(r"^mcp-.+\.zip$")

_MACOS_BLENDER_BIN = "/Applications/Blender.app/Contents/MacOS/Blender"

# Blender's default user repository for extensions installed from disk.
_BLENDER_REPO = "user_default"


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--blender",
        metavar="PATH",
        help=(
            "Blender binary to install into "
            "(default: $BLENDER_PATH, then blender in PATH, then /Applications/Blender.app on macOS)."
        ),
    )
    parser.add_argument(
        "--zip",
        metavar="PATH",
        help="Install this add-on zip instead of downloading it from the latest GitHub Release.",
    )


def select_release(releases: list[dict[str, object]]) -> tuple[str, str, str] | None:
    """
    Return ``(tag, asset_name, download_url)`` of the newest fork add-on release, or ``None``.

    Releases are ordered by the add-on version, then by the fork release number,
    so the result does not depend on when the releases were created.
    """
    best_key: tuple[tuple[int, ...], int] | None = None
    best: tuple[str, str, str] | None = None
    for release in releases:
        if release.get("draft") or release.get("prerelease"):
            continue
        tag = release.get("tag_name")
        if not isinstance(tag, str):
            continue
        m = _TAG_RE.match(tag)
        if m is None:
            continue
        assets = release.get("assets")
        if not isinstance(assets, list):
            continue
        for asset in assets:
            if not isinstance(asset, dict):
                continue
            name = asset.get("name")
            url = asset.get("browser_download_url")
            if isinstance(name, str) and isinstance(url, str) and _ASSET_RE.match(name):
                key = (tuple(int(part) for part in m.group(1).split(".")), int(m.group(2)))
                if best_key is None or key > best_key:
                    best_key = key
                    best = (tag, name, url)
                break
    return best


def find_blender(explicit: str | None) -> str | None:
    """
    Return the Blender binary to use, or ``None`` when none is found.

    An explicit path or ``BLENDER_PATH`` is returned as given,
    so a wrong value fails loudly instead of silently picking another Blender.
    """
    if explicit:
        return explicit
    env_path = os.environ.get("BLENDER_PATH")
    if env_path:
        return env_path
    in_path = shutil.which("blender")
    if in_path:
        return in_path
    if sys.platform == "darwin" and os.access(_MACOS_BLENDER_BIN, os.X_OK):
        return _MACOS_BLENDER_BIN
    return None


def _urlopen(url: str, accept: str) -> bytes:
    request = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": "blender-mcp"})
    with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
        data: bytes = response.read()
    return data


def _download_latest(dest_dir: str) -> str:
    """Download the newest fork add-on zip into *dest_dir* and return its path."""
    releases = json.loads(_urlopen(_RELEASES_URL, "application/vnd.github+json"))
    if not isinstance(releases, list):
        raise TypeError("Unexpected response from {:s}".format(_RELEASES_URL))
    selected = select_release(releases)
    if selected is None:
        raise RuntimeError(
            "No add-on release (tag v<version>-fork.<N> with an mcp-<version>.zip) "
            "found in https://github.com/{:s}/releases".format(_REPO)
        )
    tag, name, url = selected
    print("Downloading {:s} from release {:s}".format(name, tag), flush=True)
    path = os.path.join(dest_dir, name)
    with open(path, "wb") as fh:
        fh.write(_urlopen(url, "application/octet-stream"))
    return path


def _install(blender: str, zip_path: str) -> int:
    cmd = [blender, "--command", "extension", "install-file", "-r", _BLENDER_REPO, "--enable", zip_path]
    print("Installing with: {:s}".format(" ".join(cmd)), flush=True)
    try:
        return subprocess.run(cmd, check=False).returncode
    except OSError as ex:
        print("Failed to run Blender ({:s}): {:s}".format(blender, str(ex)), file=sys.stderr)
        return 1


def run(args: argparse.Namespace) -> int:
    blender = find_blender(args.blender)
    if blender is None:
        print(
            "Blender not found: pass --blender, set BLENDER_PATH or add blender to PATH",
            file=sys.stderr,
        )
        return 1

    if args.zip:
        if not os.path.isfile(args.zip):
            print("Add-on zip not found: {:s}".format(args.zip), file=sys.stderr)
            return 1
        returncode = _install(blender, args.zip)
    else:
        with tempfile.TemporaryDirectory() as tmp_dir:
            try:
                zip_path = _download_latest(tmp_dir)
            except (OSError, ValueError, TypeError, RuntimeError) as ex:
                # `URLError` and `HTTPError` are `OSError` subclasses,
                # `ValueError` and `TypeError` cover an invalid JSON response.
                if isinstance(ex, urllib.error.HTTPError):
                    message = "HTTP {:d} from {:s}".format(ex.code, ex.url)
                else:
                    message = str(ex)
                print("Failed to download the add-on: {:s}".format(message), file=sys.stderr)
                return 1
            returncode = _install(blender, zip_path)

    if returncode != 0:
        print("Blender failed to install the add-on (exit code {:d})".format(returncode), file=sys.stderr)
        return returncode
    print("Installed and enabled the add-on. Restart Blender if it is running.")
    return 0
