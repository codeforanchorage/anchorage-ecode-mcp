# Deployment

The server runs on AWS Lambda behind API Gateway, deployed with Terraform by
`scripts/deploy.sh`. Deploys are manual; CI checks the code but does not
deploy.

This page describes the live deployment. To deploy a copy to your own AWS
account, start with [Getting Started](GETTING_STARTED.md), which lists what
to change first.

## Prerequisites

- AWS account and the AWS CLI, configured
- Terraform >= 1.0
- Python 3.11+ and `uv`
- eCode360 API credentials

## AWS permissions

The deploying identity needs to manage Lambda, IAM roles and policies,
CloudWatch Logs and alarms, API Gateway, ACM certificates, WAF web ACLs and
associations and the cache S3 bucket; to read the WAF SSM parameter when the
shared web ACL is used; and to read and write the Terraform state bucket.

## Deploying

```bash
./scripts/deploy.sh --environment prod
```

The script:

1. Refuses to run if the working tree has uncommitted changes, so what is
   deployed is always a commit (`--allow-dirty` overrides, for emergencies)
2. Validates `config.yaml` (exactly one plugin enabled)
3. Builds the Lambda package: `plugins/`, `config.yaml` and the dependencies
   at the exact versions in `uv.lock`, including `opencontext-core`. It also
   writes `BUILD_INFO.json` into the package: the commit, whether the tree
   was dirty, and whether the versions came from the lock
4. Selects the Terraform workspace (`anchorage-ecode-prod`)
5. Runs `terraform plan` with `prod.tfvars` and saves the plan
6. Asks for confirmation, then applies the saved plan

The locked install needs `uv` (on `PATH`, or as a module of the Python the
script finds: `python -m pip install uv`). Without it the script warns and
falls back to `pip` and `requirements.txt`, which only gives lower bounds, so
the versions shipped are whatever is newest that day. To take newer library
versions on purpose: `uv lock --upgrade`, run the tests, commit `uv.lock`,
then deploy.

Read the plan before confirming. A code-only change shows one in-place update
of the Lambda function with a new `source_code_hash`.

After a deploy, run the smoke test against production:

```bash
python scripts/smoke_prod.py
```

### Always go through the script

`terraform/aws/config.yaml` is a build artifact. The script copies the
repo-root `config.yaml` over it while packaging. A bare `terraform plan` run
inside `terraform/aws/` reads the stale copy, so a config change can appear to
apply while changing nothing.

### Workspaces and state

State lives in a shared S3 bucket, `anchorage-gis-opencontext-tfstate`, used by
several MCP servers. Deployments are isolated by Terraform **workspace**, not
by bucket. Before any manual Terraform command, check the workspace:

```bash
cd terraform/aws
terraform workspace show     # must print anchorage-ecode-prod
```

Applying in the wrong workspace changes a different server. Do not rename the
bucket: that would orphan every workspace's state.

For a deployment in a new AWS account, create a state bucket first (see
[terraform/bootstrap/README.md](../terraform/bootstrap/README.md)) and point
`terraform/aws/backend.tf` at it.

### Environments

Only `prod` is defined: the script reads `terraform/aws/<environment>.tfvars`,
and `prod.tfvars` is the only one in the repo. To add a staging environment,
create `staging.tfvars`; the script would use the `anchorage-ecode-staging`
workspace for it.

## Credentials

The eCode360 key and secret reach the Lambda as environment variables set by
Terraform. Put them in `terraform/aws/secrets.auto.tfvars` (gitignored; copy
`secrets.auto.tfvars.example`), or export `TF_VAR_ecode_api_key` and
`TF_VAR_ecode_api_secret`.

Never put them in `config.yaml`. That file is packaged into the Lambda zip.

## Endpoint

```bash
cd terraform/aws
terraform output -raw api_gateway_url   # ends in /mcp
```

Production is also served on the custom domain,
`https://anchorage-ecode.codeforanchorage.org/mcp`. There is no Lambda
Function URL.

## Limits in production

| Setting | Value | Where |
| --- | --- | --- |
| Lambda memory | 1024 MB | `config.yaml` (`aws.lambda_memory`) |
| Lambda timeout | 28 s | `config.yaml` (`aws.lambda_timeout`) |
| Lambda reserved concurrency | 30 | `prod.tfvars` |
| API Gateway throttle | 15 req/s sustained, 30 burst | `prod.tfvars` |
| API Gateway daily quota | 3,000 | `prod.tfvars` |
| WAF per-IP limit | 2,000 requests per 5 minutes | shared WAF |

Requests over a limit get a 429.

`lambda_memory` and `lambda_timeout` are read from `config.yaml` in preference
to `prod.tfvars`. Changing them in the tfvars alone does nothing. Keep the two
files in step.

The Lambda timeout must stay below the API Gateway integration timeout of
29 s, which is a hard limit, and above the plugin's 20 s HTTP timeout. See
[Architecture](ARCHITECTURE.md#timeouts).

Memory also sets the CPU share. PDF text extraction in `read_attachment` is
CPU-bound, which is why the function runs at 1024 MB while using about 150 MB.

## Monitoring

- **Lambda logs:** `/aws/lambda/anchorage-ecode-mcp-prod`, structured JSON,
  14-day retention
- **API Gateway access logs:** a separate log group, 30-day retention
- **Alarms:** Lambda errors, Lambda throttles, Lambda duration near the
  timeout, and API Gateway 5xx

```bash
aws logs tail /aws/lambda/anchorage-ecode-mcp-prod --follow
```

The logs include client IP addresses and what was searched for. Treat
downloaded logs, and anything derived from them, as private: do not put
users' queries, or what they were researching, in commits, issues, code
comments or documentation. Describe the behaviour that was fixed instead.

## Updating and removing

**Update:** change the code or config, then run the deploy script again.
Clients that connected before a new tool was added may need to reconnect to
see it.

**Destroy:**

```bash
cd terraform/aws
terraform workspace show
terraform destroy -var-file=prod.tfvars
```

## Troubleshooting

| Issue | Solution |
| --- | --- |
| "Multiple Plugins Enabled" | Enable only one plugin in `config.yaml` |
| Plan shows no change after a config edit | Terraform was run directly; use the deploy script |
| Plan touches unexpected resources | Wrong workspace; check `terraform workspace show` |
| Tool errors that mention credentials | The Lambda's `ECODE_API_KEY` / `ECODE_API_SECRET` are missing or wrong |
| 504 from the gateway | The Lambda outlived 29 s; check the timeout ordering |
| 5xx | Check the CloudWatch logs |
