# Scripts Directory

This directory contains utility scripts for OpenContext MCP server.

## Scripts

### `deploy.sh`

Deployment script that validates configuration and deploys the MCP server to AWS Lambda.

**Usage:**
```bash
./scripts/deploy.sh --environment <staging|prod> [--tfworkspace <name>] [--allow-dirty]
```

**Options:**

| Flag | Short | Required | Description |
|------|-------|----------|-------------|
| `--environment` | `-e` | Yes | `staging` or `prod` |
| `--tfworkspace` | `-w` | No | Terraform workspace name (default: `anchorage-ecode-staging` or `anchorage-ecode-prod`) |
| `--allow-dirty` | | No | Package the working tree even with uncommitted changes (emergencies only; the build is stamped as dirty) |
| `--help` | `-h` | No | Show help |

**Examples:**
```bash
./scripts/deploy.sh --environment staging
./scripts/deploy.sh -e prod
./scripts/deploy.sh --environment staging --tfworkspace my-workspace
./scripts/deploy.sh -e prod -w anchorage-ecode-prod-v2
```

**What it does:**
- Validates that exactly ONE plugin is enabled
- Packages the code for Lambda deployment
- Selects (or creates) the specified Terraform workspace
- Deploys to AWS using Terraform
- Shows the plan, asks for confirmation, applies it, and prints the API Gateway URL

**Requirements:**
- Python 3.11+
- AWS CLI configured
- Terraform installed
- Valid `config.yaml` in project root

### `local_server.py`

Local development server for testing the MCP server without deploying to Lambda.

**Usage:**
```bash
python3 scripts/local_server.py
```

**What it does:**
- Starts a local HTTP server on `http://localhost:8000/mcp`
- Supports Streamable HTTP transport with session management
- Provides detailed logging for debugging
- Uses the same MCP server logic as Lambda deployment

**Requirements:**
- Python 3.11+
- `aiohttp` package (in `requirements-dev.txt`)
- `ECODE_API_KEY` and `ECODE_API_SECRET` in the environment
- Valid `config.yaml` in project root

**Testing:**
```bash
# Test with curl
curl -X POST http://localhost:8000/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"ping"}'

# Or use the test script
./scripts/test_streamable_http.sh
```

### `test_streamable_http.sh`

Test script for Streamable HTTP transport. Tests the full MCP lifecycle.

**Usage:**
```bash
./scripts/test_streamable_http.sh [BASE_URL]
```

**Default:** `http://localhost:8000/mcp`

**What it tests:**
1. Initialize connection and extract session ID
2. List available tools
3. Call a tool (`ecode__search_code`)

**Requirements:**
- `jq` installed (`brew install jq` on macOS)
- MCP server running (local or deployed)

**Example:**
```bash
# Test local server
./scripts/test_streamable_http.sh

# Test the deployed server
./scripts/test_streamable_http.sh https://anchorage-ecode.codeforanchorage.org/mcp
```

## Notes

- All scripts should be run from the project root directory
- Scripts automatically handle path resolution relative to their location
- Make sure scripts are executable: `chmod +x scripts/*.sh`
