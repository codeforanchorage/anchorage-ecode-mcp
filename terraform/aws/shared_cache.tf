# Shared cache for the eCode plugin (plugins/ecode/plugin.py,
# SHARED_CACHE_ENV).
#
# eCode360 takes about 9 s to return the Ordinance Disposition Table that
# find_ordinance reads. The plugin keeps it in memory, but memory belongs to
# one Lambda container: each new container made a caller wait, and a burst
# of simultaneous lookups landed on new containers that all fetched it at
# once (14-16 s each, measured 2026-11-19). This bucket holds that one
# response so any container can read it in tens of milliseconds.
#
# What is in it: published municipal code text only (the same text
# ecode360.com serves to anyone). No credentials, no caller data, no logs.
# Who can touch it: this Lambda's role (get/put objects, list) and account
# administrators. It is not public and is never served to a caller as a
# file; the plugin parses it and refuses anything of an unexpected shape.
# Losing it costs nothing: the plugin refetches from eCode360.

data "aws_caller_identity" "current" {}

resource "aws_s3_bucket" "shared_cache" {
  # Bucket names are global; the account id keeps this one unique.
  bucket = "${local.lambda_name}-cache-${data.aws_caller_identity.current.account_id}"

  # Only derived data: safe to destroy with objects in it.
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "shared_cache" {
  bucket = aws_s3_bucket.shared_cache.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "shared_cache" {
  bucket = aws_s3_bucket.shared_cache.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "shared_cache" {
  bucket = aws_s3_bucket.shared_cache.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# The plugin ignores an object older than 7 days; this removes what it
# would ignore anyway (and the objects of a republished code, whose guids
# change and are never read again).
resource "aws_s3_bucket_lifecycle_configuration" "shared_cache" {
  bucket = aws_s3_bucket.shared_cache.id

  rule {
    id     = "expire-cache-objects"
    status = "Enabled"

    filter {}

    expiration {
      days = 30
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }
}

# Read and write objects, and list the bucket. Listing is there for one
# reason: without it S3 answers a read of a MISSING object with AccessDenied
# instead of NoSuchKey, so the first lookup after a republish logged a
# warning that read like a permission fault. The role cannot delete
# anything or change the bucket.
resource "aws_iam_role_policy" "shared_cache" {
  name = "${local.lambda_name}-shared-cache"
  role = aws_iam_role.lambda_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject"]
        Resource = "${aws_s3_bucket.shared_cache.arn}/*"
      },
      {
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = aws_s3_bucket.shared_cache.arn
      }
    ]
  })
}
