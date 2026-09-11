# Blender MCP

> **This repository is a fork.**
>
> The upstream project is Blender Lab's
> [projects.blender.org/lab/blender_mcp](https://projects.blender.org/lab/blender_mcp).
> [soundkitchen/blender-mcp](https://github.com/soundkitchen/blender-mcp) is
> derived from it and maintained independently.
>
> Main differences from upstream:
>
> - Dependencies (such as the `mcp` Python SDK) are updated to recent versions as needed
>   (upstream pins `mcp<2`; this fork has migrated to the `mcp>=2` API).
> - The server can be run directly with `uvx`.
>
> For the upstream documentation see
> [blender.org/lab/mcp-server](https://www.blender.org/lab/mcp-server/).
> Upstream changes are merged in regularly.

## Running with uvx

The Python package lives under `mcp/`, so pass the subdirectory via `--from`.
The Blender add-on (see "Blender Add-on" below) must be installed separately.

```sh
uvx --from "git+https://github.com/soundkitchen/blender-mcp#subdirectory=mcp" blender-mcp
```

Example MCP client configuration (Claude Desktop, Claude Code and others using the `mcpServers` format):

```json
{
  "mcpServers": {
    "blender": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/soundkitchen/blender-mcp#subdirectory=mcp",
        "blender-mcp"
      ]
    }
  }
}
```

To use the HTTP transport (e.g. for the llama.cpp web UI):

```sh
uvx --from "git+https://github.com/soundkitchen/blender-mcp#subdirectory=mcp" blender-mcp --transport http --port 8000
```

To try a local checkout, use `uvx --from ./mcp blender-mcp`.

## Overview

A lightweight MCP (Model Context Protocol) server for Blender.
It offers a natural language interface with Blender's Python API,
improving access to documentation, and allowing users to explore
and understand complex setups.

Read the documentation at [blender.org/lab/mcp-server](https://www.blender.org/lab/mcp-server/)

----

The project is deliberately small, maintainable, and does no more than
necessary. It has two components that communicate over a TCP socket:

- A **Blender add-on** that runs inside Blender and executes requests.
- An **MCP server** that runs as a separate process, launched by the
  MCP client (e.g. [Llama.cpp](https://projects.blender.org/lab/blender_mcp/wiki/Llama.cpp)).

The data flow is:
```
MCP Client  ⇐ MCP/stdio ⇒  blender-mcp  ⇐ TCP socket ⇒  Blender Add-on
```


## Blender Add-on

Located in ``addon/blender_mcp_addon/``.

A Blender extension that allows the MCP server to communicate with a
running Blender instance. It must be installed and enabled for any of
the MCP tools to work.

The add-on provides a preferences panel for configuring the host, port,
and an optional auto-start setting.

### Functionality Overview

Note that this is intended to be a fairly minimal add-on.

Connectivity
   - Auto-start (optional), is non-blocking any issues can be viewed from the preferences.
   - Configurable polling intervals (active and idle rates) from preferences to avoid excessive overhead.
   - Client timeout protection - stalled connections are evicted.
   - Start/stop operators accessible from the preferences panel.
   - Deferred responses are supported only by the interactive add-on server;
     background mode requires requests to complete synchronously and rejects deferred results.




## MCP Server

Located in ``mcp/blmcp/``, installed as a Python package with the
entry point ``blender-mcp``.

An MCP client launches this process and communicates with it over
stdio. The server connects to the add-on's TCP socket to relay
requests to Blender.

``mcp/blmcp/data/``
   Data files bundled with the package.

   - ``prompts.yml`` provides instructions sent to the LLM at
     connection time.
   - ``api/`` contains Blender Python API reference in RST format.
   - ``manual/`` contains Blender user manual excerpts in RST format.

``mcp/blmcp/tools/``
   Each tool is a single module, auto-discovered at startup.
   Modules ending in ``_toolcode`` contain code that runs inside
   Blender (sent to the add-on for execution) and are skipped during
   discovery.

``mcp/blmcp/tools_helpers/``
   Shared utilities used by tools. Tools should not import from each
   other; shared logic lives here instead.


### Tools

See [readme_tools.rst](readme_tools.rst) for the tools the MCP server exposes.
