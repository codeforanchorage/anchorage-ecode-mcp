# Stdio client

A small Go program that lets an MCP client which only speaks stdio (for
example Claude Desktop through its config file) talk to this server over
HTTP. It is optional: the usual way to connect is a custom connector, which
needs nothing installed. See the [Quick Start](../docs/QUICKSTART.md).

The client comes from upstream OpenContext and keeps its names
(`opencontext-client`, `OPENCONTEXT_*`).

## Build

There are no prebuilt binaries for this repo. Build from source with Go 1.21
or newer:

```bash
cd client
make build        # this platform
make build-all    # macOS (Intel/ARM), Linux (amd64/arm64), Windows (amd64)
```

## Use

Give it the server address. `/mcp` is added if the address does not already
end with it.

```bash
./opencontext-client https://anchorage-ecode.codeforanchorage.org/mcp

# or
export OPENCONTEXT_LAMBDA_URL=https://anchorage-ecode.codeforanchorage.org/mcp
./opencontext-client
```

For your own deployment, use the address printed by
`terraform output -raw api_gateway_url`; for a local server,
`http://localhost:8000/mcp`.

### Claude Desktop config file

```json
{
  "mcpServers": {
    "anchorage-municipal-code": {
      "command": "/path/to/opencontext-client",
      "args": ["https://anchorage-ecode.codeforanchorage.org/mcp"]
    }
  }
}
```

## Environment variables

- `OPENCONTEXT_LAMBDA_URL`: the server address, if not given as an argument.
  The name is historical; any address of this server works.
- `OPENCONTEXT_TIMEOUT`: HTTP request timeout in seconds (default 30)

## How it works

The client reads MCP JSON-RPC messages from stdin, sends each to the server's
`/mcp` endpoint with an HTTP POST, and writes the responses to stdout.
`stdio_bridge.py` in the repo root does the same job in Python.
