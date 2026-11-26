# Getting Started

This guide is for running the Anchorage Municipal Code MCP server yourself:
locally for development, as your own deployment on AWS, or for another
municipality whose code is published on eCode360.

If you only want to use the hosted server from Claude, follow the
[Quick Start](QUICKSTART.md) instead.

The server uses the Model Context Protocol (MCP), which connects AI assistants
to external data. It exposes eleven read-only tools for searching and reading
a municipal code published on eCode360.

> **Coming from OpenContext?** This repo started as a fork of OpenContext and
> no longer follows its setup. There is no `cp config-example.yaml
> config.yaml` step, no plugin to enable, and the server core is an installed
> package rather than code in the repo. Follow this page, not the upstream
> README. The [README](../README.md#how-this-relates-to-opencontext) lists the
> differences.

## What you need

| | For |
| --- | --- |
| eCode360 API key and secret | Everything. The API is licensed by General Code, and a key is scoped to the libraries it was granted. The hosted server's key is not in this repo and only covers Anchorage (`AN6998`). Ask General Code for a key for your own library. |
| Your library's customer ID | Serving another city. It is the code in the library's address: `https://ecode360.com/AN6998` is Anchorage. |
| Python 3.11 or newer | Running and testing. The Lambda runs 3.14; the deploy script fetches 3.14 packages whatever your local version is. |
| `git`, and `bash` | Everything. On Windows use Git Bash. |
| `uv` | Deploying. `python -m pip install uv` is enough. |
| Terraform >= 1.0 and the AWS CLI, configured | Deploying. |
| A domain name you can add DNS records to | Optional. Without one the server is served on the API Gateway address. |

## Part 1: Run it locally

### 1. Clone and install

```bash
git clone https://github.com/codeforanchorage/anchorage-ecode-mcp.git
cd anchorage-ecode-mcp
python -m venv venv
venv/bin/pip install -r requirements-dev.txt     # Windows: venv\Scripts\pip
```

`requirements-dev.txt` includes `requirements.txt`, which installs the
`opencontext-core` package from GitHub at a pinned tag. That package provides
the `core` and `server` modules. Do not create `core/` or `server/`
directories in this repo: a local copy would shadow the package.

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
the `ecode` plugin enabled. Run from the repo root, which is where the
installed core looks for `plugins/`:

```bash
venv/bin/python scripts/local_server.py
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

The answer names the library your key can read. An error that mentions
credentials means the key or secret is missing or wrong; a 403 for a library
means the key was not granted that library.

The unit tests need no credentials and make no network calls:

```bash
venv/bin/python -m pytest tests/ -n auto -q
```

See [Testing](TESTING.md) for more ways to test, including Claude Desktop and
MCP Inspector.

---

## Part 2: Make it yours

The repo is the configuration of one live deployment. These are the places
that name Anchorage or this project's AWS account. Change each before you
deploy.

| File | What to change |
| --- | --- |
| `config.yaml` | `server_name`, `description`, `organization`; `plugins.ecode.customer_id` and `city_name`; `aws.region` and `aws.lambda_name`; and the `instructions` text, which is sent to every client and names Anchorage, its titles and its section numbers throughout. |
| `config-ecode.yaml` | Keep it byte-identical to `config.yaml` (`cp config.yaml config-ecode.yaml`). A test checks. |
| `terraform/aws/backend.tf` | The state bucket. The one checked in belongs to this project and you cannot use it. See step 1 of Part 3. |
| `terraform/aws/prod.tfvars` | `lambda_name`, `aws_region`, `custom_domain` (set `""` to skip), `use_shared_waf = false`, `alarm_sns_topic_arn` (yours, or `""`), and the rate limits. Details below. |
| `terraform/aws/secrets.auto.tfvars` | Create it from `secrets.auto.tfvars.example` with your key and secret. It is gitignored. |
| `scripts/smoke_prod.py` | `DEFAULT_URL`, or pass your URL as the argument. |
| `.github/workflows/ci.yml` | Nothing, unless you drop the Go client or change Python versions. |

### `prod.tfvars` settings that will not work as checked in

- **`use_shared_waf = true`** attaches the API to a web ACL that lives in this
  project's AWS account, found through an SSM parameter you do not have. Set
  it to `false`. Terraform then creates a dedicated web ACL with a per-IP
  limit of `waf_rate_limit_per_5min` (about $8 a month in fixed AWS charges);
  `0` creates no WAF at all, which leaves a public, unauthenticated endpoint
  with only the API Gateway throttle in front of it.
- **`alarm_sns_topic_arn`** is this project's topic. Create an SNS topic with
  an email subscription and put its ARN here, or set `""`; with `""` no
  alarms are created, so nothing watches for errors, throttles or a refused
  API key.
- **`custom_domain`** is this project's domain. See step 4 of Part 3, or set
  `""`.
- **`lambda_name`** must be unique in your account and region. It also names
  the IAM role, the log group and the cache bucket.

`lambda_memory` and `lambda_timeout` are read from `config.yaml` in
preference to `prod.tfvars`; keep the two in step. The timeout must stay
below API Gateway's 29 seconds and above the plugin's 20-second HTTP timeout.

### Serving a different municipality

The plugin takes any eCode360 library: set `customer_id` and `city_name`.
It has only ever been run against Anchorage's code, because the key this
project holds reads nothing else, so expect to adjust it:

- **Work anywhere the API does:** `search_code`, `get_section`,
  `get_sections`, `browse_structure`, `get_table_of_contents`,
  `find_definition`, `find_references`, `read_attachment` and
  `get_customer_info` use only the general eCode360 endpoints. Reading by
  section number depends on how your code numbers its sections.
- **Built on Anchorage's tables:** `recent_ordinances` and `find_ordinance`
  read two tables that Anchorage's code publishes ("Ordinance List and
  Disposition Table" and "Ordinance Disposition Table"), found by those
  titles, and parse Anchorage's ordinance numbering (`AO No. 2025-94`). A
  code without such tables, or with other layouts, needs those tools
  rewritten or removed.
- **Names from Anchorage's outline:** reading or searching "the Charter", and
  the way title-wide attachments are found, follow Anchorage's outline.
- **Wording:** the tool descriptions in `plugins/ecode/plugin.py` and the
  `instructions` in `config.yaml` use Anchorage sections as examples. Models
  read them, so replace the examples with ones from your code.
- **Tests:** the unit tests run on recorded Anchorage responses and pass
  whatever library you configure. They will not tell you whether the tools
  work on yours. `scripts/smoke_prod.py` does run against a live server, and
  many of its checks name Anchorage sections; the transport and protocol
  checks apply to any deployment, the rest need your own section numbers.

One deployment serves one library. For several cities, deploy one copy each.

---

## Part 3: Deploy to AWS

What gets created: a Lambda function, an API Gateway REST API (with a
certificate and domain name if you set one), a WAF web ACL, an IAM role,
CloudWatch log groups and alarms, and a small private S3 bucket the Lambda
uses as a cache. The eCode360 credentials are stored as Lambda environment
variables and therefore also in the Terraform state, so keep the state bucket
private.

### 1. Create a Terraform state bucket

Once per AWS account:

```bash
cd terraform/bootstrap
terraform init
terraform apply -var="state_bucket_name=<a-globally-unique-name>" -var="aws_region=<your-region>"
```

Then edit `terraform/aws/backend.tf` so `bucket`, `region` and
`dynamodb_table` match what you created (see
[terraform/bootstrap/README.md](../terraform/bootstrap/README.md)).

### 2. Commit your changes

The deploy script builds from a commit and refuses a working tree with
uncommitted changes. `secrets.auto.tfvars` is gitignored and does not count.

### 3. Deploy

```bash
./scripts/deploy.sh --environment prod --tfworkspace <your-name>-prod
```

Without `--tfworkspace` the workspace is called `anchorage-ecode-prod`, which
is harmless in your own state bucket but misleading.

The script validates the config, builds the Lambda package from `uv.lock`,
shows the Terraform plan and asks for confirmation before applying. Read the
plan: a first deploy creates everything; later ones normally change only the
Lambda function. See [Deployment](DEPLOYMENT.md) for what each step does.

### 4. Point a domain at it (optional)

With `custom_domain` set, Terraform requests a certificate that is validated
by DNS. There are two records to add wherever your DNS is hosted:

```bash
cd terraform/aws
terraform output acm_validation_cname_name    # CNAME name  (certificate validation)
terraform output acm_validation_cname_value   # CNAME value
terraform output custom_domain_target         # CNAME target for the domain itself
```

The domain name cannot be created until the certificate is issued, so the
first deploy can stop there with an error. Add the validation record, wait
for the certificate to show as issued in ACM, and run the deploy script
again; then add the second record. Keep the validation record in place: the
certificate renews through it.

Once a custom domain is set, the default `execute-api` address is turned off
and the domain is the only address.

### 5. Check it and connect

```bash
cd terraform/aws && terraform output -raw api_gateway_url    # ends in /mcp
python scripts/smoke_prod.py <that URL>
```

Add the URL to Claude as a custom connector, as in the
[Quick Start](QUICKSTART.md).

### 6. Keep it current

- **Code or config change:** commit, then run the deploy script again.
- **Library versions:** `uv lock --upgrade-package <name>`, run the tests,
  commit `uv.lock`, deploy.
- **Server core:** protocol and transport behaviour live in
  [`opencontext-core`](https://github.com/codeforanchorage/opencontext-core).
  To take a newer release, move the tag in `requirements.txt` and
  `pyproject.toml`, run `uv lock`, test, commit and deploy.
- **Key rotation:** update `secrets.auto.tfvars` and deploy.
- **Logs:** the Lambda logs hold client IP addresses and what was searched
  for. Treat them, and anything derived from them, as private.

---

## Troubleshooting

| Issue | Solution |
|-------|----------|
| `ModuleNotFoundError: aiohttp` | Install `requirements-dev.txt`, not only `requirements.txt` |
| `ModuleNotFoundError: core` | `opencontext-core` is not installed; run the install step from the repo root |
| "Plugin ecode initialization returned False" | `ECODE_API_KEY` / `ECODE_API_SECRET` are not in the environment |
| Tool errors that mention credentials | The key or secret is wrong or expired |
| 403 from eCode360 for a library | The key is scoped; it was not granted that library |
| `terraform init` cannot read the state bucket | `backend.tf` still names this project's bucket |
| Plan fails reading an SSM parameter `/mcp-fleet/waf/...` | `use_shared_waf` is still `true` |
| Deploy stops with "the working tree has uncommitted changes" | Commit first |
| Deploy fails creating the domain name | The certificate is not issued yet; add the validation CNAME and deploy again |
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
