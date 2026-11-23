# Terraform Configurations

## AWS (Primary)

Deploys the Anchorage Municipal Code MCP server to AWS Lambda behind API
Gateway. See [Getting Started](../docs/GETTING_STARTED.md) to deploy your own
copy and [Deployment](../docs/DEPLOYMENT.md) for the live deployment.

### First-time setup: Bootstrap backend

The AWS configuration keeps its state in S3. `aws/backend.tf` as checked in
names this project's bucket, which you cannot use. In your own account, run
the bootstrap **once** and then edit `aws/backend.tf` to match. See
[bootstrap/README.md](bootstrap/README.md):

```bash
cd bootstrap
terraform init
terraform apply -var="state_bucket_name=<a-globally-unique-name>" -var="aws_region=<your-region>"
```

### Deploy

Always through the deploy script, from the repo root:

```bash
./scripts/deploy.sh --environment prod
```

The script builds the Lambda package and copies `config.yaml` into
`terraform/aws/` before planning. A bare `terraform plan` in `aws/` reads a
stale copy of both, so do not deploy by hand.

## Other Clouds

`gcp/` and `azure/` are placeholders left from upstream OpenContext. Only AWS
is implemented.
