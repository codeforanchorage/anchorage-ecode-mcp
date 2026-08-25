terraform {
  backend "s3" {
    # NOT a leftover, despite the name: this is the SHARED state bucket for
    # the whole MCP fleet (checkbook, ecode, gis, parcels, esri-uc), which
    # happens to have been created for the GIS server first. Deployments are
    # isolated by terraform WORKSPACE, not by bucket. Renaming this orphans
    # the state for every one of them -- leave it alone.
    bucket         = "anchorage-gis-opencontext-tfstate"
    key            = "terraform.tfstate"
    region         = "us-west-2"
    dynamodb_table = "terraform-state-lock"
    encrypt        = true
  }
}
