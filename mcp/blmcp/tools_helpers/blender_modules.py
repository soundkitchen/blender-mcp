# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""
Register the modules in ``blmcp/blender_modules`` in Blender, for code that imports them.

The module source is sent to Blender in a separate request before the code itself,
so the line numbers in the code's traceback stay unchanged.
"""

__all__ = (
    "blender_modules_setup_code",
)

import functools
import hashlib
import os
import re

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


@functools.cache
def _module_setup_code(name: str) -> str:
    filepath = os.path.join(
        os.path.dirname(os.path.dirname(os.path.normpath(os.path.abspath(__file__)))),
        "blender_modules",
        name + ".py",
    )
    with open(filepath, "r", encoding="utf-8") as fh:
        source = fh.read()
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
    return _SETUP_TEMPLATE.format(name=name, digest=digest, source=source)


def blender_modules_setup_code(code: str) -> str | None:
    """
    Return code that registers the modules *code* mentions, or ``None`` when it uses none.
    """
    names = [name for name in _MODULE_NAMES if re.search(r"\b{:s}\b".format(name), code)]
    if not names:
        return None
    return "".join(_module_setup_code(name) for name in names) + "result = {}\n"
