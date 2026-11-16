# Getting Started

This guide is for running the Anchorage Municipal Code MCP server yourself:
locally for development, or as your own deployment on AWS.

If you only want to use the hosted server from Claude, follow the
[Quick Start](QUICKSTART.md) instead.

The server uses the Model Context Protocol (MCP), which connects AI assistants
to external data. It exposes eight read-only tools for searching and reading
the Municipality of Anchorage municipal code published on eCode360.

## Prerequisites

- Python 3.11+ (the Lambda runs 3.14)
- eCode360 API credentials (a key and secret from General Code). The API is
  licensed; the key this project uses is scoped to the Anchorage library,
  `AN6998`.
- For deployment only: Terraform >= 1.0 and the AWS CLI, configured

## Run locally

### 1. Install

```bash
python -m venv venv
venv/bin/pip install -r requirements-dev.txt     # Windows: venv\Scripts\pip
```

`requirements-dev.txt` includes `requirements.txt`, which installs the
`opencontext-core` package that provides `core/` and `server/`.

### 2. Provide the credentials

The plugin reads `ECODE_API_KEY` and `ECODE_API_SECRET` from the environment.
Either export them, or copy `.ecode.env.example` to `.ecode.env` (gitignored),
fill it in, and load it:

```bash
set -a && . ./.ecode.env && set +a
```

Never put the key or secret in `config.yaml`. That file is packaged into the
Lambda zip, and it does not expand `${ENV_VAR}` references.

### 3. Start the server

`config.yaml` in the repo root is already this server's configuration, with
the `ecode` plugin enabled. Start the local server:

```bash
python scripts/local_server.py
```

The server runs at `http://localhost:8000/mcp`. It goes through the same HTTP
handler as the Lambda, so the Origin allowlist and protocol checks match
production.

### 4. Verify

```bash
curl -X POST http://localhost:8000/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"ecode__get_customer_info","arguments":{}}}'
```

See [Testing](TESTING.md) for more ways to test, including Claude Desktop and
MCP Inspector.

---

## Deploy your own copy

The hosted server is deployed from this repo to AWS Lambda behind API Gateway.
To deploy a copy:

1. Fork [codeforanchorage/anchorage-ecode-mcp](https://github.com/codeforanchorage/anchorage-ecode-mcp) and clone it.
2. Edit `config.yaml`: `customer_id`, `city_name`, the `aws` block, and the
   `instructions` text, which names Anchorage throughout. Keep
   `config-ecode.yaml` identical to it.
3. Edit `terraform/aws/prod.tfvars`: Lambda name, region, custom domain and
   rate limits.
4. Put the credentials in `terraform/aws/secrets.auto.tfvars` (gitignored;
   copy `secrets.auto.tfvars.example`). Terraform injects them as Lambda
   environment variables.
5. Deploy:

```bash
./scripts/deploy.sh --environment prod
```

The script validates the config, builds the Lambda package, shows the
Terraform plan and asks for confirmation before applying. See
[Deployment](DEPLOYMENT.md) for the details, including the Terraform
workspaces and the state bucket.

Then add the URL to Claude as a custom connector, as in the
[Quick Start](QUICKSTART.md). Get the URL with:

```bash
cd terraform/aws
terraform output -raw api_gateway_url
```

The output already ends in `/mcp`.

---

## Troubleshooting

| Issue | Solution |
|-------|----------|
| `ModuleNotFoundError: aiohttp` | Install `requirements-dev.txt`, not only `requirements.txt` |
| `ModuleNotFoundError: core` | `opencontext-core` is not installed; run the install step from the repo root |
| Tool errors that mention credentials | `ECODE_API_KEY` / `ECODE_API_SECRET` are missing or wrong |
| 403 from eCode360 for another city | The key is scoped to one library; other cities need their own key |
| Claude can't connect | Check the URL ends in `/mcp` and the connector is enabled in the chat |
| Lambda 5xx | Check the CloudWatch logs and validate the config |

---

## Next steps

- [Architecture](ARCHITECTURE.md): how the pieces fit
- [The eCode plugin](BUILT_IN_PLUGINS.md): tools, configuration and limits
- [Deployment](DEPLOYMENT.md): AWS details and monitoring
- [Testing](TESTING.md): local testing and the production smoke test
- [Security](SECURITY.md): threat model

## Support

[GitHub Issues](https://github.com/codeforanchorage/anchorage-ecode-mcp/issues)
