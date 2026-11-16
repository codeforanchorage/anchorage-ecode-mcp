# Architecture

## Overview

This repo is a fork of OpenContext, a plugin-based MCP framework. A deployment
runs **one** server with **one** plugin. This one runs the `ecode` plugin and
serves the Municipality of Anchorage municipal code from eCode360.

## One Fork = One Server

**Enforcement:**
- `scripts/deploy.sh` validates the config before deployment
- the plugin manager refuses to start if more than one plugin is enabled

**Multiple servers:** fork again per plugin and deploy each separately.

## Components

The MCP server core is a dependency, not code in this repo. `core/` and
`server/` come from the
[`opencontext-core`](https://github.com/codeforanchorage/opencontext-core)
package, pinned to a tag in `requirements.txt` and `pyproject.toml`. Do not
re-create those directories here: a local copy would shadow the package.

```
opencontext-core (installed package)
├── core/
│   ├── interfaces.py       # MCPPlugin, DataPlugin, ToolDefinition, ToolResult
│   ├── plugin_manager.py   # Discovery, loading, routing
│   ├── mcp_server.py       # MCP JSON-RPC handler
│   └── validators.py       # Config loading and validation
└── server/
    ├── adapters/aws_lambda.py   # Lambda entry point
    └── http_handler.py          # HTTP handling shared by Lambda and local

this repo
├── plugins/ecode/          # The eCode360 plugin
│   ├── plugin.py
│   └── config_schema.py
├── custom_plugins/         # Extra plugins, auto-discovered
│   └── template/plugin_template.py
├── config.yaml             # This server's configuration
├── scripts/                # deploy.sh, local_server.py, smoke_prod.py
├── terraform/aws/          # Lambda, API Gateway, WAF, alarms
├── client/                 # Go stdio-to-HTTP client (optional)
├── stdio_bridge.py         # Python stdio-to-HTTP bridge (optional)
└── tests/
```

## Request flow

```
Claude (connector, or stdio via the Go client / stdio_bridge.py)
    → HTTPS POST /mcp
    → WAF → API Gateway (custom domain)
    → Lambda: server.adapters.aws_lambda.lambda_handler
        (locally: scripts/local_server.py)
    → server/http_handler.py      Origin, method, path and header checks
    → core/mcp_server.py          JSON-RPC 2.0, MCP methods
    → core/plugin_manager.py      routes ecode__<tool> to the plugin
    → plugins/ecode/plugin.py
    → eCode360 API (api.ecode360.com)
```

`read_attachment` makes one further kind of request: it downloads a PDF from
the public eCode360 website and extracts its text with `pypdf`.

## Protocol

The server speaks two generations of MCP over Streamable HTTP:

- **Legacy revisions (2025-11-25 and earlier):** `initialize`, `tools/list`,
  `tools/call` and `ping` on a session.
- **Modern revision (2026-07-28):** stateless. Every request carries `_meta`
  with the protocol version and client info, `server/discover` replaces
  `initialize`, and the `Mcp-Method`, `Mcp-Name` and `MCP-Protocol-Version`
  headers must mirror the body.

Tools declare an `outputSchema` and return `structuredContent` alongside the
text block. The tests validate real tool output against the declared schema.

## The plugin

See [The eCode plugin](BUILT_IN_PLUGINS.md) for the tools, configuration and
limits. The main design points:

- **Read-only.** Every upstream call is a `GET`.
- **No call at startup.** `initialize()` does not contact eCode360. The Lambda
  adapter starts and shuts down the plugin on every invocation, so a startup
  probe would run on every request.
- **Process-level cache.** The table of contents and library metadata are
  cached at module level, so they survive between invocations in a warm
  Lambda. Section text is not cached.
- **References resolved from the outline.** Chapter and title references are
  looked up in the cached table of contents; three-part section numbers are
  resolved by search.
- **Credentials from the environment.** The plugin reads `ECODE_API_KEY` and
  `ECODE_API_SECRET`. They are never in `config.yaml`.

## Plugin interface

```python
class MCPPlugin(ABC):
    plugin_name: str
    plugin_type: PluginType
    plugin_version: str

    async def initialize() -> bool
    async def shutdown() -> None
    def get_tools() -> List[ToolDefinition]
    async def execute_tool(tool_name, arguments) -> ToolResult
    async def health_check() -> bool
```

Tool names are returned bare from `get_tools()`; the plugin manager adds the
`<plugin>__` prefix. To write another plugin, see
[Custom Plugins](CUSTOM_PLUGINS.md).

## Endpoint

There is one public endpoint: API Gateway, fronted by a WAF, on the custom
domain `anchorage-ecode.codeforanchorage.org`. There is no Lambda Function URL.

## Configuration

A single `config.yaml`, read locally from the repo root and passed to Lambda
through the `OPENCONTEXT_CONFIG` environment variable. It is validated at
deploy time and at runtime. It does not expand `${ENV_VAR}` references.

`lambda_memory` and `lambda_timeout` in `config.yaml` take precedence over the
same values in `terraform/aws/prod.tfvars`.

## Timeouts

Each layer must time out before the one above it:

| Layer | Value |
| --- | --- |
| API Gateway integration | 29 s (hard limit) |
| Lambda | 28 s |
| Plugin HTTP request | 20 s |

`tests/test_deployment_config.py` pins this ordering.

## Security and scaling

- **Rate limiting:** WAF per-IP limit, API Gateway throttle and daily quota,
  and a cap on Lambda concurrency. See [Deployment](DEPLOYMENT.md) for values.
- **Stateless:** no shared state between requests beyond the in-process cache.
- **Logging:** structured JSON to CloudWatch, with request IDs.
- **Threat model:** see [Security](SECURITY.md).
