# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-3.0-or-later

# pylint: disable=C0114  # See tool doc-string.

__all__ = (
    "register",
)

from blmcp.tools_helpers.rst_doc_search import (
    SEARCH_TOOL_DESCRIPTION,
    search,
    with_doc,
)
from mcp.server.mcpserver import MCPServer  # pylint: disable=import-error,no-name-in-module
from mcp.types import ToolAnnotations  # pylint: disable=import-error,no-name-in-module


def register(mcp: MCPServer) -> None:
    @mcp.tool(
        annotations=ToolAnnotations(
            title="Search API Docs",
            read_only_hint=True,
        )
    )
    @with_doc(SEARCH_TOOL_DESCRIPTION.format(scope_name="Python API reference"))
    def search_api_docs(
        query: str,
        max_results: int = 20,
        context: int = 0,
        index: int | None = None,
    ) -> dict[str, object]:
        """
        Full-text search over the bundled Blender Python API reference.
        """
        return search(
            query=query,
            scope="api",
            max_results=max_results,
            context=context,
            index=index,
        )
