# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-3.0-or-later

# pylint: disable=C0114  # See tool doc-string.

__all__ = (
    "register",
)

import os
from typing import Literal

from blmcp.tools_helpers import data_dir
from mcp.server.mcpserver import MCPServer  # pylint: disable=import-error,no-name-in-module
from mcp.types import ToolAnnotations  # pylint: disable=import-error,no-name-in-module

# Each topic is a Markdown file in `data/guides/`.
GuideTopic = Literal[
    "character",
]


def register(mcp: MCPServer) -> None:
    @mcp.tool(
        annotations=ToolAnnotations(
            title="Get Modeling Guide",
            read_only_hint=True,
        )
    )
    def get_modeling_guide(topic: GuideTopic) -> dict[str, object]:
        """
        Return a step-by-step modeling guide for *topic*, with pitfalls, checks and tested example code.

        Read it before modeling, it doesn't access Blender.
        ``character``: a character base mesh as a subdivision cage
        (box modeling a half body with Mirror and Subdivision Surface modifiers).
        """
        filepath = os.path.join(data_dir(), "guides", topic + ".md")
        with open(filepath, encoding="utf-8") as fh:
            return {"topic": topic, "guide": fh.read()}
