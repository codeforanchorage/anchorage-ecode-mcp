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
from the table of contents. It also accepts the parts of the code that are
cited by name, not number: `Charter`, one article of it (`Charter Article IV`,
`Article 13 of the Charter`), `Part I` to `Part III`, and `Regulations`. A bare
`Article IV` is refused, since it does not say it is the Charter's.
`get_section`, `get_sections` and `browse_structure` take the same names
(`get_section` cannot return text for a whole Part; it says to browse it
instead). eCode360 has no scoped search, so the server does
the filtering: it reads up to 200 hits for the query (10 upstream pages),
places each one using the table of contents, and returns those under the
requested node.

- With `within`, `page` moves through the query's hits 200 at a time.
- `total_count` is the number of hits inside the scope when every hit for the
  query was checked, and `null` otherwise, with a `scope_scan_partial` caveat.
- PDF documents have no place in the outline and are left out, with a
  `scope_unplaced_hits` caveat that names them (the first three). The exception is a PDF attached to a whole
  title (the Charter Commission Commentary, the Title 21 use tables): it is
  kept when `within` takes in that title, and left out with the caveat when
  `within` is a chapter or section of it.
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

A section with no children that is a heading for other sections (see `parts`
under `get_section`) lists those sections as `parts`, with a
`section_text_in_parts` caveat in place of the usual note that the node is a
leaf.

### `get_section(guid, offset=0)`

The full text of a section with its nested subsections, the public
`ecode360.com` link, and a list of any attachments. The `guid` argument accepts:

- a guid, such as `49422044`
- a section number, such as `21.05.010` or `§ 21.05.010`
- a chapter or title reference, such as `Chapter 17.05`, `Ch. 17.05` or `Title 21`

A bare two-part number such as `17.05` can be ambiguous, because the Charter
has a Section 17.05 and the code has a Chapter 17.05. In that case the tool
returns an error naming both, and the caller picks one.

Some PDFs are attached to a whole title rather than to a section: the Charter
Commission Commentary, the Title 21 use tables, the community council bylaws.
A section that relies on one says so in a note ("... is included as an
attachment to this title") but eCode360 gives the section no link to it. When
the text has such a note, the result lists that title's PDFs in
`title_attachments` (each with a `url` for `read_attachment`) and carries a
`title_attachments` caveat. The list is every PDF of the title, so the caller
picks the one the note names.

An editor's note arrives from eCode360 inside the text, at the place its
footnote mark stood. Where the end of the note can be told (it ends
"... included as an attachment to this title.", or it stood in the middle of
a sentence) it is shown on a line of its own. Other notes are left as
published.

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

`history` is the section's history note, parsed: one entry per ordinance in the
order printed, each with its `citation` (`AO No. 2025-36`), the `date` printed
with it (the adoption date, as `YYYY-MM-DD`), an `effective_date` when the note
gives one (`eff. 4-1-2025`), and the entry as printed. `latest_date` and
`latest_citation` name the newest dated entry, which answers "when was this
section last amended" as far as the published code knows. Old entries carry no
date. `history` is null for a node with no note of its own, such as a chapter.

The note is printed at the end of a section. When the text block stops before
the end, the header repeats the note (up to 3,000 characters, newest entries
kept), so finding out which ordinances amended a long section does not take a
read of its last part.

`parts` is filled for a section that is only a heading. The building codes of
Title 23 print a heading as a section with no text, and its parts (numbered
`N.1`, `N.2`) as separate sections beside it in the chapter. Reading the
heading returns no text, so the result lists the sections the outline numbers
under it, each with its guid, and carries a `section_text_in_parts` caveat.
Read them with `get_sections`. `parts` is empty for every other section.

### `get_sections(sections)`

Up to 10 sections in one call, each given as a guid or a number. The list is
also accepted under the names `guids` and `guid`, and written out as a JSON
string. A section that
cannot be fetched is reported in its place with a `section_unavailable` caveat,
and the rest are still returned.

The sections of a batch share 80,000 characters of text, with at least 8,000
each. A section shorter than its even share leaves the rest to the longer
ones, so one long section among short ones is shown whole when the batch has
room. One that is cut short names the `get_section` offset to read on from.

### `find_definition(term)`

How the code defines a term: the defined term, its full text, and the section
it is defined in. A term defined in more than one place returns up to 8
definitions with a `multiple_definitions` caveat.

A definition that contains a list is rendered as `get_section` renders it:
the list is placed after the colon that introduces it. When that placement
was inferred, or could not be worked out, the result carries a
`list_position_inferred` caveat naming the definition(s) by their number in
the result.

### `recent_ordinances(within=None, limit=25)`

The ordinances most recently worked into the code, newest first, for questions
such as "what changed recently" or "has Title 21 been amended this year".
eCode360 has no endpoint for changes, so the tool reads a section of the code
itself: the publisher's Ordinance List and Disposition Table (`§ OL.010`),
found by its title in the table of contents. The API returns that table as one
run of text, which the tool splits into rows.

Each row has the `ordinance` number as printed, the `adopted` date, the
`subject`, the `chapters` it was codified in (`21.05`, `R11.10` for a
regulation, `Charter`), the `supplement` number when printed, a `search_query`
and the row as printed. The table names chapters, not sections: passing the
`search_query` (`"2026-40"`, the number in quotes) to `search_code` lists the
sections whose history note cites the ordinance. It is the bare number, not
`"AO No. 2026-40"`: history notes of 2012 to 2014 write `AO 2013-117` with no
"No.", which that phrase misses (1 section against 68, checked live).

- `within` keeps the ordinances codified in a title or chapter: `Title 21`,
  `Chapter 21.05`, `21`, `R21` or `Charter`. A section number narrows to its
  chapter.
- `limit` is 1 to 100. `total_count` is the number of matching rows.
- `covers` gives the size and date range of the whole table. It holds roughly
  the last year or two of ordinances already in the published code. An ordinance
  adopted since the code was last updated is not in it, and older history is in
  the Ordinance Disposition Table (`§ CCT.020`), which `find_ordinance` reads.
- A few rows have no adoption date in the table; they are listed last with an
  `ordinance_rows_undated` caveat.
- `not_yet_codified` lists the ordinances adopted but not yet worked into the
  code text, which the publisher holds as PDFs. They are newer than every row
  of the table. One search (`title:"AO No"`) finds them; on 2026-11-19 there
  were two, and none of the table's 54 ordinances had a PDF, because a PDF
  exists only until its ordinance is codified. `within` does not filter them:
  nothing says which chapters they amend until they are read. Each has a `url`
  for `read_attachment`, and the result carries an
  `ordinances_not_yet_codified` caveat. If the search fails the list is empty
  and the rest of the result is unaffected.
- Each of those ordinances also has a `subject`, the ordinance's own title
  read from the first page of its PDF ("AN ORDINANCE ... AMENDING ANCHORAGE
  MUNICIPAL CODE CHAPTER ..."), and `amends`, the chapter and section numbers
  that title names. With `within`, `in_scope` says whether the title names
  that title or chapter, and those that do are listed first. This lets a
  caller pick which PDFs to read; before it, finding the ones that touch a
  given title meant reading every PDF in full. A title lists what its drafter
  chose to, so `in_scope: false` is not proof an ordinance leaves that part
  of the code alone. Titles are cached per container and in the shared cache
  bucket; one that cannot be read is null and nothing else is affected.
  `find_ordinance` returns the same `subject` and `amends` in
  `ordinance_pdfs`.
- If the table is missing or its layout changes so that no row can be read, the
  result is empty with an `ordinance_list_unavailable` caveat, which is not
  evidence that nothing changed.

### `find_ordinance(ordinance)`

The code sections one ordinance added, amended or repealed, and when it took
effect, for questions such as "what did AO 2025-94 change" or "which sections
did ordinance 2011-113 touch". It reads the publisher's Ordinance Disposition
Table (`§ CCT.020`), found by its title in the table of contents: about 1,560
ordinances from the 1977 code onward, one row per provision changed.

`ordinance` is the number, with or without a prefix or version suffix:
`2025-94`, `AO 2025-94`, `AO No. 2025-115(S-2)`. Rows are matched on the base
number, so every version the table prints is returned. Years before 2000 have
two digits (`85-8` is no. 8 of 1985; `1985-8` is also accepted), and 1975-76
ordinances are written number first (`13-75`). A resolution (`AR 2025-94`) is
refused: resolutions share the numbering but are not in the table.

Each match has the `ordinance` number as printed, its `effective_date`, its
`entries` and the distinct `sections` they name (ready for `get_sections`). An
entry has the `ordinance_section` that made the change, the `action` when the
table marks one (`added`, `repealed`, `amended`, `renumbered`), the
`disposition` as printed and the section numbers in it. Each number is checked
against the table of contents: `sections` holds those that are sections of
the code today, `sections_not_in_code` those repealed or renumbered since (or
mistyped in the table), which `get_sections` would not find.

- The dates are effective dates, unlike `recent_ordinances`, which gives
  adoption dates. A date that is not one plain date is kept as printed in
  `effective_as_printed` (`12-8-2020/election of 4-6-2021`), with
  `effective_date` null.
- When the ordinance is also in the Ordinance List (`§ OL.010`), `listing`
  carries its adoption date, subject and chapters.
- When the table does not list the ordinance (usually because it is too new),
  `citing_sections` lists the sections of the code that cite it, found by one
  search for its number. A section cites an ordinance in its history note, so
  these are the sections it enacted or amended. Each has `cited_as`, the
  citation as printed (`AO No. 2025-112, § 12, 10-21-2025`: the ordinance's
  own section, then its adoption date). A hit is kept only when its snippet
  shows the number after "AO", which leaves out the ordinance tables, PDFs
  and resolutions with the same number. Up to 60 hits are checked;
  `citing_sections_complete` is false when there were more. The result carries
  an `ordinance_citing_sections` caveat, and its summary says how many
  sections cite the ordinance.
- When the publisher holds the ordinance itself as a PDF (adopted, not yet
  codified), `ordinance_pdfs` gives its title and a `url` for
  `read_attachment`, with an `ordinance_pdf_available` caveat. It is matched
  on the number in the PDF's title, never on a mention in its text.
- The table is a finding aid, not the ordinance. A section it names may have
  changed since; every result says so in an `ordinance_table_scope` caveat.
- `found` is false with an `ordinance_not_in_table` caveat when the table has
  no row. That is not evidence the ordinance does not exist: the table leaves
  out ordinances that were repealed, superseded or not of a general and
  permanent nature, and one newer than its last row. `search_query` finds the
  sections whose history cites the number.
- A number listed under more than one form (`2008-134` and `2008-134(S)`, or a
  later `Amd. of` row) returns one match per form with an
  `ordinance_several_versions` caveat.
- The table is typed by hand and the API returns it as one run of cells, so the
  parser guesses nothing: a cell that does not read as a section number or a
  date stays in the disposition as printed.
- If the table is missing or no row can be read, the result has an
  `ordinance_table_unavailable` caveat.
- eCode360 takes about 9 seconds to return this table, so it is cached for an
  hour per Lambda container, and a copy is kept in the shared cache bucket
  (`ECODE_SHARED_CACHE_BUCKET`, created by `terraform/aws/shared_cache.tf`)
  so a new container reads it from S3 instead of waiting on eCode360. A
  stored copy is used only if it was saved under the code's current
  last-updated date and is under 7 days old. Without the bucket (local
  development) the table is fetched from eCode360 as before.

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

### `read_attachment(url, page=1, layout=true, find=None)`

The text of a PDF the code publishes as an attachment. Some tables exist only
as PDFs, notably Table 21.05-1, the table of allowed uses by zoning district.
Pass a URL from the `attachments` list of `get_section`, or from a
`search_code` result marked as a PDF.

- `page` selects where to start in a long document; the result says whether
  more pages remain.
- `layout=true` keeps the column alignment of tables. Set it to `false` for
  plain reading order.
- `find` locates a word, phrase or section number in the document instead of
  returning page text. Some attached documents run to hundreds of pages and a
  call returns about a dozen, so paging is no way to reach a passage. The
  result has an empty `pages` list and a `find` object: the pages the phrase
  is on (the first 25), how often on each, and a snippet or two. Matching
  ignores case and line breaks and has no operators. If the phrase occurs
  nowhere, pages containing all of its words are listed instead, marked
  `all_words` with a `find_words_not_phrase` caveat. Searching starts at
  `page`. Then read a page with `page=<that page>`.

  Finding needs the text of every page. That is kept per container, and for
  documents of 20 pages or more in the shared cache bucket. Stored text
  carries the SHA-256 of its PDF: once the code's last-updated date has
  changed, or the object is over 7 days old, the PDF is downloaded again and
  the text is kept only if the file is unchanged. Extracting a very long document can take more than one call: the
  result then carries a `find_incomplete` caveat saying which pages were
  searched and `next_page` to continue from, and the pages already extracted
  are kept, so the following call picks up where this one stopped.

It also reads an ordinance that `search_code` returns as a PDF (a hit of type
`law`, labelled "Ordinance PDF", with a URL like
`https://live.ecode360.com/AN6998/laws/LF2907762.pdf`). These are ordinances
adopted but not yet worked into the code text. The result leads with an
`ordinance_pdf` caveat: it is the ordinance, not the code; text extraction
drops underlining and strikethrough, so bracketed text (deleted language)
reads as if it were law; and the PDF is the place to see what was added and
removed.

Only `https` URLs on `live.ecode360.com` and `ecode360.com` with one of three
path shapes are accepted. See [Security](SECURITY.md) before changing that.

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
| `ordinance_list_scope` | On every `recent_ordinances` result: what the table covers, that dates are adoption dates, and how to get from an ordinance to its sections |
| `ordinance_table_scope` | On every `find_ordinance` match: the table is a finding aid, dates are effective dates, unmarked rows do not say how a provision changed |
| `ordinance_not_in_table` | `find_ordinance` found no row: what the table leaves out, and how to search for the number instead |
| `ordinance_several_versions` | `find_ordinance` found the number under more than one printed form |
| `ordinance_entries_truncated` | `find_ordinance` matched more than 300 rows and shows the first 300 |
| `ordinance_table_unavailable` | The Ordinance Disposition Table is missing or could not be parsed |
| `relaxed_match` | A quoted phrase matched nothing; the same words in any order matched the sections listed under `relaxed_match` |
| `definition_lookup` | A zero-result query read like a definition lookup; the definition is in `related_definitions` |
| `node_too_large` | A whole chapter or title exceeded 200,000 characters and was cut; read its parts individually |
| `wildcard_in_quotes` | An empty search had a `*` inside double quotes; a quoted wildcard often matches nothing, so the message suggests the unquoted form |
| `proximity_ignored` | The query used `"a b"~N`. eCode360 has no proximity search, so the phrase was matched exactly; the message suggests the words joined with `AND` |
| `title_attachments` | A note in the section says something is "included as an attachment to this title"; the title's PDFs are listed in `title_attachments` |
| `ordinance_pdf` | `read_attachment` read an ordinance PDF: it is not code text, and extraction loses the marks that show deleted language |
| `find_incomplete` | `read_attachment` with `find`: only part of the document has been searched so far; call again from `next_page` |
| `find_more_pages` | `read_attachment` with `find`: more than 25 pages match; the rest follow from `next_page` |
| `find_words_not_phrase` | `read_attachment` with `find`: the phrase occurs nowhere; the pages listed contain all of its words, apart |
| `find_snippets` | `read_attachment` with `find`: snippets only locate the phrase; read the page before quoting |
| `ordinance_pdf_available` | `find_ordinance`: the publisher holds this ordinance as a PDF; the message gives the link |
| `ordinance_citing_sections` | `find_ordinance`: the table does not list the ordinance, so the sections that cite it were found by search; the message lists them with their citations |
| `ordinances_not_yet_codified` | `recent_ordinances`: ordinances adopted but not yet in the code text, newer than the whole list, with their PDF links |
| `title_field_is_heading` | A search used `title:` with a number; `title:` matches headings, and `within` is the way to limit a search to a Title |
| `scope_scan_partial` | A `within` search checked 200 hits and the query has more; the in-scope count is not complete |
| `no_match_in_scope` | The query matches elsewhere in the code but not inside the `within` scope |
| `scope_unplaced_hits` | PDF documents and nodes outside the outline were left out of a `within` search |
| `section_unavailable` | One section of a batch could not be fetched |
| `section_text_in_parts` | The section is a heading with no text; `parts` lists the sections numbered under it, which hold the text |
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
