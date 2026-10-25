# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""
Integration test for MCP & Blender.

Verifies: Mock-LLM-Client -> MCP server -> Blender addon -> Blender.

The "Mock-LLM-Client" simply sends requests to the MCP server,
the "useful" part of these tests is that it connects to a real Blender instance
and runs the MCP tools verifying they give correct results.

This runs in both background & foreground mode (headless)
where it's possible to check that taking screenshots works properly.

Defaults to ``blender`` and ``blender-mcp`` from ``PATH``.
Override with ``BLENDER_BIN`` and ``BLENDER_MCP`` environment variables.

Foreground tests run headless via a Wayland display server (Weston).
Set ``BLENDER_MCP_FOREGROUND=1`` to use the real display instead.
"""

__all__ = ()

import base64
import glob
import inspect
import json
import math
import os
import shlex
import shutil
import signal
import socket
import struct
import subprocess
import tempfile
import textwrap
import threading
import time
import unittest

import sys

import atexit

# Root of the repository.
_REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Ensure the repository root is on the path so `tests` resolves as a package.
if _REPO_DIR not in sys.path:
    sys.path.insert(0, _REPO_DIR)

from tests.mcp_client import MCPClient

# Fixed ports for the test servers (background and foreground).
_PORT_BACKGROUND = 9876
_PORT_FOREGROUND = 9877
_PORT_INTERACTIVE = 9878

# Scale all timeouts (e.g. `GLOBAL_TIMEOUT_SCALE=2` doubles every limit).
_TIMEOUT_SCALE = float(os.environ.get("GLOBAL_TIMEOUT_SCALE", "1"))

# Maximum time to wait for Blender to start (seconds).
_TIMEOUT_STARTUP = int(int(os.environ.get("BLENDER_MCP_TIMEOUT", "10")) * _TIMEOUT_SCALE)

# Maximum time to wait for a local process to respond or exit (seconds).
_TIMEOUT_LOCAL_PROC = int(10 * _TIMEOUT_SCALE)

# Tool coverage tracking.
_all_tools: set[str] = set()
_tested_tools: set[str] = set()


def _print_untested_tools() -> None:
    """
    Print tools that were not exercised by any test.
    """
    untested = sorted(_all_tools - _tested_tools)
    if not untested:
        return
    print("\nUntested tools ({:d}/{:d}):".format(len(untested), len(_all_tools)))
    for name in untested:
        print("  - {:s}".format(name))


atexit.register(_print_untested_tools)


def _python_fn_body_as_string(fn: object) -> str:
    """
    Return the body of *fn* as a dedented string.
    """
    source = inspect.getsource(fn)
    lines = source.splitlines()
    body_lines = lines[1:]
    code = textwrap.dedent("\n".join(body_lines))
    assert code.strip(), "Function body is empty"
    return code


def _blender_env(tmpdir: str) -> dict[str, str]:
    """
    Return an environment dict for Blender sub-processes.

    Sets ``HOME`` to *tmpdir* so that Blender reads and writes its
    configuration there instead of touching the real user directory.
    Disables ASAN leak checking so debug builds exit cleanly.
    """
    env = os.environ.copy()
    env["HOME"] = tmpdir
    env["ASAN_OPTIONS"] = ":".join(filter(None, [
        env.get("ASAN_OPTIONS", ""),
        "alloc_dealloc_mismatch=0",
        "leak_check_at_exit=0",
    ]))
    return env


def _run_blender(args: list[str], env: dict[str, str]) -> None:
    """
    Run a Blender command and raise on failure, including stderr in the message.
    """
    result = subprocess.run(args, capture_output=True, env=env)
    if result.returncode != 0:
        raise RuntimeError(
            "Command failed (exit {:d}):\n  {:s}\n{:s}".format(
                result.returncode,
                " ".join(args),
                result.stderr.decode("utf-8", errors="replace"),
            )
        )


def _drain_stdout(proc: "subprocess.Popen[bytes]") -> list[str]:
    """
    Read *proc* stdout in a daemon thread, collecting lines.

    This prevents the pipe buffer from filling up and blocking the child.
    Returns a list that is appended to from the reader thread.
    """
    lines: list[str] = []

    def _reader() -> None:
        assert proc.stdout is not None
        for raw in proc.stdout:
            lines.append(raw.decode("utf-8", errors="replace"))

    threading.Thread(target=_reader, daemon=True).start()
    return lines


def _start_headless_display(env: dict[str, str]) -> "subprocess.Popen[bytes]":
    """
    Start a headless Wayland display server and add ``WAYLAND_DISPLAY`` to *env*.

    Returns the weston process. The caller must call
    ``_stop_headless_display`` when done.
    """
    from tests.utils.blender_headless import backend_wayland

    weston_socket = "wl-blmcp-{:d}".format(os.getpid())
    weston_bin = os.environ.get("WESTON_BIN", "weston")
    weston_env, weston_ini = backend_wayland._weston_env_and_ini()

    ini_fd, ini_path = tempfile.mkstemp(prefix="weston_", suffix=".ini")
    with os.fdopen(ini_fd, "w", encoding="utf-8") as fh:
        fh.write(weston_ini)

    cmd = [
        weston_bin,
        "--socket={:s}".format(weston_socket),
        "--backend=headless",
        "--width=800",
        "--height=600",
        "--config={:s}".format(ini_path),
    ]
    weston_kw: dict[str, object] = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
    }
    if weston_env is not None:
        weston_kw["env"] = weston_env

    proc = subprocess.Popen(cmd, **weston_kw)

    if not backend_wayland._wait_for_wayland_server(
        socket=weston_socket, timeout=_TIMEOUT_LOCAL_PROC,
    ):
        proc.send_signal(signal.SIGINT)
        proc.communicate()
        os.remove(ini_path)
        raise RuntimeError("Failed to start headless Wayland display server")

    env["WAYLAND_DISPLAY"] = weston_socket
    # Store for cleanup.
    proc._weston_ini_path = ini_path  # type: ignore[attr-defined]
    return proc


def _stop_headless_display(proc: "subprocess.Popen[bytes]") -> None:
    """
    Stop the headless Wayland display server started by ``_start_headless_display``.
    """
    proc.send_signal(signal.SIGINT)
    proc.communicate(timeout=_TIMEOUT_LOCAL_PROC)
    ini_path = getattr(proc, "_weston_ini_path", None)
    if ini_path is not None and os.path.exists(ini_path):
        os.remove(ini_path)


def _wait_for_port(
        port: int,
        timeout: int,
        proc: "subprocess.Popen[bytes]",
        output: list[str],
) -> None:
    """
    Block until a TCP connection to *port* on localhost succeeds.

    Checks *proc* on each iteration so an early crash is reported
    immediately instead of waiting for the full timeout.
    Raises ``RuntimeError`` if *timeout* seconds elapse or *proc* exits.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rc = proc.poll()
        if rc is not None:
            raise RuntimeError(
                "Blender exited with code {:d} before the server became reachable\n{:s}".format(
                    rc, "".join(output[-50:]),
                )
            )
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(1)
                sock.connect(("localhost", port))
                return
        except (ConnectionRefusedError, OSError):
            time.sleep(0.2)
    raise RuntimeError(
        "Port {:d} not reachable within {:d}s\n{:s}".format(
            port, timeout, "".join(output[-50:]),
        )
    )


class _TestServerMixin:
    """
    Shared setup, cleanup, helpers and test methods for both background
    and foreground server modes.

    Concrete subclasses set ``_background`` and ``_port`` as class variables.
    """

    _background: bool
    _interactive: bool = False
    _port: int

    @classmethod
    def setUpClass(cls) -> None:
        blender_bin = os.environ.get("BLENDER_BIN", "blender")
        blender_mcp = os.environ.get("BLENDER_MCP", "blender-mcp")

        cls._tmpdir = tempfile.TemporaryDirectory()
        tmpdir = cls._tmpdir.name
        cls.addClassCleanup(cls._tmpdir.cleanup)

        env = _blender_env(tmpdir)

        # Build the extension zip.
        addon_src = os.path.join(_REPO_DIR, "addon", "blender_mcp_addon")
        _run_blender(
            [
                blender_bin, "--command", "extension", "build",
                "--source-dir=" + addon_src,
                "--output-dir=" + tmpdir,
            ],
            env=env,
        )

        zips = glob.glob(os.path.join(tmpdir, "mcp-*.zip"))
        if not zips:
            raise RuntimeError("Extension build did not produce a zip")

        # Install the extension into the isolated HOME.
        _run_blender(
            [
                blender_bin, "--online-mode", "--background", "--factory-startup",
                "--command", "extension", "install-file",
                zips[0], "--repo", "user_default", "--enable",
            ],
            env=env,
        )

        if cls._interactive:
            # Save preferences before launching so the autostart timer
            # reads the correct port with no delay.
            # This could be supported more generically by passing arbitrary
            # preference overrides, but port and delay are all we need for now.
            _run_blender(
                [
                    blender_bin, "--background",
                    "--python-expr",
                    (
                        "import bpy; "
                        "prefs = bpy.context.preferences.addons"
                        "['bl_ext.user_default.mcp'].preferences; "
                        "prefs.port = {:d}; "
                        "prefs.autostart_delay = 0.0; "
                        "bpy.ops.wm.save_userpref()"
                    ).format(cls._port),
                ],
                env=env,
            )

        # Start a headless display server for non-background tests.
        # Registered before Blender so cleanup order is Blender first,
        # then the display server (LIFO).
        if not cls._background and not os.environ.get("BLENDER_MCP_FOREGROUND"):
            cls._weston_proc = _start_headless_display(env)
            cls.addClassCleanup(_stop_headless_display, cls._weston_proc)

        # Start Blender with the installed addon.
        # Omit `--factory-startup` so saved preferences (with the
        # extension enabled) are loaded from the isolated HOME.
        blender_args = [blender_bin, "--online-mode"]
        if cls._background:
            blender_args.append("--background")
        # Use Vulkan when not in background mode. OpenGL (llvmpipe) is known
        # to crash during shader JIT compilation under headless Weston with
        # recent LLVM/Mesa versions.
        if not cls._background:
            blender_args.extend(["--gpu-backend", "vulkan"])
        if not cls._interactive:
            blender_args.extend([
                "--command", "blender_mcp", "--port", str(cls._port),
            ])

        cls._blender_proc = subprocess.Popen(
            blender_args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
        )
        cls.addClassCleanup(cls._cleanup_blender)

        output = _drain_stdout(cls._blender_proc)
        _wait_for_port(cls._port, _TIMEOUT_STARTUP, cls._blender_proc, output)

        mcp_env = _blender_env(tmpdir)
        mcp_env["BLENDER_MCP_PORT"] = str(cls._port)
        mcp_env["BLENDER_PATH"] = blender_bin

        cls._client = MCPClient(shlex.split(blender_mcp), env=mcp_env)
        cls.addClassCleanup(cls._client.close)
        cls._client.initialize()
        _all_tools.update(cls._client.list_tools())

        # Save a blend file for CLI tools.
        cls._blend_path = os.path.join(tmpdir, "test.blend")
        cls._client.call_tool("execute_blender_code", {
            "code": (
                "import bpy\n"
                "bpy.ops.wm.save_as_mainfile(filepath={!r})\n"
                "result = {{'saved': True}}\n"
            ).format(cls._blend_path),
        })

    @classmethod
    def _cleanup_blender(cls) -> None:
        """
        Terminate Blender and close its stdout pipe.
        """
        cls._blender_proc.terminate()
        cls._blender_proc.wait(timeout=_TIMEOUT_LOCAL_PROC)
        if cls._blender_proc.stdout is not None:
            cls._blender_proc.stdout.close()

    def _call_tool(
        self,
        name: str,
        arguments: dict[str, object] | None = None,
    ) -> list[dict[str, object]]:
        """
        Call a tool, verify the response is not an error, and return the content list.
        """
        _tested_tools.add(name)
        result = self._client.call_tool(name, arguments)
        content = result.get("content", [])
        self.assertFalse(
            result.get("isError", False),
            "Tool {:s} returned an error: {!r}".format(name, content),
        )
        self.assertIsInstance(content, list)
        self.assertTrue(
            len(content) > 0,
            "Expected at least one content item for {:s}".format(name),
        )
        return content

    def _call_tool_expect_error(
        self,
        name: str,
        arguments: dict[str, object] | None = None,
    ) -> list[dict[str, object]]:
        """
        Call a tool and assert that the response is an MCP-level error.
        """
        _tested_tools.add(name)
        result = self._client.call_tool(name, arguments)
        self.assertTrue(
            result.get("isError", False),
            "Expected {:s} to return isError".format(name),
        )
        return result.get("content", [])

    def _test_tool(
        self,
        name: str,
        arguments: dict[str, object] | None = None,
    ) -> dict[str, object]:
        """
        Call a tool that returns text and return the parsed JSON result.

        When the response contains ``"status": "ok"`` the ``"result"``
        value is returned directly. Error responses are returned as-is.
        """
        content = self._call_tool(name, arguments)
        text_item = content[0]
        self.assertEqual(
            text_item.get("type"), "text",
            "Expected text content for {:s}, got {!r}".format(
                name, text_item.get("type"),
            ),
        )
        data = json.loads(text_item["text"])
        if data.get("status") == "ok":
            # Tools that run code in Blender wrap their response in
            # `{"status": "ok", "result": ...}`, but toolcode-based
            # tools may omit the `result` key.
            return data.get("result", data)
        return data

    def setUp(self) -> None:
        """Reload the default scene so each test starts from a clean state."""
        self._client.call_tool("execute_blender_code", {
            "code": (
                "import bpy\n"
                "bpy.ops.wm.read_homefile(use_empty=False)\n"
                "result = {'reset': True}\n"
            ),
        })

    # -----------------------------------------------------------------
    # Interactive tools.

    def test_execute_blender_code(self) -> None:
        data = self._test_tool("execute_blender_code", {
            "code": "result = {'value': 1 + 1}",
        })
        self.assertEqual(data["value"], 2)

    def test_execute_blender_code_registers_blmcp_subd(self) -> None:
        """
        Code that imports ``blmcp_subd`` gets the module, replaced when its source changes.
        """
        def code_import() -> None:
            import sys
            import blmcp_subd  # type: ignore[import-not-found]
            result = {  # noqa: F841
                "id": id(sys.modules["blmcp_subd"]),
                "has_insert_loop": callable(blmcp_subd.insert_loop),
            }
        data = self._test_tool("execute_blender_code", {"code": _python_fn_body_as_string(code_import)})
        self.assertTrue(data["has_insert_loop"])
        first_id = data["id"]
        # Registered again only when the source differs.
        data = self._test_tool("execute_blender_code", {"code": _python_fn_body_as_string(code_import)})
        self.assertEqual(data["id"], first_id)

        def code_outdated() -> None:
            import sys
            sys.modules["blmcp_subd"].__blmcp_digest__ = "outdated"
            result = {'ok': True}  # noqa: F841
        self._test_tool("execute_blender_code", {"code": _python_fn_body_as_string(code_outdated)})
        data = self._test_tool("execute_blender_code", {"code": _python_fn_body_as_string(code_import)})
        self.assertNotEqual(data["id"], first_id)

        # Errors in the module show its source lines, the code's line numbers are unchanged.
        def code_error() -> None:
            import blmcp_subd  # type: ignore[import-not-found]
            import bmesh  # type: ignore[import-not-found]
            blmcp_subd.extrude(bmesh.new(), [])
        data = self._test_tool("execute_blender_code", {"code": _python_fn_body_as_string(code_error)})
        self.assertEqual(data["status"], "error")
        message = str(data["message"])
        self.assertIn('File "<blmcp_subd>"', message)
        self.assertIn('raise ValueError("No faces to extrude")', message)
        self.assertIn('File "<string>", line 3, in <module>', message)

    def test_get_blendfile_summary_datablocks(self) -> None:
        data = self._test_tool("get_blendfile_summary_datablocks")
        self.assertEqual(data["scene_name"], "Scene")
        self.assertIn("Layout", data["workspaces"])
        self.assertIsInstance(data["datablock_counts"], dict)

    def test_get_blendfile_summary_missing_files(self) -> None:
        data = self._test_tool("get_blendfile_summary_missing_files")
        self.assertIsInstance(data["missing_files"], list)
        self.assertEqual(data["missing_files"], [])

    def test_get_blendfile_summary_of_linked_libraries(self) -> None:
        data = self._test_tool("get_blendfile_summary_of_linked_libraries")
        self.assertEqual(data["total_library_count"], 0)
        self.assertEqual(data["direct_libraries"], [])
        self.assertEqual(data["indirect_libraries"], [])

    def test_get_blendfile_summary_path_info(self) -> None:
        data = self._test_tool("get_blendfile_summary_path_info")
        self.assertEqual(data["filepath"], "")
        self.assertFalse(data["is_saved"])
        self.assertIsNone(data["age_seconds"])
        self.assertIsNone(data["file_size_bytes"])
        self.assertIsNone(data["backups"])

    def test_get_blendfile_summary_usage_guess(self) -> None:
        data = self._test_tool("get_blendfile_summary_usage_guess")
        guesses = data["usage_guesses"]
        self.assertIn("Animation", guesses)
        self.assertIn("Modeling", guesses)
        for scores in guesses.values():
            self.assertIn("score", scores)
            self.assertIn("certainty", scores)

    def _call_tool_screenshot(
        self,
        name: str,
        arguments: dict[str, object] | None = None,
    ) -> list[dict[str, object]]:
        _tested_tools.add(name)
        result = self._client.call_tool(name, arguments)
        content = result.get("content", [])
        if not self._interactive:
            # Non-interactive modes (--background and --command) have
            # bpy.app.background == True, so screenshots are unavailable.
            self.assertTrue(
                result.get("isError", False),
                "Expected {:s} to fail in non-interactive mode".format(name),
            )
            # The reason reaches the client.
            self.assertIn("background mode", str(content[0].get("text", "")))
        else:
            self.assertFalse(
                result.get("isError", False),
                "Tool {:s} returned an error: {!r}".format(name, content),
            )
            self.assertEqual(content[0].get("type"), "image")
            image_data = content[0].get("data", "")
            self.assertTrue(len(image_data) > 0)
            width, height = self._image_size(image_data)
            self.assertGreater(width, 1)
            self.assertLess(width, 4096)
            self.assertGreater(height, 1)
            self.assertLess(height, 4096)
        return content

    @staticmethod
    def _image_size(image_base64: str) -> tuple[int, int]:
        """Return (width, height) of a base64-encoded PNG by reading the IHDR chunk."""
        data = base64.b64decode(image_base64)
        if data[:8] != b"\x89PNG\r\n\x1a\n":
            return (-1, -1)
        width, height = struct.unpack(">II", data[16:24])
        return (width, height)

    def test_get_screenshot_of_area_as_image(self) -> None:
        self._call_tool_screenshot("get_screenshot_of_area_as_image", {
            "area_ui_type": "VIEW_3D",
        })

    def test_get_screenshot_of_area_as_image_error(self) -> None:
        self._call_tool_expect_error("get_screenshot_of_area_as_image", {
            "area_ui_type": "NONEXISTENT",
        })

    def test_get_screenshot_of_window_as_image(self) -> None:
        self._call_tool_screenshot("get_screenshot_of_window_as_image")

    def test_get_screenshot_of_window_as_image_size_limit(self) -> None:
        size_limit = 16 * 1024  # 16 KB.
        content = self._call_tool_screenshot("get_screenshot_of_window_as_image", {
            "size_limit_in_bytes": size_limit,
        })
        if self._interactive:
            image_data = content[0].get("data", "")
            raw_bytes = base64.b64decode(image_data)
            self.assertLessEqual(
                len(raw_bytes), size_limit,
                "Screenshot exceeds {:d} byte limit ({:d} bytes)".format(size_limit, len(raw_bytes)),
            )

    def test_get_screenshot_of_window_as_json(self) -> None:
        data = self._test_tool("get_screenshot_of_window_as_json")
        if not self._interactive:
            self.assertEqual(data["status"], "error")
        else:
            self.assertEqual(data["scene"], "Scene")
            self.assertIsInstance(data["areas"], list)
            self.assertTrue(len(data["areas"]) > 0)
            self.assertIsNotNone(data["active_object"])
            self.assertIn("name", data["active_object"])

    # -----------------------------------------------------------------
    # CLI tools.

    def test_execute_blender_code_for_cli(self) -> None:
        data = self._test_tool("execute_blender_code_for_cli", {
            "blend_file": self._blend_path,
            "code": "result = {'version': 1}",
        })
        self.assertEqual(data["version"], 1)

    def test_execute_blender_code_for_cli_blmcp_subd(self) -> None:
        data = self._test_tool("execute_blender_code_for_cli", {
            "blend_file": self._blend_path,
            "code": "import blmcp_subd\nresult = {'ok': callable(blmcp_subd.extrude)}",
        })
        self.assertTrue(data["ok"])

    def test_get_blendfile_summary_datablocks_for_cli(self) -> None:
        data = self._test_tool("get_blendfile_summary_datablocks_for_cli", {
            "blend_file": self._blend_path,
        })
        self.assertEqual(data["scene_name"], "Scene")
        self.assertIsInstance(data["datablock_counts"], dict)

    def test_get_blendfile_summary_missing_files_for_cli(self) -> None:
        data = self._test_tool("get_blendfile_summary_missing_files_for_cli", {
            "blend_file": self._blend_path,
        })
        self.assertEqual(data["missing_files"], [])

    def test_get_blendfile_summary_of_linked_libraries_for_cli(self) -> None:
        data = self._test_tool("get_blendfile_summary_of_linked_libraries_for_cli", {
            "blend_file": self._blend_path,
        })
        self.assertEqual(data["total_library_count"], 0)

    def test_get_blendfile_summary_path_info_for_cli(self) -> None:
        data = self._test_tool("get_blendfile_summary_path_info_for_cli", {
            "blend_file": self._blend_path,
        })
        self.assertTrue(data["is_saved"])
        self.assertTrue(data["filepath"].endswith(".blend"))

    def test_get_blendfile_summary_usage_guess_for_cli(self) -> None:
        data = self._test_tool("get_blendfile_summary_usage_guess_for_cli", {
            "blend_file": self._blend_path,
        })
        guesses = data["usage_guesses"]
        self.assertIn("Animation", guesses)
        self.assertIn("Modeling", guesses)

    # -----------------------------------------------------------------
    # Object inspection tools.

    def test_get_object_detail_summary(self) -> None:
        data = self._test_tool("get_object_detail_summary", {"name": "Cube"})
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["name"], "Cube")
        self.assertEqual(data["type"], "MESH")
        self.assertEqual(data["data_name"], "Cube")
        self.assertEqual(data["location"], [0.0, 0.0, 0.0])
        self.assertEqual(data["rotation"], [0.0, 0.0, 0.0])
        self.assertEqual(data["rotation_mode"], "XYZ")
        self.assertIsNone(data["rotation_quaternion"])
        self.assertIsNone(data["rotation_axis_angle"])
        self.assertEqual(data["scale"], [1.0, 1.0, 1.0])
        self.assertEqual(data["dimensions"], [2.0, 2.0, 2.0])
        self.assertIsNone(data["parent"])
        self.assertEqual(data["children"], [])
        self.assertEqual(data["modifiers"], [])
        self.assertEqual(data["constraints"], [])
        self.assertEqual(data["materials"], ["Material"])
        self.assertEqual(data["visibility"], {
            "hide_viewport": False,
            "hide_render": False,
            "hide_get": False,
        })
        self.assertIn("Collection", data["collections"])

    def test_get_object_detail_summary_rotation_quaternion(self) -> None:
        def code() -> None:
            import math
            import bpy  # type: ignore[import-not-found]
            ob = bpy.data.objects["Cube"]
            # Leave a stale Euler value to ensure it is not reported.
            ob.rotation_euler = (1.0, 2.0, 3.0)
            ob.rotation_mode = 'QUATERNION'
            half = math.radians(45.0) / 2.0
            ob.rotation_quaternion = (math.cos(half), math.sin(half), 0.0, 0.0)
            # Negative scale and delta rotation must not affect the result.
            ob.scale = (-2.0, 3.0, 4.0)
            ob.delta_rotation_euler = (0.0, 0.0, math.radians(90.0))
            ob.delta_rotation_quaternion = (math.cos(math.radians(45.0)), 0.0, 0.0, math.sin(math.radians(45.0)))
            result = {'ok': True}  # noqa: F841
        self._test_tool("execute_blender_code", {
            "code": _python_fn_body_as_string(code),
        })
        data = self._test_tool("get_object_detail_summary", {"name": "Cube"})
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["rotation_mode"], "QUATERNION")
        self.assertIsNone(data["rotation_axis_angle"])
        rotation = data["rotation"]
        quaternion = data["rotation_quaternion"]
        assert isinstance(rotation, list) and isinstance(quaternion, list)
        for value, expected in zip(rotation, (math.radians(45.0), 0.0, 0.0)):
            self.assertAlmostEqual(value, expected, places=5)
        self.assertAlmostEqual(quaternion[0], math.cos(math.radians(45.0) / 2.0), places=5)
        self.assertAlmostEqual(quaternion[1], math.sin(math.radians(45.0) / 2.0), places=5)

    def test_get_object_detail_summary_rotation_axis_angle(self) -> None:
        def code() -> None:
            import math
            import bpy  # type: ignore[import-not-found]
            ob = bpy.data.objects["Cube"]
            ob.rotation_euler = (1.0, 2.0, 3.0)
            ob.rotation_mode = 'AXIS_ANGLE'
            ob.rotation_axis_angle = (math.radians(30.0), 0.0, 0.0, 1.0)
            # Negative scale and delta rotation must not affect the result.
            ob.scale = (-1.0, 1.0, 1.0)
            ob.delta_rotation_euler = (0.0, 0.0, math.radians(90.0))
            ob.delta_rotation_quaternion = (math.cos(math.radians(45.0)), 0.0, 0.0, math.sin(math.radians(45.0)))
            result = {'ok': True}  # noqa: F841
        self._test_tool("execute_blender_code", {
            "code": _python_fn_body_as_string(code),
        })
        data = self._test_tool("get_object_detail_summary", {"name": "Cube"})
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["rotation_mode"], "AXIS_ANGLE")
        self.assertIsNone(data["rotation_quaternion"])
        rotation = data["rotation"]
        axis_angle = data["rotation_axis_angle"]
        assert isinstance(rotation, list) and isinstance(axis_angle, list)
        for value, expected in zip(rotation, (0.0, 0.0, math.radians(30.0))):
            self.assertAlmostEqual(value, expected, places=5)
        self.assertAlmostEqual(axis_angle[0], math.radians(30.0), places=5)

    def test_get_object_detail_summary_error(self) -> None:
        data = self._test_tool("get_object_detail_summary", {"name": "NonExistent"})
        self.assertEqual(data["status"], "error")
        self.assertIn("'NonExistent' not found", data["message"])
        self.assertIn("Cube", data["message"])

    def test_get_objects_summary(self) -> None:
        data = self._test_tool("get_objects_summary")
        self.assertEqual(data, {
            "status": "ok",
            "scene_name": "Scene",
            "active_workspace": "Layout",
            "active_object": "Cube",
            "object_mode": "OBJECT",
            "camera_object": "Camera",
            "collections": [
                {
                    "name": "Scene Collection",
                    "exclude": False,
                    "hide_viewport": False,
                    "objects": [],
                    "children": [
                        {
                            "name": "Collection",
                            "exclude": False,
                            "hide_viewport": False,
                            "objects": [
                                {
                                    "name": "Camera",
                                    "type": "CAMERA",
                                    "parent": None,
                                    "data_name": "Camera",
                                    "selected": False,
                                    "visible": True,
                                    "hide_viewport": False,
                                },
                                {
                                    "name": "Cube",
                                    "type": "MESH",
                                    "parent": None,
                                    "data_name": "Cube",
                                    "selected": True,
                                    "visible": True,
                                    "hide_viewport": False,
                                },
                                {
                                    "name": "Light",
                                    "type": "LIGHT",
                                    "parent": None,
                                    "data_name": "Light",
                                    "selected": False,
                                    "visible": True,
                                    "hide_viewport": False,
                                },
                            ],
                            "children": [],
                        },
                    ],
                },
            ],
        })

    def test_get_object_geometry_summary(self) -> None:
        def code() -> None:
            import bpy  # type: ignore[import-not-found]
            cube = bpy.data.objects["Cube"]
            cube.location = (0.0, 0.0, 1.0)
            # Simple subdivision keeps the box shape, the array doubles it along X.
            sub = cube.modifiers.new("Subdivision", 'SUBSURF')
            sub.subdivision_type = 'SIMPLE'
            sub.levels = 1
            arr = cube.modifiers.new("Array", 'ARRAY')
            arr.count = 2
            arr.show_render = False
            result = {'ok': True}  # noqa: F841
        self._test_tool("execute_blender_code", {
            "code": _python_fn_body_as_string(code),
        })
        data = self._test_tool("get_object_geometry_summary", {"names": ["Cube", "Light", "NonExistent"]})
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["not_found"], ["NonExistent"])

        objects = data["objects"]
        assert isinstance(objects, list)
        self.assertEqual([o["name"] for o in objects], ["Cube", "Light"])
        cube, light = objects

        self.assertEqual(cube["type"], "MESH")
        self.assertTrue(cube["is_evaluated"])
        self.assertEqual(cube["instance_count"], 0)
        bounds = cube["bounds_world"]
        for key, expected in (("min", (-1.0, -1.0, 0.0)), ("max", (3.0, 1.0, 2.0)), ("size", (4.0, 2.0, 2.0))):
            for value, expected_value in zip(bounds[key], expected):
                self.assertAlmostEqual(value, expected_value, places=5)
        self.assertEqual(cube["counts_original"], {"vertices": 8, "edges": 12, "faces": 6, "triangles": 12})
        self.assertEqual(cube["counts_evaluated"], {"vertices": 52, "edges": 96, "faces": 48, "triangles": 96})

        modifiers = cube["modifiers"]
        self.assertEqual([m["name"] for m in modifiers], ["Subdivision", "Array"])
        subdivision, array = modifiers
        self.assertEqual(subdivision["type"], "SUBSURF")
        self.assertEqual(subdivision["settings"]["levels"], 1)
        self.assertEqual(subdivision["settings"]["subdivision_type"], "SIMPLE")
        self.assertFalse(array["show_render"])
        self.assertEqual(array["settings"]["count"], 2)
        self.assertIsNone(array["settings"]["offset_object"])
        # Common and UI properties are not repeated under `settings`.
        self.assertNotIn("name", array["settings"])
        self.assertNotIn("show_expanded", array["settings"])

        self.assertEqual(light["type"], "LIGHT")
        self.assertIsNone(light["bounds_world"])
        self.assertIsNone(light["counts_original"])
        self.assertIsNone(light["counts_evaluated"])
        self.assertEqual(light["modifiers"], [])

        # The temporary mesh is transformed to world space, the object must be left untouched.
        self.assertEqual(
            self._test_tool("get_object_geometry_summary", {"names": ["Cube", "Light", "NonExistent"]}),
            data,
        )
        data = self._test_tool("get_object_detail_summary", {"name": "Cube"})
        self.assertEqual(data["location"], [0.0, 0.0, 1.0])

    def test_get_object_geometry_summary_instances(self) -> None:
        def code() -> None:
            import bpy  # type: ignore[import-not-found]
            scene = bpy.context.scene
            cube = bpy.data.objects["Cube"]

            # Instance the cube on a 3x3 grid (4 units wide) with Geometry Nodes.
            scatter = bpy.data.objects.new("Scatter", bpy.data.meshes.new("Scatter"))
            scatter.location = (10.0, 0.0, 0.0)
            scene.collection.objects.link(scatter)
            group = bpy.data.node_groups.new("Scatter", 'GeometryNodeTree')
            group.interface.new_socket("Geometry", in_out='INPUT', socket_type='NodeSocketGeometry')
            group.interface.new_socket("Geometry", in_out='OUTPUT', socket_type='NodeSocketGeometry')
            grid = group.nodes.new("GeometryNodeMeshGrid")
            grid.inputs["Vertices X"].default_value = 3
            grid.inputs["Vertices Y"].default_value = 3
            grid.inputs["Size X"].default_value = 4.0
            grid.inputs["Size Y"].default_value = 4.0
            object_info = group.nodes.new("GeometryNodeObjectInfo")
            object_info.inputs["Object"].default_value = cube
            instance_on_points = group.nodes.new("GeometryNodeInstanceOnPoints")
            node_out = group.nodes.new("NodeGroupOutput")
            group.links.new(grid.outputs["Mesh"], instance_on_points.inputs["Points"])
            group.links.new(object_info.outputs["Geometry"], instance_on_points.inputs["Instance"])
            group.links.new(instance_on_points.outputs["Instances"], node_out.inputs[0])
            scatter.modifiers.new("GeometryNodes", 'NODES').node_group = group

            # Instance a collection holding the cube with an empty.
            # The light and the empty in it have no geometry and must be ignored.
            collection = bpy.data.collections.new("Instanced")
            collection.objects.link(cube)
            collection.objects.link(bpy.data.objects["Light"])
            nested_empty = bpy.data.objects.new("Nested Empty", None)
            nested_empty.location = (-5.0, -5.0, -5.0)
            collection.objects.link(nested_empty)
            empty = bpy.data.objects.new("Collection Instance", None)
            empty.instance_type = 'COLLECTION'
            empty.instance_collection = collection
            empty.location = (0.0, 20.0, 0.0)
            scene.collection.objects.link(empty)
            result = {'ok': True}  # noqa: F841
        self._test_tool("execute_blender_code", {
            "code": _python_fn_body_as_string(code),
        })
        data = self._test_tool("get_object_geometry_summary", {"names": ["Scatter", "Collection Instance"]})
        scatter, empty = data["objects"]

        self.assertEqual(scatter["instance_count"], 9)
        # The generator has no mesh of its own.
        self.assertEqual(scatter["counts_evaluated"]["vertices"], 0)
        for key, expected in (("min", (7.0, -3.0, -1.0)), ("max", (13.0, 3.0, 1.0))):
            for value, expected_value in zip(scatter["bounds_world"][key], expected):
                self.assertAlmostEqual(value, expected_value, places=5)

        self.assertEqual(empty["instance_count"], 1)
        self.assertIsNone(empty["counts_evaluated"])
        for key, expected in (("min", (-1.0, 19.0, -1.0)), ("max", (1.0, 21.0, 1.0))):
            for value, expected_value in zip(empty["bounds_world"][key], expected):
                self.assertAlmostEqual(value, expected_value, places=5)

    def test_get_object_geometry_summary_not_evaluated(self) -> None:
        def code() -> None:
            import bpy  # type: ignore[import-not-found]
            scene = bpy.context.scene
            mesh = bpy.data.meshes["Cube"]
            unlinked = bpy.data.objects.new("Unlinked", mesh)
            unlinked.location = (5.0, 5.0, 5.0)
            excluded = bpy.data.collections.new("Excluded")
            scene.collection.children.link(excluded)
            excluded.objects.link(bpy.data.objects.new("In Excluded", mesh))
            bpy.context.view_layer.layer_collection.children["Excluded"].exclude = True
            result = {'ok': True}  # noqa: F841
        self._test_tool("execute_blender_code", {
            "code": _python_fn_body_as_string(code),
        })
        data = self._test_tool("get_object_geometry_summary", {"names": ["Unlinked", "In Excluded"]})
        for obj in data["objects"]:
            self.assertFalse(obj["is_evaluated"], obj["name"])
            self.assertIsNone(obj["bounds_world"], obj["name"])
            self.assertIsNone(obj["counts_evaluated"], obj["name"])
            self.assertIsNone(obj["instance_count"], obj["name"])
            # The mesh itself is still available.
            self.assertEqual(obj["counts_original"]["vertices"], 8, obj["name"])

    def test_get_object_geometry_summary_nodes_inputs(self) -> None:
        def code() -> None:
            import bpy  # type: ignore[import-not-found]
            group = bpy.data.node_groups.new("Geometry Group", 'GeometryNodeTree')
            group.interface.new_socket("Geometry", in_out='INPUT', socket_type='NodeSocketGeometry')
            group.interface.new_socket("Size", in_out='INPUT', socket_type='NodeSocketFloat')
            group.interface.new_socket("Target", in_out='INPUT', socket_type='NodeSocketObject')
            group.interface.new_socket("Geometry", in_out='OUTPUT', socket_type='NodeSocketGeometry')
            node_in = group.nodes.new("NodeGroupInput")
            node_out = group.nodes.new("NodeGroupOutput")
            group.links.new(node_in.outputs[0], node_out.inputs[0])
            mod = bpy.data.objects["Cube"].modifiers.new("GeometryNodes", 'NODES')
            mod.node_group = group
            # Blender 5.2+ exposes the inputs as RNA, older versions as ID properties.
            properties = getattr(mod, "properties", None)
            for item in group.interface.items_tree:
                if item.item_type != 'SOCKET' or item.in_out != 'INPUT':
                    continue
                value = {"Size": 2.5, "Target": bpy.data.objects["Camera"]}.get(item.name)
                if value is None:
                    continue
                if properties is not None:
                    getattr(properties.inputs, item.identifier).value = value
                else:
                    mod[item.identifier] = value
            result = {'ok': True}  # noqa: F841
        self._test_tool("execute_blender_code", {
            "code": _python_fn_body_as_string(code),
        })
        data = self._test_tool("get_object_geometry_summary", {"names": ["Cube"]})
        modifier = data["objects"][0]["modifiers"][0]
        self.assertEqual(modifier["type"], "NODES")
        self.assertEqual(modifier["settings"]["node_group"], "Geometry Group")
        inputs = {i["name"]: i for i in modifier["inputs"]}
        # The geometry input is not listed.
        self.assertEqual(sorted(inputs), ["Size", "Target"])
        self.assertAlmostEqual(inputs["Size"]["value"], 2.5)
        self.assertEqual(inputs["Size"]["socket_type"], "NodeSocketFloat")
        self.assertEqual(inputs["Target"]["value"], "Camera")

    def test_get_node_tree_summary_material(self) -> None:
        def code() -> None:
            import bpy  # type: ignore[import-not-found]
            tree = bpy.data.materials["Material"].node_tree
            bsdf = tree.nodes["Principled BSDF"]

            # Noise -> Reroute -> Color Ramp -> Base Color.
            noise = tree.nodes.new("ShaderNodeTexNoise")
            reroute = tree.nodes.new("NodeReroute")
            ramp = tree.nodes.new("ShaderNodeValToRGB")
            ramp.color_ramp.elements[0].position = 0.25
            tree.links.new(noise.outputs["Fac"], reroute.inputs[0])
            tree.links.new(reroute.outputs[0], ramp.inputs["Fac"])
            tree.links.new(ramp.outputs["Color"], bsdf.inputs["Base Color"])

            # Not connected to any output.
            tree.nodes.new("ShaderNodeMix").name = "Unused Mix"

            # A group node behind a muted link is not used.
            group = bpy.data.node_groups.new("Shader Group", 'ShaderNodeTree')
            group.interface.new_socket("Value", in_out='OUTPUT', socket_type='NodeSocketFloat')
            group.nodes.new("NodeGroupOutput")
            group_node = tree.nodes.new("ShaderNodeGroup")
            group_node.node_tree = group
            tree.links.new(group_node.outputs[0], bsdf.inputs["Roughness"]).is_muted = True
            bsdf.inputs["Roughness"].default_value = 0.25
            result = {'ok': True}  # noqa: F841
        self._test_tool("execute_blender_code", {
            "code": _python_fn_body_as_string(code),
        })
        data = self._test_tool("get_node_tree_summary", {"kind": "material", "name": "Material"})
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["tree_type"], "ShaderNodeTree")
        self.assertEqual(data["output_node"], "Material Output")
        self.assertIsNone(data["interface"])
        self.assertEqual(data["groups_used"], ["Shader Group"])

        nodes = {node["name"]: node for node in data["nodes"]}
        # Reroute nodes are skipped.
        self.assertEqual(
            sorted(nodes),
            ["Color Ramp", "Group", "Material Output", "Noise Texture", "Principled BSDF", "Unused Mix"],
        )
        self.assertEqual(
            {name: node["used"] for name, node in nodes.items()},
            {
                "Color Ramp": True,
                "Group": False,
                "Material Output": True,
                "Noise Texture": True,
                "Principled BSDF": True,
                "Unused Mix": False,
            },
        )

        bsdf_inputs = {i["identifier"]: i for i in nodes["Principled BSDF"]["inputs"]}
        self.assertEqual(bsdf_inputs["Base Color"]["linked_from"], ["Color Ramp.Color"])
        self.assertNotIn("value", bsdf_inputs["Base Color"])
        # The link is muted, the input uses its own value.
        self.assertNotIn("linked_from", bsdf_inputs["Roughness"])
        self.assertAlmostEqual(bsdf_inputs["Roughness"]["value"], 0.25)
        self.assertEqual(nodes["Principled BSDF"]["settings"]["distribution"], "MULTI_GGX")

        ramp_inputs = {i["identifier"]: i for i in nodes["Color Ramp"]["inputs"]}
        self.assertEqual(ramp_inputs["Fac"]["linked_from"], ["Noise Texture.Fac"])
        elements = nodes["Color Ramp"]["settings"]["color_ramp"]["elements"]
        self.assertAlmostEqual(elements[0]["position"], 0.25)
        self.assertEqual(elements[1]["color"], [1.0, 1.0, 1.0, 1.0])

        # Only the inputs of the current data type are listed.
        self.assertEqual(
            [i["identifier"] for i in nodes["Unused Mix"]["inputs"]],
            ["Factor_Float", "A_Float", "B_Float"],
        )
        self.assertEqual(nodes["Group"]["settings"]["node_tree"], "Shader Group")

        # An output targeting the render engine takes precedence.
        def code_cycles_output() -> None:
            import bpy  # type: ignore[import-not-found]
            bpy.context.scene.render.engine = 'CYCLES'
            tree = bpy.data.materials["Material"].node_tree
            output = tree.nodes.new("ShaderNodeOutputMaterial")
            output.name = "Cycles Output"
            output.target = 'CYCLES'
            tree.links.new(tree.nodes["Unused Mix"].outputs[0], output.inputs["Surface"])
            result = {'ok': True}  # noqa: F841
        self._test_tool("execute_blender_code", {
            "code": _python_fn_body_as_string(code_cycles_output),
        })
        data = self._test_tool("get_node_tree_summary", {"kind": "material", "name": "Material"})
        self.assertEqual(data["output_node"], "Cycles Output")
        nodes = {node["name"]: node for node in data["nodes"]}
        self.assertTrue(nodes["Unused Mix"]["used"])
        self.assertFalse(nodes["Principled BSDF"]["used"])

    def test_get_node_tree_summary_node_group(self) -> None:
        def code() -> None:
            import bpy  # type: ignore[import-not-found]
            group = bpy.data.node_groups.new("Geometry Group", 'GeometryNodeTree')
            group.interface.new_socket("Geometry", in_out='INPUT', socket_type='NodeSocketGeometry')
            size = group.interface.new_socket("Size", in_out='INPUT', socket_type='NodeSocketFloat')
            size.default_value = 2.0
            group.interface.new_socket("Geometry", in_out='OUTPUT', socket_type='NodeSocketGeometry')
            node_in = group.nodes.new("NodeGroupInput")
            node_out = group.nodes.new("NodeGroupOutput")
            transform = group.nodes.new("GeometryNodeTransform")
            group.links.new(node_in.outputs["Geometry"], transform.inputs["Geometry"])
            group.links.new(transform.outputs["Geometry"], node_out.inputs["Geometry"])
            group.nodes.new("NodeFrame")
            result = {'ok': True}  # noqa: F841
        self._test_tool("execute_blender_code", {
            "code": _python_fn_body_as_string(code),
        })
        data = self._test_tool("get_node_tree_summary", {"kind": "node_group", "name": "Geometry Group"})
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["tree_type"], "GeometryNodeTree")
        self.assertEqual(data["output_node"], "Group Output")
        self.assertEqual(data["groups_used"], [])

        interface = {(i["in_out"], i["name"]): i for i in data["interface"]}
        self.assertEqual(
            sorted(interface),
            [("INPUT", "Geometry"), ("INPUT", "Size"), ("OUTPUT", "Geometry")],
        )
        self.assertAlmostEqual(interface[("INPUT", "Size")]["default_value"], 2.0)
        self.assertEqual(interface[("INPUT", "Size")]["socket_type"], "NodeSocketFloat")

        nodes = {node["name"]: node for node in data["nodes"]}
        # Frame nodes are skipped.
        self.assertEqual(sorted(nodes), ["Group Input", "Group Output", "Transform Geometry"])
        self.assertTrue(all(node["used"] for node in nodes.values()))
        # The empty slot of the group output is not listed.
        output_inputs = nodes["Group Output"]["inputs"]
        self.assertEqual([i["name"] for i in output_inputs], ["Geometry"])
        self.assertEqual(output_inputs[0]["linked_from"], ["Transform Geometry.Geometry"])

    def test_get_node_tree_summary_used_disabled_and_muted(self) -> None:
        def code() -> None:
            import bpy  # type: ignore[import-not-found]

            # Mix node socket names are not unique, look them up by identifier.
            def socket(sockets, identifier):  # type: ignore[no-untyped-def]
                return next(s for s in sockets if s.identifier == identifier)

            group = bpy.data.node_groups.new("Geometry Group", 'GeometryNodeTree')
            group.interface.new_socket("Geometry", in_out='INPUT', socket_type='NodeSocketGeometry')
            group.interface.new_socket("Geometry", in_out='OUTPUT', socket_type='NodeSocketGeometry')
            node_in = group.nodes.new("NodeGroupInput")
            node_out = group.nodes.new("NodeGroupOutput")

            # Group Input -> Muted (Set Position) -> Active (Set Position) -> Group Output.
            muted = group.nodes.new("GeometryNodeSetPosition")
            muted.name = "Muted"
            muted.mute = True
            active = group.nodes.new("GeometryNodeSetPosition")
            active.name = "Active"
            group.links.new(node_in.outputs["Geometry"], muted.inputs["Geometry"])
            group.links.new(muted.outputs["Geometry"], active.inputs["Geometry"])
            group.links.new(active.outputs["Geometry"], node_out.inputs["Geometry"])

            # A muted node only passes its geometry, its offset is not evaluated.
            muted_offset = group.nodes.new("FunctionNodeInputVector")
            muted_offset.name = "Muted Offset"
            group.links.new(muted_offset.outputs[0], muted.inputs["Offset"])

            # A float Mix node feeds the active offset, the link to its disabled
            # vector input is kept but not evaluated.
            mix = group.nodes.new("ShaderNodeMix")
            mix.data_type = 'VECTOR'
            disabled_source = group.nodes.new("FunctionNodeInputVector")
            disabled_source.name = "Disabled Source"
            group.links.new(disabled_source.outputs[0], socket(mix.inputs, "A_Vector"))
            mix.data_type = 'FLOAT'
            enabled_source = group.nodes.new("ShaderNodeValue")
            enabled_source.name = "Enabled Source"
            group.links.new(enabled_source.outputs[0], socket(mix.inputs, "A_Float"))
            group.links.new(socket(mix.outputs, "Result_Float"), active.inputs["Offset"])
            result = {'ok': True}  # noqa: F841
        data = self._test_tool("execute_blender_code", {
            "code": _python_fn_body_as_string(code),
        })
        self.assertEqual(data, {"ok": True})
        data = self._test_tool("get_node_tree_summary", {"kind": "node_group", "name": "Geometry Group"})
        nodes = {node["name"]: node for node in data["nodes"]}
        self.assertTrue(nodes["Muted"]["mute"])
        self.assertEqual(
            {name: node["used"] for name, node in nodes.items()},
            {
                "Group Input": True,
                "Group Output": True,
                "Muted": True,
                "Muted Offset": False,
                "Active": True,
                "Mix": True,
                "Enabled Source": True,
                "Disabled Source": False,
            },
        )
        # The disabled input is not listed, consistent with `used`.
        self.assertNotIn("A_Vector", [i["identifier"] for i in nodes["Mix"]["inputs"]])

    def test_get_node_tree_summary_world_and_light(self) -> None:
        data = self._test_tool("get_node_tree_summary", {"kind": "world", "name": "World"})
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["output_node"], "World Output")
        nodes = {node["name"]: node for node in data["nodes"]}
        background = {i["identifier"]: i for i in nodes["Background"]["inputs"]}
        self.assertAlmostEqual(background["Strength"]["value"], 1.0)

        data = self._test_tool("get_node_tree_summary", {"kind": "light", "name": "Light"})
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["tree_type"], "ShaderNodeTree")

    def test_get_node_tree_summary_error(self) -> None:
        data = self._test_tool("get_node_tree_summary", {"kind": "material", "name": "NonExistent"})
        self.assertEqual(data["status"], "error")
        self.assertIn("'NonExistent' not found", data["message"])
        self.assertIn("Material", data["message"])

    def test_get_mesh_topology_summary_mirror(self) -> None:
        def code() -> None:
            import bmesh  # type: ignore[import-not-found]
            import bpy  # type: ignore[import-not-found]
            # The positive X half of a cube, open on the mirror plane.
            mesh = bpy.data.meshes.new("Half")
            bm = bmesh.new()
            bmesh.ops.create_cube(bm, size=2.0)
            bmesh.ops.bisect_plane(
                bm, geom=bm.verts[:] + bm.edges[:] + bm.faces[:],
                plane_co=(0.0, 0.0, 0.0), plane_no=(1.0, 0.0, 0.0), clear_inner=True,
            )
            # The bisect fills nothing, so the cut is an open boundary on X=0.
            # Move one vertex on the plane slightly off it: further than half the
            # merge distance (0.001), so the Mirror modifier doesn't weld it.
            moved = next(v for v in bm.verts if abs(v.co.x) < 1e-6 and v.co.y > 0.0 and v.co.z > 0.0)
            moved.co.x = 0.0007
            bm.to_mesh(mesh)
            bm.free()
            obj = bpy.data.objects.new("Half", mesh)
            bpy.context.scene.collection.objects.link(obj)
            mirror = obj.modifiers.new("Mirror", 'MIRROR')
            mirror.use_clip = True
            obj.modifiers.new("Subdivision", 'SUBSURF').levels = 2
            result = {'ok': True}  # noqa: F841
        self._test_tool("execute_blender_code", {
            "code": _python_fn_body_as_string(code),
        })
        data = self._test_tool("get_mesh_topology_summary", {"name": "Half"})
        self.assertEqual(data["status"], "ok")
        self.assertFalse(data["is_edit_mode"])
        self.assertEqual(data["counts"], {"vertices": 8, "edges": 12, "faces": 5})
        self.assertEqual(data["faces"]["quads"], 5)
        self.assertEqual(data["faces"]["triangles"]["count"], 0)
        self.assertEqual(data["islands"]["count"], 1)

        # Only the 4 outer corners are poles, those on the mirror plane get 4 edges once mirrored.
        poles = data["poles"]
        self.assertEqual(poles["valence_3"]["count"], 4)
        for item in poles["valence_3"]["items"]:
            self.assertAlmostEqual(item["co"][0], 1.0, places=5)
        self.assertEqual(poles["valence_5_plus"]["count"], 0)

        (symmetry,) = data["symmetry"]
        self.assertEqual(symmetry["axis"], "X")
        self.assertEqual(symmetry["modifier"], "Mirror")
        self.assertAlmostEqual(symmetry["tolerance"], 0.0005, places=6)
        self.assertTrue(symmetry["is_joined"])
        self.assertTrue(symmetry["use_clip"])
        self.assertFalse(symmetry["is_separate_copy"])
        # 3 vertices on the plane, 4 at X=1 and the moved one: all the vertices.
        self.assertEqual(symmetry["vertices_on_plane"], 3)
        self.assertEqual(symmetry["vertices_positive_side"], 5)
        self.assertEqual(symmetry["vertices_negative_side"], 0)
        off_center = symmetry["off_center_vertices"]
        self.assertEqual(off_center["count"], 1)
        self.assertAlmostEqual(off_center["items"][0]["co"][0], 0.0007, places=6)
        self.assertEqual(symmetry["crossing_faces"]["count"], 0)
        # Edges on the mirror plane are not holes, the 2 using the moved vertex are.
        self.assertEqual(data["boundary_edges"]["count"], 2)

        self.assertEqual([m["name"] for m in data["modifiers"]], ["Mirror", "Subdivision"])
        self.assertEqual(data["modifiers"][1]["settings"]["levels"], 2)
        self.assertNotIn("open_advanced_panel", data["modifiers"][1]["settings"])
        self.assertEqual(data["warnings"], [])

        # Subdivision shrinks the mirrored cage (2 x 2 x 2).
        size = data["subdivision_size"]
        for cage, ratio in zip(size["cage"], size["ratio"]):
            self.assertAlmostEqual(cage, 2.0, places=2)
            self.assertLess(ratio, 1.0)

    def test_get_mesh_topology_summary_problems(self) -> None:
        def code() -> None:
            import bmesh  # type: ignore[import-not-found]
            import bpy  # type: ignore[import-not-found]
            from mathutils import Vector  # type: ignore[import-not-found]
            mesh = bpy.data.meshes.new("Problems")
            bm = bmesh.new()
            # A 3 x 3 grid of quads, face centers at -2/3, 0 and 2/3 on X and Y.
            bmesh.ops.create_grid(bm, x_segments=3, y_segments=3, size=1.0)

            def face_at(x: float, y: float) -> object:
                return min(bm.faces, key=lambda f: (f.calc_center_median() - Vector((x, y, 0.0))).length)

            third = 2.0 / 3.0
            face_at(-third, third).normal_flip()
            bmesh.ops.triangulate(bm, faces=[face_at(third, third)])
            # Merge the two bottom-left quads into a hexagon.
            quad_a = face_at(-third, -third)
            quad_b = face_at(0.0, -third)
            shared = next(e for e in quad_a.edges if quad_b in e.link_faces)
            bmesh.ops.dissolve_edges(bm, edges=[shared])
            # A separate wire edge and a loose vertex.
            v1 = bm.verts.new((3.0, 0.0, 0.0))
            v2 = bm.verts.new((3.0, 1.0, 0.0))
            bm.edges.new((v1, v2))
            bm.verts.new((4.0, 0.0, 0.0))
            bm.to_mesh(mesh)
            bm.free()
            mesh.edge_creases_ensure().data[0].value = 1.0
            sharp = mesh.attributes.new("sharp_edge", 'BOOLEAN', 'EDGE')
            sharp.data[0].value = True
            sharp.data[1].value = True
            obj = bpy.data.objects.new("Problems", mesh)
            bpy.context.scene.collection.objects.link(obj)
            # Subdivision before Mirror, without clipping.
            obj.modifiers.new("Subdivision", 'SUBSURF')
            obj.modifiers.new("Mirror", 'MIRROR').use_clip = False
            result = {'ok': True}  # noqa: F841
        self._test_tool("execute_blender_code", {
            "code": _python_fn_body_as_string(code),
        })
        data = self._test_tool("get_mesh_topology_summary", {"name": "Problems"})
        self.assertEqual(data["status"], "ok")
        faces = data["faces"]
        self.assertEqual(faces["triangles"]["count"], 2)
        self.assertEqual(faces["ngons"]["count"], 1)
        self.assertEqual(faces["ngons"]["items"][0]["sides"], 6)
        self.assertEqual(data["wire_edges"]["count"], 1)
        self.assertEqual(data["loose_vertices"]["count"], 1)
        self.assertEqual(data["loose_vertices"]["items"][0]["co"], [4.0, 0.0, 0.0])
        # Only the grid: the wire edge and the loose vertex have no faces (reported above).
        islands = data["islands"]
        self.assertEqual(islands["count"], 1)
        (grid,) = islands["items"]
        self.assertEqual((grid["vertices"], grid["faces"]), (16, 9))
        self.assertEqual(grid["first_vertex"], 0)
        self.assertEqual(grid["center"], [0.0, 0.0, 0.0])
        self.assertEqual(data["non_manifold_edges"]["count"], 0)
        # The flipped face is joined to 2 neighbors.
        self.assertEqual(data["inconsistent_normal_edges"]["count"], 2)
        self.assertGreater(data["boundary_edges"]["count"], 0)
        self.assertEqual(data["creases"], {"edges": 1, "vertices": 0})
        self.assertEqual(data["sharp_edges"], 2)

        warnings = data["warnings"]
        self.assertEqual(len(warnings), 2)
        self.assertIn("comes before Mirror", warnings[0])
        self.assertIn("clipping disabled", warnings[1])

    def test_get_mesh_topology_summary_islands(self) -> None:
        def code() -> None:
            import bmesh  # type: ignore[import-not-found]
            import bpy  # type: ignore[import-not-found]
            from mathutils import Matrix  # type: ignore[import-not-found]
            mesh = bpy.data.meshes.new("Parts")
            bm = bmesh.new()
            # A small cube first (lower vertex indices), then a larger grid, then a loose vertex.
            bmesh.ops.create_cube(bm, size=0.2, matrix=Matrix.Translation((0.5, 0.0, 0.0)))
            bmesh.ops.create_grid(bm, x_segments=3, y_segments=3, size=1.0)
            bm.verts.new((3.0, 0.0, 0.0))
            bm.to_mesh(mesh)
            bm.free()
            obj = bpy.data.objects.new("Parts", mesh)
            bpy.context.scene.collection.objects.link(obj)
            # The cube doesn't touch the mirror plane, it is still one part of the cage.
            obj.modifiers.new("Mirror", 'MIRROR')
            result = {'ok': True}  # noqa: F841
        self._test_tool("execute_blender_code", {
            "code": _python_fn_body_as_string(code),
        })
        data = self._test_tool("get_mesh_topology_summary", {"name": "Parts"})
        self.assertEqual(data["status"], "ok")
        islands = data["islands"]
        # The loose vertex is not a part, the largest part comes first.
        self.assertEqual(islands["count"], 2)
        grid, cube = islands["items"]
        self.assertEqual((grid["vertices"], grid["faces"], grid["first_vertex"]), (16, 9, 8))
        self.assertEqual((cube["vertices"], cube["faces"], cube["first_vertex"]), (8, 6, 0))
        for value, expected in zip(cube["center"], (0.5, 0.0, 0.0)):
            self.assertAlmostEqual(value, expected, places=5)
        self.assertEqual(data["loose_vertices"]["count"], 1)

    def test_get_mesh_topology_summary_mirror_planes(self) -> None:
        def code() -> None:
            import bmesh  # type: ignore[import-not-found]
            import bpy  # type: ignore[import-not-found]

            def add(name: str, build: object) -> object:
                mesh = bpy.data.meshes.new(name)
                bm = bmesh.new()
                build(bm)  # type: ignore[operator]
                bm.to_mesh(mesh)
                bm.free()
                obj = bpy.data.objects.new(name, mesh)
                bpy.context.scene.collection.objects.link(obj)
                return obj

            # A quarter of a quad split by the diagonal from the corner at the origin.
            def quarter(bm: object) -> None:
                a = bm.verts.new((0.0, 0.0, 0.0))  # type: ignore[attr-defined]
                b = bm.verts.new((1.0, 0.0, 0.0))  # type: ignore[attr-defined]
                c = bm.verts.new((1.0, 1.0, 0.0))  # type: ignore[attr-defined]
                d = bm.verts.new((0.0, 1.0, 0.0))  # type: ignore[attr-defined]
                bm.faces.new((a, b, c))  # type: ignore[attr-defined]
                bm.faces.new((a, c, d))  # type: ignore[attr-defined]

            obj = add("Quarter", quarter)
            mirror = obj.modifiers.new("Mirror", 'MIRROR')  # type: ignore[attr-defined]
            mirror.use_axis = (True, True, False)
            obj.modifiers.new("Subdivision", 'SUBSURF')  # type: ignore[attr-defined]

            # The positive X half of a cube, open on X=0, mirrored without merging.
            def half(bm: object) -> None:
                bmesh.ops.create_cube(bm, size=2.0)
                bmesh.ops.bisect_plane(
                    bm, geom=bm.verts[:] + bm.edges[:] + bm.faces[:],  # type: ignore[attr-defined]
                    plane_co=(0.0, 0.0, 0.0), plane_no=(1.0, 0.0, 0.0), clear_inner=True,
                )

            obj = add("Unmerged", half)
            mirror = obj.modifiers.new("Mirror", 'MIRROR')  # type: ignore[attr-defined]
            mirror.use_mirror_merge = False
            mirror.use_clip = True
            obj.modifiers.new("Subdivision", 'SUBSURF')  # type: ignore[attr-defined]

            # One of a pair of parts: a small cube 2 mm off the plane, mirrored as a separate copy.
            def part(bm: object) -> None:
                bmesh.ops.create_cube(bm, size=0.1)
                for v in bm.verts:  # type: ignore[attr-defined]
                    v.co.x += 0.052
            obj = add("PairPart", part)
            mirror = obj.modifiers.new("Mirror", 'MIRROR')  # type: ignore[attr-defined]
            mirror.use_mirror_merge = False
            mirror.use_clip = False
            obj.modifiers.new("Subdivision", 'SUBSURF')  # type: ignore[attr-defined]
            result = {'ok': True}  # noqa: F841
        self._test_tool("execute_blender_code", {
            "code": _python_fn_body_as_string(code),
        })

        # Mirrored on X and Y, the corner gets 2 copies of each edge on a plane
        # and 4 of the diagonal: 2 + 2 + 4 edges.
        data = self._test_tool("get_mesh_topology_summary", {"name": "Quarter"})
        self.assertEqual(data["status"], "ok")
        self.assertEqual([s["axis"] for s in data["symmetry"]], ["X", "Y"])
        (pole,) = data["poles"]["valence_5_plus"]["items"]
        self.assertEqual(pole["co"], [0.0, 0.0, 0.0])
        self.assertEqual(pole["valence"], 8)
        # Only the 2 outer edges (X=1 and Y=1) are open.
        self.assertEqual(data["boundary_edges"]["count"], 2)

        # Without merging, the seam on the mirror plane stays open.
        data = self._test_tool("get_mesh_topology_summary", {"name": "Unmerged"})
        self.assertEqual(data["status"], "ok")
        self.assertFalse(data["symmetry"][0]["is_joined"])
        self.assertEqual(data["boundary_edges"]["count"], 4)
        self.assertEqual(data["poles"]["valence_3"]["count"], 4)
        self.assertEqual(data["warnings"], ["Mirror 'Mirror' has merge disabled: the mirror seam stays open"])
        self.assertIsNone(data["subdivision_size"])
        # With vertices on the plane, it's a seam left open, not a separate copy.
        self.assertFalse(data["symmetry"][0]["is_separate_copy"])

        # A separate copy: no seam, so vertices near the plane (2 mm) are not off-center,
        # and merging and clipping don't matter.
        data = self._test_tool("get_mesh_topology_summary", {"name": "PairPart"})
        self.assertEqual(data["status"], "ok")
        (symmetry,) = data["symmetry"]
        self.assertTrue(symmetry["is_separate_copy"])
        self.assertEqual(symmetry["vertices_on_plane"], 0)
        self.assertEqual(symmetry["vertices_positive_side"], 8)
        self.assertEqual(symmetry["off_center_vertices"]["count"], 0)
        self.assertEqual(data["warnings"], [])

    def test_get_mesh_topology_summary_enabled_modifiers(self) -> None:
        def code() -> None:
            import bpy  # type: ignore[import-not-found]
            obj = bpy.data.objects["Cube"]
            # A disabled Mirror without clipping is ignored.
            old = obj.modifiers.new("Old Mirror", 'MIRROR')
            old.use_clip = False
            old.show_viewport = False
            obj.modifiers.new("Subdivision", 'SUBSURF')
            # The enabled Mirror after the subdivision is not joined.
            mirror = obj.modifiers.new("Mirror", 'MIRROR')
            mirror.use_clip = True
            obj.modifiers.new("Array", 'ARRAY')
            result = {'ok': True}  # noqa: F841
        self._test_tool("execute_blender_code", {
            "code": _python_fn_body_as_string(code),
        })
        data = self._test_tool("get_mesh_topology_summary", {"name": "Cube"})
        self.assertEqual(data["status"], "ok")
        self.assertEqual([s["modifier"] for s in data["symmetry"]], ["Mirror"])
        self.assertFalse(data["symmetry"][0]["is_joined"])
        self.assertEqual(data["warnings"], [
            "Subdivision 'Subdivision' comes before Mirror 'Mirror': the mirror seam is not smoothed, "
            "move the Mirror modifier above it",
        ])
        # The Array modifier changes the size too, it is not compared.
        self.assertIsNone(data["subdivision_size"])

    def test_get_mesh_topology_summary_non_finite(self) -> None:
        def code() -> None:
            import bmesh  # type: ignore[import-not-found]
            import bpy  # type: ignore[import-not-found]
            obj = bpy.data.objects["Cube"]
            bm = bmesh.new()
            bm.from_mesh(obj.data)
            bm.verts.new((float("inf"), 0.0, 0.0))
            bm.to_mesh(obj.data)
            bm.free()
            result = {'ok': True}  # noqa: F841
        self._test_tool("execute_blender_code", {
            "code": _python_fn_body_as_string(code),
        })
        data = self._test_tool("get_mesh_topology_summary", {"name": "Cube"})
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["loose_vertices"]["items"][0]["co"], ["inf", 0.0, 0.0])

    def test_get_mesh_topology_summary_edit_mode(self) -> None:
        def code() -> None:
            import bmesh  # type: ignore[import-not-found]
            import bpy  # type: ignore[import-not-found]
            obj = bpy.data.objects["Cube"]
            bpy.context.view_layer.objects.active = obj
            bpy.ops.object.mode_set(mode='EDIT')
            # Poke one face into 4 triangles, without syncing to the mesh.
            bm = bmesh.from_edit_mesh(obj.data)
            bm.faces.ensure_lookup_table()
            bmesh.ops.poke(bm, faces=[bm.faces[0]])
            # Mark a vertex with an index of its own, without flagging the indices as dirty,
            # so only an explicit index update changes it.
            bm.verts.index_update()
            bm.verts.ensure_lookup_table()
            bm.verts[-1].index = 4321
            result = {'faces': len(obj.data.polygons)}  # noqa: F841
        data = self._test_tool("execute_blender_code", {
            "code": _python_fn_body_as_string(code),
        })
        self.assertEqual(data["faces"], 6)

        data = self._test_tool("get_mesh_topology_summary", {"name": "Cube"})
        self.assertEqual(data["status"], "ok")
        self.assertTrue(data["is_edit_mode"])
        self.assertEqual(data["counts"], {"vertices": 9, "edges": 16, "faces": 9})
        self.assertEqual(data["faces"]["triangles"]["count"], 4)
        # The corners of the poked face get a 4th edge, the other 4 corners keep 3.
        self.assertEqual(data["poles"]["valence_3"]["count"], 4)
        self.assertIsNone(data["subdivision_size"])
        self.assertEqual(data["symmetry"], [])

        # The tool reads the edit-mesh without updating its indices.
        data_index = self._test_tool("execute_blender_code", {
            "code": (
                "import bmesh, bpy\n"
                "bm = bmesh.from_edit_mesh(bpy.data.objects['Cube'].data)\n"
                "bm.verts.ensure_lookup_table()\n"
                "result = {'index': bm.verts[-1].index}\n"
            ),
        })
        self.assertEqual(data_index["index"], 4321)

    def test_get_mesh_topology_summary_error(self) -> None:
        data = self._test_tool("get_mesh_topology_summary", {"name": "Light"})
        self.assertEqual(data["status"], "error")
        self.assertIn("not a mesh", data["message"])

        data = self._test_tool("get_mesh_topology_summary", {"name": "NonExistent"})
        self.assertEqual(data["status"], "error")
        self.assertIn("'NonExistent' not found", data["message"])

    def _call_tool_mesh_wireframe(self, arguments: dict[str, object]) -> dict[str, object]:
        """
        Call ``render_mesh_wireframe_as_image``, check the image and return the info.
        """
        content = self._call_tool("render_mesh_wireframe_as_image", arguments)
        self.assertEqual(len(content), 2)
        self.assertEqual(content[0].get("type"), "image")
        self.assertEqual(content[1].get("type"), "text")
        info = json.loads(str(content[1]["text"]))
        self.assertEqual(
            self._image_size(str(content[0]["data"])),
            (info["image_width"], info["image_height"]),
        )
        return info

    def test_render_mesh_wireframe_as_image(self) -> None:
        # The default cube: FRONT (looking along +Y) sees the 4 vertices at Y=-1,
        # RIGHT (looking along -X) the 4 at X=+1, the 2 at X=-1 & Y=+1 are hidden in both.
        info = self._call_tool_mesh_wireframe({"name": "Cube"})
        self.assertEqual([v["view"] for v in info["views"]], ["FRONT", "RIGHT"])
        front, right = info["views"]
        self.assertEqual((front["right"], front["up"]), ("+X", "+Z"))
        self.assertEqual((right["right"], right["up"]), ("+Y", "+Z"))
        # Panels are side by side and fill the image.
        self.assertEqual(front["rect"][0] + front["rect"][2], right["rect"][0])
        self.assertEqual(right["rect"][0] + right["rect"][2], info["image_width"])
        self.assertFalse(info["is_edit_mode"])
        self.assertEqual(info["vertices_labeled"], 6)
        self.assertEqual(info["vertices_unlabeled"], [])
        self.assertEqual(info["vertices_hidden"], 2)
        self.assertEqual(info["vertices_outside_bounds"], 0)

    def test_render_mesh_wireframe_as_image_bounds(self) -> None:
        # Only the top of the cube, seen from above: the 4 top vertices.
        info = self._call_tool_mesh_wireframe({
            "name": "Cube",
            "views": ["TOP"],
            "bounds_min": [-1.5, -1.5, 0.5],
            "bounds_max": [1.5, 1.5, 1.5],
        })
        (top,) = info["views"]
        self.assertEqual((top["right"], top["up"]), ("+X", "+Y"))
        self.assertEqual(info["bounds_min"], [-1.5, -1.5, 0.5])
        self.assertEqual(info["bounds_max"], [1.5, 1.5, 1.5])
        self.assertEqual(info["vertices_outside_bounds"], 4)
        self.assertEqual(info["vertices_labeled"], 4)
        self.assertEqual(info["vertices_hidden"], 0)

    def test_render_mesh_wireframe_as_image_downscaled(self) -> None:
        # A small size limit downscales the image, the layout info matches the returned image.
        full = self._call_tool_mesh_wireframe({"name": "Cube", "views": ["FRONT", "RIGHT", "TOP"]})
        small = self._call_tool_mesh_wireframe({
            "name": "Cube", "views": ["FRONT", "RIGHT", "TOP"], "size_limit_in_bytes": 20000,
        })
        self.assertLess(small["image_width"], full["image_width"])
        scale = small["image_width"] / full["image_width"]
        self.assertAlmostEqual(small["units_per_pixel"], full["units_per_pixel"] / scale, delta=1e-4)
        last = small["views"][-1]["rect"]
        self.assertLessEqual(abs(last[0] + last[2] - small["image_width"]), 1)
        self.assertLessEqual(abs(last[3] - small["image_height"]), 1)

    def test_render_mesh_wireframe_as_image_width_limit(self) -> None:
        # Two wide views and a narrow one widened to the minimum panel width still fit the maximum width.
        info = self._call_tool_mesh_wireframe({
            "name": "Cube",
            "views": ["RIGHT", "LEFT", "FRONT"],
            "bounds_min": [-0.01, -1.0, -1.0],
            "bounds_max": [0.01, 1.0, 1.0],
        })
        self.assertLessEqual(info["image_width"], 2400)
        self.assertEqual(info["views"][-1]["rect"][0] + info["views"][-1]["rect"][2], info["image_width"])

    def test_render_mesh_wireframe_as_image_edit_mode_hidden(self) -> None:
        def code() -> None:
            import bmesh  # type: ignore[import-not-found]
            import bpy  # type: ignore[import-not-found]
            obj = bpy.data.objects["Cube"]
            bpy.context.view_layer.objects.active = obj
            bpy.ops.object.mode_set(mode='EDIT')
            # Hide the front vertices (Y=-1) and the elements using them.
            bm = bmesh.from_edit_mesh(obj.data)
            for v in bm.verts:
                if v.co.y < 0.0:
                    v.hide_set(True)
            bmesh.update_edit_mesh(obj.data)
            result = {'ok': True}  # noqa: F841
        self._test_tool("execute_blender_code", {
            "code": _python_fn_body_as_string(code),
        })
        info = self._call_tool_mesh_wireframe({"name": "Cube", "views": ["FRONT"]})
        self.assertEqual(info["vertices_hidden_in_edit_mode"], 4)
        # With the front face hidden, the back vertices are seen from the front.
        self.assertEqual(info["vertices_labeled"], 4)
        self.assertEqual(info["vertices_hidden"], 0)
        self.assertEqual(info["vertices_outside_bounds"], 0)

    def test_render_mesh_wireframe_as_image_edit_mode(self) -> None:
        def code() -> None:
            import bmesh  # type: ignore[import-not-found]
            import bpy  # type: ignore[import-not-found]
            obj = bpy.data.objects["Cube"]
            bpy.context.view_layer.objects.active = obj
            bpy.ops.object.mode_set(mode='EDIT')
            # Add a vertex in front of the cube, without syncing to the mesh.
            bm = bmesh.from_edit_mesh(obj.data)
            bm.verts.new((0.0, -2.0, 0.0))
            result = {'vertices': len(obj.data.vertices)}  # noqa: F841
        data = self._test_tool("execute_blender_code", {
            "code": _python_fn_body_as_string(code),
        })
        self.assertEqual(data["vertices"], 8)

        info = self._call_tool_mesh_wireframe({"name": "Cube", "views": ["FRONT"]})
        self.assertTrue(info["is_edit_mode"])
        # The 4 front vertices and the new one.
        self.assertEqual(info["vertices_labeled"], 5)
        self.assertEqual(info["vertices_hidden"], 4)

    def test_render_mesh_wireframe_as_image_error(self) -> None:
        for arguments, message in (
                ({"name": "Light"}, "not a mesh"),
                ({"name": "NonExistent"}, "'NonExistent' not found"),
                ({"name": "Cube", "bounds_min": [0.0, 0.0, 0.0]}, "Pass both bounds_min and bounds_max"),
                ({"name": "Cube", "bounds_min": [0.0, 0.0], "bounds_max": [1.0, 1.0, 1.0]}, "3 finite numbers"),
        ):
            content = self._call_tool_expect_error("render_mesh_wireframe_as_image", arguments)
            self.assertIn(message, str(content[0].get("text", "")))

    def test_blmcp_subd_helpers(self) -> None:
        """
        The ``blmcp_subd`` helpers on a half tube, open on the mirror plane.
        """
        def code() -> None:
            import bmesh  # type: ignore[import-not-found]
            import bpy  # type: ignore[import-not-found]
            import blmcp_subd as subd  # type: ignore[import-not-found]

            def half_tube(bm, zs):  # type: ignore[no-untyped-def]
                # Rings of 4 points on the right half, open on X = 0, joined by 3 quads per segment.
                pts = [(0.0, -1.0), (1.0, -1.0), (1.0, 1.0), (0.0, 1.0)]
                rings = [[bm.verts.new((x, y, z)) for x, y in pts] for z in zs]
                for a, b in zip(rings, rings[1:]):
                    for k in range(3):
                        bm.faces.new((a[k], a[k + 1], b[k + 1], b[k]))
                return rings

            def stats(bm):  # type: ignore[no-untyped-def]
                return {
                    "verts": len(bm.verts),
                    "sides": sorted({len(f.verts) for f in bm.faces}),
                    "wire_edges": sum(1 for e in bm.edges if not e.link_faces),
                    "faces_on_plane": sum(1 for f in bm.faces if all(abs(v.co.x) < 1e-6 for v in f.verts)),
                }

            mesh = bpy.data.meshes.new("Tube")
            obj = bpy.data.objects.new("Tube", mesh)
            bpy.context.collection.objects.link(obj)
            result = {}
            with subd.edit_mesh(obj) as bm:
                rings = half_tube(bm, (0.0, 1.0, 2.0))
                # A cap with an edge on the mirror plane: extruding it leaves no face on the plane.
                top = bm.faces.new(rings[2])
                ext = subd.extrude(bm, [top])
                result["extrude_sources"] = sorted(rings[2].index(src) for src in ext.source.values())
                result["source_same_place"] = all(v.co == src.co for v, src in ext.source.items())
                for v in ext.faces[0].verts:
                    v.co.z += 1.0
                result["extrude"] = stats(bm)
                result["cap_index_ok"] = bm.faces[ext.faces[0].index] == ext.faces[0]

                # A ring across the 3 quads of the first segment, open on both ends.
                edge = bm.edges.get((rings[0][1], rings[1][1]))
                result["ring"] = len(subd.edge_ring(edge))
                loops = subd.insert_loop(bm, edge, cuts=2)
                result["loops_z"] = [sorted({round(v.co.z, 3) for v in loop}) for loop in loops]
                result["loops_x"] = [[round(v.co.x, 3) for v in loop] for loop in loops]
                result["loop_index_ok"] = all(bm.verts[v.index] == v for loop in loops for v in loop)
                result["insert_loop"] = stats(bm)

                # Bridge the top ring of a separate tube to the bottom ring of the first one.
                lower = half_tube(bm, (-3.0, -2.0))
                faces = subd.bridge_loops(bm, lower[1], rings[0])
                result["bridge_faces"] = len(faces)
                bm.normal_update()
                result["bridge_contiguous"] = all(e.is_contiguous for e in bm.edges if len(e.link_faces) == 2)
                result["bridge"] = stats(bm)
            result["mesh_verts"] = len(mesh.vertices)

            # A face is not a ring, a loop needs quads to cut across.
            with subd.edit_mesh(obj) as bm:
                wire = bm.edges.new((bm.verts.new((5.0, 0.0, 0.0)), bm.verts.new((6.0, 0.0, 0.0))))
                try:
                    subd.insert_loop(bm, wire)
                except ValueError as ex:
                    result["wire_error"] = str(ex)

            obj.modifiers.new("Sub", 'SUBSURF')
            # A Mirror on Y too: kept, X is added.
            obj.modifiers.new("Mir", 'MIRROR').use_axis = (False, True, False)
            result["modifiers"] = subd.setup_mirror_subsurf(obj, levels=3)
            mirror = obj.modifiers["Mir"]
            result["mirror"] = [tuple(mirror.use_axis), mirror.use_clip, mirror.use_mirror_merge]
            result["levels"] = obj.modifiers["Sub"].levels

            # The merge distance follows the cage's size (mirrored on X), at most 1 mm.
            small = bpy.data.objects.new("SmallCage", bpy.data.meshes.new("SmallCage"))
            small.data.from_pydata([(0.0, 0.0, 0.0), (0.03, 0.0, 0.0), (0.03, 0.0, 0.17)], [], [(0, 1, 2)])
            result["merge_small"] = subd.setup_mirror_subsurf(small)["merge_threshold"]
            result["merge_given"] = subd.setup_mirror_subsurf(small, merge_threshold=0.0002)["merge_threshold"]
            # A value set on a reused Mirror is kept, Blender's default is replaced.
            result["merge_kept"] = subd.setup_mirror_subsurf(small, levels=3)["merge_threshold"]
            small.modifiers[0].merge_threshold = 0.001
            result["merge_default"] = subd.setup_mirror_subsurf(small)["merge_threshold"]
            for value in (0.0, float("nan")):
                try:
                    subd.setup_mirror_subsurf(small, merge_threshold=value)
                except ValueError as ex:
                    result.setdefault("merge_invalid", []).append(str(ex))
            result["merge_empty"] = subd.setup_mirror_subsurf(
                bpy.data.objects.new("EmptyCage", bpy.data.meshes.new("EmptyCage")))["merge_threshold"]
            # A Mirror on Y too: Y is measured with its mirrored copy (0.06 wide).
            wide = bpy.data.objects.new("WideCage", bpy.data.meshes.new("WideCage"))
            wide.data.from_pydata([(0.0, 0.0, 0.0), (0.01, 0.03, 0.0), (0.0, 0.03, 0.01)], [], [(0, 1, 2)])
            wide.modifiers.new("Mirror", 'MIRROR').use_axis = (True, True, False)
            result["merge_y"] = subd.setup_mirror_subsurf(wide)["merge_threshold"]
            # In Edit Mode, the edit-mesh's vertices are measured.
            bpy.context.collection.objects.link(small)
            bpy.context.view_layer.objects.active = small
            small.modifiers.clear()
            bpy.ops.object.mode_set(mode='EDIT')
            try:
                bm = bmesh.from_edit_mesh(small.data)
                for v in bm.verts:
                    v.co *= 0.5
                result["merge_edit"] = subd.setup_mirror_subsurf(small)["merge_threshold"]
            finally:
                bpy.ops.object.mode_set(mode='OBJECT')

        data = self._test_tool("execute_blender_code", {"code": _python_fn_body_as_string(code)})
        self.assertEqual(data["extrude_sources"], [0, 1, 2, 3])
        self.assertTrue(data["source_same_place"])
        self.assertEqual(data["extrude"], {"verts": 16, "sides": [4], "wire_edges": 0, "faces_on_plane": 0})
        self.assertTrue(data["cap_index_ok"])
        self.assertEqual(data["ring"], 4)
        # From the `edge.verts[0]` side (z = 0), following the ring (x = 0, 1, 1, 0).
        self.assertEqual(data["loops_z"], [[0.333], [0.667]])
        self.assertEqual(data["loops_x"], [[0.0, 1.0, 1.0, 0.0]] * 2)
        self.assertTrue(data["loop_index_ok"])
        self.assertEqual(data["insert_loop"], {"verts": 24, "sides": [4], "wire_edges": 0, "faces_on_plane": 0})
        self.assertEqual(data["bridge_faces"], 3)
        self.assertTrue(data["bridge_contiguous"])
        self.assertEqual(data["bridge"]["sides"], [4])
        # Written back to the mesh at the end of the block.
        self.assertEqual(data["mesh_verts"], 32)
        self.assertIn("no quad", data["wire_error"])
        # Mirror moved before subdivision.
        self.assertEqual(data["modifiers"]["modifiers"], ["Mir", "Sub"])
        # The test mesh spans more than 1 m: Blender's default.
        self.assertAlmostEqual(data["modifiers"]["merge_threshold"], 0.001)
        self.assertAlmostEqual(data["merge_small"], 0.00017)
        self.assertAlmostEqual(data["merge_given"], 0.0002)
        self.assertAlmostEqual(data["merge_kept"], 0.0002)
        self.assertAlmostEqual(data["merge_default"], 0.00017)
        self.assertEqual(len(data["merge_invalid"]), 2)
        self.assertIn("merge_threshold must be more than 0", data["merge_invalid"][0])
        self.assertAlmostEqual(data["merge_y"], 0.00006)
        self.assertAlmostEqual(data["merge_edit"], 0.000085)
        self.assertAlmostEqual(data["merge_empty"], 0.001)
        self.assertEqual(data["mirror"], [[True, True, False], True, True])
        self.assertEqual(data["levels"], 3)

        # Edit Mode: the edit-mesh is updated, also when the block raises.
        def code_edit_mode() -> None:
            import bpy  # type: ignore[import-not-found]
            import blmcp_subd as subd  # type: ignore[import-not-found]
            obj = bpy.data.objects["Tube"]
            bpy.context.view_layer.objects.active = obj
            bpy.ops.object.mode_set(mode='EDIT')
            with subd.edit_mesh(obj) as bm:
                edge = next(e for e in bm.edges if len(e.link_faces) == 2)
                count = len(subd.insert_loop(bm, edge)[0])
            try:
                with subd.edit_mesh(obj) as bm:
                    edge = next(e for e in bm.edges if len(e.link_faces) == 2)
                    count += len(subd.insert_loop(bm, edge)[0])
                    raise KeyError("stop")
            except KeyError:
                pass
            bpy.ops.object.mode_set(mode='OBJECT')
            result = {"count": count, "verts": len(obj.data.vertices)}  # noqa: F841
        data = self._test_tool("execute_blender_code", {"code": _python_fn_body_as_string(code_edit_mode)})
        self.assertEqual(data["verts"], 32 + 2 + data["count"])

    def test_blmcp_subd_helpers_edge_cases(self) -> None:
        """
        ``blmcp_subd`` with overlapping vertices, extra edges and an open half cage.
        """
        def code() -> None:
            import bmesh  # type: ignore[import-not-found]
            import blmcp_subd as subd  # type: ignore[import-not-found]

            def half_tube(bm, zs):  # type: ignore[no-untyped-def]
                pts = [(0.0, -1.0), (1.0, -1.0), (1.0, 1.0), (0.0, 1.0)]
                rings = [[bm.verts.new((x, y, z)) for x, y in pts] for z in zs]
                for a, b in zip(rings, rings[1:]):
                    for k in range(3):
                        bm.faces.new((a[k], a[k + 1], b[k + 1], b[k]))
                return rings

            result = {}
            # Two quads side by side, not merged: their touching corners overlap.
            bm = bmesh.new()
            quads = [
                [bm.verts.new(co) for co in ((x, 0.0, 0.0), (x + 1.0, 0.0, 0.0), (x + 1.0, 1.0, 0.0), (x, 1.0, 0.0))]
                for x in (0.0, 1.0)
            ]
            faces = [bm.faces.new(q) for q in quads]
            ext = subd.extrude(bm, faces)
            result["overlap_sources"] = [
                {ext.source[v] for v in cap.verts} == set(q) for cap, q in zip(ext.faces, quads)
            ]
            bm.free()

            # An inner vertex (the center of a 2x2 grid) overlapped by another vertex to extrude:
            # its source is ambiguous, the extrusion stops and leaves the mesh as it was.
            bm = bmesh.new()
            bmesh.ops.create_grid(bm, x_segments=2, y_segments=2, size=1.0)
            corner = [bm.verts.new(co) for co in ((0.0, 0.0, 0.0), (-1.0, 0.0, 0.0), (-1.0, -1.0, 0.0), (0.0, -1.0, 0.0))]
            bm.faces.new(corner)
            counts = (len(bm.verts), len(bm.edges), len(bm.faces))
            try:
                subd.extrude(bm, list(bm.faces))
            except ValueError as ex:
                result["overlap_error"] = str(ex)
            result["overlap_unchanged"] = (len(bm.verts), len(bm.edges), len(bm.faces)) == counts
            # The lookup tables are usable after the error.
            result["overlap_lookup_ok"] = bm.verts[0].index == 0 and bm.faces[len(bm.faces) - 1] is not None
            bm.free()

            # Bridging an open half tube leaves the normals of its faces as they are.
            bm = bmesh.new()
            upper = half_tube(bm, (2.0, 3.0))
            lower = half_tube(bm, (0.0, 1.0))
            bm.normal_update()
            before = {f: f.normal.copy() for f in bm.faces}
            faces = subd.bridge_loops(bm, lower[1], upper[0])
            result["kept_normals"] = all(f.normal.dot(n) > 0.99 for f, n in before.items())
            result["bridge_contiguous"] = all(e.is_contiguous for e in bm.edges if len(e.link_faces) == 2)
            bm.free()

            # An extra edge from a ring end to the far end of the next ring edge (across a quad)
            # doesn't flip the order of the loop.
            bm = bmesh.new()
            rings = half_tube(bm, (0.0, 1.0))
            bm.edges.new((rings[0][1], rings[1][2]))
            edge = bm.edges.get((rings[0][1], rings[1][1]))
            (loop,) = subd.insert_loop(bm, edge)
            result["diagonal_loop_z"] = sorted({round(v.co.z, 3) for v in loop})
            result["diagonal_loop_x"] = [round(v.co.x, 3) for v in loop]
            bm.free()

            # A ring edge without length.
            bm = bmesh.new()
            rings = half_tube(bm, (0.0, 1.0))
            rings[1][2].co = rings[0][2].co
            try:
                subd.insert_loop(bm, bm.edges.get((rings[0][1], rings[1][1])))
            except ValueError as ex:
                result["zero_length_error"] = str(ex)
            bm.free()

        data = self._test_tool("execute_blender_code", {"code": _python_fn_body_as_string(code)})
        self.assertEqual(data["overlap_sources"], [True, True])
        self.assertIn("overlap", data["overlap_error"])
        self.assertTrue(data["overlap_unchanged"])
        self.assertTrue(data["overlap_lookup_ok"])
        self.assertTrue(data["kept_normals"])
        self.assertTrue(data["bridge_contiguous"])
        self.assertEqual(data["diagonal_loop_z"], [0.5])
        self.assertEqual(data["diagonal_loop_x"], [0.0, 1.0, 1.0, 0.0])
        self.assertIn("no length", data["zero_length_error"])

    def test_blmcp_subd_shaping(self) -> None:
        """
        The ``blmcp_subd`` helpers that shape and clean up a cage.
        """
        def code() -> None:
            import bmesh  # type: ignore[import-not-found]
            import bpy  # type: ignore[import-not-found]
            import blmcp_subd as subd  # type: ignore[import-not-found]

            def half_tube(bm, zs, x=0.0):  # type: ignore[no-untyped-def]
                pts = [(0.0, -1.0), (1.0, -1.0), (1.0, 1.0), (0.0, 1.0)]
                rings = [[bm.verts.new((px + x, y, z)) for px, y in pts] for z in zs]
                for a, b in zip(rings, rings[1:]):
                    for k in range(3):
                        bm.faces.new((a[k], a[k + 1], b[k + 1], b[k]))
                return rings

            result = {}
            # scale_loop: a half ring on the mirror plane scales around X = 0 and stays on it,
            # other vertices around their average.
            bm = bmesh.new()
            rings = half_tube(bm, (0.0, 1.0, 2.0))
            result["half_ring_center"] = subd.scale_loop(rings[1], (2.0, 0.5, 1.0))
            result["half_ring"] = [tuple(v.co) for v in rings[1]]
            result["side_center"] = subd.scale_loop(rings[2][1:3], 2.0)
            # Without `mirror` (a whole mesh), the vertices on X = 0 are like the others.
            result["whole_center"] = subd.scale_loop(rings[0], (2.0, 1.0, 1.0), mirror=False)
            result["whole_ring_x"] = [v.co.x for v in rings[0]]
            weights = subd.move_smooth(bm, rings[2], (1.0, 0.0, 0.0), radius=0.5, mirror=False)
            result["whole_moved_x"] = sorted(v.co.x for v in weights)
            bm.free()

            # move_smooth: along the edges only the tube follows, in a straight line also the
            # separate tube nearby. Vertices on the mirror plane stay on it.
            for connected in (True, False):
                bm = bmesh.new()
                tube = half_tube(bm, (0.0, 1.0, 2.0, 3.0))
                other = half_tube(bm, (3.0, 4.0), x=1.5)
                weights = subd.move_smooth(
                    bm, [tube[3][1], tube[3][2]], (0.0, 0.0, 1.0), radius=1.5, connected=connected)
                result["move_{:d}".format(connected)] = {
                    "moved_z": round(tube[3][1].co.z, 3),
                    "ring_below_z": round(tube[2][1].co.z, 3),
                    "plane_x": [tube[3][0].co.x, tube[2][0].co.x],
                    "other_follows": any(v in weights for ring in other for v in ring),
                }
                bm.free()

            # set_crease: on a bmesh without the attribute, the given edges are still set.
            bm = bmesh.new()
            half_tube(bm, (0.0, 1.0))
            bm.edges.ensure_lookup_table()
            subd.set_crease(bm, [bm.edges[0], bm.edges[1]], 1.0)
            layer = bm.edges.layers.float["crease_edge"]
            bm.edges.ensure_lookup_table()
            result["creases"] = [bm.edges[i][layer] for i in range(3)]
            bm.free()
            # With `edit_mesh`, the crease is written to the mesh.
            mesh = bpy.data.meshes.new("Creased")
            obj = bpy.data.objects.new("Creased", mesh)
            bpy.context.collection.objects.link(obj)
            with subd.edit_mesh(obj) as bm:
                half_tube(bm, (0.0, 1.0))
                bm.edges.ensure_lookup_table()
                subd.set_crease(bm, [bm.edges[0]], 0.5)
            result["mesh_crease"] = mesh.edge_creases.data[0].value

            # make_half: a whole cube (2x2 per side) with its middle vertices just off X = 0.
            bm = bmesh.new()
            bmesh.ops.create_cube(bm, size=2.0)
            bmesh.ops.subdivide_edges(bm, edges=bm.edges[:], cuts=1, use_grid_fill=True)
            for v in bm.verts:
                v.co.x += 0.0003
            result["make_half"] = subd.make_half(bm)
            result["half"] = {
                "min_x": min(v.co.x for v in bm.verts),
                "faces": len(bm.faces),
                "sides": sorted({len(f.verts) for f in bm.faces}),
                "boundary_on_plane": all(
                    _v.co.x == 0.0 for e in bm.edges if e.is_boundary for _v in e.verts),
                "wire_edges": sum(1 for e in bm.edges if not e.link_faces),
            }
            bm.free()
            # Cut through faces (3x3 per side, no vertices near X = 0): the vertices made by the
            # cut are not counted. Wire edges of the mesh stay: one on +X, one across the plane.
            bm = bmesh.new()
            bmesh.ops.create_cube(bm, size=2.0)
            bmesh.ops.subdivide_edges(bm, edges=bm.edges[:], cuts=2, use_grid_fill=True)
            bm.edges.new((bm.verts.new((2.0, 0.0, 0.0)), bm.verts.new((3.0, 0.0, 0.0))))
            bm.edges.new((bm.verts.new((-1.0, 5.0, 0.0)), bm.verts.new((1.0, 5.0, 0.0))))
            result["make_half_cut"] = subd.make_half(bm)
            result["half_cut"] = {
                "min_x": min(v.co.x for v in bm.verts),
                "wire_edges": sorted(
                    sorted(round(v.co.x, 3) for v in e.verts) for e in bm.edges if not e.link_faces),
            }
            bm.free()

            # symmetrize: the +X side (wider) is copied onto the -X side.
            bm = bmesh.new()
            bmesh.ops.create_cube(bm, size=2.0)
            for v in bm.verts:
                if v.co.x > 0.0:
                    v.co.y *= 2.0
            # A shape key (twice the basis on the +X side) is mirrored too (the vertices made
            # by the cut at X = 0 get interpolated values, not checked).
            key = bm.verts.layers.shape.new("Key")
            for v in bm.verts:
                v[key] = v.co * 2.0 if v.co.x > 0.0 else (0.0, 0.0, 0.0)
            subd.symmetrize(bm, '+X')
            result["symmetrize"] = sorted({(round(v.co.x, 3), round(abs(v.co.y), 3)) for v in bm.verts})
            result["symmetrize_key"] = all(
                (v[key] - v.co * 2.0).length < 1e-5 for v in bm.verts if abs(v.co.x) > 0.5)
            bm.free()

        data = self._test_tool("execute_blender_code", {"code": _python_fn_body_as_string(code)})
        self.assertEqual(data["half_ring_center"], [0.0, 0.0, 1.0])
        self.assertEqual(data["half_ring"], [[0.0, -0.5, 1.0], [2.0, -0.5, 1.0], [2.0, 0.5, 1.0], [0.0, 0.5, 1.0]])
        self.assertEqual(data["side_center"], [1.0, 0.0, 2.0])
        self.assertEqual(data["whole_center"], [0.5, 0.0, 0.0])
        self.assertEqual(data["whole_ring_x"], [-0.5, 1.5, 1.5, -0.5])
        # All 4 vertices move by 1, including the 2 on X = 0.
        self.assertEqual(data["whole_moved_x"], [1.0, 1.0, 2.0, 2.0])
        for key, other_follows in (("move_1", False), ("move_0", True)):
            move = data[key]
            self.assertEqual(move["moved_z"], 4.0, key)
            # One edge (1.0) below with radius 1.5: t = 1/3, smooth weight 0.259.
            self.assertEqual(move["ring_below_z"], 2.259, key)
            self.assertEqual(move["plane_x"], [0.0, 0.0], key)
            self.assertEqual(move["other_follows"], other_follows, key)
        self.assertEqual(data["creases"], [1.0, 1.0, 0.0])
        self.assertEqual(data["mesh_crease"], 0.5)
        # The -X side (9 vertices) is removed, the middle ring (8 vertices) is moved onto the plane.
        self.assertEqual(data["make_half"], {"removed_vertices": 9, "snapped_vertices": 8, "tolerance": 0.001})
        self.assertEqual(data["half"]["min_x"], 0.0)
        self.assertEqual(data["half"]["sides"], [4])
        self.assertTrue(data["half"]["boundary_on_plane"])
        self.assertEqual(data["half"]["wire_edges"], 0)
        # The -X side (16 + 12 vertices) and the -X end of the wire edge across the plane.
        self.assertEqual(data["make_half_cut"], {"removed_vertices": 29, "snapped_vertices": 0, "tolerance": 0.001})
        self.assertEqual(data["half_cut"]["min_x"], 0.0)
        self.assertEqual(data["half_cut"]["wire_edges"], [[0.0, 1.0], [2.0, 3.0]])
        self.assertEqual(data["symmetrize"], [[-1.0, 2.0], [0.0, 1.5], [1.0, 2.0]])
        self.assertTrue(data["symmetrize_key"])

    def test_get_modeling_guide_character(self) -> None:
        """
        The guide's example code builds a clean cage: keep it working.
        """
        data = self._test_tool("get_modeling_guide", {"topic": "character"})
        self.assertEqual(data["topic"], "character")
        guide = str(data["guide"])
        self.assertIn("get_mesh_topology_summary", guide)

        def example(name: str) -> str:
            # The code block following the `<!-- example: name -->` marker.
            after = guide.split("<!-- example: {:s} -->".format(name), 1)[1]
            return after.split("```python\n", 1)[1].split("```", 1)[0]

        code = example("character")

        data = self._test_tool("execute_blender_code", {"code": code})
        self.assertEqual(data["object"], "Character")

        data = self._test_tool("get_mesh_topology_summary", {"name": "Character"})
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["islands"]["count"], 1)
        self.assertEqual(data["faces"]["triangles"]["count"], 0)
        self.assertEqual(data["faces"]["ngons"]["count"], 0)
        for key in (
                "boundary_edges", "non_manifold_edges", "wire_edges",
                "loose_vertices", "inconsistent_normal_edges",
        ):
            self.assertEqual(data[key]["count"], 0, key)
        (symmetry,) = data["symmetry"]
        self.assertTrue(symmetry["is_joined"])
        self.assertEqual(symmetry["off_center_vertices"]["count"], 0)
        self.assertEqual(symmetry["crossing_faces"]["count"], 0)
        self.assertEqual(data["warnings"], [])
        # The poles the guide describes as expected.
        self.assertEqual(data["poles"]["valence_3"]["count"], 12)
        self.assertEqual(data["poles"]["valence_5_plus"]["count"], 8)
        self.assertEqual({p["valence"] for p in data["poles"]["valence_5_plus"]["items"]}, {5})

        # The helper example: a loop around the waist, across the 5 faces of the half torso.
        data = self._test_tool("execute_blender_code", {"code": example("insert-loop")})
        self.assertEqual(len(data["loop"]), 6)
        data = self._test_tool("get_mesh_topology_summary", {"name": "Character"})
        self.assertEqual(data["counts"]["vertices"], 96 + 6)
        self.assertEqual(data["faces"]["triangles"]["count"] + data["faces"]["ngons"]["count"], 0)
        self.assertEqual(data["boundary_edges"]["count"], 0)
        self.assertEqual(data["symmetry"][0]["off_center_vertices"]["count"], 0)
        self.assertEqual(data["poles"]["valence_3"]["count"], 12)
        self.assertEqual(data["poles"]["valence_5_plus"]["count"], 8)

        # About 1.7 m tall after subdivision, standing on the ground.
        data = self._test_tool("get_object_geometry_summary", {"names": ["Character"]})
        bounds = data["objects"][0]["bounds_world"]
        self.assertAlmostEqual(bounds["size"][2], 1.7, delta=0.02)
        self.assertAlmostEqual(bounds["min"][2], 0.0, delta=0.01)

        # The ground contact snippet, also with a rotated, scaled and raised parent.
        def code_parent() -> None:
            import math
            import bpy  # type: ignore[import-not-found]
            parent = bpy.data.objects.new("Parent", None)
            bpy.context.scene.collection.objects.link(parent)
            parent.location = (0.0, 0.0, 0.5)
            parent.rotation_euler = (0.0, 0.0, math.radians(30.0))
            parent.scale = (2.0, 2.0, 2.0)
            bpy.data.objects["Character"].parent = parent
            result = {'ok': True}  # noqa: F841
        self._test_tool("execute_blender_code", {"code": _python_fn_body_as_string(code_parent)})
        self._test_tool("execute_blender_code", {"code": example("ground-contact")})
        data = self._test_tool("get_object_geometry_summary", {"names": ["Character"]})
        self.assertAlmostEqual(data["objects"][0]["bounds_world"]["min"][2], 0.0, places=4)

        # Tilted (around Y): moving the cage would open the mirror seam, the snippet stops.
        def code_tilt() -> None:
            import math
            import bpy  # type: ignore[import-not-found]
            parent = bpy.data.objects["Parent"]
            parent.rotation_euler = (0.0, math.radians(10.0), 0.0)
            parent.location = (0.0, 0.0, 1.0)
            result = {'ok': True}  # noqa: F841
        self._test_tool("execute_blender_code", {"code": _python_fn_body_as_string(code_tilt)})
        data = self._test_tool("execute_blender_code", {"code": example("ground-contact")})
        self.assertEqual(data["status"], "error")
        self.assertIn("mirror seam", str(data["message"]))
        data = self._test_tool("get_mesh_topology_summary", {"name": "Character"})
        self.assertEqual(data["symmetry"][0]["off_center_vertices"]["count"], 0)

    def test_get_modeling_guide_error(self) -> None:
        content = self._call_tool_expect_error("get_modeling_guide", {"topic": "NONEXISTENT"})
        # Rejected by the argument validation, listing the valid topics.
        text = str(content[0].get("text", ""))
        self.assertIn("literal_error", text)
        self.assertIn("'character'", text)

    def test_get_scene_render_summary(self) -> None:
        data = self._test_tool("get_scene_render_summary")
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["scene_name"], "Scene")

        render = data["render"]
        assert isinstance(render, dict)
        self.assertEqual(render["engine"], "BLENDER_EEVEE")
        self.assertEqual(render["samples"], render["engine_settings"]["taa_render_samples"])
        self.assertEqual(render["resolution"], [1920, 1080])
        self.assertEqual(render["resolution_percentage"], 100)

        self.assertEqual(data["color_management"]["view_transform"], "AgX")

        world = data["world"]
        assert isinstance(world, dict)
        self.assertEqual(world["name"], "World")
        self.assertEqual([b["node"] for b in world["background"]], ["Background"])
        self.assertIsNone(world["background"][0]["color_linked_from"])
        self.assertEqual(world["environment_textures"], [])

        lights = data["lights"]
        assert isinstance(lights, list)
        self.assertEqual([light["name"] for light in lights], ["Light"])
        self.assertEqual(lights[0]["type"], "POINT")
        self.assertIn("shadow_soft_size", lights[0])
        self.assertIsNone(lights[0]["direction"])
        self.assertTrue(lights[0]["visible"])

        camera = data["camera"]
        assert isinstance(camera, dict)
        self.assertEqual(camera["name"], "Camera")
        self.assertEqual(camera["type"], "PERSP")
        self.assertAlmostEqual(camera["lens"], 50.0)
        self.assertFalse(camera["dof"]["use_dof"])

    def test_get_scene_render_summary_variants(self) -> None:
        def code() -> None:
            import bpy  # type: ignore[import-not-found]
            scene = bpy.context.scene
            scene.render.engine = 'CYCLES'
            scene.cycles.samples = 32
            scene.view_settings.view_transform = 'Standard'

            # World: environment texture feeding one of two mixed backgrounds.
            world = scene.world
            tree = world.node_tree
            output = tree.get_output_node('ALL')
            bg_a = tree.nodes["Background"]
            bg_b = tree.nodes.new("ShaderNodeBackground")
            bg_b.name = "Background Env"
            env = tree.nodes.new("ShaderNodeTexEnvironment")
            env.image = bpy.data.images.new("EnvImage", 4, 2)
            tree.links.new(env.outputs["Color"], bg_b.inputs["Color"])
            mix = tree.nodes.new("ShaderNodeMixShader")
            tree.links.new(bg_a.outputs["Background"], mix.inputs[1])
            tree.links.new(bg_b.outputs["Background"], mix.inputs[2])
            tree.links.new(mix.outputs["Shader"], output.inputs["Surface"])
            # Not connected to the output, must not be listed as a background.
            tree.nodes.new("ShaderNodeBackground").name = "Background Unused"

            # A sun hidden from rendering and a rectangular area light.
            sun = bpy.data.objects.new("Sun", bpy.data.lights.new("Sun", 'SUN'))
            sun.hide_render = True
            area_data = bpy.data.lights.new("Area", 'AREA')
            area_data.shape = 'RECTANGLE'
            area_data.size_y = 2.0
            area = bpy.data.objects.new("Area", area_data)
            scene.collection.objects.link(sun)
            scene.collection.objects.link(area)

            cam = scene.camera.data
            cam.dof.use_dof = True
            cam.dof.focus_object = bpy.data.objects["Cube"]
            result = {'ok': True}  # noqa: F841
        self._test_tool("execute_blender_code", {
            "code": _python_fn_body_as_string(code),
        })
        data = self._test_tool("get_scene_render_summary")

        render = data["render"]
        assert isinstance(render, dict)
        self.assertEqual(render["engine"], "CYCLES")
        self.assertEqual(render["samples"], 32)
        self.assertNotIn("taa_render_samples", render["engine_settings"])
        self.assertEqual(data["color_management"]["view_transform"], "Standard")

        world = data["world"]
        assert isinstance(world, dict)
        background = {b["node"]: b for b in world["background"]}
        self.assertEqual(sorted(background), ["Background", "Background Env"])
        self.assertEqual(background["Background Env"]["color_linked_from"], "Environment Texture")
        self.assertEqual(
            [(t["node"], t["image"]) for t in world["environment_textures"]],
            [("Environment Texture", "EnvImage")],
        )

        lights = {light["name"]: light for light in data["lights"]}
        self.assertEqual(sorted(lights), ["Area", "Light", "Sun"])
        self.assertTrue(lights["Sun"]["hide_render"])
        self.assertIn("angle", lights["Sun"])
        # Unrotated lights point down the -Z axis.
        for value, expected in zip(lights["Sun"]["direction"], (0.0, 0.0, -1.0)):
            self.assertAlmostEqual(value, expected, places=5)
        self.assertEqual(lights["Area"]["shape"], "RECTANGLE")
        self.assertAlmostEqual(lights["Area"]["size_y"], 2.0)

        self.assertEqual(data["camera"]["dof"]["focus_object"], "Cube")

    def test_get_scene_render_summary_world_output_target(self) -> None:
        def code_cycles_only() -> None:
            import bpy  # type: ignore[import-not-found]
            scene = bpy.context.scene
            scene.render.engine = 'CYCLES'
            # The only output targets Cycles, there is no output for all engines.
            scene.world.node_tree.get_output_node('ALL').target = 'CYCLES'
            result = {'ok': True}  # noqa: F841
        self._test_tool("execute_blender_code", {
            "code": _python_fn_body_as_string(code_cycles_only),
        })
        data = self._test_tool("get_scene_render_summary")
        self.assertEqual([b["node"] for b in data["world"]["background"]], ["Background"])

        def code_add_all_output() -> None:
            import bpy  # type: ignore[import-not-found]
            tree = bpy.context.scene.world.node_tree
            output_all = tree.nodes.new("ShaderNodeOutputWorld")
            output_all.target = 'ALL'
            bg_all = tree.nodes.new("ShaderNodeBackground")
            bg_all.name = "Background All"
            tree.links.new(bg_all.outputs["Background"], output_all.inputs["Surface"])
            result = {'ok': True}  # noqa: F841
        self._test_tool("execute_blender_code", {
            "code": _python_fn_body_as_string(code_add_all_output),
        })
        # The output targeting the engine takes precedence over the one for all engines.
        data = self._test_tool("get_scene_render_summary")
        self.assertEqual([b["node"] for b in data["world"]["background"]], ["Background"])

        # Other engines fall back to the output for all engines.
        self._test_tool("execute_blender_code", {
            "code": (
                "import bpy\n"
                "bpy.context.scene.render.engine = 'BLENDER_EEVEE'\n"
                "result = {'ok': True}\n"
            ),
        })
        data = self._test_tool("get_scene_render_summary")
        self.assertEqual([b["node"] for b in data["world"]["background"]], ["Background All"])

    def test_get_scene_render_summary_world_muted_and_reroute(self) -> None:
        def code() -> None:
            import bpy  # type: ignore[import-not-found]
            tree = bpy.context.scene.world.node_tree
            output = tree.get_output_node('ALL')
            bg = tree.nodes["Background"]

            # The color comes through a Reroute node.
            env = tree.nodes.new("ShaderNodeTexEnvironment")
            reroute = tree.nodes.new("NodeReroute")
            tree.links.new(env.outputs["Color"], reroute.inputs[0])
            tree.links.new(reroute.outputs[0], bg.inputs["Color"])
            # A muted link to the strength passes nothing.
            value = tree.nodes.new("ShaderNodeValue")
            tree.links.new(value.outputs[0], bg.inputs["Strength"]).is_muted = True

            # A background behind a muted link does not reach the output.
            bg_muted = tree.nodes.new("ShaderNodeBackground")
            bg_muted.name = "Background Muted"
            mix = tree.nodes.new("ShaderNodeMixShader")
            tree.links.new(bg.outputs["Background"], mix.inputs[1])
            tree.links.new(bg_muted.outputs["Background"], mix.inputs[2]).is_muted = True
            tree.links.new(mix.outputs["Shader"], output.inputs["Surface"])
            result = {'ok': True}  # noqa: F841
        self._test_tool("execute_blender_code", {
            "code": _python_fn_body_as_string(code),
        })
        data = self._test_tool("get_scene_render_summary")
        background = data["world"]["background"]
        self.assertEqual([b["node"] for b in background], ["Background"])
        self.assertEqual(background[0]["color_linked_from"], "Environment Texture")
        self.assertIsNone(background[0]["strength_linked_from"])

    def test_get_scene_render_summary_no_world_or_camera(self) -> None:
        self._test_tool("execute_blender_code", {
            "code": (
                "import bpy\n"
                "bpy.context.scene.world = None\n"
                "bpy.context.scene.camera = None\n"
                "result = {'ok': True}\n"
            ),
        })
        data = self._test_tool("get_scene_render_summary")
        self.assertEqual(data["status"], "ok")
        self.assertIsNone(data["world"])
        self.assertIsNone(data["camera"])

    # -----------------------------------------------------------------
    # Navigation tools.

    def test_jump_to_tab_by_name(self) -> None:
        data = self._test_tool("jump_to_tab_by_name", {"name": "Layout"})
        if not self._interactive:
            self.assertEqual(data["status"], "error")
            return
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["workspace"], "Layout")

    def test_jump_to_tab_by_space_type(self) -> None:
        data = self._test_tool("jump_to_tab_by_space_type", {"space_type": "VIEW_3D"})
        if not self._interactive:
            self.assertEqual(data["status"], "error")
            return
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["space_type"], "VIEW_3D")

    def test_jump_to_view3d_object_by_name(self) -> None:
        data = self._test_tool("jump_to_view3d_object_by_name", {"name": "Cube"})
        if not self._interactive:
            self.assertEqual(data["status"], "error")
            return
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["object"], "Cube")
        self.assertEqual(data["type"], "MESH")

    def test_jump_to_view3d_object_data_by_name(self) -> None:
        data = self._test_tool("jump_to_view3d_object_data_by_name", {"name": "Cube"})
        if not self._interactive:
            self.assertEqual(data["status"], "error")
            return
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["data_name"], "Cube")
        self.assertEqual(data["type"], "MESH")

    # -----------------------------------------------------------------
    # Render tools.

    def _assert_valid_png(self, filepath: str) -> None:
        """
        Ask Blender to verify that *filepath* is a valid PNG file.
        """
        data = self._test_tool("execute_blender_code", {
            "code": (
                "import os\n"
                "with open({!r}, 'rb') as fh:\n"
                "    header = fh.read(8)\n"
                "result = {{\n"
                "    'size': os.path.getsize({!r}),\n"
                "    'png_magic': header == b'\\x89PNG\\r\\n\\x1a\\n',\n"
                "}}\n"
            ).format(filepath, filepath),
        })
        self.assertGreater(data["size"], 0)
        self.assertTrue(data["png_magic"])

    def _set_cycles_cpu(self) -> None:
        """Switch to Cycles CPU so rendering works in headless environments."""
        def code() -> None:
            import bpy  # type: ignore[import-not-found]
            bpy.context.scene.render.engine = 'CYCLES'
            bpy.context.scene.cycles.device = 'CPU'
            result = {'engine': 'CYCLES'}  # noqa: F841
        self._test_tool("execute_blender_code", {
            "code": _python_fn_body_as_string(code),
        })

    def test_render_thumbnail_to_path(self) -> None:
        self._set_cycles_cpu()
        data = self._test_tool("render_thumbnail_to_path", {
            "output_path": "thumb.png",
        })
        self.assertEqual(data["status"], "ok")
        self.assertTrue(data["filepath"].endswith("thumb.png"))
        self._assert_valid_png(data["filepath"])

    def test_render_viewport_to_path(self) -> None:
        self._set_cycles_cpu()
        data = self._test_tool("render_viewport_to_path", {
            "output_path": "render.png",
        })
        self.assertEqual(data["status"], "ok")
        self.assertTrue(data["filepath"].endswith("render.png"))
        self._assert_valid_png(data["filepath"])

    def _call_tool_render_as_image(
        self,
        name: str,
        arguments: dict[str, object] | None = None,
    ) -> tuple[bytes, dict[str, object]]:
        """
        Call a ``render_*_as_image`` tool, return the PNG bytes and the render info.
        """
        content = self._call_tool(name, arguments)
        self.assertEqual(len(content), 2)
        self.assertEqual(content[0].get("type"), "image")
        self.assertEqual(content[1].get("type"), "text")
        image_data = base64.b64decode(str(content[0].get("data", "")))
        self.assertEqual(image_data[:8], b"\x89PNG\r\n\x1a\n")
        info = json.loads(str(content[1]["text"]))
        self.assertEqual(
            self._image_size(str(content[0]["data"])),
            (info["image_width"], info["image_height"]),
        )
        self.assertIsInstance(info["render_time_seconds"], float)
        self.assertTrue(str(info["filepath"]).endswith(".png"))
        self._assert_valid_png(str(info["filepath"]))
        return image_data, info

    def test_render_thumbnail_as_image(self) -> None:
        self._set_cycles_cpu()

        def code_setup() -> None:
            import bpy  # type: ignore[import-not-found]
            # A non-PNG output format must not affect the returned image.
            bpy.context.scene.render.image_settings.file_format = 'OPEN_EXR'
            result = {'ok': True}  # noqa: F841
        self._test_tool("execute_blender_code", {
            "code": _python_fn_body_as_string(code_setup),
        })
        _image_data, info = self._call_tool_render_as_image("render_thumbnail_as_image")
        self.assertEqual(info["engine"], "CYCLES")
        # The longest dimension is clamped (default 1920x1080 -> 320x180).
        self.assertEqual((info["render_width"], info["render_height"]), (320, 180))
        self.assertEqual((info["image_width"], info["image_height"]), (320, 180))

        def code_check() -> None:
            import bpy  # type: ignore[import-not-found]
            rd = bpy.context.scene.render
            result = {  # noqa: F841
                'file_format': rd.image_settings.file_format,
                'resolution': [rd.resolution_x, rd.resolution_y],
                'samples': bpy.context.scene.cycles.samples,
            }
        data = self._test_tool("execute_blender_code", {
            "code": _python_fn_body_as_string(code_check),
        })
        self.assertEqual(data["file_format"], "OPEN_EXR")
        self.assertEqual(data["resolution"], [1920, 1080])
        self.assertNotEqual(data["samples"], 16)

    def test_render_viewport_as_image_size_limit(self) -> None:
        self._set_cycles_cpu()

        def code_setup() -> None:
            import bpy  # type: ignore[import-not-found]
            rd = bpy.context.scene.render
            rd.resolution_x = 640
            rd.resolution_y = 480
            bpy.context.scene.cycles.samples = 4
            result = {'ok': True}  # noqa: F841
        self._test_tool("execute_blender_code", {
            "code": _python_fn_body_as_string(code_setup),
        })
        size_limit = 16 * 1024  # 16 KB.
        image_data, info = self._call_tool_render_as_image("render_viewport_as_image", {
            "size_limit_in_bytes": size_limit,
        })
        self.assertLessEqual(len(image_data), size_limit)
        self.assertEqual((info["render_width"], info["render_height"]), (640, 480))
        self.assertLess(int(str(info["image_width"])), 640)

    def test_render_viewport_as_image_dims_max(self) -> None:
        self._set_cycles_cpu()

        def code_setup() -> None:
            import bpy  # type: ignore[import-not-found]
            rd = bpy.context.scene.render
            rd.resolution_x = 2560
            rd.resolution_y = 1440
            bpy.context.scene.cycles.samples = 1
            result = {'ok': True}  # noqa: F841
        self._test_tool("execute_blender_code", {
            "code": _python_fn_body_as_string(code_setup),
        })
        _image_data, info = self._call_tool_render_as_image("render_viewport_as_image")
        self.assertEqual((info["render_width"], info["render_height"]), (2560, 1440))
        self.assertLessEqual(max(int(str(info["image_width"])), int(str(info["image_height"]))), 2048)

    def test_render_viewport_as_image_video_output(self) -> None:
        """A video output (where PNG is not an available format) is restored after rendering."""
        self._set_cycles_cpu()

        def code_setup() -> None:
            import bpy  # type: ignore[import-not-found]
            rd = bpy.context.scene.render
            rd.resolution_x = 320
            rd.resolution_y = 240
            bpy.context.scene.cycles.samples = 1
            rd.filepath = "//custom_output_"
            rd.image_settings.media_type = 'VIDEO'
            rd.image_settings.file_format = 'FFMPEG'
            result = {'ok': True}  # noqa: F841
        self._test_tool("execute_blender_code", {
            "code": _python_fn_body_as_string(code_setup),
        })
        self._call_tool_render_as_image("render_viewport_as_image")

        def code_check() -> None:
            import bpy  # type: ignore[import-not-found]
            rd = bpy.context.scene.render
            result = {  # noqa: F841
                'filepath': rd.filepath,
                'media_type': rd.image_settings.media_type,
                'file_format': rd.image_settings.file_format,
            }
        data = self._test_tool("execute_blender_code", {
            "code": _python_fn_body_as_string(code_check),
        })
        self.assertEqual(data["filepath"], "//custom_output_")
        self.assertEqual(data["media_type"], "VIDEO")
        self.assertEqual(data["file_format"], "FFMPEG")

    def _set_render_small(self) -> None:
        """Use a small resolution and a single sample for fast renders."""
        def code() -> None:
            import bpy  # type: ignore[import-not-found]
            rd = bpy.context.scene.render
            rd.resolution_x = 320
            rd.resolution_y = 240
            bpy.context.scene.cycles.samples = 1
            result = {'ok': True}  # noqa: F841
        self._test_tool("execute_blender_code", {
            "code": _python_fn_body_as_string(code),
        })

    def test_render_viewport_as_image_error_after_success(self) -> None:
        """A failed render must not return the image of a previous render."""
        self._set_cycles_cpu()
        self._set_render_small()
        self._call_tool_render_as_image("render_viewport_as_image")

        def code_remove_camera() -> None:
            import bpy  # type: ignore[import-not-found]
            bpy.data.objects.remove(bpy.data.objects["Camera"], do_unlink=True)
            result = {'ok': True}  # noqa: F841
        self._test_tool("execute_blender_code", {
            "code": _python_fn_body_as_string(code_remove_camera),
        })
        content = self._call_tool_expect_error("render_viewport_as_image")
        self.assertNotIn("image", [item.get("type") for item in content])
        # The reason reaches the client.
        self.assertIn("no camera", str(content[0].get("text", "")))

    def test_render_viewport_as_image_outputs(self) -> None:
        """
        Each call writes its own output. Outputs that were read are removed
        (keeping the newest), outputs that were not read are kept.
        """
        self._set_cycles_cpu()
        self._set_render_small()
        filepaths = [
            str(self._call_tool_render_as_image("render_viewport_as_image")[1]["filepath"])
            for _ in range(2)
        ]
        # An output whose deferred checker has not read it yet (not possible to
        # reproduce in background mode, so write a file in its place).
        filepath_unread = os.path.join(os.path.dirname(filepaths[0]), "render_viewport_as_image_0.png")
        self._test_tool("execute_blender_code", {
            "code": "import shutil\nshutil.copyfile({!r}, {!r})\nresult = {{}}\n".format(filepaths[1], filepath_unread),
        })
        filepaths.append(str(self._call_tool_render_as_image("render_viewport_as_image")[1]["filepath"]))
        self.assertEqual(len(set(filepaths)), 3)
        data = self._test_tool("execute_blender_code", {
            "code": "import os\nresult = {{'exists': [os.path.exists(f) for f in {!r}]}}\n".format(
                [*filepaths, filepath_unread],
            ),
        })
        self.assertEqual(data["exists"], [False, True, True, True])

    def test_render_viewport_as_image_multiview(self) -> None:
        """A multi-view (stereo) render writes a file for each view, it must still return an image."""
        self._set_cycles_cpu()
        self._set_render_small()

        def code_setup() -> None:
            import bpy  # type: ignore[import-not-found]
            rd = bpy.context.scene.render
            rd.use_multiview = True
            rd.views_format = 'STEREO_3D'
            rd.image_settings.views_format = 'INDIVIDUAL'
            result = {'ok': True}  # noqa: F841
        self._test_tool("execute_blender_code", {
            "code": _python_fn_body_as_string(code_setup),
        })
        self._call_tool_render_as_image("render_viewport_as_image")
        data = self._test_tool("execute_blender_code", {
            "code": "import bpy\nresult = {'use_multiview': bpy.context.scene.render.use_multiview}\n",
        })
        self.assertTrue(data["use_multiview"])

    # -----------------------------------------------------------------
    # Deferred tool response.

    def test_deferred_tool_response(self) -> None:
        """Synthetic test for the deferred response mechanism, not a real tool."""
        if not self._interactive:
            # Deferred responses only apply to interactive (timer-based) mode.
            # Background mode rejects them explicitly.
            return

        # Send code that sets check_is_finished. The deferred response
        # mechanism polls it until it returns a non-None result.
        def deferred_code() -> None:
            import time
            deadline = time.monotonic() + 0.3

            def check_is_finished():  # noqa: F841 (read by the exec namespace)
                if time.monotonic() < deadline:
                    return None
                return {'deferred': True, 'value': 42}
            result = {}  # noqa: F841
        data = self._test_tool("execute_blender_code", {
            "code": _python_fn_body_as_string(deferred_code),
        })
        self.assertTrue(data["deferred"])
        self.assertEqual(data["value"], 42)

    def test_deferred_tool_response_blocking_mode_error(self) -> None:
        """Synthetic test: blocking server modes reject deferred responses clearly."""
        if self._interactive:
            return

        def deferred_code() -> None:
            def check_is_finished():  # noqa: F841 (read by the exec namespace)
                return {'deferred': True, 'value': 42}
            result = {}  # noqa: F841
        data = self._test_tool("execute_blender_code", {
            "code": _python_fn_body_as_string(deferred_code),
        })
        self.assertEqual(data["status"], "error")
        self.assertIn(
            "Deferred responses via `check_is_finished` are only supported by the interactive addon server",
            data["message"],
        )

    def test_deferred_tool_render(self) -> None:
        """Test the deferred path with a real render of a non-trivial scene."""
        if not self._interactive:
            return

        def _setup_render_scene() -> None:
            import bpy  # type: ignore[import-not-found]
            bpy.ops.wm.read_homefile(use_empty=True)
            scene = bpy.context.scene
            # Use Cycles CPU so the render works in headless environments
            # where EEVEE may not have a usable GPU context.
            scene.render.engine = 'CYCLES'
            scene.cycles.device = 'CPU'
            scene.cycles.samples = 16
            scene.render.resolution_x = 960
            scene.render.resolution_y = 540
            # Camera.
            bpy.ops.object.camera_add(location=(7, -7, 5))
            cam = bpy.context.active_object
            cam.rotation_euler = (1.1, 0, 0.8)
            scene.camera = cam
            # Light.
            bpy.ops.object.light_add(type='SUN', location=(5, -3, 8))
            # Grid of spheres with different materials.
            for i in range(6):
                for j in range(6):
                    bpy.ops.mesh.primitive_uv_sphere_add(
                        segments=32, ring_count=16,
                        radius=0.4, location=(i, j, 0),
                    )
                    ob = bpy.context.active_object
                    mat = bpy.data.materials.new('M_{:d}_{:d}'.format(i, j))
                    mat.use_nodes = True
                    bsdf = mat.node_tree.nodes['Principled BSDF']
                    bsdf.inputs['Base Color'].default_value = (
                        i / 5.0, j / 5.0, 0.5, 1.0,
                    )
                    bsdf.inputs['Roughness'].default_value = i / 5.0
                    bsdf.inputs['Metallic'].default_value = j / 5.0
                    ob.data.materials.append(mat)
            # Ground plane.
            bpy.ops.mesh.primitive_plane_add(size=20, location=(2.5, 2.5, -0.5))
            result = {'objects': len(bpy.data.objects)}  # noqa: F841

        self._test_tool("execute_blender_code", {
            "code": _python_fn_body_as_string(_setup_render_scene),
        })

        data = self._test_tool("render_viewport_to_path", {
            "output_path": "deferred_render.png",
        })
        self.assertEqual(data["status"], "ok")
        self.assertTrue(data["filepath"].endswith("deferred_render.png"))
        self._assert_valid_png(data["filepath"])

    def test_deferred_tool_response_error(self) -> None:
        """Synthetic test: verify that an exception in check_is_finished is reported."""
        if not self._interactive:
            return

        def deferred_error_code() -> None:
            def check_is_finished():  # noqa: F841
                raise RuntimeError('checker failed')
            result = {}  # noqa: F841
        data = self._test_tool("execute_blender_code", {
            "code": _python_fn_body_as_string(deferred_error_code),
        })
        self.assertEqual(data["status"], "error")
        self.assertIn("checker failed", data["message"])

    # -----------------------------------------------------------------
    # Large responses.

    def test_large_response_slow_client(self) -> None:
        """A response larger than the socket send buffer must arrive in full."""
        if not self._interactive:
            # Only the timer-based server writes responses without blocking,
            # the other modes use blocking sockets which never queue.
            return

        # Comfortably over the kernel send buffer, so the add-on has to
        # write the response over multiple polls, see `_flush_pending_writes`.
        size = 8 * 1024 * 1024
        request = json.dumps({
            "type": "execute",
            "code": "result = {{'data': 'x' * {:d}}}".format(size),
            "strict_json": True,
        }) + "\0"

        buf = bytearray()
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(_TIMEOUT_LOCAL_PROC)
            sock.connect(("localhost", self._port))
            sock.sendall(request.encode("utf-8"))
            # Stall before reading so the send buffer fills while the response is written.
            time.sleep(1.0)
            while b"\0" not in buf:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                buf.extend(chunk)

        self.assertIn(
            b"\0", buf,
            "Response truncated at {:d} bytes, the null terminator is missing".format(len(buf)),
        )
        data = json.loads(buf[:buf.index(b"\0")].decode("utf-8"))
        self.assertEqual(data["status"], "ok")
        self.assertEqual(len(data["result"]["data"]), size)

    # -----------------------------------------------------------------
    # Error handling.

    def test_execute_blender_code_error(self) -> None:
        data = self._test_tool("execute_blender_code", {
            "code": "raise ValueError('test error')",
        })
        self.assertEqual(data["status"], "error")
        self.assertIn("ValueError", data["message"])

    def test_execute_blender_code_blocked_operator(self) -> None:
        """Verify that the sandbox blocks actions which may exit Blender or lose our connection."""
        data = self._test_tool("execute_blender_code", {
            "code": "import bpy; bpy.ops.wm.read_factory_settings()",
        })
        self.assertEqual(data["status"], "error")
        self.assertIn(
            "RuntimeError: Operator 'bpy.ops.wm.read_factory_settings()' is not allowed "
            "in LLM-generated code: Resets all user preferences and startup file, "
            "use bpy.ops.wm.read_homefile() or "
            "bpy.ops.wm.read_homefile(use_empty=True, use_factory_startup=True) instead",
            data["message"],
        )

    def test_execute_blender_code_blocked_sys_exit(self) -> None:
        """Verify that the sandbox blocks sys.exit()."""
        data = self._test_tool("execute_blender_code", {
            "code": "import sys; sys.exit(1)",
        })
        self.assertEqual(data["status"], "error")
        self.assertIn(
            "RuntimeError: sys.exit() is not allowed in LLM-generated code",
            data["message"],
        )

    def test_jump_to_tab_by_name_error(self) -> None:
        if not self._interactive:
            return
        data = self._test_tool("jump_to_tab_by_name", {"name": "NonExistent"})
        self.assertEqual(data["status"], "error")
        self.assertIsInstance(data["available_workspaces"], list)

    def test_execute_blender_code_for_cli_error(self) -> None:
        self._call_tool_expect_error("execute_blender_code_for_cli", {
            "blend_file": self._blend_path,
            "code": "raise ValueError('cli test error')",
        })

    def test_jump_to_view3d_object_by_name_error(self) -> None:
        if not self._interactive:
            return
        data = self._test_tool("jump_to_view3d_object_by_name", {"name": "NonExistent"})
        self.assertEqual(data["status"], "error")
        self.assertEqual(data["message"], "Object 'NonExistent' not found")

    def test_jump_to_view3d_object_data_by_name_error(self) -> None:
        if not self._interactive:
            return
        data = self._test_tool("jump_to_view3d_object_data_by_name", {"name": "NonExistent"})
        self.assertEqual(data["status"], "error")
        self.assertEqual(data["message"], "No object found with data named 'NonExistent'")

    # -----------------------------------------------------------------
    # State verification.

    def test_jump_to_view3d_object_by_name_allow_edits(self) -> None:
        """
        Verify that ``allow_edits`` un-hides a hidden object.
        """
        if not self._interactive:
            return
        # Hide the default Cube.
        self._test_tool("execute_blender_code", {
            "code": (
                "import bpy\n"
                "bpy.data.objects['Cube'].hide_viewport = True\n"
                "result = {'hidden': True}\n"
            ),
        })
        # Jump to it with allow_edits enabled.
        data = self._test_tool("jump_to_view3d_object_by_name", {
            "name": "Cube", "allow_edits": True,
        })
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["object"], "Cube")
        # Verify the object is no longer hidden.
        check = self._test_tool("execute_blender_code", {
            "code": (
                "import bpy\n"
                "result = {'hide_viewport': bpy.data.objects['Cube'].hide_viewport}\n"
            ),
        })
        self.assertFalse(check["hide_viewport"])

    def test_jump_to_view3d_object_data_by_name_allow_edits(self) -> None:
        """
        Verify that ``allow_edits`` un-hides an object found by data name.
        """
        if not self._interactive:
            return
        # Hide the default Cube.
        self._test_tool("execute_blender_code", {
            "code": (
                "import bpy\n"
                "bpy.data.objects['Cube'].hide_viewport = True\n"
                "result = {'hidden': True}\n"
            ),
        })
        # Jump to it via data name with allow_edits enabled.
        data = self._test_tool("jump_to_view3d_object_data_by_name", {
            "name": "Cube", "allow_edits": True,
        })
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["data_name"], "Cube")
        # Verify the object is no longer hidden.
        check = self._test_tool("execute_blender_code", {
            "code": (
                "import bpy\n"
                "result = {'hide_viewport': bpy.data.objects['Cube'].hide_viewport}\n"
            ),
        })
        self.assertFalse(check["hide_viewport"])

    def test_execute_blender_code_stateful(self) -> None:
        """
        Verify that the Blender session is stateful across tool calls.
        """
        # Create an object.
        self._test_tool("execute_blender_code", {
            "code": (
                "import bpy\n"
                "bpy.ops.mesh.primitive_ico_sphere_add()\n"
                "bpy.context.active_object.name = 'TestSphere'\n"
                "result = {'created': True}\n"
            ),
        })
        # Verify it exists in a separate call.
        data = self._test_tool("execute_blender_code", {
            "code": "import bpy\nresult = {'found': 'TestSphere' in bpy.data.objects}\n",
        })
        self.assertTrue(data["found"])


# -----------------------------------------------------------------------------
# Concrete test classes.

class TestBackgroundServer(_TestServerMixin, unittest.TestCase):
    """
    Run all tests against Blender in ``--background`` mode.
    """

    _background = True
    _port = _PORT_BACKGROUND


class TestForegroundServer(_TestServerMixin, unittest.TestCase):
    """
    Run all tests against Blender without ``--background`` (full GUI).
    """

    _background = False
    _port = _PORT_FOREGROUND


class TestInteractiveServer(_TestServerMixin, unittest.TestCase):
    """
    Run all tests against Blender in interactive mode (timer-based polling).
    """

    _background = False
    _interactive = True
    _port = _PORT_INTERACTIVE


BLENDER_VERSION_MIN = (5, 1)


def test_binaries_available() -> bool:
    """
    Check required binaries are available, print errors for any that are missing.
    """
    blender_bin = os.environ.get("BLENDER_BIN", "blender")
    blender_mcp = os.environ.get("BLENDER_MCP", "blender-mcp")
    ok = True
    if not shutil.which(blender_bin):
        print("ERROR: '{:s}' not found in PATH (set BLENDER_BIN)".format(blender_bin))
        ok = False
    if not shutil.which(blender_mcp):
        print("ERROR: '{:s}' not found in PATH (set BLENDER_MCP)".format(blender_mcp))
        ok = False
    return ok


def test_blender_version() -> bool:
    """
    Check the Blender version is at least ``BLENDER_VERSION_MIN``.
    """
    import re
    blender_bin = os.environ.get("BLENDER_BIN", "blender")
    result = subprocess.run(
        [blender_bin, "--version"],
        capture_output=True,
    )
    output = result.stdout.decode("utf-8", errors="replace")
    match = re.search(r"Blender\s+(\d+)\.(\d+)", output)
    if not match:
        print("ERROR: could not parse Blender version from: {:s}".format(output.strip()))
        return False
    version = (int(match.group(1)), int(match.group(2)))
    if version < BLENDER_VERSION_MIN:
        print("ERROR: Blender {:d}.{:d} found, {:d}.{:d} or newer required".format(
            *version, *BLENDER_VERSION_MIN,
        ))
        return False
    return True


if __name__ == "__main__":
    if not test_binaries_available():
        sys.exit(1)
    if not test_blender_version():
        sys.exit(1)
    unittest.main()
