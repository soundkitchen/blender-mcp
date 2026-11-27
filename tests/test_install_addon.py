# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""
Checks for ``blender-mcp install-addon``: release selection, Blender lookup and the install command.

No network access or Blender is needed, downloads and Blender are mocked.
"""

__all__ = ()

import argparse
import importlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from typing import Any
from unittest import mock

_REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MCP_DIR = os.path.join(_REPO_DIR, "mcp")


def _import_install_addon() -> Any:
    if _MCP_DIR not in sys.path:
        sys.path.insert(0, _MCP_DIR)
    return importlib.import_module("blmcp.install_addon")


def _release(tag: str, assets: list[str], draft: bool = False, prerelease: bool = False) -> dict[str, object]:
    return {
        "tag_name": tag,
        "draft": draft,
        "prerelease": prerelease,
        "assets": [
            {"name": name, "browser_download_url": "https://example.invalid/{:s}/{:s}".format(tag, name)}
            for name in assets
        ],
    }


def _args(blender: str | None = None, zip_path: str | None = None) -> argparse.Namespace:
    return argparse.Namespace(command="install-addon", blender=blender, zip=zip_path)


class TestSelectRelease(unittest.TestCase):

    def setUp(self) -> None:
        self.mod = _import_install_addon()

    def test_empty(self) -> None:
        self.assertIsNone(self.mod.select_release([]))

    def test_picks_highest_version_then_fork_number(self) -> None:
        releases = [
            _release("v1.0.3-fork.2", ["mcp-1.0.3.zip"]),
            _release("v1.0.3-fork.10", ["mcp-1.0.3.zip"]),
            _release("v1.0.4-fork.1", ["mcp-1.0.4.zip"]),
            _release("v1.0.3-fork.1", ["mcp-1.0.3.zip"]),
        ]
        tag, name, url = self.mod.select_release(releases)
        self.assertEqual((tag, name), ("v1.0.4-fork.1", "mcp-1.0.4.zip"))
        self.assertEqual(url, "https://example.invalid/v1.0.4-fork.1/mcp-1.0.4.zip")

    def test_fork_number_is_numeric(self) -> None:
        releases = [
            _release("v1.0.3-fork.9", ["mcp-1.0.3.zip"]),
            _release("v1.0.3-fork.10", ["mcp-1.0.3.zip"]),
        ]
        self.assertEqual(self.mod.select_release(releases)[0], "v1.0.3-fork.10")

    def test_version_is_numeric(self) -> None:
        releases = [
            _release("v1.0.9-fork.1", ["mcp-1.0.9.zip"]),
            _release("v1.0.10-fork.1", ["mcp-1.0.10.zip"]),
        ]
        self.assertEqual(self.mod.select_release(releases)[0], "v1.0.10-fork.1")

    def test_skips_non_fork_tags(self) -> None:
        releases = [
            _release("v9.9.9", ["mcp-9.9.9.zip"]),
            _release("v9.9.9-fork.x", ["mcp-9.9.9.zip"]),
            _release("v1.0.3-fork.1", ["mcp-1.0.3.zip"]),
        ]
        self.assertEqual(self.mod.select_release(releases)[0], "v1.0.3-fork.1")

    def test_skips_draft_and_prerelease(self) -> None:
        releases = [
            _release("v1.0.4-fork.1", ["mcp-1.0.4.zip"], draft=True),
            _release("v1.0.4-fork.2", ["mcp-1.0.4.zip"], prerelease=True),
            _release("v1.0.3-fork.1", ["mcp-1.0.3.zip"]),
        ]
        self.assertEqual(self.mod.select_release(releases)[0], "v1.0.3-fork.1")

    def test_skips_releases_without_addon_zip(self) -> None:
        releases = [
            _release("v1.0.4-fork.1", ["notes.txt"]),
            _release("v1.0.3-fork.1", ["notes.txt", "mcp-1.0.3.zip"]),
        ]
        tag, name, _url = self.mod.select_release(releases)
        self.assertEqual((tag, name), ("v1.0.3-fork.1", "mcp-1.0.3.zip"))


class TestFindBlender(unittest.TestCase):

    def setUp(self) -> None:
        self.mod = _import_install_addon()

    def _find(
            self,
            explicit: str | None = None,
            env: str | None = None,
            which: str | None = None,
            platform: str = "linux",
            macos_app: bool = False,
    ) -> str | None:
        environ = {"BLENDER_PATH": env} if env is not None else {}
        with (
            mock.patch.dict(os.environ, environ, clear=True),
            mock.patch.object(self.mod.shutil, "which", return_value=which),
            mock.patch.object(self.mod.sys, "platform", platform),
            mock.patch.object(self.mod.os, "access", return_value=macos_app),
        ):
            result: str | None = self.mod.find_blender(explicit)
        return result

    def test_explicit_wins(self) -> None:
        self.assertEqual(self._find(explicit="/x/blender", env="/env/blender", which="/usr/bin/blender"), "/x/blender")

    def test_env_before_path(self) -> None:
        self.assertEqual(self._find(env="/env/blender", which="/usr/bin/blender"), "/env/blender")

    def test_path(self) -> None:
        self.assertEqual(self._find(which="/usr/bin/blender"), "/usr/bin/blender")

    def test_macos_app(self) -> None:
        self.assertEqual(self._find(platform="darwin", macos_app=True), self.mod._MACOS_BLENDER_BIN)

    def test_macos_app_only_on_macos(self) -> None:
        self.assertIsNone(self._find(platform="linux", macos_app=True))

    def test_not_found(self) -> None:
        self.assertIsNone(self._find(platform="darwin", macos_app=False))


class TestRun(unittest.TestCase):

    def setUp(self) -> None:
        self.mod = _import_install_addon()

    def _run(self, args: argparse.Namespace) -> tuple[int, str, str]:
        out = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            returncode: int = self.mod.run(args)
        return returncode, out.getvalue(), err.getvalue()

    def test_local_zip_runs_install_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            zip_path = os.path.join(tmp, "mcp-1.0.3.zip")
            with open(zip_path, "wb"):
                pass
            completed: subprocess.CompletedProcess[bytes] = subprocess.CompletedProcess([], 0)
            with mock.patch.object(self.mod.subprocess, "run", return_value=completed) as run_mock:
                returncode, _out, _err = self._run(_args(blender="/x/blender", zip_path=zip_path))
        self.assertEqual(returncode, 0)
        run_mock.assert_called_once()
        self.assertEqual(
            run_mock.call_args.args[0],
            ["/x/blender", "--command", "extension", "install-file", "-r", "user_default", "--enable", zip_path],
        )

    def test_missing_local_zip(self) -> None:
        with mock.patch.object(self.mod.subprocess, "run") as run_mock:
            returncode, _out, err = self._run(_args(blender="/x/blender", zip_path="/no/such/mcp.zip"))
        self.assertEqual(returncode, 1)
        self.assertIn("not found", err)
        run_mock.assert_not_called()

    def test_blender_not_found(self) -> None:
        with mock.patch.object(self.mod, "find_blender", return_value=None):
            returncode, _out, err = self._run(_args())
        self.assertEqual(returncode, 1)
        self.assertIn("Blender not found", err)

    def test_blender_failure_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            zip_path = os.path.join(tmp, "mcp-1.0.3.zip")
            with open(zip_path, "wb"):
                pass
            completed: subprocess.CompletedProcess[bytes] = subprocess.CompletedProcess([], 3)
            with mock.patch.object(self.mod.subprocess, "run", return_value=completed):
                returncode, _out, err = self._run(_args(blender="/x/blender", zip_path=zip_path))
        self.assertEqual(returncode, 3)
        self.assertIn("exit code 3", err)

    def test_download_installs_selected_asset(self) -> None:
        releases = [_release("v1.0.3-fork.1", ["mcp-1.0.3.zip"])]
        payloads = {
            self.mod._RELEASES_URL: json.dumps(releases).encode("utf-8"),
            "https://example.invalid/v1.0.3-fork.1/mcp-1.0.3.zip": b"zip-bytes",
        }
        installed: list[tuple[str, bytes]] = []

        def fake_run(cmd: list[str], check: bool) -> subprocess.CompletedProcess[bytes]:
            del check
            with open(cmd[-1], "rb") as fh:
                installed.append((os.path.basename(cmd[-1]), fh.read()))
            return subprocess.CompletedProcess(cmd, 0)

        with (
            mock.patch.object(self.mod, "_urlopen", side_effect=lambda url, accept: payloads[url]),
            mock.patch.object(self.mod.subprocess, "run", side_effect=fake_run),
        ):
            returncode, out, _err = self._run(_args(blender="/x/blender"))
        self.assertEqual(returncode, 0)
        self.assertEqual(installed, [("mcp-1.0.3.zip", b"zip-bytes")])
        self.assertIn("v1.0.3-fork.1", out)

    def test_no_release(self) -> None:
        with (
            mock.patch.object(self.mod, "_urlopen", return_value=b"[]"),
            mock.patch.object(self.mod.subprocess, "run") as run_mock,
        ):
            returncode, _out, err = self._run(_args(blender="/x/blender"))
        self.assertEqual(returncode, 1)
        self.assertIn("No add-on release", err)
        run_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
