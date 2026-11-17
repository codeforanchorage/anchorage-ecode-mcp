# The eCode plugin

This repo runs one plugin, `ecode` (`plugins/ecode/plugin.py`). It gives
read-only access to a municipal code published on eCode360, General Code's
platform. This deployment serves the Municipality of Anchorage library,
`AN6998`.

The generic CKAN, ArcGIS and Socrata plugins of upstream OpenContext are not in
this fork. They live in
[CityOfBoston/OpenContext](https://github.com/CityOfBoston/OpenContext).

## Configuration

```yaml
plugins:
  ecode:
    enabled: true
    base_url: "https://api.ecode360.com"
    customer_id: "AN6998"                 # the eCode360 library to serve
    city_name: "Municipality of Anchorage"
    timeout: 20                           # per upstream request, seconds
    api_key_env: "ECODE_API_KEY"          # NAMES of the env vars, not values
    api_secret_env: "ECODE_API_SECRET"
    # secrets_file: ".ecode.env"          # local dev only
```

| Setting | Meaning |
| --- | --- |
| `customer_id` | The eCode360 library ID. The API key must be licensed for it; a key scoped to one library gets 403 for any other. |
| `timeout` | HTTP timeout per upstream request. It must stay below the Lambda timeout (28 s) so a hung upstream call returns a readable tool error. |
| `api_key_env`, `api_secret_env` | Names of the environment variables that hold the credentials. The values never go in `config.yaml`. |
| `secrets_file` | Optional path to a gitignored `KEY=VALUE` file, loaded into the environment at startup. For local development only. |

`config.yaml` is not expanded for `${ENV_VAR}` references. See
[Getting Started](GETTING_STARTED.md) for how to supply the credentials.

## Tools

On the wire each tool name is prefixed with the plugin name, for example
`ecode__search_code`.

### `search_code(query, page=0, within=None)`

Full-text search. Returns up to 20 sections per page, each with its number,
title, a highlighted snippet and a guid.

Terms written side by side are matched with OR, so a list of words matches
every section containing any of them. For precise results, quote phrases and
join required terms with `AND`; exclude with `NOT`; group with parentheses.
Proximity search (`"a b"~3`) is not supported.

```
"snow" AND "sidewalk" AND "occupant"     → 4 sections, led by § 24.80.090
```

`within` limits the results to one part of the code. It accepts what
`get_section` accepts: `Title 9`, `Chapter 9.28`, a section number, or a guid
from the table of contents. eCode360 has no scoped search, so the server does
the filtering: it reads up to 200 hits for the query (10 upstream pages),
places each one using the table of contents, and returns those under the
requested node.

- With `within`, `page` moves through the query's hits 200 at a time.
- `total_count` is the number of hits inside the scope when every hit for the
  query was checked, and `null` otherwise, with a `scope_scan_partial` caveat.
- PDF documents have no place in the outline and are left out, with a
  `scope_unplaced_hits` caveat.
- A scope that matches more than one node (`17.05` is both a chapter and a
  Charter section) is refused with the candidates listed.
- `title:` in a query is a different thing: it matches words in a section's
  heading and does not select a Title.

### `get_table_of_contents()`

The outline of the code: every title, chapter and section with its guid, and
no body text.

### `browse_structure(guid="ROOT")`

One node of the hierarchy and its immediate children. Start at `ROOT` and walk
down by child guid.

### `get_section(guid, offset=0)`

The full text of a section with its nested subsections, the public
`ecode360.com` link, and a list of any attachments. The `guid` argument accepts:

- a guid, such as `49422044`
- a section number, such as `21.05.010` or `§ 21.05.010`
- a chapter or title reference, such as `Chapter 17.05`, `Ch. 17.05` or `Title 21`

A bare two-part number such as `17.05` can be ambiguous, because the Charter
has a Section 17.05 and the code has a Chapter 17.05. In that case the tool
returns an error naming both, and the caller picks one.

The text block holds up to 40,000 characters. A longer section is marked
`INCOMPLETE` with a `rendered_text_truncated` caveat that gives the character
range shown and the `offset` to pass on the next call; each part ends on a
line break and the next one starts exactly there. `structuredContent.text`
always carries the whole section.

### `get_sections(sections)`

Up to 10 sections in one call, each given as a guid or a number. A section that
cannot be fetched is reported in its place with a `section_unavailable` caveat,
and the rest are still returned.

The sections of a batch share 80,000 characters of text evenly, with at least
8,000 each. One that is cut short names the `get_section` offset to read on
from.

### `find_definition(term)`

How the code defines a term: the defined term, its full text, and the section
it is defined in. A term defined in more than one place returns up to 8
definitions with a `multiple_definitions` caveat.

### `read_attachment(url, page=1, layout=true)`

The text of a PDF the code publishes as an attachment. Some tables exist only
as PDFs, notably Table 21.05-1, the table of allowed uses by zoning district.
Pass a URL from the `attachments` list of `get_section`, or from a
`search_code` result marked as a PDF.

- `page` selects where to start in a long document; the result says whether
  more pages remain.
- `layout=true` keeps the column alignment of tables. Set it to `false` for
  plain reading order.

Only `https` attachment URLs on `live.ecode360.com` and `ecode360.com` are
accepted. See [Security](SECURITY.md) before changing that.

### `get_customer_info()`

Metadata about the library: official name, state, last-updated date and public
URL. The last-updated date says how current the published text is.

## Output

Every tool returns two forms of the same result:

- a text block for the model to read, capped for readability (40,000
  characters for a section)
- `structuredContent`, validated against the tool's declared `outputSchema`,
  which carries the complete text

Conditions the caller should know about are reported as `caveats`, each with a
code and a message. The ones seen most often:

| Caveat | Meaning |
| --- | --- |
| `resolved_from_section_number` | The reference was a number and was resolved to a guid |
| `rendered_text_truncated` | The text block shows part of the section; the caveat gives the `offset` for the next part, and the full text is in `structuredContent` |
| `node_too_large` | A whole chapter or title exceeded 200,000 characters and was cut; read its parts individually |
| `title_field_is_heading` | A search used `title:` with a number; `title:` matches headings, and `within` is the way to limit a search to a Title |
| `scope_scan_partial` | A `within` search checked 200 hits and the query has more; the in-scope count is not complete |
| `no_match_in_scope` | The query matches elsewhere in the code but not inside the `within` scope |
| `scope_unplaced_hits` | PDF documents and nodes outside the outline were left out of a `within` search |
| `section_unavailable` | One section of a batch could not be fetched |
| `multiple_definitions` | The term is defined in more than one place |
| `no_definition_found` | No definition node matched the term |
| `pdf_text_extraction` | The text was extracted from a PDF and table layout may be imperfect |
| `more_attachment_pages` | The PDF has pages beyond those returned |
| `no_text_layer` | The PDF is a scan with no extractable text |

## Limits

| Limit | Value |
| --- | --- |
| Search results per page | 20 |
| Sections per `get_sections` call | 10 |
| Definitions per `find_definition` call | 8 |
| PDF download size | 20 MB |
| PDF text per call | 40,000 characters |

## Upstream API

The plugin calls the eCode360 API at `api.ecode360.com` with an `api-key` /
`api-secret` pair. Every upstream call is a `GET`. `read_attachment` also
downloads PDFs from the public eCode360 website, without the API credentials.

The table of contents and library metadata are cached in the Lambda process
between requests. Section text is always fetched fresh.

## Writing a different plugin

To serve a source other than eCode360, see [Custom Plugins](CUSTOM_PLUGINS.md).
One deployment runs exactly one plugin.
