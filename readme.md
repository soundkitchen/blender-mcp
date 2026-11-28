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
The Blender add-on must be installed separately; `blender-mcp install-addon`
installs it from the latest GitHub Release (see "Installing the add-on" below).

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

### Installing the add-on

Install and enable the add-on from the latest
[GitHub Release](https://github.com/soundkitchen/blender-mcp/releases) with:

```sh
uvx --from "git+https://github.com/soundkitchen/blender-mcp#subdirectory=mcp" blender-mcp install-addon
```

This downloads the release's `mcp-<version>.zip` and installs it with
`blender --command extension install-file -r user_default --enable`.
Blender is looked up from `--blender PATH`, then `BLENDER_PATH`, then `blender`
in `PATH`, then `/Applications/Blender.app` on macOS. Restart Blender if it was
running. Running the command again reinstalls the latest release.

Alternatively, download `mcp-<version>.zip` from the release yourself and drag
and drop it onto a Blender window (or use *Edit → Preferences → Get Extensions
→ Install from Disk*), then enable the add-on.

### Building from this repository

Build an installable zip with:

```sh
make build_addon
```

Blender is looked up from `BLENDER_BIN`, then `blender` in `PATH`, then
`/Applications/Blender.app` on macOS. Set `BLENDER_BIN` to use another binary.

This writes `build/mcp-<version>.zip`. Install it with
`uvx --from ./mcp blender-mcp install-addon --zip build/mcp-<version>.zip`,
or drag and drop it onto a Blender window as above.

This fork keeps the upstream extension id (`mcp`) and version. If the add-on
from the Blender Lab extensions repository is already installed, uninstall it
first so it is clear which one is running.

### Releasing the add-on

Prebuilt zips are attached to the
[GitHub Releases](https://github.com/soundkitchen/blender-mcp/releases) of this
fork. Install the attached `mcp-<version>.zip`; the "Source code" archives that
GitHub adds to every release are not installable add-ons.

Release tags are named `v<version>-fork.<N>`, where `<version>` is the add-on
version from `blender_manifest.toml` (kept equal to upstream) and `<N>` counts
up from 1 for each release of that version. The suffix keeps the tags apart
from the upstream `v<version>` tags.

To publish a release (maintainers), run on an up-to-date, clean `main`:

```sh
make release_addon
```

This builds the zip with `make build_addon` and creates the release with
`gh release create`, which also creates the next `v<version>-fork.<N>` tag on
the current commit when the release is published (the tag is then fetched
locally). It requires the [GitHub CLI](https://cli.github.com/) to be
authenticated. If the upload fails, no tag is created, but a draft release may
be left behind; delete it with `gh release delete <tag>` before trying again.

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
   - ``guides/`` contains modeling guides in Markdown, returned by
     ``get_modeling_guide``. Their example code is run by the tests.

``mcp/blmcp/tools/``
   Each tool is a single module, auto-discovered at startup.
   Modules ending in ``_toolcode`` contain code that runs inside
   Blender (sent to the add-on for execution) and are skipped during
   discovery.

``mcp/blmcp/tools_helpers/``
   Shared utilities used by tools. Tools should not import from each
   other; shared logic lives here instead.

``mcp/blmcp/blender_modules/``
   Modules that run inside Blender, for code sent with
   ``execute_blender_code`` (e.g. ``blmcp_subd``, ``bmesh`` helpers for
   subdivision cage modeling). When the code mentions a module, the server
   first asks Blender which version of it is registered in ``sys.modules``
   and, when it is missing or its source changed, registers it with a separate
   request, so the code can ``import`` it.
   The server itself never imports them.


### Tools

See [readme_tools.rst](readme_tools.rst) for the tools the MCP server exposes.

## Development

The repository root holds a development environment (`pyproject.toml`), not
distributed, that installs `mcp/` together with the static checking tools at
the versions pinned in `uv.lock`. Run the checks and the unit tests with
[uv](https://docs.astral.sh/uv/):

```sh
make check_all PYTHON="uv run python"
make test PYTHON="uv run python"
```

`mcp/requirements_dev.txt` lists the same tools (without versions) for setups
that do not use uv.
