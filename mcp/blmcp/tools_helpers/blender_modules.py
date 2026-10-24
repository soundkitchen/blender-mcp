# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""
Register the modules in ``blmcp/blender_modules`` in Blender, for code that imports them.

The module source is sent to Blender in a separate request before the code itself,
so the line numbers in the code's traceback stay unchanged.
"""

__all__ = (
    "blender_modules_register",
    "blender_modules_setup_code",
)

import functools
import hashlib
import os
import re

from blmcp.tools_helpers.connection import send_code

# Modules that code run in Blender can import, from `blmcp/blender_modules`.
_MODULE_NAMES = (
    "blmcp_subd",
)

# Registers a module in `sys.modules`, unless the same source is already registered.
# The source is added to `linecache` so tracebacks show the lines of the module.
_SETUP_TEMPLATE = (
    "import linecache\n"
    "import sys\n"
    "import types\n"
    "_name, _digest, _source = {name!r}, {digest!r}, {source!r}\n"
    "if getattr(sys.modules.get(_name), '__blmcp_digest__', None) != _digest:\n"
    "    _filename = '<' + _name + '>'\n"
    "    linecache.cache[_filename] = (len(_source), None, _source.splitlines(True), _filename)\n"
    "    _module = types.ModuleType(_name)\n"
    "    _module.__file__ = _filename\n"
    "    _module.__blmcp_digest__ = _digest\n"
    "    sys.modules[_name] = _module\n"
    "    try:\n"
    "        exec(compile(_source, _filename, 'exec'), _module.__dict__)\n"
    "    except BaseException:\n"
    "        del sys.modules[_name]\n"
    "        raise\n"
)

# Returns the digest of the registered source of each module (`None` when not registered).
_DIGESTS_TEMPLATE = (
    "import sys\n"
    "result = {{_name: getattr(sys.modules.get(_name), '__blmcp_digest__', None) for _name in {names!r}}}\n"
)


@functools.cache
def _module_source(name: str) -> tuple[str, str]:
    # Return the source of the module *name* and its digest.
    filepath = os.path.join(
        os.path.dirname(os.path.dirname(os.path.normpath(os.path.abspath(__file__)))),
        "blender_modules",
        name + ".py",
    )
    with open(filepath, "r", encoding="utf-8") as fh:
        source = fh.read()
    return source, hashlib.sha256(source.encode("utf-8")).hexdigest()


def _module_names(code: str) -> list[str]:
    return [name for name in _MODULE_NAMES if re.search(r"\b{:s}\b".format(name), code)]


def _setup_code(names: list[str]) -> str:
    parts = []
    for name in names:
        source, digest = _module_source(name)
        parts.append(_SETUP_TEMPLATE.format(name=name, digest=digest, source=source))
    return "".join(parts) + "result = {}\n"


def blender_modules_setup_code(code: str) -> str | None:
    """
    Return code that registers the modules *code* mentions, or ``None`` when it uses none.

    For a new Blender process (e.g. ``blender --background``), where nothing is registered.
    """
    names = _module_names(code)
    if not names:
        return None
    return _setup_code(names)


def blender_modules_register(code: str) -> dict[str, object] | None:
    """
    Register the modules *code* mentions in the connected Blender, when missing or outdated.

    Blender is asked for the registered versions first, the sources are only sent
    when they differ. Return the error response of a failed request, otherwise ``None``.
    """
    names = _module_names(code)
    if not names:
        return None
    response = send_code(_DIGESTS_TEMPLATE.format(names=names), strict_json=True)
    if response.get("status") != "ok":
        return response
    registered = response.get("result")
    if not isinstance(registered, dict):
        registered = {}
    outdated = [name for name in names if registered.get(name) != _module_source(name)[1]]
    if not outdated:
        return None
    response = send_code(_setup_code(outdated), strict_json=True)
    if response.get("status") != "ok":
        return response
    return None
