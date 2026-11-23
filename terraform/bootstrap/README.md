# Terraform Backend Bootstrap

Creates the S3 bucket and DynamoDB table that `terraform/aws` uses for remote state. Needed once per AWS account, and only if you deploy your own copy: the live deployment's state bucket already exists.

## Prerequisites

- AWS CLI configured
- Terraform >= 1.0

## Usage

Run this **once** before using the main `terraform/aws` configuration:

```bash
cd terraform/bootstrap
terraform init
terraform apply -var="state_bucket_name=<a-globally-unique-name>" -var="aws_region=<your-region>"
```

Bucket names are global, so the default name is almost certainly taken.

This creates:

- **S3 bucket** `opencontext-terraform-state` – stores Terraform state with versioning and encryption
- **DynamoDB table** `terraform-state-lock` – available for state locking (optional)

Then edit `terraform/aws/backend.tf` so its `bucket`, `region` and `dynamodb_table` match what you created. As checked in, that file names this project's bucket.

## Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `aws_region` | `us-east-1` | AWS region for backend resources |
| `state_bucket_name` | `opencontext-terraform-state` | S3 bucket name (must match `terraform/aws/backend.tf`) |
| `lock_table_name` | `terraform-state-lock` | DynamoDB table name |

## After Bootstrap

Deploy with the script, from the repo root: `./scripts/deploy.sh --environment prod`. See [Getting Started](../../docs/GETTING_STARTED.md).
