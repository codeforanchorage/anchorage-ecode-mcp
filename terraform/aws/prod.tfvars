lambda_name = "anchorage-ecode-mcp-prod"
stage_name  = "prod"
aws_region  = "us-west-2"
config_file = "config.yaml"
# 512 MB is ample: the eCode plugin is a thin async HTTP proxy to the eCode360
# API (no in-memory geometry work), so it needs little memory or CPU.
lambda_memory   = 512
lambda_timeout  = 120
api_quota_limit = 3000
api_rate_limit  = 5
api_burst_limit = 10
# First deploy uses the default API Gateway URL. Set this to
# "anchorage-ecode.codeforanchorage.org" once the Route53 zone for
# codeforanchorage.org (apex) is reachable from this AWS account.
custom_domain   = ""

# Cap concurrent Lambda executions. Cost and blast-radius protection if
# WAF is bypassed via distributed sources. Conversational MCP traffic does
# not need horizontal scale; raise if legitimate users start getting throttled.
lambda_reserved_concurrency = 10

# WAF per-IP rate limit (rolling 5-minute window). The MCP tools are
# conversational, so 1 rps sustained per IP (~300/5min) is plenty for
# real users and tight enough to slow scrapers and denial-of-wallet probes.
waf_rate_limit_per_5min = 300

# GIS/M365-Copilot leftover; the eCode server only serves the public /mcp
# route, so the extra API-key-gated /mcp-gcc route is disabled.
enable_gcc_route = false
