# Testing

## Unit tests

```bash
pip install -r requirements-dev.txt
pytest tests/ -n auto                       # all tests, in parallel
pytest tests/test_ecode_plugin.py -v        # one file
pytest tests/ --cov=plugins --cov-report=term-missing
```

The tests do not call eCode360 and need no credentials. Coverage of
`plugins/` must stay at 80% or more.

Lint with ruff, on the 0.15 line that pre-commit and CI use:

```bash
ruff check plugins/ tests/
```

CI (`.github/workflows/ci.yml`) runs the lint, the tests on Python 3.11 and
3.14, a dependency audit and the Go client tests on every push to `main` and on
pull requests.

---

## Testing a local server

1. Supply the eCode360 credentials (see [Getting Started](GETTING_STARTED.md))
2. Start the server: `python scripts/local_server.py`

It runs at `http://localhost:8000/mcp`. Keep it running while you test.

### Method 1: Terminal (cURL)

**Ping:**

```bash
curl -X POST http://localhost:8000/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"ping"}'
```

**List tools:**

```bash
curl -X POST http://localhost:8000/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list"}'
```

**Call a tool:**

```bash
curl -X POST http://localhost:8000/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"ecode__search_code","arguments":{"query":"\"snow\" AND \"sidewalk\""}}}'
```

For the full sequence (initialize, list tools, call a tool), run:

```bash
./scripts/test_streamable_http.sh
```

It needs `jq`.

### Method 2: Claude Desktop

1. Add a custom connector with the URL `http://localhost:8000/mcp`
2. Enable it in a conversation ("+" → Connectors → toggle on)
3. Ask a question about the Anchorage code

Localhost only works with Claude Desktop, because the connection runs from
your machine. Claude.ai on the web cannot reach it.

### Method 3: MCP Inspector

1. With the server running, run `npx @modelcontextprotocol/inspector`
2. In the Inspector, select **streamable-http** as the transport
3. Enter the URL `http://localhost:8000/mcp`
4. Use the Tools tab to list and call tools

---

## Quick checks

**Config validation:**

```bash
python -c "
from core.validators import load_and_validate_config
config = load_and_validate_config('config.yaml')
print('Config valid:', config['server_name'])
"
```

---

## Testing production

The smoke test exercises the deployed server end to end: transport rules, the
protocol handshake, the tools, and the refusals. Run it after each deploy:

```bash
python scripts/smoke_prod.py                              # production
python scripts/smoke_prod.py http://localhost:8000/mcp    # a local server
```

It prints one line per check and a summary, and makes real calls to eCode360.

A single request by hand:

```bash
curl -X POST https://anchorage-ecode.codeforanchorage.org/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```
