# Frequently Asked Questions

## General

### What is this?

An MCP server that gives AI assistants read-only access to the Municipality of
Anchorage municipal code, as published on eCode360. It lets an assistant
search the code, read sections, look up definitions and read the PDF tables,
and cite what it found.

### Is this legal advice?

No. The server returns the published code text. It does not interpret it, and
it does not cover state or federal law. Every result carries the section number
and an `ecode360.com` link so the source can be checked.

### How current is the text?

It is whatever eCode360 currently publishes. The `get_customer_info` tool
returns the library's last-updated date. Ordinances adopted but not yet
codified by the publisher may not be included.

### What is MCP?

The Model Context Protocol connects AI assistants to external data sources.
See [modelcontextprotocol.io](https://modelcontextprotocol.io).

### What is OpenContext?

The City of Boston's template for deploying one MCP server with one plugin,
which this project started from. The two have since diverged: the server
core is now the separate `opencontext-core` package, the only plugin here is
`ecode`, and upstream's setup steps do not apply. See the
[README](../README.md#how-this-relates-to-opencontext) and
[Architecture](ARCHITECTURE.md).

## Using it

### How do I connect from Claude?

Add `https://anchorage-ecode.codeforanchorage.org/mcp` as a custom connector.
The [Quick Start](QUICKSTART.md) has the steps.

### Do I need an account or an API key?

No. The hosted server is public and read-only.

### I don't see the newer tools

Start a new conversation: one already in progress may keep the tool list it
began with. If the new tools or parameters still do not appear, disconnect and
reconnect the connector in Settings → Connectors. The server has eleven tools;
see the [README](../README.md#tools).

### Can it read the zoning use tables?

Yes. Tables published only as PDF attachments, such as Table 21.05-1, are read
with the `read_attachment` tool. Text extracted from a PDF can lose some table
alignment, and a scanned PDF with no text layer cannot be read.

### Can I use it without Claude?

Yes. Any MCP client that supports Streamable HTTP can connect. You can also
POST JSON-RPC directly:

```bash
curl -X POST https://anchorage-ecode.codeforanchorage.org/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

### Are there rate limits?

Yes: a per-IP limit, a gateway throttle and a daily quota. Requests over a
limit get a 429. The values are in [Deployment](DEPLOYMENT.md).

## Other cities

### Can this serve another municipality's code?

The plugin takes any eCode360 library: set `customer_id` and `city_name` in
`config.yaml`. The API is licensed, though, and a key is scoped to the
libraries General Code grants it. The key this project uses only grants
Anchorage (`AN6998`); other libraries return 403. Another city needs its own
key and its own deployment, and some tools are built on tables that
Anchorage's code publishes.
[Getting Started](GETTING_STARTED.md#serving-a-different-municipality) says
which.

### Can one deployment serve several cities or several plugins?

No. Each deployment runs exactly one plugin for one library. Deploy another
copy for each.

## Configuration

### Can I use environment variables in config.yaml?

No. `config.yaml` is loaded as plain YAML and `${VAR}` is not expanded, so it
would reach the plugin as that literal string. Secrets are read from the
environment by the plugin itself: `ECODE_API_KEY` and `ECODE_API_SECRET`.

### Why can't the API key go in config.yaml?

`config.yaml` is packaged into the Lambda zip. Terraform injects the
credentials as Lambda environment variables instead.

### How do I change configuration after deployment?

Edit `config.yaml` and run `./scripts/deploy.sh --environment prod` again.
Keep `config-ecode.yaml` identical to it.

## Deployment

### What AWS resources are created?

A Lambda function, an API Gateway REST API (with a certificate and domain
name if one is set), an IAM role, CloudWatch log groups and alarms, a WAF web
ACL or an association with a shared one, and a private S3 bucket used as a
cache.

### How do I update an existing deployment?

Run `./scripts/deploy.sh --environment prod` again and review the plan. See
[Deployment](DEPLOYMENT.md).

### Does CI deploy?

No. CI runs lint, tests and a dependency audit. Deploys are manual.

## Development

### How do I test locally?

See [Testing](TESTING.md): unit tests, a local server at
`http://localhost:8000/mcp`, and the smoke test.

### How do I change protocol or transport behaviour?

That code is in the
[`opencontext-core`](https://github.com/codeforanchorage/opencontext-core)
package, not in this repo. Change it there, tag a release, and move the pin in
`requirements.txt` and `pyproject.toml`.

### How do I add a plugin for a different data source?

See [Custom Plugins](CUSTOM_PLUGINS.md). Remember that a deployment runs only
one plugin.

### Can I contribute?

Yes. Open an issue or a pull request.

## Troubleshooting

### Claude can't connect

1. Check the URL ends in `/mcp`
2. Check the connector is added in Settings → Connectors
3. Check it is enabled for the conversation ("+" → Connectors → toggle on)

### A tool returns an error about credentials

The server's eCode360 key or secret is missing, wrong or expired. On a local
server, check the environment variables; on Lambda, check the Terraform
secrets and redeploy.

### A search returns nothing

No section matched the query as written. The rule may be state law, repealed,
or worded differently. Try fewer terms, synonyms joined with `OR`, or browse the
table of contents.

### A search returns hundreds of results

Terms side by side are matched with OR. Quote phrases and join required terms
with `AND`.

## Support

- [GitHub Issues](https://github.com/codeforanchorage/anchorage-ecode-mcp/issues)
- Report a bug with the steps to reproduce, the error message, and the
  question or tool call that caused it. Do not include API keys.
