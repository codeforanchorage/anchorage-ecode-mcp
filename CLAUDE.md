# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Build & Development Commands

```bash
# Install dependencies (uv preferred, pip fallback)
uv sync                              # or: pip install -r requirements.txt

# Run local MCP server (no Lambda needed)
python3 scripts/local_server.py      # Serves on http://localhost:8000/mcp
# Single entry point. It routes through UniversalHTTPHandler, so local dev
# enforces the same Origin allowlist, MCP-Protocol-Version check and
# path/method validation as prod.

# Validate config
python3 -c "from core.validators import load_and_validate_config; load_and_validate_config('config.yaml')"

# Tests
uv run pytest tests/ -n auto                                    # All tests, parallel
uv run pytest tests/test_ckan_plugin.py -v                      # Single file
uv run pytest tests/test_ckan_plugin.py::TestClass::test_name -v  # Single test
uv run pytest tests/ --cov=core --cov=plugins --cov-report=term-missing  # With coverage (80% minimum)

# Linting (ruff)
uv run ruff check core/ plugins/ server/ tests/      # Check
uv run ruff check core/ plugins/ server/ tests/ --fix # Auto-fix
uv run ruff format core/ plugins/ server/ tests/      # Format

# Pre-commit hooks
pre-commit run --all-files

# Go client (requires Go 1.21+)
cd client && make build

# Deploy to AWS
./scripts/deploy.sh --environment staging
```

## Architecture

**Core rule: One Fork = One MCP Server.** Each deployment runs exactly ONE plugin. This is enforced at config validation time (`core/validators.py`) and at runtime (`PluginManager.load_plugins()`). To deploy multiple MCP servers, fork the repo per plugin.

**Request flow:**
```
Claude (stdio) → Go client (client/) or stdio_bridge.py → HTTP POST /mcp
  → Lambda (server/adapters/aws_lambda.py) or scripts/local_server.py
  → server/http_handler.py → core/mcp_server.py (JSON-RPC 2.0)
  → core/plugin_manager.py → Plugin → External API
```

**Key modules:**
- `core/interfaces.py` — Abstract bases: `MCPPlugin`, `DataPlugin`, plus `ToolDefinition`, `ToolResult`, `PluginType` enum
- `core/plugin_manager.py` — Discovers plugins by scanning `plugins/` and `custom_plugins/` for `plugin.py` files. Registers tools with `pluginname__toolname` prefix. Routes `tools/call` to the correct plugin.
- `core/mcp_server.py` — Handles MCP JSON-RPC methods. Dual-era: legacy revisions (≤2025-11-25) get `initialize`, `tools/list`, `tools/call`, `ping` on a session; the modern revision (2026-07-28) is stateless — every request carries `_meta` (protocol version, client info/capabilities), `server/discover` replaces `initialize`, results carry `resultType` and cache hints, and the Streamable HTTP headers (`Mcp-Method`, `Mcp-Name`, `MCP-Protocol-Version`) must mirror the body or the request is a 400/-32020
- `core/validators.py` — Loads config from `config.yaml` (local) or `OPENCONTEXT_CONFIG` env var (Lambda). Enforces single-plugin rule.
- `server/adapters/aws_lambda.py` — the ONLY AWS Lambda entry point (handler: `server.adapters.aws_lambda.lambda_handler`), and the only thing Terraform points at.
- `server/http_handler.py` — Cloud-agnostic HTTP handler shared by Lambda and local server
- `stdio_bridge.py` — Python stdio-to-HTTP bridge for connecting Claude Desktop/Code to the local server (alternative to Go client)

**Built-in plugins** (`plugins/`): `ckan`, `arcgis`, `socrata` — each implements `DataPlugin` with `search_datasets`, `get_dataset`, `query_data`. Custom plugins go in `custom_plugins/` and are auto-discovered.

## Plugin Development

New plugins must implement `MCPPlugin` (or `DataPlugin` for data sources). Place in `custom_plugins/<name>/plugin.py`. The class must define `plugin_name`, `plugin_type`, `plugin_version` and implement `initialize()`, `shutdown()`, `get_tools()`, `execute_tool()`, `health_check()`. Tool names are auto-prefixed — return bare names from `get_tools()`.

## Configuration

Copy `config-example.yaml` to `config.yaml`. Enable exactly one plugin. Config supports `${ENV_VAR}` substitution. For Lambda, config is serialized to the `OPENCONTEXT_CONFIG` env var by Terraform.

Two AWS sizing values are read from `config.yaml` in preference to `terraform/aws/prod.tfvars` — `lambda_memory` and `lambda_timeout` (see the `locals` block in `terraform/aws/main.tf`). Editing them in the tfvars alone silently does nothing. `lambda_name` uses the opposite precedence, so check `main.tf` per variable rather than assuming.

**`terraform/aws/config.yaml` is a BUILD ARTIFACT, not a source file.** `scripts/deploy.sh` (line 307) copies the repo-root `config.yaml` over it during packaging, and it is gitignored. Two consequences: edits made directly to it vanish on the next deploy, and a bare `terraform plan` run inside `terraform/aws/` (without the packaging steps) reads the STALE copy — so a config change shows up as nothing but a code-hash diff, and a "timeout fix" can appear to apply while changing nothing. Always go through `./scripts/deploy.sh -e prod`, which repackages before planning.

**Timeout ladder** — each layer must sit under the one above it:

| Layer | Value | Why |
|---|---|---|
| API Gateway integration | 29s | hard REST limit, not adjustable |
| Lambda (`lambda_timeout`) | 28s | self-terminates before the gateway gives up |
| Plugin HTTP (`plugins.ecode.timeout`) | 20s | a hung upstream returns a readable tool error instead of the Lambda being killed mid-flight |

`tests/test_deployment_config.py` pins this ordering so it cannot silently invert again.

## CI

GitHub Actions (`.github/workflows/ci.yml`) runs ruff lint/format, pip-audit, pytest with coverage, and Go tests on push to main/develop and on PRs.
