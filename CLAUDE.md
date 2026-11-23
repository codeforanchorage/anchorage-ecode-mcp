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
uv run pytest tests/test_ecode_plugin.py -v                      # Single file
uv run pytest tests/test_ecode_plugin.py::TestClass::test_name -v  # Single test
uv run pytest tests/ --cov=plugins --cov-report=term-missing  # With coverage (80% minimum)

# Linting (ruff)
uv run ruff check plugins/ tests/      # Check
uv run ruff check plugins/ tests/ --fix # Auto-fix
uv run ruff format plugins/ tests/      # Format

# Pre-commit hooks
pre-commit run --all-files

# Go client (requires Go 1.21+)
cd client && make build

# Deploy to AWS
./scripts/deploy.sh --environment prod   # the only environment defined
```

## Architecture

**Core rule: One Fork = One MCP Server.** Each deployment runs exactly ONE plugin. This is enforced at config validation time (`core/validators.py`) and at runtime (`PluginManager.load_plugins()`). To deploy multiple MCP servers, fork the repo per plugin.

**The MCP server core is a dependency, not code in this repo.** `core/` and `server/` come from the [`opencontext-core`](https://github.com/codeforanchorage/opencontext-core) package, pinned to a tag in `requirements.txt` and `pyproject.toml` (keep the two in step; `tests/test_core_dependency.py` checks). To change protocol or transport behaviour, change it there, tag a release, and move the pin here. Do not re-create `core/` or `server/` in this repo: a local copy shadows the package. `scripts/deploy.sh` installs the package next to `plugins/` in the Lambda zip, so the handler string is unchanged. To work on both at once: `pip install -e ../opencontext-core`. Run commands from the repo root, which is where the installed core looks for `plugins/`.

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

**Plugins** (`plugins/`): `ecode`. This fork keeps only what it runs or imports; the generic `ckan`, `arcgis` and `socrata` plugins of upstream OpenContext that it did not use were deleted (they remain in [CityOfBoston/OpenContext](https://github.com/CityOfBoston/OpenContext)). A plugin implements `MCPPlugin` (or `DataPlugin` with `search_datasets`, `get_dataset`, `query_data`). Custom plugins go in `custom_plugins/` and are auto-discovered.

## Plugin Development

New plugins must implement `MCPPlugin` (or `DataPlugin` for data sources). Place in `custom_plugins/<name>/plugin.py`. The class must define `plugin_name`, `plugin_type`, `plugin_version` and implement `initialize()`, `shutdown()`, `get_tools()`, `execute_tool()`, `health_check()`. Tool names are auto-prefixed — return bare names from `get_tools()`.

## Configuration

Copy `config-example.yaml` to `config.yaml`. Enable exactly one plugin. Config does **NOT** support `${ENV_VAR}` substitution: `core/validators.py::load_and_validate_config` is a plain `yaml.safe_load`, so a `${VAR}` (as shown in `config-example.yaml`) reaches the plugin as that literal string. Read secrets from `os.environ` in the plugin's `initialize()` instead — the eCode plugin reads `ECODE_API_KEY` / `ECODE_API_SECRET` that way. Never put a secret in `config.yaml`: it is baked into the Lambda zip. For Lambda, config is serialized to the `OPENCONTEXT_CONFIG` env var by Terraform.

Two AWS sizing values are read from `config.yaml` in preference to `terraform/aws/prod.tfvars` — `lambda_memory` and `lambda_timeout` (see the `locals` block in `terraform/aws/main.tf`). Editing them in the tfvars alone silently does nothing. `lambda_name` uses the opposite precedence, so check `main.tf` per variable rather than assuming.

**`terraform/aws/config.yaml` is a BUILD ARTIFACT, not a source file.** `scripts/deploy.sh` copies the repo-root `config.yaml` over it during packaging, and it is gitignored. Two consequences: edits made directly to it vanish on the next deploy, and a bare `terraform plan` run inside `terraform/aws/` (without the packaging steps) reads the STALE copy — so a config change shows up as nothing but a code-hash diff, and a "timeout fix" can appear to apply while changing nothing. Always go through `./scripts/deploy.sh -e prod`, which repackages before planning.

**Timeout ladder** — each layer must sit under the one above it:

| Layer | Value | Why |
|---|---|---|
| API Gateway integration | 29s | hard REST limit, not adjustable |
| Lambda (`lambda_timeout`) | 28s | self-terminates before the gateway gives up |
| Plugin HTTP (`plugins.ecode.timeout`) | 20s | a hung upstream returns a readable tool error instead of the Lambda being killed mid-flight |

`tests/test_deployment_config.py` pins this ordering so it cannot silently invert again.

**Deploys are built from a commit and from `uv.lock`.** `scripts/deploy.sh` refuses a dirty working tree (`--allow-dirty` overrides, for emergencies), installs the exact versions in `uv.lock` (it needs `uv`, on `PATH` or as `python -m uv`; without it, it warns and falls back to `pip` and the lower bounds in `requirements.txt`), and writes `BUILD_INFO.json` (commit, dirty, locked) into the package. So commit before deploying, and to move a library version: `uv lock --upgrade-package <name>`, run the tests, commit `uv.lock`. Moving the `opencontext-core` pin means `requirements.txt`, `pyproject.toml` and `uv lock`.

## Privacy of usage data

The production logs hold client IP addresses and what users searched for. Never put a user's query, the topic they were researching, who the users are, or per-day usage counts into this repo: not in code comments, test names or docstrings, docs, commit messages, issues or PRs. Describe the behaviour and the fix ("a quoted wildcard returned nothing"), not who hit it or when. Examples in tests and tool descriptions should be ones written for the purpose.

## CI

GitHub Actions (`.github/workflows/ci.yml`) runs on push to main and on PRs: ruff lint (no format check), config validation and pytest (serial, then parallel with the 80% coverage floor) on Python 3.11 and 3.14, `pip-audit` on the runtime versions in `uv.lock` (what ships in the Lambda zip; the job also fails if the lock has drifted from `pyproject.toml`), and `go vet` / `go test` for `client/`. Deploys stay manual.
