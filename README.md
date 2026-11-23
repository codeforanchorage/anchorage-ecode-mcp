# Anchorage Municipal Code MCP

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![MCP Compatible](https://img.shields.io/badge/MCP-Compatible-green.svg)](https://modelcontextprotocol.io/)

An MCP server that gives AI assistants read-only access to the Municipality
of Anchorage municipal code as published on eCode360. It is deployed at
`https://anchorage-ecode.codeforanchorage.org/mcp` and can be added to Claude
as a custom connector with that URL.

Results are the published code text, not legal advice. Every result carries
the section number and an `https://ecode360.com/<guid>` link for verification.

## Tools

| Tool | Arguments | What it does |
| --- | --- | --- |
| `search_code` | `query`, `page` (default 0), `within` (optional) | Full-text search of the code. Returns up to 20 sections per page, each with its number, title, a snippet and a guid. Terms side by side are matched with OR; use quotes, `AND` and `NOT` for precise queries. `within` (`Title 9`, `Chapter 9.28`, `Charter`, `Charter Article IV`, a section number or a guid) keeps only the hits under that part of the code. A lone quoted phrase that matches nothing also gets a labelled relaxed match on the same words in any order, limited to `within` when it is given. |
| `get_table_of_contents` | none | The outline of the code, three levels deep: parts, titles and chapters, each with a guid. Sections and body text are not listed; `browse_structure` lists a chapter's sections. |
| `browse_structure` | `guid` (default `ROOT`) | One node of the hierarchy and its immediate children, for walking the outline. |
| `get_section` | `guid`, `offset` (default 0) | Full text of a section and its subsections, with any attachments listed (those of its title too, when a note in the text points to one) and its history note parsed into ordinances and dates. Up to 40,000 characters come back per call; a longer section says so and gives the `offset` to continue from. Accepts a guid, a section number (`21.05.010`), or a chapter or title reference (`Chapter 17.05`, `Title 21`). The text of a very large node, such as a whole title, is cut at a size cap and flagged with a `node_too_large` caveat; read its chapters or sections individually. |
| `get_sections` | `sections` | Full text of up to 10 sections in one call, by guid or number. A section that cannot be fetched is reported in place without failing the rest. |
| `find_definition` | `term` | How the code defines a term: the defined term, its full text and the section it is defined in. A term defined in several places returns up to 8 definitions, flagged with a caveat. A definition containing a list whose position had to be inferred is flagged with `list_position_inferred`. |
| `recent_ordinances` | `within` (optional), `limit` (default 25) | The ordinances most recently worked into the code, newest first: number, adoption date, subject and the chapters amended, from the publisher's Ordinance List and Disposition Table. `within` (`Title 21`, `Chapter 21.05`, `Charter`) keeps those codified there. Also names any ordinance adopted but not yet in the code text, with a link to its PDF. |
| `find_ordinance` | `ordinance` | The code sections one ordinance added, amended or repealed, with its effective date, from the publisher's Ordinance Disposition Table (1977 on). Accepts `2025-94`, `AO 2025-94`, `AO No. 2025-115(S-2)`. For a recent ordinance it also returns the subject and adoption date. When the ordinance is adopted but not yet in the code text, it returns a link to the ordinance PDF. When the table does not list the ordinance yet, it returns the sections of the code that cite it. |
| `find_references` | `section`, `page` (default 0) | Where the rest of the code cites a section: its row in a fine schedule, penalty sections and other sections that refer to it, including citations to its subsections. Fine and penalty sections are listed first. |
| `read_attachment` | `url`, `page` (default 1), `layout` (default true) | Text of a PDF the code publishes as an attachment, chiefly tables that are not in the section text, such as Table 21.05-1 (allowed uses by zoning district). Also reads a recently adopted ordinance that a search returns as a PDF, with a warning that extraction loses the marks showing deleted language. Only this code's PDF URLs on ecode360.com are accepted. |
| `get_customer_info` | none | Metadata about the code library: official name, state, last-updated date and public URL. |

Tool names are prefixed with the plugin name on the wire, for example
`ecode__search_code`. See [Security](docs/SECURITY.md) for the limits on what
`read_attachment` will fetch.

---

## Use the hosted server

Connect via **Claude Connectors** (same steps on Claude.ai and Claude Desktop):

1. Go to **Settings** → **Connectors** (or **Customize** → **Connectors** on claude.ai)
2. Click **Add custom connector**
3. Enter a name (e.g. "Anchorage Municipal Code") and the URL `https://anchorage-ecode.codeforanchorage.org/mcp`

No account or key is needed. See the [Quick Start](docs/QUICKSTART.md).

## Run your own copy

You need your own eCode360 API key and secret from General Code, for your
own code library. The hosted server's key is not in this repo and only
covers Anchorage.

```bash
git clone https://github.com/codeforanchorage/anchorage-ecode-mcp.git
cd anchorage-ecode-mcp
python -m venv venv
venv/bin/pip install -r requirements-dev.txt     # Windows: venv\Scripts\pip

export ECODE_API_KEY=...  ECODE_API_SECRET=...
venv/bin/python scripts/local_server.py          # http://localhost:8000/mcp
```

`config.yaml` in the repo root is this server's real configuration; there is
nothing to copy or enable. To serve another municipality, or to deploy to
your own AWS account, follow [Getting Started](docs/GETTING_STARTED.md): it
lists every file that names Anchorage or this project's AWS account and what
to put there instead.

## How this relates to OpenContext

This project began as a fork of
[OpenContext](https://github.com/CityOfBoston/OpenContext), the City of
Boston's template for deploying one MCP server with one plugin, and has since
moved away from it. The upstream setup instructions no longer apply here:

| | Upstream OpenContext | This repo |
| --- | --- | --- |
| MCP server core (`core/`, `server/`) | In the repo | The [`opencontext-core`](https://github.com/codeforanchorage/opencontext-core) package, pinned by tag in `requirements.txt`, `pyproject.toml` and `uv.lock` |
| Plugins | Generic CKAN, ArcGIS and Socrata plugins; enable one | Only `plugins/ecode`, already enabled |
| Configuration | Copy `config-example.yaml` and edit | `config.yaml` is checked in and is what production runs |
| Secrets | `${VAR}` placeholders in the config, as `config-example.yaml` shows | Environment variables read by the plugin; `config.yaml` does not expand `${VAR}` |
| Endpoint | Lambda Function URL | API Gateway with a WAF, throttles and an optional custom domain |
| Deploy | `./scripts/deploy.sh` | `./scripts/deploy.sh --environment prod`, from a clean commit, with the versions in `uv.lock` |

`config-example.yaml` and `custom_plugins/template/` remain from upstream as a
starting point for a different plugin; this server does not use them.

---

## Documentation


| Doc                                        | Description                                     |
| ------------------------------------------ | ----------------------------------------------- |
| [Quick Start](docs/QUICKSTART.md)          | Use the hosted server from Claude               |
| [Getting Started](docs/GETTING_STARTED.md) | Run it locally, deploy your own copy, serve another city |
| [The eCode plugin](docs/BUILT_IN_PLUGINS.md) | Tools, configuration and limits               |
| [Architecture](docs/ARCHITECTURE.md)       | System design and plugins                       |
| [Deployment](docs/DEPLOYMENT.md)           | AWS, Terraform, monitoring                      |
| [Testing](docs/TESTING.md)                 | Local testing (Terminal, Claude, MCP Inspector) |
| [Security](docs/SECURITY.md)               | Threat model and the attachment fetch limits    |
| [FAQ](docs/FAQ.md)                         | Common questions                                |


---

## Examples

- **This server's configuration:** [config.yaml](config.yaml) (the `ecode` plugin, its settings and the instructions sent to clients)
- **This server's plugin:** [plugins/ecode/plugin.py](plugins/ecode/plugin.py)
- **Writing your own plugin:** [docs/CUSTOM_PLUGINS.md](docs/CUSTOM_PLUGINS.md)
- **Generic CKAN, ArcGIS and Socrata plugins:** not in this repo; see upstream [CityOfBoston/OpenContext](https://github.com/CityOfBoston/OpenContext)

---

## Contributing

Pre-commit hooks (optional):

```bash
pip install pre-commit
pre-commit install
```

Hooks: Ruff, yamllint, gofmt. Run manually: `pre-commit run --all-files`.

---

## License

MIT — see [LICENSE](LICENSE).

**OpenContext**, which this project started from, was written by Srihari Raman, City of Boston Department of Innovation and Technology.

**This project** (the Anchorage Municipal Code MCP server and its `ecode` plugin) is maintained by [Code for Anchorage](https://github.com/codeforanchorage).
