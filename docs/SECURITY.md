# Security Model — Anchorage Municipal Code MCP

This document describes the threat model and the defenses currently
enforced for the public deployment at
`https://anchorage-ecode.codeforanchorage.org/mcp`. Future contributors
should read this before changing anything in `plugins/ecode/`,
`server/`, `core/`, or `terraform/aws/`.

> This file previously described the **Anchorage GIS** MCP, which this
> repo was forked from. It was rewritten on 2026-08-25 when the GIS
> plugin was removed. Anything here about ArcGIS tenant scoping, SSRF
> host allowlists, or SQL `WHERE` validation is gone because the code it
> described is gone — not because the checks were relaxed.

## Threat model

- **Endpoint:** publicly reachable, unauthenticated, read-only over
  HTTPS. Anyone can issue JSON-RPC calls. Browsers, native MCP clients,
  and arbitrary HTTP clients all reach the same Lambda.
- **Data:** the MCP proxies only to the eCode360 API
  (`api.ecode360.com`) for a single fixed library, Anchorage's
  (`customer_id: AN6998`). The content it returns is published municipal
  law — already public at `ecode360.com`. No write paths exist; every
  upstream call is a `GET`.
- **This deployment DOES hold a secret**, unlike the GIS server this
  repo was forked from: eCode360 is a licensed API and requires an
  `api-key` / `api-secret` pair. Protecting that pair is the main
  confidentiality concern here; the code text itself is public.
- **Realistic risks:**
  1. Leaking the eCode360 API credentials (logs, config, the zip).
  2. Denial of wallet — Lambda invocations, and burning the licensed
     API quota upstream.
  3. Prompt injection — municipal code text reaching the calling LLM as
     context. The text is authored by the Municipality, so this is low,
     but it is untrusted input to a model all the same.
  4. Misrepresentation — the server citing a section it did not read, or
     reporting a complete count of zero for law that exists. Not a
     classic security risk, but for a legal-text server it is the one
     that does real-world harm.

## Defenses in place

### Network and request shape

- **WAF per-IP rate limit** at 300 req / 5 min, blocking mode. This
  deployment uses the **shared fleet ACL** (`use_shared_waf = true` in
  `prod.tfvars`), which keeps this MCP's limit as its own counter
  aggregated on `(IP, Host)`. The effective limit lives in mcp-stats'
  `fleet_waf_members` under the key `anchorage-ecode`, not in this repo.
- **API Gateway throttling** at 5 rps / 10 burst with a 3000 req/day
  quota (`terraform/aws/api_gateway.tf`, `prod.tfvars`).
- **AWS managed rule sets** (`KnownBadInputsRuleSet`, `CommonRuleSet`)
  in block mode.
- **Lambda reserved concurrency** capped at 10 to bound cost and blast
  radius if WAF is bypassed (`main.tf`, `prod.tfvars`).
- **HTTP method allowlist:** only `POST /mcp` and `OPTIONS /mcp` reach
  plugin code; `GET`/`DELETE` return 405 (`server/http_handler.py`).
- **Timeout ladder** — API Gateway 29s (hard) > Lambda 28s > plugin
  httpx 20s. Beyond correctness this is a availability control: with the
  Lambda above the gateway ceiling, a slow upstream held one of only ten
  concurrency slots for up to 91 seconds after the client had already
  received a 504. Pinned by `tests/test_deployment_config.py`.

### Cross-origin and DNS rebinding

- **Disallowed browser Origins are REFUSED with 403**, on both the
  request path and the OPTIONS preflight
  (`server/http_handler.py::_origin_rejected`). This is the Streamable
  HTTP transport's DNS-rebinding defence and it is a MUST. Note the
  distinction: echoing CORS headers only for allowlisted origins — which
  is all this handler did before 2026-08-25 — is **not** the same thing,
  because the request still executed and anything ignoring CORS never
  noticed.
- Requests with **no Origin at all are allowed** by design: native MCP
  clients, curl and the Lambda console send none.
- **`mcp-session-id` is a logging/tracing identifier**, not an auth
  token. Do not extend authorization decisions to depend on it without
  adding a real signing/verification step.
- **`MCP-Protocol-Version` is validated**; an unrecognized revision gets
  400 / -32600.

### Input validation

The SSRF surface that dominated the GIS server's model **does not exist
here**. `base_url` and `customer_id` are fixed in `config.yaml`; no tool
argument can influence which host is contacted or which library is
read. Every path is built from `_cust_path()` around the configured
customer id, and caller-supplied path segments are `quote(..., safe="")`
encoded.

- **`guid`** must be alphanumeric, or the literal `ROOT`
  (`plugins/ecode/plugin.py::_guid_arg`). This also catches the common
  model mistake of passing a section *number*.
- **`page`** is coerced through `_int_arg` and must be >= 0.
- **`arguments`** must be a JSON object; a non-object is rejected at the
  JSON-RPC layer with -32602 and never reaches the plugin
  (`core/mcp_server.py::_handle_tools_call`).
- There is **no SQL surface**: the eCode360 API takes a search string,
  not a query language, so nothing corresponding to `WhereValidator`
  is needed.

### Logs

- **Sensitive headers and request-body keys redacted** by name match in
  `core/logging_utils.py`. `api-key` and `api-secret` are both covered
  (`api-key` explicitly, `api-secret` via the `secret` substring) —
  verified, not assumed. The credentials are outbound-only headers set
  on the httpx client and are never logged in the first place; the
  redaction is defence in depth in case a caller ever sends them
  inbound.
- **Caller mistakes log at WARNING with no traceback; genuine faults log
  at ERROR with one.** This is a security property, not just tidiness:
  before 2026-08-25 routine client probes (`server/discover` arrived
  ~9 times a day) produced ERROR entries with stack traces, which is
  exactly what makes a real incident unfindable in CloudWatch.
- **CloudWatch retention** is 14 days (Lambda) and 30 days (API Gateway
  access logs) — `terraform/aws/main.tf`, `terraform/aws/access_logs.tf`.

### Configuration and secrets

- **No secrets in `config.yaml`.** This is an invariant. `config.yaml`
  is packaged *inside the Lambda zip* (`scripts/deploy.sh` copies it to
  `terraform/aws/config.yaml`, which is a gitignored build artifact), so
  anything written there ships in the deployment package.
- The eCode360 credentials reach the Lambda as **environment variables**
  injected from sensitive Terraform variables
  (`terraform/aws/secrets.auto.tfvars`, gitignored, or
  `TF_VAR_ecode_api_key` / `TF_VAR_ecode_api_secret`). The plugin reads
  them from `os.environ` in `initialize()`.
- Note that `${ENV_VAR}` substitution in `config.yaml` **does not
  exist** despite what `config-example.yaml` implies — `load_and_validate_config`
  is a plain `yaml.safe_load`. A `${VAR}` there is passed through as a
  literal string. Always read secrets from `os.environ` in the plugin.
- For local development, `secrets_file` points at a gitignored
  `KEY=VALUE` file (`.ecode.env.txt`). It only fills vars that are not
  already set, so real env vars always win.

## Architectural invariants

Enforced by code today; do not relax without explicit discussion.

1. **One fork = one MCP server.** The single-plugin rule is enforced at
   config validation (`core/validators.py`) and at runtime
   (`PluginManager.load_plugins()`). Multiple enabled plugins is a hard
   error.
2. **No write paths.** Every eCode360 call is a `GET`; all five tools
   are annotated `readOnlyHint: true`. A tool that writes would be a
   security-review-required change.
3. **The upstream host and library are fixed by config, never by a tool
   argument.** This is what keeps the server from being usable as a
   general proxy.
4. **A declared `outputSchema` is binding.** The spec requires
   structured results to conform. Do not add a constraint real eCode360
   data can violate, and do not add an early-return branch that skips
   `structuredContent` — see `_empty_search_result`.

## Deferred / known gaps

| # | Issue | Status |
|---|---|---|
| 1 | `/mcp` is fully unauthenticated; rate-limit bypass via rotating IPs is feasible | accepted given public-data scope |
| 2 | API-key rotation is manual across two gitignored files plus a redeploy | accepted; documented in the project runbook |
| 3 | Origin allowlist includes `localhost:6274` / `127.0.0.1:6274` (MCP Inspector) in prod | accepted; revisit when non-public tools land |
| 4 | Response bodies are written to CloudWatch with name-key redaction only — no value-pattern scrub | low risk here: the payload is published law, and the credentials are outbound-only |
| 5 | Access-log `sourceIp` is raw; not hashed | open |
| 6 | No request-body size cap at API Gateway (Lambda's 6 MB ceiling applies) | open |
| 7 | No CI; lint and tests are run locally | open — `.github/workflows/` does not exist in this fork despite CLAUDE.md's CI section |

## Verifying a change

Before merging anything that touches `plugins/ecode/`, `server/`,
`core/`, or `terraform/aws/`:

1. `.venv/Scripts/python.exe -m ruff check core/ plugins/ server/ tests/`
   — use ruff **0.15.1**, the version pinned in
   `.pre-commit-config.yaml`. Never run `ruff format`; these files are
   hand-wrapped.
2. `.venv/Scripts/python.exe -m pytest tests/ -q` — all must pass.
3. Run the server locally and exercise it — local now routes through
   `UniversalHTTPHandler`, so the Origin allowlist, the protocol-version
   check and method validation are all reproducible off-prod:
   ```bash
   OPENCONTEXT_CONFIG=<copy with secrets_file> \
     .venv/Scripts/python.exe scripts/local_server.py
   .venv/Scripts/python.exe scripts/smoke_prod.py http://localhost:8000/mcp
   ```
4. Deploy with `./scripts/deploy.sh --environment prod`; it plans first
   and requires an explicit `yes` before applying. Check
   `terraform workspace show` is `anchorage-ecode-prod` — the shared
   state bucket also holds other MCPs' workspaces.
5. `.venv/Scripts/python.exe scripts/smoke_prod.py` against deployed
   prod. This asserts capabilities, not the content of the code, so it
   should stay at 27/27 across eCode360 republications.

## Reporting issues

Email `brendanbabb@gmail.com` for sensitive reports. Public issues can
go to <https://github.com/codeforanchorage/anchorage-ecode-mcp/issues>.
