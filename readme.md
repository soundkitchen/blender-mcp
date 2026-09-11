# Blender MCP

> **このリポジトリは fork です。**
>
> 本家は Blender Lab の
> [projects.blender.org/lab/blender_mcp](https://projects.blender.org/lab/blender_mcp) です。
> [soundkitchen/blender-mcp](https://github.com/soundkitchen/blender-mcp) は
> そこから派生し、独自にメンテナンスしています。
>
> 本家との主な違い:
>
> - 依存パッケージ(`mcp` Python SDK など)を必要に応じて最新版へ更新する
>   (本家は `mcp<2` に固定。この fork は `mcp>=2` の API に移行済み)
> - `uvx` で直接実行できるようにする
>
> 本家のドキュメントは
> [blender.org/lab/mcp-server](https://www.blender.org/lab/mcp-server/) を参照してください。
> 上流の変更は随時取り込みます。

## uvx で実行する

Python パッケージは `mcp/` 以下にあるため、`--from` で subdirectory を指定します。
Blender 側には別途 add-on(下記「Blender Add-on」参照)のインストールが必要です。

```sh
uvx --from "git+https://github.com/soundkitchen/blender-mcp#subdirectory=mcp" blender-mcp
```

MCP クライアントの設定例(Claude Desktop / Claude Code など、`mcpServers` 形式):

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

HTTP transport で起動する場合(llama.cpp の Web UI など向け):

```sh
uvx --from "git+https://github.com/soundkitchen/blender-mcp#subdirectory=mcp" blender-mcp --transport http --port 8000
```

ローカルの checkout から試す場合は `uvx --from ./mcp blender-mcp` と指定します。

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
