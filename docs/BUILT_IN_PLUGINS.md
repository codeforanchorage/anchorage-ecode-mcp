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
Proximity search (`"a b"~3`) is not supported. A trailing `*` is a wildcard
and must be written without quotes (`indemnif*`, not `"indemnif*"`).

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

A search that matches nothing says that no section matched the query as
written, which is not proof that the text is absent. When the whole query is
one quoted phrase, two follow-ups are added to the result, both kept apart
from `rows` and `total_count`:

- `relaxed_match`: the phrase's words required in any order, with a
  `relaxed_match` caveat. These are not matches for the phrase. With
  `within`, only relaxed hits inside the scope are kept: the first 200 hits
  for the relaxed query are checked, and its `total_count` is `null` when
  the query has more.
- `related_definitions`: when the phrase reads like a definition lookup
  (`"labor agreement means"`), what `find_definition` returns for the term,
  with a `definition_lookup` caveat. A definition is its own entry whose
  heading is the term, so a phrase never spans the term and its text.
  This lookup covers the whole code even with `within`, because a
  chapter's governing definition often sits in its title's general
  definitions section; the caveat says so.

### `get_table_of_contents()`

The outline of the code, three levels deep: the parts (Charter, Municipal
Code, Code of Regulations, tables), the titles under each part, and the
chapters under each title, each with its guid. Sections and body text are not
listed; a chapter shows how many entries it has, and `browse_structure` with
its guid lists them. For this code that is about 480 entries.

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

`length` reports every size in characters, each named for what it measures:
`text_chars` (the length of `text`), `shown_from` and `shown_to` (the part of
`text` in the text block; offsets index `text`), and `whole_node_chars`, which
is null unless a chapter or title was cut, and is then the size of all its
text.

The section heading appears once, in the heading line. Subsections show their
label (`A.`, `1.`, `a.`) and their own text. The history note (`(AO No. ...)`)
closes the section.

The eCode360 API returns a paragraph's words before and after a list as one
string, with the list items separate. The server puts the list back after the
colon that introduces it, so the sentence that follows a list is shown after
it, and says so with a `list_position_inferred` caveat. Where the position is
ambiguous it leaves the text as received and says that instead.

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

A definition that contains a list is rendered as `get_section` renders it:
the list is placed after the colon that introduces it. When that placement
was inferred, or could not be worked out, the result carries a
`list_position_inferred` caveat naming the definition(s) by their number in
the result.

### `find_references(section, page=0)`

Where the rest of the code cites a section, for questions such as "what is the
fine for violating § 9.14.040" or "what refers to this section". It searches
for the number with a trailing wildcard (`9.14.040*`), because fine schedules
cite subsections (`§ 9.14.040A.3.a`) that a quoted search for the number does
not match. The section's own text is left out.

Each reference has a snippet of the text around the citation and a `kind`:
`fine_or_penalty` (judged from the heading), `section`, `document` (a PDF) or
`reference_table` (editorial tables such as the Ordinance Disposition Table).
Fine and penalty sections are listed first and reference tables last.

`section` must be a full section number, optionally with a subsection
(`9.28.019B`). A chapter or title number is refused.

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
| `list_position_inferred` | A list was placed after the colon that introduces it, because the API does not say where in the paragraph it sits; check the order at the link |
| `no_match_in_code` | No section matched the query as written; not proof the text is absent |
| `relaxed_match` | A quoted phrase matched nothing; the same words in any order matched the sections listed under `relaxed_match` |
| `definition_lookup` | A zero-result query read like a definition lookup; the definition is in `related_definitions` |
| `node_too_large` | A whole chapter or title exceeded 200,000 characters and was cut; read its parts individually |
| `wildcard_in_quotes` | An empty search had a `*` inside double quotes; a quoted wildcard often matches nothing, so the message suggests the unquoted form |
| `title_field_is_heading` | A search used `title:` with a number; `title:` matches headings, and `within` is the way to limit a search to a Title |
| `scope_scan_partial` | A `within` search checked 200 hits and the query has more; the in-scope count is not complete |
| `no_match_in_scope` | The query matches elsewhere in the code but not inside the `within` scope |
| `scope_unplaced_hits` | PDF documents and nodes outside the outline were left out of a `within` search |
| `section_unavailable` | One section of a batch could not be fetched |
| `multiple_definitions` | The term is defined in more than one place |
| `no_definition_found` | No definition node matched the term |
| `no_references_found` | Nothing else in the code cites the section; a general penalty provision may still cover it |
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
between requests for an hour. Search pages, section text and structure
nodes are cached for five minutes, so a repeated search or a re-read of a
section in the same conversation usually costs no upstream request; an
amended section can read stale for up to five minutes.
`get_customer_info` reports the date the published code was last updated,
and `get_section` / `get_sections` repeat it on every result as
`code_last_updated`. It is the date of the whole code, not of the section:
eCode360 gives no per-section date.

## Writing a different plugin

To serve a source other than eCode360, see [Custom Plugins](CUSTOM_PLUGINS.md).
One deployment runs exactly one plugin.
