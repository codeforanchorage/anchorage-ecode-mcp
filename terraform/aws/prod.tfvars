lambda_name = "anchorage-ecode-mcp-prod"
stage_name  = "prod"
aws_region  = "us-west-2"
config_file = "config.yaml"
# 512 MB is ample: the eCode plugin is a thin async HTTP proxy to the eCode360
# API (no in-memory geometry work), so it needs little memory or CPU.
lambda_memory   = 512
lambda_timeout  = 28
api_quota_limit = 3000
# These two feed BOTH the usage plan and the stage-wide method settings
# (api_gateway.tf), so they are the effective throttle on the public /mcp
# route. Sized 2026-11-13 for ~12 attorneys onboarding via Claude.ai.
api_rate_limit  = 15
api_burst_limit = 30
# DNS for codeforanchorage.org is managed externally at DreamHost, so the
# ACM validation CNAME and the routing CNAME are added there by hand (see
# outputs acm_validation_cname_name/value and custom_domain_target).
custom_domain   = "anchorage-ecode.codeforanchorage.org"

# Cap concurrent Lambda executions. Cost and blast-radius protection if
# WAF is bypassed via distributed sources. Raised 10 -> 30 on 2026-11-13 for
# ~12 attorneys onboarding via Claude.ai (14-day peak before: 4 concurrent,
# p99 5.3s). The Lambda Throttles alarm fires on any throttle at this cap.
lambda_reserved_concurrency = 30

# WAF per-IP rate limit (rolling 5-minute window). The MCP tools are
# conversational, so 1 rps sustained per IP (~300/5min) is plenty for
# real users and tight enough to slow scrapers and denial-of-wallet probes.
# Raised to 2000 on 2026-11-13: Claude.ai connector traffic arrives from
# Anthropic's egress IPs, so many users share one per-IP counter.
waf_rate_limit_per_5min = 2000

# GIS/M365-Copilot leftover; the eCode server only serves the public /mcp
# route, so the extra API-key-gated /mcp-gcc route is disabled.
enable_gcc_route = false

# Use the fleet-wide WAF instead of a dedicated ACL for this MCP. A dedicated
# ACL costs ~$8/mo in fixed AWS charges regardless of traffic; the shared ACL
# keeps this MCP's 2000/5min limit as its own counter, aggregated on
# (IP, Host) so it stays independent of the other MCPs sharing that limit.
#
# The effective limit now lives in mcp-stats' `fleet_waf_members` under the key
# `anchorage-ecode` — change it there, not here. The rate-limit value above is retained
# so that rolling back (use_shared_waf = false) restores the original limit.
# See mcp-stats/docs/waf-consolidation.md.
use_shared_waf = true
