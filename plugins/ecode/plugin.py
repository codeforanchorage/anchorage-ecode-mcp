"""eCode360 (General Code) municipal code plugin for OpenContext.

Provides conversational, read-only access to a single municipality's published
municipal code hosted on the eCode360 platform (the "EcodeGateway" API at
https://api.ecode360.com). The municipality is fixed per deployment by
``customer_id`` in config (e.g. ``AN6998`` for the Municipality of Anchorage),
so the tools never ask the model for a customer id.

API reference (v2.7): two API-key headers (``api-key`` / ``api-secret``) on
every request; all endpoints are GET under ``/v1`` and return JSON.
"""

import asyncio
import contextvars
import datetime
import hashlib
import io
import html
import json
import logging
import os
import re
import time
import zlib
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import quote, unquote, urlsplit

import httpx

from core.interfaces import (
    MCPPlugin,
    PluginType,
    ToolDefinition,
    ToolInputError,
    ToolResult,
)
from plugins.ecode.config_schema import EcodePluginConfig

logger = logging.getLogger(__name__)

# Public, human-facing URL for any node — its guid is the path segment.
PUBLIC_URL = "https://ecode360.com/{guid}"

# Cap on the rendered section text in one response. It was 14,000 at
# first, which cut long sections short (§ 9.28.020 is 27,585 characters,
# § 9.48.130 18,745): models did not go back for the rest, and a client
# that shows the model only the text block never sees
# structuredContent.text. 40,000 returns most long sections whole;
# anything longer is read on with get_section's `offset`.
MAX_CONTENT_CHARS = 40_000

# Node guids are opaque alphanumeric identifiers (in this library, all
# numeric -- e.g. 49401992). ROOT is the reserved alias for the top of the
# structure tree. Section NUMBERS (21.05.010) are a different namespace and
# are never accepted as guids by the API.
GUID_RE = re.compile(r"^[A-Za-z0-9]+$")

# eCode360 statuses that mean the CALLER asked for something wrong rather
# than the server failing: 404 (no such node) and 400 (malformed request).
# 401/403 are our credentials and 5xx is upstream -- both genuine faults.
CALLER_ERROR_STATUSES = frozenset({400, 404})

# A section REFERENCE as a caller (or a model) writes it: "21.05.010",
# "§ 21.05.010", "CCT.020", "CT-R.010". Dotted, so it can never collide
# with a guid (GUID_RE forbids the dot).
SECTION_REF_RE = re.compile(
    r"^(?:§\s*)?(?=.*\d)([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)$"
)

# A reference as people write it, with an optional word saying what KIND of
# node is meant: "Chapter 17.05", "Ch. 17.05", "Title 21", "Section 17.05",
# "Charter Section 17.05", "AMC 21.05.010", "§ 21.05.010". Group 1 is the
# kind word (may be absent), group 2 the number. A kind WORD must be
# followed by whitespace, or "CHA-CT.010" would parse as "ch" + "A-CT.010".
REF_WITH_KIND_RE = re.compile(
    r"^(charter\s+(?:(?:section|sec\.?)\s+|§\s*)"
    r"|(?:title|ttl\.?|chapter|ch\.?|section|sec\.?|amc)\s+"
    r"|§+\s*)?"
    r"([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*)$",
    re.IGNORECASE,
)
# Look a section number up in the table of contents before falling back
# to search (see _resolve_reference). A switch only so tests that script
# the upstream responses in order can keep the search path.
RESOLVE_SECTIONS_FROM_OUTLINE = True
KIND_TITLE = "title"
KIND_CHAPTER = "chapter"
KIND_SECTION = "section"
# What the table of contents calls each kind (its `label`), lowercased.
_TOC_LABEL_KINDS = {
    "title": KIND_TITLE,
    "chapter": KIND_CHAPTER,
    "regulation": KIND_CHAPTER,
    "section": KIND_SECTION,
}
# The Charter's index number in the table of contents. Charter sections are
# numbered like code CHAPTERS ("Section 17.05" vs "Ch 17.05"), which is why
# a bare two-part number can be ambiguous.
CHARTER_INDEX_NUM = "ttl cha"

# ── Upstream resilience ─────────────────────────────────────────────────
#
# One tool call may make more than one upstream request (a retry, or a
# section-number lookup followed by the fetch). Each request is capped at
# the plugin `timeout` (20s), so without a shared budget two of them could
# run past the Lambda's 28s and get it killed mid-flight -- the opaque 502
# the timeout ladder exists to prevent. Every request in a call therefore
# draws from ONE budget, kept under lambda_timeout with room to format and
# return. tests/test_deployment_config.py pins it below lambda_timeout.
TOOL_CALL_BUDGET_S = 24.0

# Transient failures (connection reset, DNS blip, upstream 5xx) are retried
# while the call is young. A TIMEOUT is never retried: it already spent the
# full per-request timeout, and a second one cannot fit in the budget.
MAX_ATTEMPTS = 3
RETRY_BACKOFF_S = 0.4
# Logged when eCode360 refuses the server's credentials. A CloudWatch
# metric filter matches this exact phrase (terraform/aws/
# cloudwatch_alarms.tf), so change the two together.
UPSTREAM_AUTH_LOG = "upstream auth failure"
RETRY_ONLY_WITHIN_S = 6.0

# Deadline (monotonic seconds) of the tool call currently executing. A
# contextvar, not an attribute: the local server runs calls concurrently on
# one plugin instance.
_call_deadline: contextvars.ContextVar[Optional[float]] = (
    contextvars.ContextVar("ecode_call_deadline", default=None)
)

# The TOC (~900 KB, ~0.9s per fetch, measured 2026-11-12) and the customer
# record change only when the code is republished, so they are cached per
# Lambda container. An hour bounds how stale a republish can look.
STATIC_CACHE_TTL_S = 3600.0

# (base_url, path) -> (expires_at monotonic, json). MODULE level on purpose:
# the Lambda adapter shuts the plugin down and builds a new instance on
# every invocation, so a cache held on the instance never saw a second
# request (18 table-of-contents calls -> 18 upstream fetches over 14 days
# of prod logs, 2026-11-15). The module outlives the instance for as long
# as the container stays warm. Plain data only -- nothing here is bound to
# an event loop. The key carries base_url and the path carries the
# customer id, so two differently-configured plugins cannot share entries.
_STATIC_CACHE: Dict[Tuple[str, str], Tuple[float, Any]] = {}

# ── Shared cache (2026-11-19) ───────────────────────────────────────────
#
# _STATIC_CACHE lives in one Lambda container. eCode360 takes about 9 s to
# return the Ordinance Disposition Table, so every new container made one
# caller wait that long, and a burst of simultaneous lookups landed on new
# containers that all fetched it at once (14-16 s each, measured). When
# ECODE_SHARED_CACHE_BUCKET names an S3 bucket, that one response is also
# kept there, where every container can read it in tens of milliseconds.
#
# An object is used only if it was stored under the code's CURRENT
# last-updated date (the table is part of the code, so it cannot change
# without that date changing) and is younger than SHARED_CACHE_MAX_AGE_S.
# The bucket is an optimisation and never a dependency: unset, unreachable,
# slow, or holding something unexpected, the table is fetched from
# eCode360 exactly as before and the call does not fail.
SHARED_CACHE_ENV = "ECODE_SHARED_CACHE_BUCKET"
SHARED_CACHE_VERSION = 1
SHARED_CACHE_MAX_AGE_S = 7 * 24 * 3600.0
# Total wait for one S3 call before giving up on the shared cache.
SHARED_CACHE_TIMEOUT_S = 4.0
SHARED_CACHE_MAX_BYTES = 5_000_000
# (bucket, client) once built; False once known to be unavailable. Module
# level for the same reason as the caches: the plugin is rebuilt per call.
_SHARED_CACHE_CLIENT: Dict[str, Any] = {}


class _SharedCacheUnusable(Exception):
    """A shared-cache object or payload that must not be used. Internal:
    always caught where it is raised and logged, never shown to a caller.
    """

# Search pages, section text and structure nodes are cached for a few
# minutes, also per container. A conversation re-reads the same things in
# bursts: get_section after a search resolved its number, each `offset`
# part of a long section (every part used to re-fetch the whole node), a
# scoped search paged or repeated. Each hit is one fewer request on the
# licensed upstream API. The TTL is short because nothing says when the
# code is republished; five minutes bounds how long an amended section
# can read stale.
RECENT_CACHE_TTL_S = 300.0
# Bounds, in characters of JSON. A whole title can be megabytes ("Title 21"
# is 3.5 MB), so one large node is not kept, and the oldest entries go
# first once the total is reached. 20M characters is a few tens of MB of
# parsed objects against a 1024 MB function.
RECENT_CACHE_ENTRY_CHARS = 1_000_000
RECENT_CACHE_TOTAL_CHARS = 20_000_000
# (base_url, path, params) -> (expires_at monotonic, json, chars), oldest
# first. Entries are shared between calls: READ ONLY, never mutate one.
_RECENT_CACHE: Dict[
    Tuple[str, str, Tuple[Tuple[str, Any], ...]], Tuple[float, Any, int]
] = {}

# ── Batch reads and definitions ─────────────────────────────────────────
#
# get_sections exists because models read a chapter one get_section at a
# time (79 bursts in 14 days of prod logs, the longest 19 calls), each a
# separate Lambda invocation.
MAX_BATCH_SECTIONS = 10
# Upstream requests in flight at once for one tool call.
BATCH_CONCURRENCY = 5
# Rendered text in a batch: the sections read share BATCH_TOTAL_CHARS,
# and none gets less than BATCH_SECTION_CHARS. A section shorter than its
# even share passes what it does not use to the longer ones, so one long
# section among short ones is shown whole when the batch has room for it.
# Two long sections get 40,000 each, ten get 8,000 each. A section cut
# short names the get_section offset to read on from; the full text is in
# structuredContent.
BATCH_TOTAL_CHARS = 80_000
BATCH_SECTION_CHARS = 8000
# Titles of left-out PDF documents named in a scoped search's note.
UNPLACED_NAMES_SHOWN = 3

# eCode360 publishes each defined term as its own node, numbered as the
# defining section plus an index in braces: "8.05.015A{9}", titled with the
# term in quotes. title:"term" finds them (verified live 2026-11-15).
DEFINITION_NUMBER_RE = re.compile(r"^(.*)\{\d+\}$")
MAX_DEFINITIONS = 8
MAX_TERM_CHARS = 100
# Caps on the free-text search arguments. A real query is a phrase or a
# few terms joined with AND; `within` is a short reference. The caps keep
# a caller from pushing kilobytes of text through to the upstream API
# (a scoped search sends the query up to SCOPE_SCAN_PAGES times).
MAX_QUERY_CHARS = 2000
MAX_WITHIN_CHARS = 200
# Highest search page accepted. eCode360 answers HTTP 500 once
# page x 20 overflows a 32-bit integer (about page 107 million), which
# was retried three times and logged as a server fault. No query has
# anywhere near this many pages: one matching every node in the code
# is under 300.
MAX_SEARCH_PAGE = 100_000

# A whole chapter or title can be read by guid or number ("Title 21" is
# 1.9 million characters, 3.5 MB of JSON -- measured 2026-11-15), and a
# Lambda response tops out at 6 MB. The structured body is therefore capped
# too, far above any real SECTION (the largest seen is 61,000 characters),
# so only containers ever hit it -- and the caveat sends the caller to the
# outline instead.
MAX_STRUCTURED_CHARS = 200_000
BATCH_STRUCTURED_CHARS = 100_000

# ── PDF attachments ─────────────────────────────────────────────────────
#
# Tables the code publishes as PDFs (the Title 21 use tables above all) are
# served from the public website, not the API. This is the ONLY place the
# server fetches a URL a caller supplied, so the URL is checked against a
# fixed host list and path shape before any request is made, redirects are
# not followed, and the API credentials are never sent (a separate client).
ATTACHMENT_HOSTS = frozenset({"live.ecode360.com", "ecode360.com"})
ATTACHMENT_PATH_RE = re.compile(
    r"^/attachment/\d+/[^/\\]+\.pdf$", re.IGNORECASE
)
# {customer} is replaced with the configured library id (re.escape'd).
DOCUMENT_PATH_TEMPLATE = r"^/documents/{customer}/(?:[^/\\]+/)*[^/\\]+\.pdf$"
# Ordinances the publisher holds as PDFs: search hits of type "law", e.g.
# https://live.ecode360.com/AN6998/laws/LF2907762.pdf. They are the
# ordinances adopted but not yet worked into the code text, reachable only
# through search. A search result already told the caller to pass such a
# link to read_attachment, which then refused it. Checked 2026-11-19:
# served without credentials, text layer present. The shape is as narrow as the links are: this library,
# "LF" and digits.
LAW_PATH_TEMPLATE = r"^/{customer}/laws/LF\d+\.pdf$"
# The publisher's "new laws": ordinances adopted but not yet worked into
# the code text, held as PDFs. Checked live 2026-11-19: there were two, and
# NONE of the 54 ordinances in the Ordinance List had one -- a PDF exists
# only until its ordinance is codified. They are reachable only by search;
# their titles all read "AO No. <number>", so this one query lists exactly
# them (total 2, both type "law"), where a search for a number alone also
# returns PDFs that merely mention it.
PENDING_ORDINANCE_QUERY = 'title:"AO No"'
PENDING_ORDINANCE_PAGES = 3
# Some PDFs are attached to a whole TITLE, not to a section: the Charter
# Commission Commentary, the Title 21 use tables, the community council
# bylaws. A section that relies on one says so in a note ("... is included
# as an attachment to this title") but its content carries no link, and
# neither does the title's structure node. Checked live 2026-11-22: 33
# sections have such a note (16 in the Charter, 13 in Title 21) and every
# one came back with `attachments` empty. The PDFs are reachable only by
# search, where each is a hit titled "Attachment <n> - <name>" whose id
# starts with its title's index number ("Ttl CHA: AN6998-CHAb Commission
# Commentary on Anchorage Municipal Charter.pdf"); this one query lists
# them all (15 PDFs across six titles).
TITLE_ATTACHMENT_NOTE_RE = re.compile(
    r"included\s+as\s+(?:an\s+)?attachments?\s+to\s+this\s+title",
    re.IGNORECASE,
)
TITLE_ATTACHMENT_QUERY = "title:attachment"
TITLE_ATTACHMENT_PAGES = 3
TITLE_ATTACHMENT_ID_RE = re.compile(r"^(Ttl\s+\S+?):\s")
# "AN6998-CHAb Commission Commentary on Anchorage Municipal Charter.pdf"
# -> "Commission Commentary on Anchorage Municipal Charter".
TITLE_ATTACHMENT_FILE_RE = re.compile(
    r"^[A-Z0-9]+-\S+\s+(.+?)\.pdf$", re.IGNORECASE
)
_TITLE_ATTACHMENTS_KEY = "title-attachments"
# Each of those ordinances states what it does in its own title, on the
# first page: "AN ORDINANCE OF THE ANCHORAGE MUNICIPAL ASSEMBLY AMENDING
# ANCHORAGE MUNICIPAL CODE CHAPTER 4.60 AND 27.30.085 TO ENACT ...". The
# list gave only numbers, so to answer "what changed in" a given title a
# model had to read every PDF in full. Checked on the three PDFs live on
# 2026-11-20: the title starts at
# "AN ORDINANCE", runs to a full stop at the end of a line, and each line
# ends with its drafting line number (1, 2, 3 ...), which is dropped.
# Sections that cite an ordinance, for one the disposition table does not
# list (too new, usually). A section's history note is where it is cited:
# "(AO No. 2025-112, § 12, 10-21-2025; ...)". Checked live 2026-11-20:
# notes of 2012-2014 write "AO 2013-117" with no "No.", so the phrase
# "AO No. 2013-117" finds 1 section where the quoted number finds 68; the
# quoted number alone is an exact match ("2026-4" does not match 2026-40)
# but also returns the ordinance tables and any PDF that mentions it, so a
# hit is kept only when its snippet shows the number after "AO".
ORDINANCE_CITING_PAGES = 3
# The citation runs to the next ";" or ")", past a version suffix such as
# "(S-1)" whose own bracket must not end it.
ORDINANCE_CITATION_TEMPLATE = (
    r"AO\s+(?:No\.\s*)?{base}(?!\d)(?:\([A-Za-z0-9-]+\))?[^;)\]]{{0,80}}"
)

ORDINANCE_TITLE_START_RE = re.compile(r"\bAN\s+ORDINANCE\b")
ORDINANCE_TITLE_END_RE = re.compile(
    r"\.\s*(?:\n|$)|\(\s*AM\b|\bWHEREAS\b"
    r"|\bTHE\s+ANCHORAGE\s+ASSEMBLY\s+ORDAINS\b"
)
ORDINANCE_TITLE_NUMBER_RE = re.compile(
    r"(?<![\d.])R?\d{1,2}\.\d{1,3}(?:\.\d{1,4})?(?![\d.]*\d)"
)
ORDINANCE_TITLE_MAX_CHARS = 1500
# All the titles together must not hold a result up for longer than this.
ORDINANCE_TITLES_TIMEOUT_S = 8.0
# url -> title ("" = read, none found). A PDF never changes under its url.
_ORDINANCE_TITLE_CACHE: Dict[str, str] = {}
ORDINANCE_TITLE_CACHE_ENTRIES = 100
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
# Rendered text per call. Layout mode pads columns with spaces, so one page
# of a wide table runs ~16,000 characters.
ATTACHMENT_TEXT_CHARS = 40_000
# Stop extracting pages when less than this is left of the call budget.
ATTACHMENT_TIME_RESERVE_S = 3.0
# url -> PDF bytes, for paging through one document in a warm container
# without downloading it again. Tiny on purpose: this is not a store.
_ATTACHMENT_CACHE: Dict[str, bytes] = {}
ATTACHMENT_CACHE_ENTRIES = 2

# ── read_attachment(find=...) ───────────────────────────────────────────
#
# One attached document runs to several hundred pages and a call returns
# about a dozen, so a search hit inside it could not be reached by paging.
# `find` locates a phrase by page. That needs the text of every page, which
# takes longer to extract than one call may spend, so the text is kept:
# in this container, and in the shared cache bucket when there is one
# (see SHARED_CACHE_ENV), where a call that ran out of time leaves what it
# extracted for the next call to continue from.
#
# Stored text carries the SHA-256 of the PDF it came from. When the code's
# last-updated date moves on (or the object passes its age limit) the PDF
# is downloaded again, which takes a second, and the text is kept if the
# PDF is byte for byte the same: a code update rarely touches a given
# attachment, and re-extracting a long one costs several calls.
FIND_MAX_CHARS = 200
FIND_PAGES_LISTED = 25
FIND_SNIPPETS_PER_PAGE = 2
FIND_SNIPPET_BEFORE = 100
FIND_SNIPPET_AFTER = 140
# Stop extracting when less than this is left of the call budget: the text
# still has to be stored (one shared-cache call) and searched.
FIND_TIME_RESERVE_S = 7.0
ATTACHMENT_TEXT_PREFIX = "attachment-text"
# Shorter documents are extracted again in well under a second, so their
# text is not worth an object in the bucket.
ATTACHMENT_TEXT_SHARED_MIN_PAGES = 20
# Decompressed size above which a stored text object is not trusted.
ATTACHMENT_TEXT_MAX_BYTES = 20_000_000
# url -> {"page_count", "texts" (pages 1..n, whitespace collapsed),
# "code_date"}.
_ATTACHMENT_TEXT_CACHE: Dict[str, Dict[str, Any]] = {}
_QUOTE_CHARS_RE = re.compile('["“”]')

# ── Structured output ───────────────────────────────────────────────────
#
# A declared outputSchema is BINDING: the MCP spec says servers MUST return
# conforming structured results and clients SHOULD validate them. So nothing
# below constrains a value real eCode360 data can violate -- no `maximum` on
# counts, no `required` on a field the API can omit, and every enum is closed
# only where this code, not the upstream, decides the value.
#
# The human-readable `content` block stays markdown rather than the
# serialized JSON the spec suggests (SHOULD, not MUST): the prose carries the
# citation formatting and the "published code text, not legal advice" framing
# that make this server steer models well, and the same data rides in
# structuredContent anyway.

# Stable identifiers a caller can branch on. The rendered prose and the
# structured array are built from ONE list per response so they cannot drift.
CAVEAT_MORE_PAGES = "more_pages_available"
CAVEAT_PAGE_PAST_END = "page_past_end"
CAVEAT_DUPLICATE_SECTIONS = "duplicate_sections_in_page"
CAVEAT_BODY_IS_ATTACHMENT = "body_is_attachment"
CAVEAT_RENDERED_TEXT_TRUNCATED = "rendered_text_truncated"
CAVEAT_SECTION_NOT_IN_FORCE = "section_not_in_force"
CAVEAT_NODE_HAS_NO_CHILDREN = "node_has_no_children"
CAVEAT_TOC_ENDPOINT_UNAVAILABLE = "toc_endpoint_unavailable"
CAVEAT_RESOLVED_FROM_SECTION_NUMBER = "resolved_from_section_number"
CAVEAT_TERMS_OR_COMBINED = "terms_or_combined"
CAVEAT_NO_MATCH_IN_CODE = "no_match_in_code"
CAVEAT_STATE_LAW_NAMED = "state_law_named"
CAVEAT_TITLE_FIELD_IS_HEADING = "title_field_is_heading"
CAVEAT_WILDCARD_IN_QUOTES = "wildcard_in_quotes"
CAVEAT_SCOPE_SCAN_PARTIAL = "scope_scan_partial"
CAVEAT_NO_MATCH_IN_SCOPE = "no_match_in_scope"
CAVEAT_SCOPE_UNPLACED_HITS = "scope_unplaced_hits"
CAVEAT_SECTION_UNAVAILABLE = "section_unavailable"
CAVEAT_DUPLICATE_REQUESTS = "duplicate_requests_merged"
CAVEAT_NO_DEFINITION_FOUND = "no_definition_found"
CAVEAT_MULTIPLE_DEFINITIONS = "multiple_definitions"
CAVEAT_MORE_DEFINITIONS = "more_definitions_available"
CAVEAT_DEFINITION_TEXT_FROM_SNIPPET = "definition_text_from_snippet"
CAVEAT_NODE_TOO_LARGE = "node_too_large"
CAVEAT_PDF_TEXT_EXTRACTION = "pdf_text_extraction"
CAVEAT_MORE_ATTACHMENT_PAGES = "more_attachment_pages"
CAVEAT_ATTACHMENT_PAGE_TRUNCATED = "attachment_page_truncated"
CAVEAT_FIND_SNIPPETS = "find_snippets"
CAVEAT_FIND_INCOMPLETE = "find_incomplete"
CAVEAT_FIND_WORDS_NOT_PHRASE = "find_words_not_phrase"
CAVEAT_FIND_MORE_PAGES = "find_more_pages"
CAVEAT_ORDINANCE_PDF = "ordinance_pdf"
CAVEAT_ORDINANCE_PDF_AVAILABLE = "ordinance_pdf_available"
CAVEAT_ORDINANCE_CITING_SECTIONS = "ordinance_citing_sections"
CAVEAT_ORDINANCES_NOT_YET_CODIFIED = "ordinances_not_yet_codified"
CAVEAT_PROXIMITY_IGNORED = "proximity_ignored"
CAVEAT_NO_TEXT_LAYER = "no_text_layer"
CAVEAT_NO_REFERENCES_FOUND = "no_references_found"
CAVEAT_LIST_POSITION_INFERRED = "list_position_inferred"
CAVEAT_TITLE_ATTACHMENTS = "title_attachments"
CAVEAT_SUBSECTION_OF_SECTION = "subsection_read_as_whole_section"
CAVEAT_DEFINITION_LOOKUP = "definition_lookup"
CAVEAT_RELAXED_MATCH = "relaxed_match"
CAVEAT_OUTLINE_DEPTH_LIMITED = "outline_depth_limited"
CAVEAT_MORE_REFERENCES = "more_references_available"
CAVEAT_ORDINANCE_LIST_SCOPE = "ordinance_list_scope"
CAVEAT_MORE_ORDINANCES = "more_ordinances_available"
CAVEAT_ORDINANCE_LIST_UNAVAILABLE = "ordinance_list_unavailable"
CAVEAT_ORDINANCE_ROWS_UNDATED = "ordinance_rows_undated"

# ── Search-syntax guidance ──────────────────────────────────────────────
#
# Verified live against AN6998 on 2026-11-13: eCode360 joins space-
# separated terms with OR ('"liable in damages" municipality' -> 2404, the
# same as 'municipality' alone), while AND / and / + require a term (-> 1).
# NOT / not / - exclude, parentheses group, a trailing * is a wildcard, and
# title:word matches section titles. Proximity ("a b"~3) is NOT supported:
# the ~N is ignored and the phrase must match exactly ("shall not liable"~2
# -> 0 although "shall not be liable" -> 6).
#
# The server never rewrites a query; it only says when OR has probably
# flooded the result set, and only for a query with no AND / + / NOT at all
# -- one that already uses an operator was written deliberately.
OR_CAVEAT_MIN_TOTAL = 200
OR_CAVEAT_MIN_TERMS = 2
# How many of the caller's terms the suggested AND query uses, in the order
# the caller wrote them. Measured on a sample of long queries: the first
# two ANDed returned results for most, while ANDing all of them, or the
# "most specific" (longest) phrases, returned 0 for most.
OR_CAVEAT_SUGGEST_TERMS = 2

# One search token: an optionally +/- prefixed, optionally field-qualified
# quoted phrase (with any ~N suffix), a parenthesis, or a bare word.
_QUERY_TOKEN_RE = re.compile(
    r'[+-]?(?:\w+:)?"[^"]*"(?:~\d+)?|[()]|[^\s()"]+'
)
_QUERY_OPERATORS = frozenset({"and", "or", "not", "&&", "||"})
# "notice of claim"~3 -- a proximity search, which eCode360 does not have.
PROXIMITY_RE = re.compile(r'"([^"]+)"~\d+')
PROXIMITY_STOP_WORDS = frozenset(
    "a an and any as at be by for from in is it not of on or shall such "
    "that the this to with".split()
)
# Operators that narrow a result set. Lowercase works too (verified).
_NARROWING_OPERATORS = frozenset({"and", "not", "&&"})

# A query that names state law. Alaska Statutes are not part of the
# municipal code, so an empty result for one of these deserves a pointer to
# how the code actually refers to them (by citation: "AS 23.40" finds
# § 3.30.050). `AS` is matched case-sensitively so ordinary "as 2" does not
# trip it.
STATE_LAW_RE = re.compile(
    r"\bAS\s?\d|(?i:\bAlaska\s+Statute"
    r"|\bPublic\s+Employment\s+Relations\s+Act\b)"
)
# title:<digit> -- a caller trying to limit a search to a numbered Title
# of the code. title: searches headings, so title:9 matches the four
# headings containing "9" and `x AND title:9*` matches nothing. There is
# no Title filter.
TITLE_NUMBER_RE = re.compile(r'(?i)\btitle:"?\d')

# A * inside double quotes. Verified live on 2026-11-18:
# '"indemnif*" AND "insurance" AND "risk"' -> 0 while
# 'indemnif* AND "insurance" AND "risk"' -> 4. Quote pairs are taken in
# order, so the text BETWEEN two quoted terms ('"a" AND b* AND "c"') is not
# mistaken for a quoted term.
# find_references: a full section number, optionally with a subsection
# suffix ("9.28.019", "9.28.019B.2", "R3.70.120"). A chapter number
# ("9.28") is refused: 9.28* matches every section of the chapter.
# Verified live on 2026-11-18: the traffic fine schedule (§ 9.48.130) cites
# subsections ("§ 9.14.040A.3.a $200.00"), so '"9.14.040"' misses it (3
# hits) while 9.14.040* finds it (4 hits, the snippet carrying the rows).
MAX_REFERENCE_CHARS = 40
REFERENCE_NUMBER_RE = re.compile(
    r"^([A-Za-z]{0,2}\d{1,3}\.\d{1,3}\.\d{1,4})([A-Za-z0-9.()]*)$"
)
REFERENCE_PREFIX_RE = re.compile(r"(?i)^(?:§+|sec(?:tion|\.)?)\s*")
# A subsection as the published code writes it: levels joined by full
# stops, the first one directly after the number ("B", "B.2", "A.3.a").
REFERENCE_SUBSECTION_RE = re.compile(r"^[A-Za-z0-9]+(?:\.[A-Za-z0-9]+)*$")
REFERENCE_TABLE_REF_RE = re.compile(r"^[A-Z]{2,}\.")
FINE_OR_PENALTY_TITLE_RE = re.compile(r"(?i)\b(fines?|penalt(?:y|ies))\b")
REFERENCE_TABLE_TITLE_RE = re.compile(
    r"(?i)\b(disposition|reference|comparative)\s+table\b"
)
REFERENCE_KIND_ORDER = ("fine_or_penalty", "section", "document", "reference_table")

# ── Section rendering (output audit, 2026-11-18) ────────────────────────
#
# The content endpoint flattens a paragraph that contains a list: the text
# before the list, the text after it and the section's history note arrive
# as ONE string on the parent node, and the list items as children. Charter
# Section 4.01: "...within two months from: If the assembly determines ...
# (AO No. 2006-154, ...)" with item (1) as the only child. The publisher's
# own search index shows the real order ("...two months from: (1) Adoption
# of a final state ...; If the assembly determines"). Rendering parent text
# then children therefore put the closing sentence BEFORE the list.
#
# A colon followed by a capital marks where the list was. Only a colon at
# least LEAD_IN_MIN_WORDS words in counts: a heading written with a colon
# ("Administration: An RFA shall be administered ...", "Protest: The
# assembly shall ...") is not a list lead-in. Surveyed on six chapters
# (743 nodes with children): 15 had such a colon, 6 of them headings.
LIST_LEAD_IN_RE = re.compile(r':\s+(?=["\u201c(]?[A-Z])')
# The content endpoint also flattens footnotes: an editor's note arrives
# inside the text at the place its footnote mark stood, with nothing to
# show where it stops. Charter Section 3.01 read "Editor's Notes \u2013 There
# are Charter Commission Commentary notes on this section. Said Commentary
# is included as an attachment to this title. The municipality may
# exercise all legislative powers ..." -- note and law as one paragraph.
#
# Surveyed 2026-11-22 on all 553 sections that have such a note (627
# notes). The end of a note can be told in two cases only, and those are
# the ones put on a line of their own:
#   - it ends "... included as an attachment to this title." (45 notes);
#   - it stood in the middle of a sentence, which leaves a full stop and
#     TWO spaces where the sentence resumes ("See AS 23.40.205.  any
#     federal or state regulation", 12 notes). Two spaces alone prove
#     nothing (492 in those sections, 13 after a note), so the note must
#     also be short.
# The rest either run to the end of their node (450, already unambiguous
# to a reader), sit inside the history note (78), or are followed by law
# after a single space with no mark at all; those are left as published.
_EDITOR_NOTE_START = r"Editor[\u2019']s\s+[Nn]otes?\s*(?:[\u2013\u2014-]|:)\s"
EDITOR_NOTE_RE = re.compile(
    r"\s*("
    + _EDITOR_NOTE_START
    + r"(?:(?!Editor[\u2019']s\s+[Nn]ote).){0,400}?"
    r"attachments?\s+to\s+this\s+title\s*\."
    r"|"
    + _EDITOR_NOTE_START
    + r"(?:(?!Editor[\u2019']s\s+[Nn]ote).){0,300}?\.(?=  +\S)"
    r")\s*",
    re.DOTALL,
)
# The quotation marks a defined term is published in.
_TERM_QUOTES = '"\u201c\u201d'
LEAD_IN_MIN_WORDS = 10
# The history note that closes a section: "(AO No. 69-75; AO No. 2023-63,
# § 1, 6-6-2023)", "(CAC 10.48.200; ...)", "(AR No. 97-180, ...)".
HISTORY_NOTE_RE = re.compile(
    r"\s*(\((?:AO|AR|EO|AM|CAC|GAAB|CBC|Ord\.|Res\.|Code)\b"
    r"(?:[^()]|\([^()]*\))*\))\s*$"
)

# ── Zero-result follow-ups (output audit, 2026-11-18) ──────────────────
#
# '"Labor agreement means"' -> 0, yet § 3.70.010 defines the term: the
# definition is its own node whose TITLE is the term and whose body starts
# "means ...", so no phrase spans the two. A query that is one quoted
# phrase and nothing else gets two follow-ups: a definition lookup when it
# reads like one, and the same words ANDed (7 hits for that example, the
# definition first).
SINGLE_PHRASE_RE = re.compile(r'^\s*"([^"]+)"\s*$')
DEFINITION_QUERY_RE = re.compile(
    r"(?i)^(?:the\s+(?:term|word|phrase)\s+)?(.+?)\s+"
    r"(?:means|shall\s+mean|is\s+defined\s+as|is\s+defined|defined\s+as)$"
)
RELAXED_MIN_WORDS = 2
RELAXED_MAX_WORDS = 8
RELAXED_MAX_ROWS = 10
RELATED_DEFINITIONS = 3
RELATED_DEFINITION_CHARS = 600

# Levels of the outline get_table_of_contents returns: parts, titles and
# chapters (5 + 57 + 422 nodes for AN6998, about 34,000 characters). The
# next level is 4,300 sections and 350,000 characters.
TOC_DEPTH = 3

# See _repair_search_json. A hit's `hits` object holds no nested braces.
BROKEN_HITS_RE = re.compile(r'"hits"\s*:\s*\{[^{}]*\}')

QUOTED_TERM_RE = re.compile(r'"([^"]*)"')
QUOTED_SINGLE_WILDCARD_RE = re.compile(r'"([^"\s]*\*[^"\s]*)"')

# ── Scoped search (`within`) ────────────────────────────────────────────
#
# eCode360 search cannot be limited to a title or chapter: verified live
# 2026-11-17, the endpoint ignores every scoping and page-size parameter
# and has no query field for a node's number or parent. So the server
# filters. It reads up to SCOPE_SCAN_PAGES upstream pages (20 hits each)
# per call and keeps the hits that sit under the requested node. Nine
# pages fetched concurrently measured 0.6-1.0 s.
#
# A hit is placed by its guid in the cached table of contents (about 92%
# of hits measured), else by the section its number extends -- definition
# and subsection nodes such as 9.04.010B{43} are not in the outline but
# belong to § 9.04.010. PDF documents have no place and are left out.
SCOPE_SCAN_PAGES = 10
SEARCH_PAGE_SIZE = 20
# Named state acts whose citation is known, so the hint can be concrete.
STATE_LAW_CITATIONS = {
    "public employment relations act": "AS 23.40",
}

# Words that, in a section's NUMBER/TITLE, mean the body is not operative
# law. Checked against the title only: "repealed" appears in the body of
# plenty of sections that are perfectly in force (an ordinance repealing
# something else), so matching content would produce constant false alarms.
INACTIVE_TITLE_MARKERS = (
    "reserved",
    "repealed",
    "superseded",
    "rescinded",
)
# The word alone is not enough: "Parking reserved for persons with
# disabilities" (§ 9.30.235) is in force, as is the definition of "Reserved
# area". The published code marks a dead heading in one of three ways, and
# only these count (surveyed 2026-11-20 across every heading containing one
# of the words):
#   in parentheses    "(Reserved)", "Penalty (Repealed).", "(REPEALED)",
#                     "through § 19.20.170. (Reserved)"
#   as the whole heading            "Repealed.", "Reserved"
#   leading, then who or what did it    "Repealed by AO No. 98-51(S)",
#                                       "Superseded by Ord. 2020-1"
_INACTIVE_WORDS = "|".join(INACTIVE_TITLE_MARKERS)
INACTIVE_TITLE_RE = re.compile(
    rf"\(\s*(?P<paren>{_INACTIVE_WORDS})\b[^)]*\)"
    rf"|^\W*(?P<whole>{_INACTIVE_WORDS})\W*$"
    rf"|^\W*(?P<lead>{_INACTIVE_WORDS})\s+(?:by|per|under|pursuant)\b",
    re.IGNORECASE,
)

_CAVEAT_SCHEMA = {
    "type": "array",
    "description": (
        "Qualifications that change how the result must be read. Each has a "
        "stable `code` so a caller can branch on it instead of noticing prose."
    ),
    "items": {
        "type": "object",
        "properties": {
            "code": {"type": "string"},
            "message": {"type": "string"},
        },
        "required": ["code", "message"],
    },
}

# The building codes of Title 23 print a section heading ("§ 23.10.103
# Enforcement.") as a section with no text, and its parts ("§ 23.10.103.1",
# "§ 23.10.103.2") as separate sections beside it in the chapter, not as
# its children. A read of the heading then returned a bare header, and the
# structure endpoint lists no children for it. Such a read now lists the
# sections the outline numbers under the heading.
CAVEAT_SECTION_TEXT_IN_PARTS = "section_text_in_parts"
PARTS_SHOWN = 40

_PARTS_SCHEMA = {
    "type": "array",
    "description": (
        "Filled only for a section that is a heading with no text of its "
        "own: the sections the outline numbers under it (N.1, N.2 ...), "
        "which hold its text. Read them with get_sections. Empty "
        "otherwise."
    ),
    "items": {
        "type": "object",
        "properties": {
            "guid": {"type": "string"},
            "ref": {"type": "string"},
            "title": {"type": ["string", "null"]},
            "url": {"type": "string"},
        },
        "required": ["guid", "ref", "title", "url"],
    },
}

# A node as it appears in the outline. `ref` is the section number
# (e.g. "21.05.010"); `guid` is the opaque id every other tool takes.
_NODE_SCHEMA = {
    "type": "object",
    "properties": {
        "guid": {"type": "string"},
        "ref": {"type": ["string", "null"]},
        "title": {"type": ["string", "null"]},
        "label": {
            "type": ["string", "null"],
            "description": (
                "What KIND of node this is in the code's own vocabulary -- "
                "Title, Part, Chapter, Article, Litem. This is the field the "
                "structure endpoint actually populates on children; `type` "
                "is only present on the node itself."
            ),
        },
        "type": {"type": ["string", "null"]},
    },
    "required": ["guid"],
}

def _outline_node_schema(levels: int) -> Dict[str, Any]:
    """_NODE_SCHEMA with `children` nested `levels` deep (no $ref, so every
    client can validate it)."""
    schema = {
        "type": "object",
        "properties": dict(_NODE_SCHEMA["properties"]),
        "required": ["guid"],
    }
    schema["properties"]["children_not_listed"] = {
        "type": "integer",
        "description": (
            "Children this node has that are NOT listed here (the outline "
            "stops at chapters). Call browse_structure with this node's "
            "guid to list them."
        ),
    }
    if levels > 1:
        schema["properties"]["children"] = {
            "type": "array",
            "items": _outline_node_schema(levels - 1),
        }
    return schema


# Files published alongside a node (mostly PDF tables). `url` goes to
# read_attachment.
_ATTACHMENTS_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "url": {"type": "string"},
        },
        "required": ["name", "url"],
    },
}

_TITLE_ATTACHMENTS_SCHEMA = {
    **_ATTACHMENTS_SCHEMA,
    "description": (
        "PDFs attached to the whole title this node belongs to, listed "
        "only when a note in the text says something is 'included as an "
        "attachment to this title' (a table, an exhibit, the Charter "
        "Commission Commentary). Empty otherwise."
    ),
}

_SEARCH_ROW_SCHEMA = {
    "type": "object",
    "properties": {
        "guid": {"type": "string"},
        "ref": {"type": ["string", "null"]},
        "title": {"type": ["string", "null"]},
        "snippet": {"type": ["string", "null"]},
        "url": {
            "type": "string",
            "description": (
                "As returned by the API where it supplies one (document "
                "hits link to a PDF on live.ecode360.com); otherwise the "
                "canonical https://ecode360.com/<guid> form."
            ),
        },
        "is_pdf": {
            "type": "boolean",
            "description": (
                "The hit is a PDF document, not a code section: pass its "
                "`url` to read_attachment to read it. get_section cannot "
                "open it."
            ),
        },
    },
    "required": ["guid", "url", "is_pdf"],
}

SEARCH_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string"},
        "page": {"type": "integer", "description": "0-indexed"},
        "total_count": {
            "type": ["integer", "null"],
            "description": (
                "Total matching sections across all pages. null means the "
                "upstream did not report one (unmeasured); 0 means the search "
                "ran and matched nothing (a known, complete count). They are "
                "not interchangeable."
            ),
        },
        "summary": {"type": "string"},
        "rows": {"type": "array", "items": _SEARCH_ROW_SCHEMA},
        "caveats": _CAVEAT_SCHEMA,
        "related_definitions": {
            "type": "array",
            "description": (
                "Present only when a search that matched nothing read like "
                "a definition lookup ('\"X means\"'): what find_definition "
                "returns for the term."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "guid": {"type": "string"},
                    "term": {"type": ["string", "null"]},
                    "exact_match": {"type": "boolean"},
                    "defined_in": {"type": ["string", "null"]},
                    "text": {"type": ["string", "null"]},
                    "url": {"type": "string"},
                },
                "required": ["guid", "exact_match", "url"],
            },
        },
        "relaxed_match": {
            "type": "object",
            "description": (
                "Present only when a quoted phrase matched nothing and the "
                "same words, required in any order anywhere in a section, "
                "matched something. These rows are NOT phrase matches and "
                "are kept out of `rows` and `total_count` for that reason."
            ),
            "properties": {
                "query": {"type": "string"},
                "total_count": {"type": ["integer", "null"]},
                "rows": {"type": "array", "items": _SEARCH_ROW_SCHEMA},
            },
            "required": ["query", "total_count", "rows"],
        },
        "within": {
            "type": "object",
            "description": (
                "Present only for a search limited with `within`: the "
                "title, chapter or section the rows were filtered to. "
                "total_count then counts hits inside it, and is null when "
                "the query has more hits than one call checks (see the "
                "scope_scan_partial caveat)."
            ),
            "properties": {
                "guid": {"type": "string"},
                "display": {"type": "string"},
                "hits_checked": {
                    "type": "integer",
                    "description": "Hits for the query examined by this call",
                },
                "query_total": {
                    "type": ["integer", "null"],
                    "description": (
                        "Hits for the query in the whole code, in or out "
                        "of the scope"
                    ),
                },
            },
            "required": ["guid", "display", "hits_checked", "query_total"],
        },
    },
    "required": ["query", "page", "total_count", "summary", "rows", "caveats"],
}

# The date on every section read. It is the publication date of the WHOLE
# code, taken from the library record -- eCode360 gives no per-section
# date -- so the wording must never suggest the section itself changed then.
_CODE_LAST_UPDATED_SCHEMA = {
    "type": ["string", "null"],
    "description": (
        "Date the published code as a whole was last updated on eCode360 "
        "(YYYY-MM-DD as returned). NOT the date this section was last "
        "amended. Ordinances adopted after this date may not be reflected "
        "in the text. null when the date could not be read."
    ),
}

# Every length a section result reports, each named for what it measures.
# Before 2026-11-18 a too-large node reported "about 1,867,764 characters"
# in one message and "242,053 characters long" in another with nothing to
# say they measured different things.
_LENGTH_SCHEMA = {
    "type": "object",
    "description": (
        "Lengths in characters. Offsets (`offset`, shown_from, shown_to) "
        "all index `text` as returned in this result."
    ),
    "properties": {
        "text_chars": {
            "type": "integer",
            "description": "Length of `text` in this result.",
        },
        "shown_from": {
            "type": "integer",
            "description": "0-based start of the text block within `text`.",
        },
        "shown_to": {
            "type": "integer",
            "description": (
                "0-based end (exclusive) of the text block within `text`; "
                "pass it as `offset` to read on when it is less than "
                "text_chars."
            ),
        },
        "whole_node_chars": {
            "type": ["integer", "null"],
            "description": (
                "null when `text` is the whole node. Otherwise the node "
                "was cut (node_too_large): this is the approximate length "
                "of ALL its code text as the API holds it, of which `text` "
                "is only the first part."
            ),
        },
    },
    "required": ["text_chars", "shown_from", "shown_to", "whole_node_chars"],
}

# ── Section history (2026-11-18) ────────────────────────────────────────
#
# A section's history note lists the ordinances that enacted and amended
# it: "(AO No. 91-182, 12-17-1991; AO No. 2011-113(S), § 165, 11-22-2011,
# eff. 12-22-2011; AO No. 2026-19 Amended, 2-17-2026)". Surveyed on 45
# chapters (579 notes, 49 entry shapes): an entry is a citation, then an
# optional "§ n" of the ordinance, then an optional date (M-D-YYYY), then
# an optional "eff." / "retro eff." date. Old entries carry no date
# ("AO No. 83-49", "CAC 10.20.010", "GAAB 9.20.020").
HISTORY_DATE_RE = re.compile(r"(?<![\d-])(\d{1,2})-(\d{1,2})-(\d{4})(?![\d-])")
HISTORY_EFFECTIVE_RE = re.compile(
    r"(?i)\beff(?:ective)?\.?\s+(\d{1,2}-\d{1,2}-\d{4})"
)
HISTORY_ENTRY_CHARS = 300
# The history note is printed at the end of a section. When a response
# stops before the end, the note is repeated in the header (up to this
# many characters, newest entries kept), so that learning which ordinances
# amended a long section does not take a read of its last part.
HISTORY_HEAD_CHARS = 3000

_HISTORY_SCHEMA = {
    "type": ["object", "null"],
    "description": (
        "The section's history note, parsed: the ordinances that enacted "
        "and amended it, in the order printed (oldest first). null when "
        "the node has no history note of its own (a chapter, a title, a "
        "reserved section). The note is also in `text`, unparsed."
    ),
    "properties": {
        "entries": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "citation": {
                        "type": "string",
                        "description": (
                            "The ordinance or prior-code citation, e.g. "
                            "'AO No. 2025-36' or 'CAC 10.20.010'."
                        ),
                    },
                    "date": {
                        "type": ["string", "null"],
                        "description": (
                            "The date printed with the entry, YYYY-MM-DD: "
                            "by the code's convention the ordinance's "
                            "adoption date. null when none is printed."
                        ),
                    },
                    "effective_date": {
                        "type": ["string", "null"],
                        "description": (
                            "The date after 'eff.' when the note gives "
                            "one, YYYY-MM-DD; else null (which does not "
                            "mean the ordinance took effect on `date`)."
                        ),
                    },
                    "text": {
                        "type": "string",
                        "description": "The entry as printed.",
                    },
                },
                "required": ["citation", "date", "effective_date", "text"],
            },
        },
        "latest_date": {
            "type": ["string", "null"],
            "description": (
                "The latest `date` among the entries: when the newest "
                "listed ordinance was adopted. An ordinance adopted after "
                "the code's last update is not in the note."
            ),
        },
        "latest_citation": {"type": ["string", "null"]},
    },
    "required": ["entries", "latest_date", "latest_citation"],
}

SECTION_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "guid": {"type": "string"},
        "ref": {"type": ["string", "null"]},
        "title": {"type": ["string", "null"]},
        "url": {"type": "string"},
        "text": {
            "type": "string",
            "description": (
                "The full section body, including nested subsections. NOT "
                "clipped for any real section: the markdown block caps its "
                "rendering for readability, but truncating the "
                "machine-readable half would defeat its purpose. Only a "
                "whole chapter or title larger than 200,000 characters is "
                "cut, and then a `node_too_large` caveat says so."
            ),
        },
        "length": _LENGTH_SCHEMA,
        "history": _HISTORY_SCHEMA,
        "has_attachment": {"type": "boolean"},
        "attachments": _ATTACHMENTS_SCHEMA,
        "title_attachments": _TITLE_ATTACHMENTS_SCHEMA,
        "parts": _PARTS_SCHEMA,
        "is_pdf": {"type": "boolean"},
        "code_last_updated": _CODE_LAST_UPDATED_SCHEMA,
        "caveats": _CAVEAT_SCHEMA,
    },
    "required": [
        "guid",
        "url",
        "text",
        "has_attachment",
        "is_pdf",
        "code_last_updated",
        "caveats",
    ],
}

SECTIONS_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "sections": {
            "type": "array",
            "description": (
                "One entry per requested section, in request order. An "
                "entry with found=false carries `error` and no text; the "
                "others carry the same fields get_section returns."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "requested": {
                        "type": "string",
                        "description": "The guid or number as it was passed.",
                    },
                    "found": {"type": "boolean"},
                    "error": {"type": ["string", "null"]},
                    "guid": {"type": ["string", "null"]},
                    "ref": {"type": ["string", "null"]},
                    "title": {"type": ["string", "null"]},
                    "url": {"type": ["string", "null"]},
                    "text": {
                        "type": ["string", "null"],
                        "description": "Full section body, NOT clipped.",
                    },
                    "length": _LENGTH_SCHEMA,
                    "history": _HISTORY_SCHEMA,
                    "has_attachment": {"type": "boolean"},
                    "attachments": _ATTACHMENTS_SCHEMA,
                    "title_attachments": _TITLE_ATTACHMENTS_SCHEMA,
                    "parts": _PARTS_SCHEMA,
                    "is_pdf": {"type": "boolean"},
                    "caveats": _CAVEAT_SCHEMA,
                },
                "required": ["requested", "found", "error", "caveats"],
            },
        },
        "summary": {"type": "string"},
        "code_last_updated": _CODE_LAST_UPDATED_SCHEMA,
        "caveats": _CAVEAT_SCHEMA,
    },
    "required": ["sections", "summary", "code_last_updated", "caveats"],
}

DEFINITION_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "term": {"type": "string"},
        "total_count": {
            "type": ["integer", "null"],
            "description": (
                "Nodes whose TITLE matches the term, across all pages -- "
                "definitions and ordinary sections alike. Not the number "
                "of definitions."
            ),
        },
        "definitions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "guid": {"type": "string"},
                    "term": {
                        "type": ["string", "null"],
                        "description": "The defined term as the code writes it.",
                    },
                    "exact_match": {
                        "type": "boolean",
                        "description": (
                            "The defined term equals the requested term "
                            "(ignoring case, quotes and a trailing period). "
                            "false means a related term matched, e.g. "
                            "'Motor vehicles' for 'vehicle'."
                        ),
                    },
                    "defined_in": {
                        "type": ["string", "null"],
                        "description": (
                            "Number of the section that contains the "
                            "definition (e.g. '9.04.010'); pass it to "
                            "get_section to read which chapter or title "
                            "the definition applies to."
                        ),
                    },
                    "text": {"type": ["string", "null"]},
                    "url": {"type": "string"},
                },
                "required": ["guid", "exact_match", "url"],
            },
        },
        "summary": {"type": "string"},
        "caveats": _CAVEAT_SCHEMA,
    },
    "required": ["term", "total_count", "definitions", "summary", "caveats"],
}

REFERENCES_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "section": {
            "type": "string",
            "description": "The section number the references are to.",
        },
        "query": {
            "type": "string",
            "description": "The search that was run; usable with search_code.",
        },
        "page": {"type": "integer"},
        "total_count": {
            "type": ["integer", "null"],
            "description": (
                "Hits for the number across all pages, INCLUDING the "
                "section itself where it mentions its own number. null "
                "means the upstream did not report a count."
            ),
        },
        "references": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "guid": {"type": "string"},
                    "ref": {"type": ["string", "null"]},
                    "title": {"type": ["string", "null"]},
                    "snippet": {
                        "type": ["string", "null"],
                        "description": (
                            "The text around the citation. An excerpt: "
                            "read the section before quoting it."
                        ),
                    },
                    "url": {"type": "string"},
                    "is_pdf": {"type": "boolean"},
                    "kind": {
                        "type": "string",
                        "enum": list(REFERENCE_KIND_ORDER),
                        "description": (
                            "fine_or_penalty: a fine schedule or penalty "
                            "section (judged from its heading). section: "
                            "any other code section. document: a PDF. "
                            "reference_table: an editorial table such as "
                            "the Ordinance Disposition Table, not law."
                        ),
                    },
                },
                "required": ["guid", "url", "is_pdf", "kind"],
            },
        },
        "summary": {"type": "string"},
        "caveats": _CAVEAT_SCHEMA,
    },
    "required": [
        "section",
        "query",
        "page",
        "total_count",
        "references",
        "summary",
        "caveats",
    ],
}

# ── Recently adopted ordinances (2026-11-18) ────────────────────────────
#
# eCode360 has no "what changed" endpoint (51 candidate paths probed, none
# exist). What the publisher does maintain is a section of the code itself,
# § OL.010 "Ordinance List and Disposition Table": one row per ordinance
# codified since the last republication -- number, adoption date, subject,
# the chapters it went into, and the supplement that carried it. The API
# returns the table as ONE run of text with single spaces between cells:
#   "2026-40(S-1) Amended 6-23-2026 Amends Purchasing: ... (Eff. 9-1-2026)
#    Ch. 7.10; Ch. 7.20 0.1 2026-41 4-14-2026 Amends Service Areas: ..."
# A row starts at an ordinance number; two live rows have no date; the
# supplement number is absent on older rows; a disposition is "Ch. 3.30",
# "CHA" (the Charter) or a bare regulation chapter ("R21.13").
ORDINANCE_LIST_TITLE = "ordinance list and disposition table"
ORDINANCE_ROW_RE = re.compile(
    r"(?<!\S)(\d{4}-\d+[A-Za-z]?(?:\([A-Z](?:-\d+)?\))*"
    r"(?:,? (?:As )?Amended)?)"
    r"(?: (\d{1,2}-\d{1,2}-\d{4}))?(?= [A-Z])"
)
# Words that make a following number a mention inside a subject ("Corrects
# AO 2025-77", "Ord. No. 2025-77"), not a row. "No." alone does not: the
# header ends "Supp. No." and the first row follows it.
ORDINANCE_MENTION_WORDS = ("ao", "ord.", "ordinance", "ar")
_ORDINANCE_LOCATION = r"(?:(?:Chs?\. )?R?\d+[A-Z]?\.\d+|CHA|Title \d+[A-Z]?)"
ORDINANCE_TAIL_RE = re.compile(
    rf"(?:^| )({_ORDINANCE_LOCATION}(?:; {_ORDINANCE_LOCATION})*)"
    r"(?: (\d+(?:\.\d+)?))?$"
)
ORDINANCE_WITHIN_RE = re.compile(
    r"(?i)^(?:title|ttl\.?|chapter|ch\.?|section|sec\.?|amc|§)?\s*"
    r"(R?\d+[A-Z]?(?:\.\d+)*)\.?$"
)
DEFAULT_ORDINANCES = 25
MAX_ORDINANCES = 100

_ORDINANCE_SUBJECT_SCHEMA = {
    "type": ["string", "null"],
    "description": (
        "The ordinance's own title, read from the first page of its PDF: "
        "'AN ORDINANCE ... AMENDING ANCHORAGE MUNICIPAL CODE CHAPTER ...'. "
        "Words in [brackets] are deletions from an earlier version of the "
        "title. null when it could not be read."
    ),
}
_ORDINANCE_AMENDS_SCHEMA = {
    "type": "array",
    "items": {"type": "string"},
    "description": (
        "The chapter and section numbers that title names ('3.102', "
        "'7.15.040'). What the drafter chose to name, not a complete "
        "list of what the ordinance changes."
    ),
}

ORDINANCES_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "ordinances": {
            "type": "array",
            "description": "Newest adoption date first; undated rows last.",
            "items": {
                "type": "object",
                "properties": {
                    "ordinance": {
                        "type": "string",
                        "description": (
                            "The ordinance number as the table prints it, "
                            "e.g. '2026-40(S-1) Amended'."
                        ),
                    },
                    "adopted": {
                        "type": ["string", "null"],
                        "description": (
                            "Adoption date, YYYY-MM-DD. Not the effective "
                            "date. null when the table gives none."
                        ),
                    },
                    "subject": {"type": ["string", "null"]},
                    "chapters": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Where the ordinance was codified: chapter "
                            "numbers ('21.05', 'R11.10' for a regulation) "
                            "or 'Charter'. Chapters, not sections."
                        ),
                    },
                    "supplement": {
                        "type": ["string", "null"],
                        "description": (
                            "The publisher's supplement number that "
                            "carried the change, when printed."
                        ),
                    },
                    "search_query": {
                        "type": "string",
                        "description": (
                            "Pass to search_code to list the sections "
                            "whose history note cites this ordinance."
                        ),
                    },
                    "text": {
                        "type": "string",
                        "description": "The table row as printed.",
                    },
                },
                "required": [
                    "ordinance",
                    "adopted",
                    "subject",
                    "chapters",
                    "supplement",
                    "search_query",
                    "text",
                ],
            },
        },
        "total_count": {
            "type": "integer",
            "description": (
                "Rows matching `within` (all rows when it is not given), "
                "of which `ordinances` holds the first `limit`."
            ),
        },
        "within": {"type": ["string", "null"]},
        "not_yet_codified": {
            "type": "array",
            "description": (
                "Ordinances adopted but NOT yet worked into the code text, "
                "which the publisher holds as PDFs. Newer than everything "
                "in `ordinances`, and never filtered by `within`: nothing "
                "says which chapters they amend until they are read. Pass "
                "a url to read_attachment. Empty when there are none, or "
                "when the check could not be made."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "ordinance": {
                        "type": "string",
                        "description": "As titled, e.g. 'AO No. 2026-108(S)'.",
                    },
                    "url": {"type": "string"},
                    "subject": _ORDINANCE_SUBJECT_SCHEMA,
                    "amends": _ORDINANCE_AMENDS_SCHEMA,
                    "in_scope": {
                        "type": ["boolean", "null"],
                        "description": (
                            "With `within`: whether the ordinance's TITLE "
                            "names that title or chapter. false means only "
                            "that its title does not; an ordinance can "
                            "amend sections its title does not list. null "
                            "without `within`, or when the title could "
                            "not be read."
                        ),
                    },
                },
                "required": ["ordinance", "url", "subject", "amends", "in_scope"],
            },
        },
        "covers": {
            "type": ["object", "null"],
            "description": "The whole table, before any `within` filter.",
            "properties": {
                "count": {"type": "integer"},
                "earliest_adopted": {"type": ["string", "null"]},
                "latest_adopted": {"type": ["string", "null"]},
            },
            "required": ["count", "earliest_adopted", "latest_adopted"],
        },
        "source": {
            "type": ["object", "null"],
            "properties": {
                "guid": {"type": "string"},
                "ref": {"type": ["string", "null"]},
                "title": {"type": ["string", "null"]},
                "url": {"type": "string"},
            },
            "required": ["guid", "url"],
        },
        "code_last_updated": _CODE_LAST_UPDATED_SCHEMA,
        "summary": {"type": "string"},
        "caveats": _CAVEAT_SCHEMA,
    },
    "required": [
        "ordinances",
        "total_count",
        "within",
        "not_yet_codified",
        "covers",
        "source",
        "code_last_updated",
        "summary",
        "caveats",
    ],
}

# ── One ordinance's sections (2026-11-19) ───────────────────────────────
#
# § CCT.020 "Ordinance Disposition Table" maps every ordinance since the
# 1977 code to the sections it changed: about 1,560 ordinances and 6,900
# rows in 169k characters. Four columns -- Ordinance Number, Section (of
# the ordinance), Effective Date, Disposition (the code section) -- that
# the API returns as ONE run of cells separated by single spaces, where an
# EMPTY cell is a lone no-break space:
#   "2025-94 1 10-21-2025 §\xa021.03.180 \xa0 2 \xa0 §\xa021.13.010A.1
#    \xa0 \xa0 Added §\xa09.46.050F 2025-101(S) 1 9-23-2025 Added ..."
# So the text must be split on " " alone: str.split() would swallow the
# empty cells, and with them the row boundaries. A row is: ordinance cell
# (empty on a continuation row), section cell, date cell, disposition.
# When the disposition starts with Added/Rpld/Amd/Rnbd the date cell is
# simply absent on a continuation row. The table is typed by hand: dates
# such as "3-a9-21" and "12-8-2020/election of 4-6-2021", glued cells
# ("2000-127(S)1"), a few ordinances listed in two places.
# Parts of the code that are cited by name, not number (see _named_scope).
SCOPE_CHARTER_NAMES = frozenset({
    "charter",
    "municipal charter",
    "anchorage charter",
    "anchorage municipal charter",
    "home rule charter",
    "title cha",
    "cha",
})
# Cheap test for "might be one of those names", so a guid or a section
# number never costs an outline lookup.
NAMED_PART_HINT_RE = re.compile(r"(?i)charter|regulations|^\s*(?:the\s+)?part\s")
SCOPE_REGULATIONS_NAMES = frozenset({
    "regulations",
    "code of regulations",
    "municipal code of regulations",
    "anchorage municipal code of regulations",
})

ORDINANCE_TABLE_TITLE = "ordinance disposition table"
ORDINANCE_TABLE_HEADER = "Ordinance Number Section Effective Date Disposition "
ORDINANCE_EMPTY_CELL = "\xa0"
# "13-75" (number-year, to 1999), "2025-94" (year-number), with a
# substitute/amended suffix: "(S)", "(S-2)", "(am)", "am", "S".
ORDINANCE_NUMBER_RE = re.compile(
    r"^(\d{1,4}-\d{1,4})((?:\([A-Za-z0-9-]+\))?[A-Za-z0-9]{0,2})$"
)
ORDINANCE_ASKED_RE = re.compile(
    r"(?<![\d-])(\d{1,4})\s*[-‐-―]\s*(\d{1,4})(?![\d-])"
    r"\s*(\([A-Za-z0-9 -]{1,8}\))?"
)
# "AR 2025-94", "ar no. 2025-94", "A.R. 2025-94", "AR2025-94".
ORDINANCE_RESOLUTION_RE = re.compile(
    r"(?i)(?<![a-z])a\.?r\.?(?=\s*(?:no\.?\s*)?\d)|\bresolution\b"
)
ORDINANCE_SECTION_CELL_RE = re.compile(
    r"^\d+(?:[—-]\d+)?,?\xa0?$|^\d+,\d+$|^\d+\(\S*\)$"
)
ORDINANCE_DATE_CELL_RE = re.compile(r"^\d{1,2}-\w{1,3}-\d{2,4}")
ORDINANCE_ACTIONS = {
    "Added": "added",
    "Rpld": "repealed",
    "Amd": "amended",
    "Rnbd": "renumbered",
    "Rbnd": "renumbered",  # the table's own misspelling
}
# The code sections a disposition names, to the section level:
# "§ 21.05.070D.13b.vii(C)" -> 21.05.070, "§ R21.20.002A.12" -> R21.20.002.
ORDINANCE_CODE_SECTION_RE = re.compile(r"§\s*(R?\d+\.\d+\.\d+)")
ORDINANCE_CITED_RE = re.compile(r"\u00a7\s*([A-Za-z0-9.()]+)")
MAX_ORDINANCE_ARG_CHARS = 100
# The largest ordinance in the table (the 2011 rewrite of Title 21) has
# 203 rows; the cap is there so a future one cannot flood a response.
MAX_ORDINANCE_ENTRIES = 300

CAVEAT_ORDINANCE_TABLE_SCOPE = "ordinance_table_scope"
CAVEAT_ORDINANCE_NOT_IN_TABLE = "ordinance_not_in_table"
CAVEAT_ORDINANCE_TABLE_UNAVAILABLE = "ordinance_table_unavailable"
CAVEAT_ORDINANCE_SEVERAL_VERSIONS = "ordinance_several_versions"
CAVEAT_ORDINANCE_ENTRIES_TRUNCATED = "ordinance_entries_truncated"

_ORDINANCE_SOURCE_SCHEMA = {
    "type": ["object", "null"],
    "properties": {
        "guid": {"type": "string"},
        "ref": {"type": ["string", "null"]},
        "title": {"type": ["string", "null"]},
        "url": {"type": "string"},
    },
    "required": ["guid", "url"],
}

FIND_ORDINANCE_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "ordinance": {
            "type": "string",
            "description": "The ordinance number looked up, e.g. '2025-94'.",
        },
        "found": {
            "type": "boolean",
            "description": (
                "true when the Ordinance Disposition Table lists it. false "
                "does not mean no such ordinance exists: see the caveats."
            ),
        },
        "matches": {
            "type": "array",
            "description": (
                "One per form of the number the table prints (usually one; "
                "'2008-134' and '2008-134(S)' are listed separately)."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "ordinance": {
                        "type": "string",
                        "description": "As printed, e.g. '2025-115(S-2)'.",
                    },
                    "effective_date": {
                        "type": ["string", "null"],
                        "description": (
                            "The first effective date printed for it, "
                            "YYYY-MM-DD. The EFFECTIVE date, not the "
                            "adoption date. null when none is printed or "
                            "it is not a single plain date."
                        ),
                    },
                    "effective_as_printed": {"type": ["string", "null"]},
                    "entry_count": {"type": "integer"},
                    "entries": {
                        "type": "array",
                        "description": "Table rows, in the table's order.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "ordinance_section": {
                                    "type": ["string", "null"],
                                    "description": (
                                        "The section OF THE ORDINANCE that "
                                        "made the change ('3', "
                                        "'4 (Exh. A)'). null when the row "
                                        "leaves it blank: the same "
                                        "ordinance section as the row "
                                        "above."
                                    ),
                                },
                                "effective_date": {
                                    "type": ["string", "null"],
                                    "description": (
                                        "Only when this row prints its own "
                                        "date (YYYY-MM-DD); null otherwise, "
                                        "which by the table's layout means "
                                        "the date printed above."
                                    ),
                                },
                                "effective_as_printed": {
                                    "type": ["string", "null"]
                                },
                                "action": {
                                    "type": ["string", "null"],
                                    "description": (
                                        "'added', 'repealed', 'amended' or "
                                        "'renumbered' when the table marks "
                                        "the row. null when unmarked: the "
                                        "ordinance changed the provision "
                                        "named, and the table does not say "
                                        "how."
                                    ),
                                },
                                "disposition": {
                                    "type": "string",
                                    "description": (
                                        "Where the change went, as "
                                        "printed: usually a code section "
                                        "and subsection, sometimes a "
                                        "chapter, a table or an 'Old' "
                                        "(superseded) Title 21 section."
                                    ),
                                },
                                "sections": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "description": (
                                        "Sections of the code today that "
                                        "the disposition names "
                                        "('21.05.070'); for a range, its "
                                        "two ends only."
                                    ),
                                },
                                "sections_not_in_code": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "description": (
                                        "Section numbers it names that "
                                        "are not sections of the code "
                                        "today: repealed or renumbered "
                                        "since, or mistyped in the table."
                                    ),
                                },
                            },
                            "required": [
                                "ordinance_section",
                                "effective_date",
                                "effective_as_printed",
                                "action",
                                "disposition",
                                "sections",
                                "sections_not_in_code",
                            ],
                        },
                    },
                    "sections": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Every distinct section of the code today "
                            "named in the entries, in table order; pass up "
                            "to 10 at a time to get_sections."
                        ),
                    },
                    "sections_not_in_code": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Section numbers named in the entries that "
                            "are not sections of the code today."
                        ),
                    },
                },
                "required": [
                    "ordinance",
                    "effective_date",
                    "effective_as_printed",
                    "entry_count",
                    "entries",
                    "sections",
                    "sections_not_in_code",
                ],
            },
        },
        "total_entries": {"type": "integer"},
        "listing": {
            "type": ["object", "null"],
            "description": (
                "The ordinance's row in the Ordinance List (the table "
                "recent_ordinances reads), when it has one: adoption date, "
                "subject and chapters. Only recent ordinances do."
            ),
            "properties": {
                "ordinance": {"type": "string"},
                "adopted": {"type": ["string", "null"]},
                "subject": {"type": ["string", "null"]},
                "chapters": {"type": "array", "items": {"type": "string"}},
                "supplement": {"type": ["string", "null"]},
            },
            "required": [
                "ordinance",
                "adopted",
                "subject",
                "chapters",
                "supplement",
            ],
        },
        "search_query": {
            "type": "string",
            "description": (
                "Pass to search_code to list the sections whose history "
                "note cites this ordinance."
            ),
        },
        "citing_sections": {
            "type": "array",
            "description": (
                "Only when the table does not list the ordinance: the "
                "sections of the code that cite it, found by searching for "
                "its number. A section cites an ordinance in its history "
                "note, so these are the sections it enacted or amended. "
                "Empty when the table lists it (use `matches`), when "
                "nothing cites it, or when the search could not be made."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "guid": {"type": "string"},
                    "ref": {"type": ["string", "null"]},
                    "title": {"type": ["string", "null"]},
                    "url": {"type": "string"},
                    "cited_as": {
                        "type": ["string", "null"],
                        "description": (
                            "The citation as the section prints it, e.g. "
                            "'AO No. 2025-112, \u00a7 12, 10-21-2025': the "
                            "\u00a7 is the ordinance's own section, the "
                            "date its adoption date."
                        ),
                    },
                },
                "required": ["guid", "ref", "title", "url", "cited_as"],
            },
        },
        "citing_sections_complete": {
            "type": "boolean",
            "description": (
                "false when there were more search hits than were checked "
                "(60), so `citing_sections` may be missing some."
            ),
        },
        "ordinance_pdfs": {
            "type": "array",
            "description": (
                "The ordinance itself, when the publisher holds it as a "
                "PDF: only while it is adopted but not yet worked into "
                "the code text. Pass the url to read_attachment. Empty "
                "for a codified ordinance, or when the check could not "
                "be made."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "ordinance": {"type": "string"},
                    "url": {"type": "string"},
                    "subject": _ORDINANCE_SUBJECT_SCHEMA,
                    "amends": _ORDINANCE_AMENDS_SCHEMA,
                },
                "required": ["ordinance", "url", "subject", "amends"],
            },
        },
        "covers": {
            "type": ["object", "null"],
            "description": "The whole table.",
            "properties": {
                "count": {"type": "integer"},
                "first_ordinance": {"type": ["string", "null"]},
                "last_ordinance": {"type": ["string", "null"]},
            },
            "required": ["count", "first_ordinance", "last_ordinance"],
        },
        "source": _ORDINANCE_SOURCE_SCHEMA,
        "code_last_updated": _CODE_LAST_UPDATED_SCHEMA,
        "summary": {"type": "string"},
        "caveats": _CAVEAT_SCHEMA,
    },
    "required": [
        "ordinance",
        "found",
        "matches",
        "total_entries",
        "listing",
        "search_query",
        "citing_sections",
        "citing_sections_complete",
        "ordinance_pdfs",
        "covers",
        "source",
        "code_last_updated",
        "summary",
        "caveats",
    ],
}

ATTACHMENT_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "url": {"type": "string"},
        "filename": {"type": "string"},
        "page_count": {"type": "integer"},
        "layout": {
            "type": "boolean",
            "description": (
                "true: columns are kept aligned with spaces (for tables). "
                "false: plain reading-order text."
            ),
        },
        "pages": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "page": {"type": "integer", "description": "1-indexed"},
                    "text": {"type": "string"},
                },
                "required": ["page", "text"],
            },
        },
        "next_page": {
            "type": ["integer", "null"],
            "description": (
                "Pass as `page` to continue (with the same `find`, if "
                "one was given); null when the last page of the document "
                "has been returned or searched."
            ),
        },
        "find": {
            "type": "object",
            "description": (
                "Present only when `find` was given: where the phrase "
                "occurs. `pages` is then empty."
            ),
            "properties": {
                "query": {"type": "string"},
                "match": {
                    "type": "string",
                    "enum": ["phrase", "all_words"],
                    "description": (
                        "phrase: the words occur together, in order. "
                        "all_words: the phrase occurs nowhere; these pages "
                        "contain every word of it somewhere."
                    ),
                },
                "searched_from": {"type": ["integer", "null"]},
                "searched_to": {"type": ["integer", "null"]},
                "pages_matched": {"type": "integer"},
                "matches": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "page": {"type": "integer"},
                            "count": {"type": "integer"},
                            "snippets": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                        },
                        "required": ["page", "count", "snippets"],
                    },
                },
            },
            "required": [
                "query",
                "match",
                "searched_from",
                "searched_to",
                "pages_matched",
                "matches",
            ],
        },
        "summary": {"type": "string"},
        "caveats": _CAVEAT_SCHEMA,
    },
    "required": [
        "url",
        "filename",
        "page_count",
        "layout",
        "pages",
        "next_page",
        "summary",
        "caveats",
    ],
}

# The outline tools return a tree, not a table. Forcing chapters ->
# articles -> sections into `rows` would throw away the parent/child
# relation that is the entire reason to call them.
STRUCTURE_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "node": _NODE_SCHEMA,
        "parent": {"type": ["string", "null"]},
        "url": {"type": ["string", "null"]},
        "children": {"type": "array", "items": _NODE_SCHEMA},
        "parts": _PARTS_SCHEMA,
        "summary": {"type": "string"},
        "caveats": _CAVEAT_SCHEMA,
    },
    "required": ["node", "children", "summary", "caveats"],
}

TOC_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "node": _NODE_SCHEMA,
        "children": {
            "type": "array",
            "description": (
                "The outline as a tree, three levels deep: parts, the "
                "titles under each part, and the chapters under each "
                "title. Sections are not listed."
            ),
            "items": _outline_node_schema(TOC_DEPTH),
        },
        "summary": {"type": "string"},
        "caveats": _CAVEAT_SCHEMA,
    },
    "required": ["node", "children", "summary", "caveats"],
}


# Customer (code library) metadata. Every key is always present; a value the
# upstream omits is null, never a missing key. Types are left wide where the
# API has never been observed to commit to one (population, pdfOnly).
CUSTOMER_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "customer_id": {"type": ["string", "null"]},
        "name": {"type": ["string", "null"]},
        "short_name": {"type": ["string", "null"]},
        "state": {"type": ["string", "null"]},
        "government_type": {"type": ["string", "null"]},
        "counties": {
            "type": ["array", "null"],
            "items": {"type": "string"},
        },
        "population": {"type": ["integer", "string", "null"]},
        "last_updated": {
            "type": ["string", "null"],
            "description": (
                "Date the published code was last updated on eCode360 "
                "(YYYY-MM-DD as returned). Ordinances adopted after this "
                "date may not be reflected in the code text yet."
            ),
        },
        "pdf_only": {"type": ["boolean", "string", "null"]},
        "url": {"type": ["string", "null"]},
        "summary": {"type": "string"},
        "caveats": _CAVEAT_SCHEMA,
    },
    "required": [
        "customer_id",
        "name",
        "short_name",
        "state",
        "government_type",
        "counties",
        "population",
        "last_updated",
        "pdf_only",
        "url",
        "summary",
        "caveats",
    ],
}


class EcodeAPIError(RuntimeError):
    """An eCode360 API call failed for a reason that is not the caller's.

    Carries the HTTP status so callers of _get_json can branch on it (the
    TOC fallback needs to recognise a 404 without string-matching the
    message, which is what it used to do).
    """

    def __init__(self, message: str, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(message)


class EcodeNotFoundError(ToolInputError):
    """The caller asked for a node the published code does not contain.

    A ToolInputError, not a server fault: a 404 here means the guid was
    wrong, which is a caller mistake and by far the most common one this
    server sees -- a model reaching get_section with a guid it parsed out
    of prose.
    """

    def __init__(self, message: str, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(message)


class EcodePlugin(MCPPlugin):
    """Plugin for querying a municipality's eCode360-hosted municipal code."""

    plugin_name = "ecode"
    plugin_type = PluginType.CUSTOM_API
    plugin_version = "1.20.0"

    def __init__(self, config: Dict[str, Any]) -> None:
        super().__init__(config)
        self.plugin_config: Optional[EcodePluginConfig] = None
        self.client: Optional[httpx.AsyncClient] = None
        # Serialises the first fetch of a cached document so a batch of
        # chapter numbers triggers one table-of-contents download, not ten.
        # One lock per document: with a single lock the library record and
        # the table of contents, which a section read asks for together,
        # were fetched one after the other (prod, 2026-11-22: 1.5 s then
        # 0.6 s at the head of a 3.3 s call, each time the hour was up).
        self._static_locks: Dict[Tuple[str, str], asyncio.Lock] = {}
        # Test seam for the attachment download client.
        self._attachment_transport: Optional[httpx.AsyncBaseTransport] = None

    # ── Lifecycle ───────────────────────────────────────────────────────

    async def initialize(self) -> bool:
        try:
            self.plugin_config = EcodePluginConfig(**self.config)

            api_key, api_secret = self._resolve_credentials(self.plugin_config)
            if not api_key or not api_secret:
                logger.error(
                    "eCode plugin missing credentials: set %s and %s in the "
                    "environment (Lambda env vars) or via secrets_file for local dev.",
                    self.plugin_config.api_key_env,
                    self.plugin_config.api_secret_env,
                )
                return False

            self.client = httpx.AsyncClient(
                base_url=f"{self.plugin_config.base_url}/v1",
                headers={
                    "api-key": api_key,
                    "api-secret": api_secret,
                    "Accept": "application/json",
                },
                timeout=self.plugin_config.timeout,
            )

            # Deliberately NO upstream call here. The Lambda adapter builds
            # a new plugin on every invocation, so a credential probe in
            # initialize() ran once per REQUEST -- ping, tools/list and the
            # fleet probe included: 88% of all eCode360 calls over 14 days
            # of prod logs, adding a median 185 ms (p95 2.5 s) to every
            # response (2026-11-15). Bad credentials now surface on the
            # first tool call instead, as a 401/403 that _get_json names
            # and logs as an error; health_check() still probes on demand.
            self._initialized = True
            logger.info(
                "eCode plugin initialized for %s (customer %s)",
                self.plugin_config.city_name,
                self.plugin_config.customer_id,
            )
            return True

        except Exception as e:
            logger.error("Failed to initialize eCode plugin: %s", e, exc_info=True)
            return False

    async def shutdown(self) -> None:
        if self.client:
            await self.client.aclose()
            self.client = None
        self._initialized = False
        logger.info("eCode plugin shut down")

    async def health_check(self) -> bool:
        try:
            resp = await self.client.get(self._cust_path(""))
            return resp.status_code == 200
        except Exception as e:
            logger.error("eCode health check failed: %s", e)
            return False

    # ── Tool definitions ────────────────────────────────────────────────

    # Human-readable display names. The wire `name` is prefixed
    # (`ecode__get_table_of_contents`) because it must be a stable,
    # collision-free identifier; that string reads poorly in a client's
    # tool picker. Clients resolve display names as title ->
    # annotations.title -> name, so these are what a user actually sees.
    # Keyed by the UNPREFIXED tool name, matching the ToolDefinition
    # entries below.
    TOOL_TITLES = {
        "search_code": "Search Code",
        "get_table_of_contents": "Table of Contents",
        "browse_structure": "Browse Structure",
        "get_section": "Get Section",
        "get_sections": "Get Sections (Batch)",
        "find_definition": "Find Definition",
        "find_references": "Find References",
        "recent_ordinances": "Recent Ordinances",
        "find_ordinance": "Find Ordinance",
        "read_attachment": "Read Attachment (PDF)",
        "get_customer_info": "Customer Info",
    }

    def get_tools(self) -> List[ToolDefinition]:
        city = (
            self.plugin_config.city_name if self.plugin_config else "the municipality"
        )
        read_only = {"readOnlyHint": True, "openWorldHint": True}
        tools = [
            ToolDefinition(
                name="search_code",
                description=(
                    f"Full-text search of {city}'s municipal code. Returns up "
                    "to 20 matching sections per page, each with its title, "
                    "section number, a highlighted snippet and a guid; pass "
                    "the guid to get_section to read the full text. Page "
                    "through results with `page`: the first page is "
                    "page=0 (the default), the next is page=1.\n\n"
                    "QUERY SYNTAX: terms written side by side are matched "
                    "with OR, so a list of words or phrases matches every "
                    "section containing ANY of them -- often hundreds or "
                    "thousands. Write precise queries by default: quote "
                    "exact phrases, join terms that must all appear with "
                    "AND (or put + before each), and use NOT (or -term) to "
                    "exclude one. Parentheses group, e.g. "
                    '("notice of claim" OR "claim for damages") AND '
                    "municipality; a trailing * is a wildcard, written "
                    'WITHOUT quotes (liab*, not "liab*"); '
                    "title:word matches words in a section's heading "
                    "(title:penalty) -- it is NOT a filter on Title number: "
                    "title:9 does not mean Title 9 (use `within` for "
                    "that). Proximity search "
                    '("a b"~3) is NOT supported -- the ~N is ignored and '
                    "the phrase must match exactly.\n\n"
                    'Example: "snow" AND "sidewalk" AND "occupant" returns '
                    "4 sections, led by the snow-removal duty in "
                    '§ 24.80.090; "snow" "sidewalk" "occupant" (OR-ed) '
                    "returns hundreds. If AND returns too few, drop the "
                    "least essential term rather than reverting to a bag of "
                    "words.\n\n"
                    "LIMIT TO PART OF THE CODE: pass `within` (e.g. "
                    "'Title 9', 'Chapter 9.28', 'Charter', or a guid) to keep only "
                    "hits under that title, chapter or section. The server "
                    "checks up to 200 hits for the query per call and "
                    "returns those inside the scope; if the query has "
                    "more, a scope_scan_partial note says so and `page` "
                    "moves to the next 200. PDF documents are left out of "
                    "a scoped search.\n\n"
                    "No results means no section matched the query AS "
                    "WRITTEN. That alone does not prove the code is silent: "
                    "a quoted phrase fails on small differences, and "
                    "punctuation inside it (quotation marks around a "
                    "defined term) can block a match. It can also be the "
                    "answer (the rule may be state law, repealed, or worded "
                    "differently). When a quoted phrase matches nothing, "
                    "the result may add a relaxed match on the same words "
                    "and, for a query like 'X means', the definition of X; "
                    "for definitions call find_definition directly. "
                    "Broaden the words or browse with "
                    "get_table_of_contents before concluding."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": (
                                "An exact phrase in double quotes, or terms "
                                "joined with AND. Bare space-separated words "
                                "are OR'd."
                            ),
                        },
                        "page": {
                            "type": "integer",
                            "description": (
                                "0-indexed result page, 20 results/page: "
                                "0 is the first page, 1 the second"
                            ),
                            "default": 0,
                            "minimum": 0,
                        },
                        "within": {
                            "type": "string",
                            "description": (
                                "Optional. Limit results to one title, "
                                "chapter or section: 'Charter', 'Title 9', 'Chapter "
                                "9.28', '9.28.020', or its guid. With "
                                "`within`, each page checks 200 hits for "
                                "the query instead of returning 20."
                            ),
                        },
                    },
                    "required": ["query"],
                },
                output_schema=SEARCH_OUTPUT_SCHEMA,
                annotations=read_only,
            ),
            ToolDefinition(
                name="get_table_of_contents",
                description=(
                    f"Get the outline of {city}'s municipal code, three "
                    "levels deep: the parts (Charter, Municipal Code, Code "
                    "of Regulations, tables), the titles under each part, "
                    "and the chapters under each title. Sections and body "
                    "text are NOT listed. Each entry has a guid: pass a "
                    "chapter's guid to browse_structure to list its "
                    "sections, or to get_section to read it. Start here to "
                    "orient before drilling in."
                ),
                input_schema={"type": "object", "properties": {}},
                output_schema=TOC_OUTPUT_SCHEMA,
                annotations=read_only,
            ),
            ToolDefinition(
                name="browse_structure",
                description=(
                    "Browse one node of the code hierarchy: returns the node's "
                    "number, title, type, and its immediate children (each with a "
                    "guid). Pass guid='ROOT' (the default) for the top of the tree, "
                    "then walk down using child guids. A title, chapter or "
                    "section written as it is cited ('Title 9', 'Chapter "
                    "9.28', '9.28.019') is accepted in place of a guid. Use "
                    "this to navigate the outline; use get_section to read "
                    "actual text."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "guid": {
                            "type": "string",
                            "description": (
                                "Node guid, 'ROOT' for the top, or a "
                                "reference such as 'Chapter 9.28'"
                            ),
                            "default": "ROOT",
                        },
                    },
                },
                output_schema=STRUCTURE_OUTPUT_SCHEMA,
                annotations=read_only,
            ),
            ToolDefinition(
                name="get_section",
                description=(
                    "Retrieve the full text of a code section (and its nested "
                    "subsections) by guid. The guid comes from search_code, "
                    "get_table_of_contents, or browse_structure. A section "
                    "NUMBER such as '21.05.010' or '§ 21.05.010' is also "
                    "accepted and resolved to its guid, and so is a whole "
                    "chapter or title written as such ('Chapter 17.05', "
                    "'Title 21', 'Charter Section 17.05'). A chapter or "
                    "title can be very long -- prefer browse_structure to "
                    "list its sections and read the ones you need. Also "
                    "lists any PDF attachments (with a url for "
                    "read_attachment) and the public ecode360.com link. "
                    "When a note in the text says something is 'included "
                    "as an attachment to this title' (a use table, an "
                    "exhibit, the Charter Commission Commentary), the "
                    "PDFs attached to that title are listed too. "
                    f"Up to {MAX_CONTENT_CHARS:,} characters of text are "
                    "returned per call; a longer section says so and "
                    "gives the `offset` to call again with for the rest "
                    "-- read it all before summarizing the section. The "
                    "history note (the ordinances that amended the "
                    "section) is in the header of every part, so there "
                    "is no need to read to the end for it. A section "
                    "that is only a heading lists the sections numbered "
                    "under it, which hold its text."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "guid": {
                            "type": "string",
                            "description": (
                                "Section/node guid (e.g. '49422044'), a "
                                "section number (e.g. '21.05.010'), or "
                                "'Chapter 17.05' / 'Title 21'"
                            ),
                        },
                        "offset": {
                            "type": "integer",
                            "description": (
                                "Character position to continue reading "
                                "from, for a section longer than one "
                                "response. Use the value given in the "
                                "rendered_text_truncated note."
                            ),
                            "default": 0,
                            "minimum": 0,
                        },
                    },
                    "required": ["guid"],
                },
                output_schema=SECTION_OUTPUT_SCHEMA,
                annotations=read_only,
            ),
            ToolDefinition(
                name="get_sections",
                description=(
                    "Retrieve the full text of SEVERAL code sections in one "
                    f"call (up to {MAX_BATCH_SECTIONS}). Use this instead of "
                    "repeated get_section calls whenever you need more than "
                    "one section -- e.g. a rule plus the definitions and "
                    "penalty sections it relies on, or the sections of a "
                    "chapter. Each entry is a guid or a section number "
                    "('21.05.010'), exactly as get_section accepts. Results "
                    "come back in request order; a section that cannot be "
                    "found is reported in place (found=false) without "
                    "failing the others."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "sections": {
                            "type": "array",
                            "items": {"type": "string"},
                            "minItems": 1,
                            "maxItems": MAX_BATCH_SECTIONS,
                            "description": (
                                "Guids (e.g. '49422044') and/or section "
                                "numbers (e.g. '21.05.010'), "
                                f"1 to {MAX_BATCH_SECTIONS} of them"
                            ),
                        },
                    },
                    "required": ["sections"],
                },
                output_schema=SECTIONS_OUTPUT_SCHEMA,
                annotations=read_only,
            ),
            ToolDefinition(
                name="find_definition",
                description=(
                    "Look up how the code DEFINES a term. Returns the "
                    "published definition(s) -- the defined term, its full "
                    "text, and the section it is defined in. Use this "
                    "instead of search_code for 'what does X mean' or "
                    "'how is X defined' questions: searching for "
                    "'\"X\" means' matches thousands of sections or none. "
                    "The same term is often defined separately in several "
                    "titles (traffic, zoning, building...), and each "
                    "definition applies only within its own chapter or "
                    "title, so report the one for the area of law the "
                    "question concerns and say which section it comes "
                    "from. Pass the bare term, singular, without quotes "
                    "(e.g. 'minor offense', 'vehicle')."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "term": {
                            "type": "string",
                            "description": (
                                "The word or phrase whose definition you "
                                "want, e.g. 'occupant'"
                            ),
                        },
                    },
                    "required": ["term"],
                },
                output_schema=DEFINITION_OUTPUT_SCHEMA,
                annotations=read_only,
            ),
            ToolDefinition(
                name="find_references",
                description=(
                    "Find where the rest of the code CITES a section: its "
                    "fine in a fine schedule, the penalty section that "
                    "covers it, and other sections that refer to it. Use "
                    "this for 'what is the fine / penalty for violating "
                    "§ X' and 'what refers to § X' instead of searching "
                    "for the number: a quoted search misses citations to "
                    "subsections (the traffic fine schedule lists "
                    "§ 9.14.040A.3.a, which '\"9.14.040\"' does not "
                    "match). Pass a full section number, e.g. '9.14.040', "
                    "or a subsection, e.g. '9.28.019B'. Fine schedules and "
                    "penalty sections are listed first, editorial "
                    "reference tables last. Each result has a snippet of "
                    "the text around the citation; read the citing section "
                    "with get_section before quoting it. A section with no "
                    "listed fine may still be covered by a general penalty "
                    "provision of its title or chapter that does not name "
                    "it."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "section": {
                            "type": "string",
                            "description": (
                                "A section number such as '9.14.040', "
                                "optionally with a subsection ('9.28.019B')"
                            ),
                        },
                        "page": {
                            "type": "integer",
                            "description": "0-indexed result page, 20 per page",
                            "default": 0,
                            "minimum": 0,
                        },
                    },
                    "required": ["section"],
                },
                output_schema=REFERENCES_OUTPUT_SCHEMA,
                annotations=read_only,
            ),
            ToolDefinition(
                name="recent_ordinances",
                description=(
                    "List the ordinances most recently adopted into the "
                    "code, newest first: ordinance number, adoption date, "
                    "subject and the chapters it amended. Use this for "
                    "'what changed recently', 'has Title 21 been amended "
                    "this year' or 'what did AO 2026-40 do'. It reads the "
                    "publisher's Ordinance List and Disposition Table, "
                    "which covers roughly the last year or two of codified "
                    "ordinances, not the code's whole history. Pass within "
                    "('Title 21', 'Chapter 21.05', 'Charter') to keep only "
                    "ordinances codified there. The table names chapters, "
                    "not sections: each row gives a ready-made query to "
                    "pass to search_code, which lists the sections whose "
                    "history cites the ordinance. The result also names "
                    "any ordinance adopted but not yet worked into the "
                    "code text, which is newer than the whole list and "
                    "can be read with read_attachment. To see when ONE section was last "
                    "amended, read it with get_section instead: its result "
                    "carries the section's parsed history."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "within": {
                            "type": "string",
                            "description": (
                                "Optional. Keep only ordinances codified "
                                "in this title or chapter: 'Title 21', "
                                "'Chapter 21.05', '21', 'R21' "
                                "(regulations) or 'Charter'."
                            ),
                        },
                        "limit": {
                            "type": "integer",
                            "description": "How many to list, newest first",
                            "default": DEFAULT_ORDINANCES,
                            "minimum": 1,
                            "maximum": MAX_ORDINANCES,
                        },
                    },
                    "required": [],
                },
                output_schema=ORDINANCES_OUTPUT_SCHEMA,
                annotations=read_only,
            ),
            ToolDefinition(
                name="find_ordinance",
                description=(
                    "Look up one ordinance by number and list the code "
                    "sections it added, amended or repealed, with its "
                    "effective date. Use this for 'what did AO 2025-94 "
                    "change', 'which sections did ordinance 2011-113 "
                    "touch' or 'when did AO 2016-53 take effect'. It reads "
                    "the publisher's Ordinance Disposition Table, which "
                    "runs from the 1977 code to the ordinances most "
                    "recently worked into the published code; for a "
                    "recent ordinance it also returns the subject and "
                    "adoption date, and for one adopted but not yet in "
                    "the code text, a link to the ordinance itself for "
                    "read_attachment. For an ordinance the table does not "
                    "list yet, it returns the sections of the code that "
                    "cite it. The table is a finding aid, not the "
                    "ordinance's text: read the sections it names with "
                    "get_sections to see what the code says now. An "
                    "ordinance that is not listed may still exist (too "
                    "new, repealed, or not of a general and permanent "
                    "nature). To go the other way -- which ordinances "
                    "amended ONE section -- read that section with "
                    "get_section and look at its history."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "ordinance": {
                            "type": "string",
                            "description": (
                                "The ordinance number: '2025-94', "
                                "'AO 2025-94', 'AO No. 2025-115(S-2)'. "
                                "Years before 2000 have two digits: "
                                "'85-8' is no. 8 of 1985."
                            ),
                        },
                    },
                    "required": ["ordinance"],
                },
                output_schema=FIND_ORDINANCE_OUTPUT_SCHEMA,
                annotations=read_only,
            ),
            ToolDefinition(
                name="read_attachment",
                description=(
                    "Read the text of a PDF the code publishes as an "
                    "attachment -- chiefly tables that are not in the "
                    "section text, such as Table 21.05-1 (Table of Allowed "
                    "Uses by zoning district) and the other tables of "
                    "Title 21. Pass the `url` of a search_code result that "
                    "has is_pdf=true, or an attachment url listed by "
                    "get_section (get_section 'Title 21' lists that "
                    "title's tables). Returns the text page by page; if "
                    "next_page is set, call again with page=next_page. "
                    "To locate something in a long document instead of "
                    "paging through it, pass find (a word, phrase or "
                    "section number): the result lists the pages where it "
                    "occurs, with a snippet of each; then read a page "
                    "with page=<that page>. "
                    "With layout=true (the default) columns are kept "
                    "aligned with spaces: to read a table cell, line the "
                    "mark up with the column headers at the top of the "
                    "page, and tell the user to confirm against the PDF. "
                    "It also reads an ORDINANCE that search_code returns "
                    "as a PDF (a recently adopted ordinance not yet in the "
                    "code text): the result says how to read its drafting "
                    "marks, which extraction loses. Only eCode360 urls for "
                    "this code are accepted."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "url": {
                            "type": "string",
                            "description": (
                                "The attachment's https url on "
                                "ecode360.com / live.ecode360.com, exactly "
                                "as a tool result gave it"
                            ),
                        },
                        "page": {
                            "type": "integer",
                            "description": "1-indexed page to start from",
                            "default": 1,
                            "minimum": 1,
                        },
                        "layout": {
                            "type": "boolean",
                            "description": (
                                "Keep table columns aligned (default). "
                                "Set false for plain running text."
                            ),
                            "default": True,
                        },
                        "find": {
                            "type": "string",
                            "maxLength": FIND_MAX_CHARS,
                            "description": (
                                "A word, phrase or section number to "
                                "locate in the document, e.g. 'conditional "
                                "use' or '21.40.030'. Not case-sensitive; "
                                "no operators or wildcards. Returns the "
                                "pages it is on instead of page text; "
                                "searching starts at `page`."
                            ),
                        },
                    },
                    "required": ["url"],
                },
                output_schema=ATTACHMENT_OUTPUT_SCHEMA,
                annotations=read_only,
            ),
            ToolDefinition(
                name="get_customer_info",
                description=(
                    f"Get metadata about {city}'s code library: official name, "
                    "state, government type, last-updated date, and public URL. "
                    "The last-updated date says how current the published "
                    "code text is. Every get_section and get_sections "
                    "result already carries that date as "
                    "`code_last_updated`, so there is no need to call this "
                    "just to check how current a section is."
                ),
                input_schema={"type": "object", "properties": {}},
                output_schema=CUSTOMER_OUTPUT_SCHEMA,
                annotations=read_only,
            ),
        ]

        # Every tool here is read-only, so `annotations` is uniform and
        # `title` is the only per-tool display concern.
        #
        # Deliberately NOT set: `idempotentHint`. The MCP schema documents it
        # as meaningful only when readOnlyHint == false, so on these tools it
        # would carry no information. A test pins its absence.
        for tool in tools:
            if tool.title is None:
                tool.title = self.TOOL_TITLES.get(tool.name)
        return tools

    # ── Tool dispatch ───────────────────────────────────────────────────

    async def execute_tool(
        self, tool_name: str, arguments: Dict[str, Any]
    ) -> ToolResult:
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            # The MCP server refuses this before it gets here (-32602); kept
            # so a direct caller gets a sentence, not an AttributeError.
            return self._err(
                "Tool arguments must be an object of named values "
                f"(got {type(arguments).__name__})."
            )
        token = _call_deadline.set(time.monotonic() + TOOL_CALL_BUDGET_S)
        try:
            return await self._execute_tool(tool_name, arguments)
        finally:
            _call_deadline.reset(token)

    async def _execute_tool(
        self, tool_name: str, arguments: Dict[str, Any]
    ) -> ToolResult:
        try:
            if tool_name == "search_code":
                query = arguments.get("query")
                if query is not None and not isinstance(query, str):
                    raise ToolInputError(
                        "query must be a string of words or phrases to "
                        f"search for (got {type(query).__name__})."
                    )
                query = (query or "").strip()
                if not query:
                    raise ToolInputError(
                        "query is required -- pass the words or phrase to search "
                        "the municipal code for (e.g. 'dog license')."
                    )
                if len(query) > MAX_QUERY_CHARS:
                    raise ToolInputError(
                        f"query is too long (at most {MAX_QUERY_CHARS} "
                        f"characters, got {len(query)}). Search for a "
                        "phrase or a few terms joined with AND."
                    )
                page = self._search_page_arg(arguments)
                within = arguments.get("within")
                if within is not None and str(within).strip():
                    if len(str(within).strip()) > MAX_WITHIN_CHARS:
                        raise ToolInputError(
                            f"within is too long (at most {MAX_WITHIN_CHARS} "
                            "characters). Pass a title, chapter or section "
                            "reference such as 'Title 9' or 'Chapter 9.28'."
                        )
                    return self._ok(
                        *await self._scoped_search(
                            query, page, str(within).strip()
                        )
                    )
                data = await self._search(query, page)
                text, structured = self._format_search(data, query, page)
                if page == 0 and structured["total_count"] == 0:
                    text, structured = await self._zero_result_followups(
                        query, text, structured
                    )
                return self._ok(text, structured)

            if tool_name == "get_table_of_contents":
                data = await self._get_toc()
                return self._ok(*self._format_toc(data))

            if tool_name == "browse_structure":
                # A title, chapter or section written as it is cited works
                # here as it does in get_section; 'R3.70.120' used to be
                # refused.
                named = await self._named_guid(arguments.get("guid"))
                parsed = None if named else self._parse_ref(arguments.get("guid"))
                if named:
                    guid = named
                elif parsed:
                    kind, number, charter_only = parsed
                    shown = " ".join(str(arguments.get("guid")).split())
                    guid = await self._resolve_reference(
                        kind, number, charter_only, shown
                    )
                else:
                    guid = self._guid_arg(arguments, default="ROOT")
                data = await self._get_recent_json(
                    self._cust_path(f"/structure/{quote(guid, safe='')}")
                )
                # structure responses wrap the node in an {error, results} envelope
                node = data.get("results", data) if isinstance(data, dict) else data
                parts: List[Dict[str, Any]] = []
                if isinstance(node, dict) and not node.get("children"):
                    parts = await self._parts_of(
                        str(node.get("guid") or node.get("id") or ""),
                        node.get("number"),
                    )
                return self._ok(*self._format_structure(node, parts))

            if tool_name == "get_section":
                offset = self._int_arg(arguments, "offset", 0)
                if offset < 0:
                    raise ToolInputError(
                        f"offset must be 0 or greater (got {offset}); it is "
                        "the character position to read on from, as given "
                        "in a rendered_text_truncated note."
                    )
                (data, resolved_from), updated = await asyncio.gather(
                    self._fetch_section(arguments.get("guid")),
                    self._code_last_updated(),
                )
                text, structured = self._format_content(
                    data, resolved_from=resolved_from, offset=offset
                )
                return self._ok(
                    *self._with_code_date(text, structured, updated)
                )

            if tool_name == "get_sections":
                requested, merged = self._sections_arg(arguments)
                outcomes, updated = await asyncio.gather(
                    self._fetch_sections(requested),
                    self._code_last_updated(),
                )
                text, structured = self._format_sections(
                    requested, outcomes, merged
                )
                return self._ok(
                    *self._with_code_date(text, structured, updated)
                )

            if tool_name == "find_definition":
                term = self._term_arg(arguments)
                data = await self._search(f'title:"{term}"', 0)
                hits = self._definition_hits(data, term)
                reordered: Set[str] = set()
                bodies = await self._fetch_definition_bodies(
                    hits[:MAX_DEFINITIONS], reordered
                )
                return self._ok(
                    *self._format_definitions(
                        term, data, hits, bodies, reordered
                    )
                )

            if tool_name == "find_references":
                number, base = self._reference_arg(arguments)
                page = self._search_page_arg(arguments)
                data = await self._search(f"{number}*", page)
                return self._ok(
                    *self._format_references(number, base, data, page)
                )

            if tool_name == "recent_ordinances":
                scope = self._ordinance_scope_arg(arguments)
                limit = self._int_arg(arguments, "limit", DEFAULT_ORDINANCES)
                if not 1 <= limit <= MAX_ORDINANCES:
                    raise ToolInputError(
                        f"limit must be between 1 and {MAX_ORDINANCES} "
                        f"(got {limit})."
                    )
                node, updated, pending = await asyncio.gather(
                    self._fetch_ordinance_list(),
                    self._code_last_updated(),
                    self._pending_ordinances(),
                )
                return self._ok(
                    *self._with_pending_ordinances(
                        *self._format_ordinances(node, scope, limit, updated),
                        pending,
                        scope,
                    )
                )

            if tool_name == "find_ordinance":
                base, asked = self._ordinance_number_arg(arguments)
                table, listing, updated = await asyncio.gather(
                    # The table takes eCode360 about 9 s to produce and
                    # changes only when the code is republished, so it is
                    # kept as long as the outline is, not for 5 minutes.
                    self._fetch_section_titled(
                        ORDINANCE_TABLE_TITLE, static=True
                    ),
                    self._fetch_ordinance_list(),
                    self._code_last_updated(),
                )
                # After the table, not beside it: the table can take 9 s,
                # and this short search must not be the one left waiting.
                pending = await self._pending_ordinances()
                numbers = self._outline_section_numbers(await self._get_toc())
                text, structured = self._format_ordinance_lookup(
                    base, asked, table, listing, updated, numbers
                )
                # The table has no row (too new, usually): find the
                # sections that cite it, which is the search a caller
                # would otherwise have to run next.
                citing = (
                    None
                    if structured["found"]
                    else await self._citing_sections(
                        structured["search_query"].strip('"')
                    )
                )
                text, structured = self._with_citing_sections(
                    text, structured, citing
                )
                return self._ok(
                    *self._with_ordinance_pdfs(text, structured, base, pending)
                )

            if tool_name == "read_attachment":
                url = self._attachment_url_arg(arguments)
                page = self._int_arg(arguments, "page", 1)
                if page < 1:
                    raise ToolInputError(
                        f"page must be 1 or greater (got {page}); pages "
                        "are 1-indexed."
                    )
                layout = arguments.get("layout", True)
                if not isinstance(layout, bool):
                    raise ToolInputError(
                        f"layout must be true or false (got {layout!r})."
                    )
                find = arguments.get("find")
                if find is not None and not isinstance(find, str):
                    raise ToolInputError(
                        f"find must be a string (got {find!r})."
                    )
                find = " ".join((find or "").split())
                if len(find) > FIND_MAX_CHARS:
                    raise ToolInputError(
                        f"find is {len(find)} characters; the limit is "
                        f"{FIND_MAX_CHARS}. Pass a word, a short phrase or "
                        "a section number."
                    )
                if find:
                    entry = await self._attachment_page_texts(url)
                    if page > entry["page_count"]:
                        raise ToolInputError(
                            f"page {page} is past the end: this document "
                            f"has {entry['page_count']} page(s). Pages are "
                            "1-indexed."
                        )
                    return self._ok(
                        *self._format_find(url, find, page, entry)
                    )
                pdf = await self._download_attachment(url)
                extracted = await asyncio.to_thread(
                    self._extract_pdf, pdf, page, layout, _call_deadline.get()
                )
                return self._ok(
                    *self._format_attachment(url, layout, extracted)
                )

            if tool_name == "get_customer_info":
                data = await self._get_static_json(self._cust_path(""))
                return self._ok(*self._format_customer(data))

            return self._err(f"Unknown tool: {tool_name}")

        except ToolInputError as e:
            # The caller asked for something invalid. That is not a server
            # fault, so no traceback -- a stack trace here is a false claim
            # that this server broke, and it buries the real ones. The
            # message still reaches the caller verbatim.
            logger.warning(
                "Invalid arguments for tool %s: %s",
                tool_name,
                e,
                extra={"tool_name": tool_name, "error_type": type(e).__name__},
            )
            return self._err(str(e) or "Invalid tool arguments")

        except Exception as e:
            logger.error("Error executing tool %s: %s", tool_name, e, exc_info=True)
            # Several exceptions (every httpx timeout among them) stringify
            # to ''. Never hand the caller a bare "failed" with no cause.
            return self._err(
                str(e) or f"Tool execution failed ({type(e).__name__})"
            )

    # ── Caller-argument coercion ────────────────────────────────────────

    @staticmethod
    def _int_arg(args: Dict[str, Any], name: str, default: Any = None) -> int:
        """Coerce a caller-supplied argument to int, or explain why not.

        A bare int() raises "invalid literal for int() with base 10: 'two'"
        -- which reads as a server fault in the logs and tells the caller
        nothing about which argument was wrong.

        An explicit null means "not supplied" (clients send it for an
        optional argument left blank). A whole number written as a string
        ("2") or a float (2.0) is taken; true/false, 1.5, NaN and infinity
        are refused rather than read as some integer -- int(inf) raises
        OverflowError, which used to surface as a server fault.
        """
        raw = args.get(name)
        if raw is None:
            raw = default
        refused = ToolInputError(f"{name} must be an integer (got {raw!r})")
        if isinstance(raw, bool):
            raise refused
        if isinstance(raw, float) and not raw.is_integer():
            # False for NaN and both infinities too.
            raise refused
        try:
            return int(raw)
        except (TypeError, ValueError, OverflowError) as exc:
            raise refused from exc

    @classmethod
    def _search_page_arg(cls, args: Dict[str, Any]) -> int:
        """The 0-indexed `page` of a search, checked at both ends."""
        page = cls._int_arg(args, "page", 0)
        if page < 0:
            raise ToolInputError(
                f"page must be 0 or greater (got {page}); results are "
                "0-indexed, 20 per page."
            )
        if page > MAX_SEARCH_PAGE:
            raise ToolInputError(
                f"page must be at most {MAX_SEARCH_PAGE} (got {page}); "
                "results are 0-indexed, 20 per page, and the result says "
                "how many pages a query has."
            )
        return page

    @staticmethod
    def _guid_arg(
        args: Dict[str, Any], name: str = "guid", default: Optional[str] = None
    ) -> str:
        """Validate a caller-supplied node guid.

        This is the most common caller error this server sees: a model
        reaches get_section or browse_structure with something it parsed out
        of prose -- usually the section NUMBER (21.05.010), which is a
        different namespace and always 404s. Catching it here turns an
        opaque upstream 404 into a message that names the mistake and says
        how to recover.
        """
        raw = args.get(name, default)
        if raw is None or not str(raw).strip():
            raise ToolInputError(
                f"{name} is required. A guid is the opaque node id returned "
                "by search_code, get_table_of_contents or browse_structure "
                "(e.g. '49401992') -- not a section number. Call search_code "
                "first and read the guid from the result."
            )
        guid = str(raw).strip()
        if guid.upper() == "ROOT":
            return "ROOT"
        if not GUID_RE.match(guid):
            looks_like_reference = any(c.isdigit() for c in guid) and (
                "." in guid or "\u00a7" in guid
            )
            hint = (
                " That looks like a section NUMBER, not a guid."
                if looks_like_reference
                else ""
            )
            if ".pdf" in guid.lower() or "attachment" in guid.lower():
                # A search hit for a PDF attachment carries its file name
                # where a guid would be; models pass it on (Table 21.05-1,
                # for one) and used to be told it "looks like a
                # section number".
                raise ToolInputError(
                    f"{name} {guid!r} is a PDF attachment, not a code "
                    "section, so it has no guid. Pass the `url` of that "
                    "search result to read_attachment instead."
                )
            raise ToolInputError(
                f"{name} must be an alphanumeric node id such as '49401992' "
                f"(got {guid!r})." + hint + " Use search_code to find the "
                "section and read its guid from the result."
            )
        return guid

    @staticmethod
    def _sections_arg(args: Dict[str, Any]) -> Tuple[List[str], int]:
        """(distinct requested sections in order, how many repeats merged)."""
        raw = args.get("sections")
        if raw is None:
            # get_section's argument is `guid`, and a caller moving from
            # one tool to the other carries the name over. What is meant
            # is not in doubt, so take it.
            for other in ("guids", "guid", "section"):
                if args.get(other) is not None:
                    raw = args[other]
                    break
        if isinstance(raw, str) and raw.strip().startswith("["):
            # The list written out as a JSON string.
            try:
                decoded = json.loads(raw)
            except ValueError:
                decoded = None
            if isinstance(decoded, list):
                raw = decoded
        if isinstance(raw, str):
            # A model that has one section in hand often passes a bare
            # string. That is unambiguous, so take it.
            raw = [raw]
        if not isinstance(raw, list) or not raw:
            raise ToolInputError(
                "sections is required -- pass a list of guids and/or "
                "section numbers, e.g. ['21.05.010', '49422044']."
            )
        seen: List[str] = []
        for item in raw:
            if not isinstance(item, (str, int)) or isinstance(item, bool):
                raise ToolInputError(
                    "sections must be a list of strings (guids or section "
                    f"numbers); got an entry of type {type(item).__name__}."
                )
            text = str(item).strip()
            if not text:
                raise ToolInputError(
                    "sections contains an empty entry; each entry must be "
                    "a guid or a section number."
                )
            if text not in seen:
                seen.append(text)
        if len(seen) > MAX_BATCH_SECTIONS:
            raise ToolInputError(
                f"sections accepts at most {MAX_BATCH_SECTIONS} entries per "
                f"call (got {len(seen)}). Split the list across calls."
            )
        return seen, len(raw) - len(seen)

    @staticmethod
    def _term_arg(args: Dict[str, Any]) -> str:
        raw = args.get("term")
        # Quotes would break out of the title:"..." phrase this is placed
        # in; the term is a phrase, never search syntax.
        term = _QUOTE_CHARS_RE.sub("", raw if isinstance(raw, str) else "")
        term = " ".join(term.split())
        if not term:
            raise ToolInputError(
                "term is required -- pass the word or phrase whose "
                "definition you want (e.g. 'minor offense')."
            )
        if len(term) > MAX_TERM_CHARS:
            raise ToolInputError(
                f"term must be a word or short phrase (at most "
                f"{MAX_TERM_CHARS} characters, got {len(term)}). To search "
                "for a sentence, use search_code."
            )
        return term

    # ── Section fetching ────────────────────────────────────────────────

    async def _fetch_section(
        self, raw: Any
    ) -> Tuple[Dict[str, Any], Optional[str]]:
        """(content JSON, the section number it was resolved from or None).

        `raw` is a guid or a section number, as get_section accepts.
        """
        raw = await self._named_guid(raw) or raw
        parsed = self._parse_ref(raw)
        if parsed:
            kind, number, charter_only = parsed
            shown = " ".join(str(raw).split())
            subsection = None
            try:
                guid = await self._resolve_reference(
                    kind, number, charter_only, shown
                )
            except ToolInputError:
                # "9.28.019B.2" is how a fine schedule cites a subsection,
                # and subsections have no number of their own to look up:
                # read the section it belongs to and say so.
                whole = REFERENCE_NUMBER_RE.match(number)
                if not whole or not whole.group(2) or charter_only:
                    raise
                guid = await self._resolve_reference(
                    kind, whole.group(1), charter_only, shown
                )
                subsection, number = whole.group(2), whole.group(1)
            resolved_from: Optional[str] = (
                number if kind is None and not charter_only else shown
            )
            try:
                data = await self._get_recent_json(
                    self._cust_path(f"/code/content/{quote(guid, safe='')}")
                )
            except EcodeNotFoundError:
                # The outline is kept for an hour. If the code was
                # republished in that time a guid taken from it may be
                # gone: ask search, which is current, before giving up.
                if kind is not None or charter_only or number.count(".") < 2:
                    raise
                fresh = await self._resolve_section_number(number)
                if fresh == guid:
                    raise
                logger.warning(
                    "outline guid %s for %s is stale; search gave %s",
                    guid,
                    number,
                    fresh,
                )
                data = await self._get_recent_json(
                    self._cust_path(f"/code/content/{quote(fresh, safe='')}")
                )
            if subsection:
                # A copy: cached responses are shared and read-only.
                data = {**data, "_subsection": subsection}
            data = await self._with_title_attachments(data)
            return await self._with_parts(data), resolved_from
        else:
            guid = self._guid_arg({"guid": raw})
            resolved_from = None
        data = await self._get_recent_json(
            self._cust_path(f"/code/content/{quote(guid, safe='')}")
        )
        data = await self._with_title_attachments(data)
        return await self._with_parts(data), resolved_from

    # ── A heading section and its parts ─────────────────────────────────

    def _outline_parts(
        self, toc: Any, guid: str, number: Any
    ) -> List[Dict[str, Any]]:
        """Sections the outline numbers under section `number` ('N.1',
        'N.2', 'N.2.1'), in outline order. See _PARTS_SCHEMA."""
        base = self._norm_number(number if isinstance(number, str) else "")
        if base.count(".") < 2:
            return []
        prefix = base + "."
        parts: List[Dict[str, Any]] = []
        seen = {guid}

        def walk(node: Any) -> None:
            if not isinstance(node, dict):
                return
            node_guid = str(node.get("guid") or "")
            if (
                node.get("type") == "section"
                and node_guid
                and node_guid not in seen
                and self._norm_number(node.get("number")).startswith(prefix)
            ):
                seen.add(node_guid)
                parts.append(
                    {
                        "guid": node_guid,
                        "ref": " ".join((node.get("number") or "").split()),
                        "title": (node.get("title") or "").strip() or None,
                        "url": PUBLIC_URL.format(guid=node_guid),
                    }
                )
            for child in node.get("children") or []:
                walk(child)

        walk(toc)
        return parts

    async def _parts_of(self, guid: str, number: Any) -> List[Dict[str, Any]]:
        """_outline_parts from the cached outline. Decorates a result and
        must never fail one."""
        if self._norm_number(
            number if isinstance(number, str) else ""
        ).count(".") < 2:
            return []
        try:
            return self._outline_parts(await self._get_toc(), guid, number)
        except (EcodeAPIError, ToolInputError) as e:
            logger.warning("could not list the parts of %s: %s", number, e)
            return []

    async def _with_parts(self, data: Any) -> Any:
        """`data` with the sections numbered under it, when it is a section
        with no text of its own."""
        if (
            not isinstance(data, dict)
            or data.get("pdf")
            or data.get("attachments")
            # The API gives a heading's own title as its content, which
            # the rendering drops; anything longer is text.
            or self._content_chars(data)
            > len(data.get("title") or "") + 2
        ):
            return data
        body = self._render_content_node(
            data,
            depth=0,
            budget=[self._UNCAPPED],
            heading_shown=True,
            notes={"inferred": 0, "uncertain": 0},
        )
        if body.strip():
            return data
        parts = await self._parts_of(
            str(data.get("id") or data.get("guid") or ""), data.get("number")
        )
        if not parts:
            return data
        # A copy: cached responses are shared and read-only.
        return {**data, "_parts": parts}

    # ── Attachments of a whole title ────────────────────────────────────

    def _mentions_title_attachment(self, node: Any) -> bool:
        if not isinstance(node, dict):
            return False
        content = node.get("content")
        if isinstance(content, str) and TITLE_ATTACHMENT_NOTE_RE.search(
            content
        ):
            return True
        return any(
            self._mentions_title_attachment(child)
            for child in node.get("children") or []
        )

    @staticmethod
    def _title_of(toc: Any, guid: str) -> Optional[Dict[str, Any]]:
        """The outline's Title node that is, or contains, node `guid`."""
        found: List[Optional[Dict[str, Any]]] = []

        def walk(node: Any, title: Optional[Dict[str, Any]]) -> bool:
            if not isinstance(node, dict):
                return False
            if (node.get("label") or "").strip().lower() == "title":
                title = node
            if str(node.get("guid") or "") == guid:
                found.append(title)
                return True
            return any(
                walk(child, title) for child in node.get("children") or []
            )

        walk(toc, None)
        return found[0] if found else None

    async def _title_attachment_index(
        self,
    ) -> Dict[str, List[Dict[str, str]]]:
        """{title index number, lower case: [{name, url}]} for every PDF
        attached to a title. See TITLE_ATTACHMENT_QUERY. Kept as long as
        the outline is."""
        key = (self.plugin_config.base_url, _TITLE_ATTACHMENTS_KEY)
        cached = _STATIC_CACHE.get(key)
        if cached and cached[0] > time.monotonic():
            return cached[1]
        index: Dict[str, List[Dict[str, str]]] = {}
        for page in range(TITLE_ATTACHMENT_PAGES):
            data = await self._search(TITLE_ATTACHMENT_QUERY, page)
            for hit in data.get("results", []) or []:
                url = hit.get("url")
                match = TITLE_ATTACHMENT_ID_RE.match(str(hit.get("id") or ""))
                if not (
                    match
                    and hit.get("pdf")
                    and isinstance(url, str)
                    and url.strip()
                ):
                    continue
                url = url.strip().replace(" ", "%20")
                filename = unquote(url.rsplit("/", 1)[-1])
                named = TITLE_ATTACHMENT_FILE_RE.match(filename)
                name = named.group(1).strip() if named else filename
                rows = index.setdefault(
                    " ".join(match.group(1).lower().split()), []
                )
                if all(row["url"] != url for row in rows):
                    rows.append({"name": name, "url": url})
            if page + 1 >= self._as_int(data.get("maxPages", 1), default=1):
                break
        for rows in index.values():
            rows.sort(key=lambda row: row["url"])
        _STATIC_CACHE[key] = (time.monotonic() + STATIC_CACHE_TTL_S, index)
        return index

    async def _with_title_attachments(self, data: Any) -> Any:
        """`data` with the PDFs attached to its title, when a note in its
        text points to one. Decorates a read and must never fail one."""
        if not self._mentions_title_attachment(data):
            return data
        try:
            title = self._title_of(
                await self._get_toc(), str(data.get("id") or "")
            )
            if not title:
                return data
            index = await self._title_attachment_index()
        except (EcodeAPIError, ToolInputError) as e:
            logger.warning("could not list the title's attachments: %s", e)
            return data
        index_num = " ".join((title.get("indexNum") or "").lower().split())
        found = index.get(index_num)
        if not found:
            return data
        number = (title.get("number") or "").strip()
        name = (title.get("title") or "").strip()
        if index_num == CHARTER_INDEX_NUM:
            display = "the Charter"
        else:
            display = f"Title {number}" + (f" ({name})" if name else "")
        # A copy: cached responses are shared and read-only.
        return {
            **data,
            "_title_attachments": {"title": display, "items": found},
        }

    async def _fetch_sections(self, requested: List[str]) -> List[Any]:
        """One outcome per request, in order: a (data, ref) pair or the
        exception that request raised. Tasks inherit the call deadline (the
        contextvar is copied into each), so the batch shares one budget."""
        gate = asyncio.Semaphore(BATCH_CONCURRENCY)

        async def one(raw: str) -> Any:
            async with gate:
                return await self._fetch_section(raw)

        return await asyncio.gather(
            *(one(raw) for raw in requested), return_exceptions=True
        )

    # ── Definitions ─────────────────────────────────────────────────────

    @staticmethod
    def _norm_term(text: Optional[str]) -> str:
        cleaned = _QUOTE_CHARS_RE.sub("", text or "")
        return " ".join(cleaned.split()).rstrip(".").lower()

    def _definition_hits(
        self, data: Dict[str, Any], term: str
    ) -> List[Dict[str, Any]]:
        """The search hits that are definition nodes, exact matches first.

        A definition node is recognised by its number alone
        ("9.04.010B{116}") -- search results type every hit as "code", so
        `type` cannot tell a definition from a section whose title merely
        contains the term.
        """
        want = self._norm_term(term)
        out: List[Dict[str, Any]] = []
        seen = set()
        for hit in data.get("results", []) or []:
            number = (hit.get("number") or "").strip()
            match = DEFINITION_NUMBER_RE.match(number)
            guid = str(hit.get("id") or "")
            if not match or not GUID_RE.match(guid) or guid in seen:
                continue
            seen.add(guid)
            defined = self._strip_html(hit.get("title", "")) or None
            # "9.04.010B{116}" -> "9.04.010": the trailing capital is the
            # subsection the definitions list sits in, which is not a
            # section number get_section can resolve.
            section = re.sub(r"^§\s*", "", match.group(1).strip())
            section = re.sub(r"(?<=\d)[A-Z]$", "", section).strip()
            out.append(
                {
                    "guid": guid,
                    "term": defined,
                    "exact_match": self._norm_term(defined) == want,
                    "defined_in": section or None,
                    "snippet": self._strip_html(hit.get("text") or "") or None,
                }
            )
        # Stable: exact matches first, otherwise the API's own ranking.
        out.sort(key=lambda d: not d["exact_match"])
        return out

    async def _fetch_definition_bodies(
        self,
        hits: List[Dict[str, Any]],
        reordered: Optional[Set[str]] = None,
    ) -> List[Optional[str]]:
        """Full text per definition, or None where the fetch failed.

        The search snippet is elided mid-sentence, so the body is read from
        the content endpoint. A failed fetch must not sink the lookup: the
        caller falls back to the snippet and says so. `reordered` collects
        the guids whose text has a list this server had to place (see
        _split_list_text), so the caller can say so as get_section does.
        """
        gate = asyncio.Semaphore(BATCH_CONCURRENCY)

        async def one(guid: str) -> Optional[str]:
            async with gate:
                try:
                    node = await self._get_recent_json(
                        self._cust_path(
                            f"/code/content/{quote(guid, safe='')}"
                        )
                    )
                except (EcodeAPIError, ToolInputError) as e:
                    logger.warning(
                        "definition body fetch failed for %s: %s", guid, e
                    )
                    return None
                notes: Dict[str, int] = {"inferred": 0, "uncertain": 0}
                body = self._render_content_node(
                    node, depth=0, budget=[self._UNCAPPED], notes=notes
                )
                if reordered is not None and any(notes.values()):
                    reordered.add(guid)
                return body.strip() or None

        return list(await asyncio.gather(*(one(h["guid"]) for h in hits)))

    # ── Section-number resolution ───────────────────────────────────────

    @staticmethod
    def _section_ref(raw: Any) -> Optional[str]:
        """The bare section number if `raw` is written as one, else None.

        "§ 21.05.010" -> "21.05.010"; "49422044" and "ROOT" -> None.
        """
        if not isinstance(raw, str):
            return None
        match = SECTION_REF_RE.match(raw.strip())
        return match.group(1) if match else None

    @staticmethod
    def _norm_ref(ref: Optional[str]) -> str:
        return re.sub(r"[\s§]", "", ref or "").lower()

    @staticmethod
    def _parse_ref(raw: Any) -> Optional[Tuple[Optional[str], str, bool]]:
        """(kind or None, number, charter_only) if `raw` is a reference.

        None means "treat it as a guid". A bare number needs a dot and a
        digit to count (as _section_ref always required), so a numeric guid
        is never mistaken for one; with a kind word in front ("Title 21")
        the number may be a single token.
        """
        if not isinstance(raw, str):
            return None
        # "21.05.010." -- a number copied from the end of a sentence. The
        # full stop is not part of it; left on, the whole thing was taken
        # for a guid and refused.
        match = REF_WITH_KIND_RE.match(raw.strip().rstrip(".,;:").rstrip())
        if not match:
            return None
        word = " ".join((match.group(1) or "").lower().split()).rstrip(".")
        number = match.group(2)
        charter_only = word.startswith("charter")
        if charter_only or word in ("section", "sec"):
            kind: Optional[str] = KIND_SECTION
        elif word in ("title", "ttl"):
            kind = KIND_TITLE
        elif word in ("chapter", "ch"):
            kind = KIND_CHAPTER
        else:  # no word, "AMC", or "§" -- a plain number, as always
            kind = None
        if kind in (None, KIND_SECTION):
            if "." not in number or not any(c.isdigit() for c in number):
                return None
        return kind, number, charter_only

    async def _resolve_reference(
        self,
        kind: Optional[str],
        number: str,
        charter_only: bool,
        shown: str,
    ) -> str:
        """Turn a parsed reference into a guid.

        Sections (three or more parts) are found by search, as before.
        Titles, chapters and Charter sections go to the table of contents:
        search knows a chapter only as "Ch 17.05", and a bare "17.05" is
        BOTH Chapter 17.05 of the code and Section 17.05 of the Charter --
        only the complete outline can say whether a number is unique.
        """
        explicit = kind in (KIND_TITLE, KIND_CHAPTER) or charter_only
        if not explicit and number.count(".") != 1:
            # The outline first: it is already in memory and lists every
            # section, while a search costs an upstream call that stalls
            # for about 5 s one time in eight. Prod, 2026-11-20: reads by
            # section number took 4 s or more in 13 of 30 calls, reads by
            # guid in 0 of 9, and 92 of the 93 numbers asked for were in
            # the outline (the 93rd does not exist). Search still answers
            # when the outline is unavailable, does not list the number
            # (a building-code part, a subsection), or lists it twice.
            listed: List[Dict[str, Any]] = []
            if RESOLVE_SECTIONS_FROM_OUTLINE:
                try:
                    listed = self._outline_matches(
                        await self._get_toc(), KIND_SECTION, number, False
                    )
                except EcodeAPIError:
                    listed = []
            if len(listed) == 1:
                return listed[0]["guid"]
            return await self._resolve_section_number(number)

        try:
            matches = self._outline_matches(
                await self._get_toc(), kind, number, charter_only
            )
        except EcodeAPIError:
            if explicit:
                raise
            # The outline is an optimisation for a bare number; search can
            # still answer when it is down.
            return await self._resolve_section_number(number)

        if len(matches) == 1:
            return matches[0]["guid"]
        if not matches:
            if explicit:
                what = "Charter section" if charter_only else kind
                raise ToolInputError(
                    f"No {what} numbered {number!r} was found in the "
                    f"published code (looked up from {shown!r}). Use "
                    "get_table_of_contents or browse_structure to see the "
                    "titles and chapters that exist."
                )
            return await self._resolve_section_number(number)
        listed = "; ".join(
            f"{m['display']} (guid {m['guid']})" for m in matches
        )
        raise ToolInputError(
            f"{shown!r} matches {len(matches)} different entries: {listed}. "
            "Call again with one of those guids, or say which you mean -- "
            f"e.g. 'Chapter {number}' or 'Charter Section {number}'."
        )

    @staticmethod
    def _norm_number(number: Optional[str]) -> str:
        """A node's number as the outline index keys it: no §, no
        'Section' word, no whitespace, lowercase."""
        text = re.sub(r"[\s§]", "", number or "").lower()
        return re.sub(r"^section", "", text)

    def _outline_matches(
        self,
        toc: Dict[str, Any],
        kind: Optional[str],
        number: str,
        charter_only: bool,
    ) -> List[Dict[str, Any]]:
        """Titles / chapters / sections in the outline with this number."""
        want = self._norm_number(number)
        out: List[Dict[str, Any]] = []
        seen = set()

        def walk(node: Any, in_charter: bool) -> None:
            if not isinstance(node, dict):
                return
            index_num = (node.get("indexNum") or "").strip().lower()
            in_charter = in_charter or index_num == CHARTER_INDEX_NUM
            label = (node.get("label") or "").strip()
            node_kind = _TOC_LABEL_KINDS.get(label.lower())
            guid = str(node.get("guid") or "")
            if (
                node_kind
                and guid
                and guid not in seen
                # A "Section" label on a non-section node is a part of the
                # building codes ("Ch 23.10 § 101"), not a code section.
                and (
                    node_kind != KIND_SECTION
                    or node.get("type") == "section"
                )
                and self._norm_number(node.get("number")) == want
                and (kind is None or node_kind == kind)
                # A bare number never means a whole title.
                and (kind is not None or node_kind != KIND_TITLE)
                and (not charter_only or in_charter)
            ):
                seen.add(guid)
                bare = re.sub(
                    r"^(?:§|section)\s*",
                    "",
                    (node.get("number") or "").strip(),
                    flags=re.IGNORECASE,
                )
                words = (
                    "Charter Section"
                    if in_charter and node_kind == KIND_SECTION
                    else label
                )
                title = (node.get("title") or "").strip()
                out.append(
                    {
                        "guid": guid,
                        "kind": node_kind,
                        "display": f"{words} {bare} {title}".strip(),
                    }
                )
            for child in node.get("children") or []:
                walk(child, in_charter)

        walk(toc, False)
        return out

    # ── Scoped search ───────────────────────────────────────────────────

    async def _named_guid(self, raw: Any) -> Optional[str]:
        """The guid of a part of the code cited by name ('Charter',
        'Charter Article IV', 'Part III'), for the tools that take a guid
        or a reference. search_code learned these names in 1.14.1; without
        this, get_section('Charter') went upstream as a guid and came back
        as "no such entry". None for anything else, and when the outline
        cannot be read (the caller then reports the reference as before).
        """
        if not isinstance(raw, str) or not NAMED_PART_HINT_RE.search(raw):
            return None  # a guid or a number: no outline lookup needed
        try:
            named = self._named_scope(await self._get_toc(), raw.strip())
        except (EcodeAPIError, ToolInputError):
            return None
        return named[0] if named else None

    @staticmethod
    def _named_scope(toc: Any, within: str) -> Optional[Tuple[str, str]]:
        """(guid, display) for a part of the code that is cited by NAME:
        'Charter', 'Charter Article IV', 'Part III', 'Regulations'.

        The Charter has no title number, so "Title 9"-style references
        cannot reach it, and the bare word was taken for a guid and
        refused. Found by the
        outline's own labels and numbers, never by a guid written here.
        None when `within` is not such a name or the outline has no such
        part.
        """
        text = " ".join(within.lower().replace(".", " ").split())
        text = re.sub(r"^the ", "", text)

        def first(match) -> Optional[Dict[str, Any]]:
            stack = [toc]
            while stack:
                node = stack.pop()
                if not isinstance(node, dict):
                    continue
                if match(node):
                    return node
                stack.extend(reversed(node.get("children") or []))
            return None

        def number_of(node: Dict[str, Any]) -> str:
            return "".join(str(node.get("number") or "").split()).upper()

        def is_charter(node: Dict[str, Any]) -> bool:
            return (
                str(node.get("label") or "").lower() == "title"
                and number_of(node) == "CHA"
            )

        def result(node: Optional[Dict[str, Any]], display: str):
            guid = str((node or {}).get("guid") or "")
            return (guid, display) if guid else None

        article = re.fullmatch(
            r"(?:charter,? )?art(?:icle)? ([ivxlc]+|\d{1,2})"
            r"(?:,? (?:of (?:the )?)?charter)?",
            text,
        )
        if article and "charter" in text:
            wanted = article.group(1).upper()
            if wanted.isdigit():
                wanted = EcodePlugin._roman(int(wanted))
            charter = first(is_charter)
            node = next(
                (
                    child
                    for child in (charter or {}).get("children") or []
                    if isinstance(child, dict)
                    and str(child.get("label") or "").lower() == "article"
                    and number_of(child) == wanted
                ),
                None,
            )
            title = str((node or {}).get("title") or "").strip()
            return result(node, f"Charter Article {wanted} {title}".strip())
        if text in SCOPE_CHARTER_NAMES:
            return result(first(is_charter), "the Charter")
        part = re.fullmatch(r"part (i{1,3}|[123])", text)
        wanted_part = None
        if part:
            wanted_part = part.group(1).upper()
            if wanted_part.isdigit():
                wanted_part = "I" * int(wanted_part)
        elif text in SCOPE_REGULATIONS_NAMES:
            wanted_part = "III"
        if wanted_part:
            node = first(
                lambda n: re.match(
                    rf"PART {wanted_part}\b(?!I)",
                    str(n.get("title") or "").upper(),
                )
                is not None
            )
            return result(node, str((node or {}).get("title") or "").strip())
        return None

    @staticmethod
    def _roman(value: int) -> str:
        out = ""
        for size, letters in (
            (50, "L"), (40, "XL"), (10, "X"), (9, "IX"),
            (5, "V"), (4, "IV"), (1, "I"),
        ):
            while value >= size:
                out += letters
                value -= size
        return out

    async def _resolve_scope(self, within: str) -> Dict[str, Any]:
        """The outline node `within` names, with what is needed to tell
        whether a search hit sits under it.

        Resolved from the table of contents only -- never by search -- so
        an ambiguous or unknown scope is an error rather than a guess.
        """
        hint = (
            " Write the title or chapter as it is cited, e.g. 'Title 9' or "
            "'Chapter 9.28', or pass the guid of a title, chapter or "
            "section from get_table_of_contents / browse_structure."
        )
        toc = await self._get_toc()
        if not isinstance(toc, dict) or toc.get("_fallback"):
            raise ToolInputError(
                "within cannot be used right now: the full outline of the "
                "code could not be loaded, so hits cannot be placed. "
                "Search without `within` and read each result's section "
                "number instead."
            )

        parsed = self._parse_ref(within)
        if parsed is None and within.isdigit() and len(within) <= 3:
            # "9" for Title 9. Guids are far longer, so this cannot hide one.
            parsed = (KIND_TITLE, within, False)
        display = ""
        named = self._named_scope(toc, within) if parsed is None else None
        if named is not None:
            guid, display = named
        elif parsed is not None:
            kind, number, charter_only = parsed
            matches = self._outline_matches(toc, kind, number, charter_only)
            if not matches:
                raise ToolInputError(
                    f"within {within!r} does not name a title, chapter or "
                    "section in the published code." + hint
                )
            if len(matches) > 1:
                listed = "; ".join(
                    f"{m['display']} (guid {m['guid']})" for m in matches
                )
                raise ToolInputError(
                    f"within {within!r} matches {len(matches)} different "
                    f"entries: {listed}. Pass one of those guids, or say "
                    f"which you mean -- e.g. 'Chapter {number}' or "
                    f"'Charter Section {number}'."
                )
            guid = matches[0]["guid"]
            display = matches[0]["display"]
        elif (
            GUID_RE.match(within)
            and within.upper() != "ROOT"
            # Every guid has digits. A bare word ("Zoning") is a name this
            # code has no part called, not a guid to go looking for.
            and any(c.isdigit() for c in within)
        ):
            guid = within
        else:
            raise ToolInputError(
                f"within {within!r} is not a title, chapter, section or "
                "guid." + hint + " The Charter is 'Charter' (one article: "
                "'Charter Article IV')."
            )

        in_scope: set = set()
        all_guids: set = set()
        by_number: Dict[str, set] = {}
        found: List[Dict[str, Any]] = []
        # Title index number ("ttl cha") -> that title's guid, and the
        # title the scope sits inside, for placing a title's attachments.
        titles: Dict[str, str] = {}
        inside_title: List[str] = []

        def walk(node: Any, inside: bool, title: str = "") -> None:
            if not isinstance(node, dict):
                return
            node_guid = str(node.get("guid") or "")
            is_title = (node.get("label") or "").strip().lower() == "title"
            if is_title and node_guid:
                index_num = " ".join(
                    (node.get("indexNum") or "").lower().split()
                )
                if index_num:
                    titles[index_num] = node_guid
            if node_guid == guid and not found:
                found.append(node)
                inside = True
                if title:
                    inside_title.append(title)
            if is_title and node_guid:
                title = node_guid
            if node_guid:
                all_guids.add(node_guid)
                if inside:
                    in_scope.add(node_guid)
                if node.get("type") == "section":
                    number = self._norm_number(node.get("number"))
                    if number:
                        by_number.setdefault(number, set()).add(node_guid)
            for child in node.get("children") or []:
                walk(child, inside, title)

        walk(toc, False)
        if not found:
            raise ToolInputError(
                f"within {within!r} is not in the outline of the published "
                "code (a definition or subsection cannot be a scope)." + hint
            )
        if found[0] is toc:
            raise ToolInputError(
                "within names the whole code; leave `within` out to search "
                "all of it."
            )
        if not display:
            node = found[0]
            display = " ".join(
                part
                for part in (
                    (node.get("label") or "").strip(),
                    (node.get("number") or "").strip(),
                    (node.get("title") or "").strip(),
                )
                if part
            ) or f"guid {guid}"
        return {
            "guid": guid,
            "display": display,
            "in_scope": in_scope,
            "all_guids": all_guids,
            "by_number": by_number,
            "titles": titles,
            "inside_title": inside_title[0] if inside_title else "",
        }

    def _hit_in_scope(
        self, hit: Dict[str, Any], scope: Dict[str, Any]
    ) -> Optional[bool]:
        """True / False when the hit can be placed, None when it cannot
        (a PDF document, or a node unknown to the outline).

        One kind of PDF can be placed: an attachment to a whole title,
        whose id starts with the title's index number ("Ttl CHA: ...").
        It belongs to that title and to nothing smaller, so it is in a
        scope that takes in the title, out of a scope elsewhere, and
        unplaced when the scope is a part of that same title."""
        if hit.get("pdf") or hit.get("type") == "doc":
            match = TITLE_ATTACHMENT_ID_RE.match(str(hit.get("id") or ""))
            if not match:
                return None
            title_guid = (scope.get("titles") or {}).get(
                " ".join(match.group(1).lower().split())
            )
            if not title_guid:
                return None
            if title_guid in scope["in_scope"]:
                return True
            if title_guid == scope.get("inside_title"):
                return None
            return False
        guid = str(hit.get("id") or self._guid_from_url(hit.get("url", "")))
        if guid in scope["all_guids"]:
            return guid in scope["in_scope"]
        # Not an outline node: a definition or subsection, numbered as an
        # extension of its section (9.04.010B{43} -> § 9.04.010). Take the
        # LONGEST section number that prefixes it, from the whole outline,
        # so a hit is never credited to a shorter look-alike in scope.
        number = self._norm_number(hit.get("number")).split("{")[0]
        while number:
            owners = scope["by_number"].get(number)
            if owners:
                return bool(owners & scope["in_scope"])
            number = number[:-1]
        return None

    async def _scoped_search(
        self, query: str, page: int, within: str
    ) -> tuple:
        """search_code limited to one title / chapter / section.

        `page` counts scan windows of SCOPE_SCAN_PAGES upstream pages, not
        single upstream pages. The query sent upstream is the caller's,
        unmodified, as everywhere else.
        """
        scope = await self._resolve_scope(within)
        hits, query_total, max_pages = await self._scan_hits(query, page)
        text, structured = self._format_scoped_search(
            query, page, scope, hits, query_total, max_pages
        )
        if page == 0 and query_total == 0:
            text, structured = await self._zero_result_followups(
                query, text, structured, scope=scope
            )
        return text, structured

    async def _scan_hits(self, query: str, page: int) -> tuple:
        """(hits, the query's total or None, upstream page count) for one
        scan window: SCOPE_SCAN_PAGES upstream pages starting at window
        `page`. A failed page fails the scan rather than hiding hits."""
        first_page = page * SCOPE_SCAN_PAGES
        first = await self._search(query, first_page)
        max_pages = self._as_int(first.get("maxPages", 1), default=1)
        raw_total = first.get("totalResultCount")
        query_total = None if raw_total is None else self._as_int(raw_total)

        gate = asyncio.Semaphore(BATCH_CONCURRENCY)

        async def one(upstream_page: int) -> Dict[str, Any]:
            async with gate:
                return await self._search(query, upstream_page)

        stop = min(max_pages, first_page + SCOPE_SCAN_PAGES)
        rest = await asyncio.gather(
            *(one(p) for p in range(first_page + 1, stop))
        )
        hits: List[Dict[str, Any]] = []
        for data in [first, *rest]:
            hits.extend(data.get("results") or [])
        return hits, query_total, max_pages

    def _format_scoped_search(
        self,
        query: str,
        page: int,
        scope: Dict[str, Any],
        hits: List[Dict[str, Any]],
        query_total: Optional[int],
        max_pages: int,
    ) -> tuple:
        display = scope["display"]
        kept: List[Dict[str, Any]] = []
        documents = 0
        document_names: List[str] = []
        unknown = 0
        for hit in hits:
            placed = self._hit_in_scope(hit, scope)
            if placed is None:
                if hit.get("pdf") or hit.get("type") == "doc":
                    documents += 1
                    name = self._strip_html(hit.get("title", ""))
                    if name and name not in document_names:
                        document_names.append(name)
                else:
                    unknown += 1
            elif placed:
                kept.append(hit)

        window = SCOPE_SCAN_PAGES * SEARCH_PAGE_SIZE
        first_hit = page * window + 1
        last_hit = page * window + len(hits)
        more_after = (page + 1) * SCOPE_SCAN_PAGES < max_pages
        # Only a scan of every hit for the query gives a complete count.
        complete = page == 0 and not more_after
        total_count: Optional[int] = len(kept) if complete else None
        of_total = (
            f" of {query_total}" if query_total is not None else ""
        )

        entries: List[tuple] = []
        if kept:
            checked = (
                f"all {len(hits)} hit(s) for the query were checked"
                if complete
                else f"hits {first_hit}-{last_hit}{of_total} were checked"
            )
            summary = (
                f"{len(kept)} result(s) for {query!r} within {display}; "
                f"{checked}."
            )
        elif query_total == 0:
            summary = f"No results for query {query!r}."
            entries.extend(self._no_match_caveats(query))
        elif not hits:
            summary = (
                f"No results on page {page + 1} for query {query!r} "
                f"within {display}."
            )
            last_window = max(0, -(-max_pages // SCOPE_SCAN_PAGES) - 1)
            entries.append(
                (
                    CAVEAT_PAGE_PAST_END,
                    f"Page {page + 1} is past the end: the query has "
                    f"{query_total if query_total is not None else 'fewer'} "
                    f"hit(s), and with `within` each page checks {window} "
                    f"of them. Re-request a page between 0 and "
                    f"{last_window}.",
                )
            )
        else:
            summary = f"No results for query {query!r} within {display}."
            entries.append(
                (
                    CAVEAT_NO_MATCH_IN_SCOPE,
                    f"None of the {len(hits)} hit(s) checked for this "
                    f"query is in {display}. The query does match "
                    "elsewhere in the code -- search again without "
                    "`within` to see where -- so this says only that "
                    f"{display} does not contain it"
                    + (
                        "."
                        if not more_after
                        else " among the hits checked so far."
                    ),
                )
            )

        if more_after and hits:
            entries.append(
                (
                    CAVEAT_SCOPE_SCAN_PARTIAL,
                    f"Only hits {first_hit}-{last_hit}{of_total} for this "
                    f"query were checked, so matches in {display} beyond "
                    "them are not shown and the count is not complete. "
                    "Call search_code again with the same query and "
                    f"within, and page={page + 1}, to check the next "
                    f"{window}; or narrow the query with AND so every hit "
                    "is checked in one call.",
                )
            )
        if documents or unknown:
            what = " and ".join(
                part
                for part in (
                    f"{documents} PDF document hit(s)"
                    + self._document_names_note(document_names)
                    if documents
                    else "",
                    f"{unknown} hit(s) with no place in the outline"
                    if unknown
                    else "",
                )
                if part
            )
            entries.append(
                (
                    CAVEAT_SCOPE_UNPLACED_HITS,
                    f"{what} cannot be assigned to a title or chapter and "
                    "were left out"
                    + (
                        " (a PDF attached to a whole title is shown only "
                        "when `within` takes in that title)"
                        if documents
                        else ""
                    )
                    + ". Search without `within` to see them.",
                )
            )
        title_caveat = self._title_number_caveat(query)
        if title_caveat:
            entries.append(title_caveat)
        proximity_caveat = self._proximity_caveat(query)
        if proximity_caveat:
            entries.append(proximity_caveat)

        lines = [summary, ""] if kept else [summary]
        hit_lines, rows = self._render_hits(kept, 1)
        lines.extend(hit_lines)
        distinct = len({row["guid"] for row in rows})
        if distinct and distinct != len(rows):
            entries.append(
                (
                    CAVEAT_DUPLICATE_SECTIONS,
                    f"{len(rows)} hits cover {distinct} distinct "
                    "section(s); count sections, not hits.",
                )
            )
        caveats, caveat_lines = self._caveats(entries)
        lines.extend(caveat_lines)
        return "\n".join(lines), {
            "query": query,
            "page": page,
            "total_count": total_count,
            "summary": summary,
            "rows": rows,
            "caveats": caveats,
            "within": {
                "guid": scope["guid"],
                "display": display,
                "hits_checked": len(hits),
                "query_total": query_total,
            },
        }

    @staticmethod
    def _document_names_note(names: List[str]) -> str:
        """' ("A", "B" and 2 more)' for the documents a scoped search left
        out, so the caller can tell whether one is worth a look."""
        if not names:
            return ""
        shown = ", ".join(
            '"' + (n if len(n) <= 80 else n[:77] + "...") + '"'
            for n in names[:UNPLACED_NAMES_SHOWN]
        )
        more = len(names) - UNPLACED_NAMES_SHOWN
        return f" ({shown}" + (f" and {more} more)" if more > 0 else ")")

    async def _resolve_section_number(self, ref: str) -> str:
        """Turn a section NUMBER into its guid via search.

        The API has no lookup by number, but a search for the number ranks
        the section itself first (verified live for 21.05.010). Only an
        EXACT match on the hit's own `number` is accepted -- a fuzzy pick
        would cite one section's number over another's text, which is worse
        than asking the caller to search.
        """
        data = await self._search(ref, 0)
        want = self._norm_ref(ref)
        guids: List[str] = []
        for hit in data.get("results", []) or []:
            if hit.get("pdf") or hit.get("type") == "doc":
                continue
            if self._norm_ref(hit.get("number")) != want:
                continue
            guid = str(hit.get("id") or self._guid_from_url(hit.get("url", "")))
            if guid and guid not in guids:
                guids.append(guid)

        if len(guids) == 1:
            return guids[0]
        if not guids:
            raise ToolInputError(
                f"No section numbered {ref!r} was found in the published "
                "code (that looks like a section NUMBER, and none matched it "
                "exactly). Use search_code to find the section and read its "
                "guid from the result."
            )
        raise ToolInputError(
            f"Section number {ref!r} matches {len(guids)} different nodes "
            f"(guids {', '.join(guids)}). Call get_section with one of those "
            "guids, or use search_code to tell them apart."
        )

    # ── HTTP helpers ────────────────────────────────────────────────────

    def _cust_path(self, suffix: str) -> str:
        """Path under /v1/customer/{customer_id}{suffix}."""
        cust = self.plugin_config.customer_id
        return f"/customer/{cust}{suffix}"

    def _request_timeout(self) -> Optional[float]:
        """Per-request timeout override, or None to use the client default.

        None whenever the call budget still covers a full plugin timeout, so
        the common path passes no extra argument at all.
        """
        deadline = _call_deadline.get()
        if deadline is None:
            return None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise self._budget_spent()
        if remaining < self.plugin_config.timeout:
            return remaining
        return None

    @staticmethod
    def _budget_spent() -> EcodeAPIError:
        return EcodeAPIError(
            f"eCode360 did not answer within this call's "
            f"{TOOL_CALL_BUDGET_S:g}s budget. The service may be slow "
            "right now; try again shortly.",
            504,
        )

    @staticmethod
    def _hard_limit() -> Optional[float]:
        """Seconds left in the call, as a wall-clock limit on one request.

        The httpx timeout is per phase (connect, each read, write, pool),
        not for the whole request: a server that keeps sending a few bytes
        inside every read window never trips it, so a slow trickle could
        run past the budget and into the Lambda timeout. Each request is
        therefore also wrapped in asyncio.timeout(this). None outside a
        tool call (no limit).
        """
        deadline = _call_deadline.get()
        if deadline is None:
            return None
        return max(deadline - time.monotonic(), 0.0)

    async def _get_json(
        self, path: str, params: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """GET + JSON, retrying transient failures inside the call budget.

        Retried: connection errors and upstream 5xx, while the call is
        younger than RETRY_ONLY_WITHIN_S. Never retried: timeouts (see
        TOOL_CALL_BUDGET_S) and 4xx. Every failure surfaces with a message
        that names the cause -- an httpx timeout stringifies to '', which is
        how the caller used to get a bare "Tool execution failed".
        """
        started = time.monotonic()
        attempt = 0
        while True:
            attempt += 1
            timeout = self._request_timeout()
            kwargs: Dict[str, Any] = {"params": params}
            if timeout is not None:
                kwargs["timeout"] = timeout
            try:
                async with asyncio.timeout(self._hard_limit()) as scope:
                    resp = await self.client.get(path, **kwargs)
                resp.raise_for_status()
            except TimeoutError as e:
                # httpx timeouts are not TimeoutError subclasses, so this
                # is the call deadline -- or a cancellation from outside,
                # which is passed on untouched.
                if not scope.expired():
                    raise
                raise self._budget_spent() from e
            except httpx.TimeoutException as e:
                seconds = timeout or self.plugin_config.timeout
                raise EcodeAPIError(
                    f"eCode360 did not respond within {seconds:.0f}s "
                    f"({type(e).__name__} on {path}). The service may be "
                    "slow right now; try again shortly.",
                    504,
                ) from e
            except httpx.TransportError as e:
                if self._may_retry(attempt, started):
                    await self._backoff(attempt, path, repr(e))
                    continue
                raise EcodeAPIError(
                    f"Could not reach eCode360 after {attempt} attempt(s): "
                    f"{type(e).__name__}{': ' + str(e) if str(e) else ''}",
                    503,
                ) from e
            except httpx.HTTPStatusError as e:
                status = e.response.status_code
                detail = self._extract_api_error(e.response)
                if status in CALLER_ERROR_STATUSES:
                    # The caller named something that is not in this code
                    # library. A caller mistake, so it must not log as a
                    # server fault.
                    raise EcodeNotFoundError(
                        f"eCode360 has no such entry (HTTP {status}): "
                        f"{detail}. Check the guid -- use search_code to look "
                        "the section up and read its guid from the result.",
                        status,
                    ) from e
                if status >= 500 and "above the chapter level" in detail:
                    # A deterministic refusal dressed as a 500 (seen for
                    # /code/content/ROOT and for a Part): retrying it three
                    # times only delays the same answer.
                    raise ToolInputError(
                        "eCode360 does not return text for this node: it is "
                        "above the chapter level (the whole code, or a part "
                        "of it). Call browse_structure with the same guid "
                        "to list what is under it, or get_table_of_contents "
                        "for the outline, then read a title, chapter or "
                        "section."
                    ) from e
                if status == 403 and detail.lstrip()[:15].lower().startswith(
                    ("<!doctype", "<html")
                ):
                    # An HTML 403 is the publisher's firewall refusing the
                    # REQUEST (seen for queries containing markup such as
                    # <script>); a credential failure answers in JSON.
                    raise ToolInputError(
                        "eCode360's firewall refused this request (HTTP "
                        "403). It blocks some characters and patterns, such "
                        "as HTML tags or code-like text. Rewrite the query "
                        "in plain words and try again."
                    ) from e
                if status >= 500 and self._may_retry(attempt, started):
                    await self._backoff(attempt, path, f"HTTP {status}")
                    continue
                if status in (401, 403):
                    # Our credentials, not the caller's request: either a
                    # bad or rotated key, or the publisher's gateway
                    # refusing a good one for a few minutes (it has). The
                    # caller cannot tell which and can fix neither, so it
                    # is told what to do; the detail goes to the log, under
                    # a fixed phrase an alarm counts (UPSTREAM_AUTH_LOG).
                    if attempt == 1 and self._may_retry(attempt, started):
                        await self._backoff(attempt, path, f"HTTP {status}")
                        continue
                    logger.error(
                        "%s: HTTP %s for %s after %d attempt(s): %s. If it "
                        "persists, check ECODE_API_KEY / ECODE_API_SECRET.",
                        UPSTREAM_AUTH_LOG,
                        status,
                        path,
                        attempt,
                        detail,
                    )
                    library = self.plugin_config.customer_id
                    raise EcodeAPIError(
                        "eCode360 is refusing this server's credentials "
                        f"right now (HTTP {status}). Nothing is wrong with "
                        "the request, and repeating it straight away will "
                        "not help. This has been a short fault at the "
                        "publisher before; if it lasts, the server's "
                        "operator has to fix the credentials. Tell the "
                        "user the code could not be read just now and to "
                        "try again in a few minutes. Do not answer from "
                        "memory instead; the code can be read at "
                        f"https://ecode360.com/{library}. (For the "
                        "operator: if this does not clear, check "
                        "ECODE_API_KEY / ECODE_API_SECRET; the server log "
                        "has eCode360's own answer.)",
                        status,
                    ) from e
                suffix = f" after {attempt} attempt(s)" if attempt > 1 else ""
                raise EcodeAPIError(
                    f"eCode API error (HTTP {status}) for {path}{suffix}: "
                    f"{detail}",
                    status,
                ) from e
            try:
                return resp.json()
            except ValueError as e:
                repaired = self._repair_search_json(resp.text)
                if repaired is not None:
                    logger.warning(
                        "eCode360 sent invalid JSON for %s; repaired by "
                        "dropping its unescaped `hits` objects",
                        path,
                    )
                    return repaired
                raise EcodeAPIError(
                    f"eCode360 returned a non-JSON response for {path} "
                    f"(content-type "
                    f"{resp.headers.get('content-type', '?')}).",
                    502,
                ) from e

    @staticmethod
    def _repair_search_json(body: str) -> Optional[Dict[str, Any]]:
        """Parse a search response whose `hits` objects are not valid JSON.

        Seen live on 2026-11-18: a phrase that matches across a quotation
        mark ('"agreement means"' against '"Labor agreement" means ...')
        comes back with the matched text as an UNESCAPED object key --
        `"hits" : { "agreement" means":1 }` -- so the whole response fails
        to parse and a search with real results was reported as an upstream
        fault. Nothing here reads `hits`, so those objects are emptied and
        the rest is parsed as usual. None if that does not make it valid.
        """
        fixed, count = BROKEN_HITS_RE.subn('"hits": {}', body or "")
        if not count:
            return None
        try:
            data = json.loads(fixed)
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    @staticmethod
    def _may_retry(attempt: int, started: float) -> bool:
        return (
            attempt < MAX_ATTEMPTS
            and time.monotonic() - started < RETRY_ONLY_WITHIN_S
        )

    @staticmethod
    async def _backoff(attempt: int, path: str, why: str) -> None:
        logger.warning(
            "transient eCode360 failure on %s (%s); retry %d",
            path,
            why,
            attempt,
        )
        await asyncio.sleep(RETRY_BACKOFF_S * attempt)

    def _static_lock(self, key: Tuple[str, str]) -> asyncio.Lock:
        """The lock for one cached document (see __init__)."""
        lock = self._static_locks.get(key)
        if lock is None:
            lock = self._static_locks[key] = asyncio.Lock()
        return lock

    async def _get_static_json(self, path: str) -> Any:
        """_get_json for data that only changes on a code republish.

        Successes are cached in the module-level _STATIC_CACHE (= per warm
        Lambda container, across plugin instances) for STATIC_CACHE_TTL_S;
        failures are never cached.
        """
        key = (self.plugin_config.base_url, path)
        hit = _STATIC_CACHE.get(key)
        if hit and hit[0] > time.monotonic():
            return hit[1]
        async with self._static_lock(key):
            # Re-check: a concurrent request in the same batch may have
            # filled it while this one waited.
            hit = _STATIC_CACHE.get(key)
            if hit and hit[0] > time.monotonic():
                return hit[1]
            data = await self._get_json(path)
            _STATIC_CACHE[key] = (
                time.monotonic() + STATIC_CACHE_TTL_S,
                data,
            )
            return data

    @staticmethod
    def _shared_cache_client() -> Optional[Tuple[Any, str]]:
        """(S3 client, bucket) when a shared cache is configured and boto3
        is importable, else None. Built once per container."""
        bucket = (os.environ.get(SHARED_CACHE_ENV) or "").strip()
        if not bucket:
            return None
        cached = _SHARED_CACHE_CLIENT.get(bucket)
        if cached is not None:
            return (cached, bucket) if cached else None
        try:
            # Imported here: boto3 costs a few hundred ms to import and
            # is only needed on a container's first ordinance lookup. It
            # is part of the Lambda runtime, not of requirements.txt.
            import boto3
            from botocore.config import Config

            client = boto3.client(
                "s3",
                config=Config(
                    connect_timeout=2,
                    read_timeout=3,
                    retries={"max_attempts": 1},
                ),
            )
        except Exception as e:  # noqa: BLE001 - never fail a tool call
            logger.warning("shared cache disabled: %s: %s", type(e).__name__, e)
            _SHARED_CACHE_CLIENT[bucket] = False
            return None
        _SHARED_CACHE_CLIENT[bucket] = client
        return client, bucket

    @staticmethod
    def _shared_cache_key(path: str) -> str:
        return f"v{SHARED_CACHE_VERSION}/{path.strip('/')}.json"

    async def _shared_cache_get(
        self, shared: Tuple[Any, str], path: str, code_date: str
    ) -> Optional[Dict[str, Any]]:
        """The stored response for `path`, if it is current. Never raises."""
        client, bucket = shared
        key = self._shared_cache_key(path)

        def read() -> bytes:
            body = client.get_object(Bucket=bucket, Key=key)["Body"]
            return body.read(SHARED_CACHE_MAX_BYTES + 1)

        try:
            raw = await asyncio.wait_for(
                asyncio.to_thread(read), SHARED_CACHE_TIMEOUT_S
            )
            if len(raw) > SHARED_CACHE_MAX_BYTES:
                raise _SharedCacheUnusable("object larger than expected")
            stored = json.loads(raw)
            age = time.time() - float(stored["fetched_at"])
            data = stored["data"]
            if (
                stored.get("version") != SHARED_CACHE_VERSION
                or stored.get("path") != path
                or not isinstance(data, dict)
                or not isinstance(data.get("content"), str)
            ):
                raise _SharedCacheUnusable("unexpected object shape")
        except Exception as e:  # noqa: BLE001 - never fail a tool call
            missing = type(e).__name__ == "NoSuchKey" or "NoSuchKey" in str(e)
            logger.log(
                logging.INFO if missing else logging.WARNING,
                "shared cache read of %s: %s: %s",
                key,
                type(e).__name__,
                str(e) or "timed out",
            )
            return None
        if stored.get("code_last_updated") != code_date:
            logger.info(
                "shared cache %s is for code dated %s, now %s; refetching",
                key,
                stored.get("code_last_updated"),
                code_date,
            )
            return None
        if not -300 <= age <= SHARED_CACHE_MAX_AGE_S:
            logger.info("shared cache %s is %.0f s old; refetching", key, age)
            return None
        logger.info("shared cache hit for %s (%.0f s old)", key, age)
        return data

    async def _shared_cache_put(
        self, shared: Tuple[Any, str], path: str, code_date: str, data: Any
    ) -> None:
        """Store a response for the other containers. Never raises."""
        client, bucket = shared
        key = self._shared_cache_key(path)
        try:
            body = json.dumps(
                {
                    "version": SHARED_CACHE_VERSION,
                    "path": path,
                    "code_last_updated": code_date,
                    "fetched_at": time.time(),
                    "data": data,
                },
                ensure_ascii=False,
            ).encode("utf-8")
            if len(body) > SHARED_CACHE_MAX_BYTES:
                raise _SharedCacheUnusable("response too large to share")
            await asyncio.wait_for(
                asyncio.to_thread(
                    client.put_object,
                    Bucket=bucket,
                    Key=key,
                    Body=body,
                    ContentType="application/json",
                ),
                SHARED_CACHE_TIMEOUT_S,
            )
            logger.info("shared cache stored %s (%d bytes)", key, len(body))
        except Exception as e:  # noqa: BLE001 - never fail a tool call
            logger.warning(
                "shared cache write of %s: %s: %s",
                key,
                type(e).__name__,
                str(e) or "timed out",
            )

    async def _get_shared_static_json(self, path: str) -> Any:
        """_get_static_json, with the shared cache between this
        container's memory and eCode360. See SHARED_CACHE_ENV."""
        key = (self.plugin_config.base_url, path)
        hit = _STATIC_CACHE.get(key)
        if hit and hit[0] > time.monotonic():
            return hit[1]
        shared = self._shared_cache_client()
        # Before the lock, so the two are never held together. Without a
        # date there is nothing to check a stored object against, so the
        # shared cache is left alone -- neither read nor written.
        code_date = await self._code_last_updated() if shared else None
        if not code_date:
            return await self._get_static_json(path)
        stored = False
        async with self._static_lock(key):
            hit = _STATIC_CACHE.get(key)
            if hit and hit[0] > time.monotonic():
                return hit[1]
            data = await self._shared_cache_get(shared, path, code_date)
            if data is None:
                data = await self._get_json(path)
                stored = True
            _STATIC_CACHE[key] = (
                time.monotonic() + STATIC_CACHE_TTL_S,
                data,
            )
        if stored:
            await self._shared_cache_put(shared, path, code_date, data)
        return data

    async def _get_recent_json(
        self, path: str, params: Optional[Dict[str, Any]] = None
    ) -> Any:
        """_get_json through the short-lived _RECENT_CACHE.

        Successes are kept for RECENT_CACHE_TTL_S; failures are never
        cached. The result may be shared with other calls, so callers
        must not mutate it.
        """
        key = (
            self.plugin_config.base_url,
            path,
            tuple(sorted((params or {}).items())),
        )
        now = time.monotonic()
        hit = _RECENT_CACHE.get(key)
        if hit and hit[0] > now:
            return hit[1]
        data = await self._get_json(path, params)
        chars = len(json.dumps(data, ensure_ascii=False))
        if chars > RECENT_CACHE_ENTRY_CHARS:
            return data
        # Drop what has expired, then the oldest until this entry fits.
        # Re-inserting moves a refreshed key to the end (newest).
        _RECENT_CACHE.pop(key, None)
        for old in [k for k, v in _RECENT_CACHE.items() if v[0] <= now]:
            del _RECENT_CACHE[old]
        total = sum(v[2] for v in _RECENT_CACHE.values())
        while _RECENT_CACHE and total + chars > RECENT_CACHE_TOTAL_CHARS:
            total -= _RECENT_CACHE.pop(next(iter(_RECENT_CACHE)))[2]
        _RECENT_CACHE[key] = (now + RECENT_CACHE_TTL_S, data, chars)
        return data

    async def _code_last_updated(self) -> Optional[str]:
        """The date the published code was last updated, or None.

        Read from the library record (cached for STATIC_CACHE_TTL_S, so
        this is an upstream call about once an hour per container). It
        decorates a section read and must never fail one: any upstream
        failure here is logged and reported as an unknown date.
        """
        try:
            data = await self._get_static_json(self._cust_path(""))
        except (EcodeAPIError, ToolInputError) as e:
            # ToolInputError too: a 404 surfaces as EcodeNotFoundError,
            # which is a caller-mistake type, and the caller made none.
            logger.warning("could not read the code's updated date: %s", e)
            return None
        results = data.get("results", data) if isinstance(data, dict) else {}
        if not isinstance(results, dict):
            return None
        value = results.get("updatedDate")
        return str(value) if value not in (None, "") else None

    @staticmethod
    def _with_code_date(
        text: str, structured: Dict[str, Any], updated: Optional[str]
    ) -> tuple:
        """Add the code's publication date to a section result."""
        structured = {**structured, "code_last_updated": updated}
        if updated:
            text = (
                f"{text}\n\nPublished code last updated {updated} (the "
                "whole code, not this section; ordinances adopted since "
                "may not be reflected)."
            )
        return text, structured

    async def _search(self, query: str, page: int) -> Dict[str, Any]:
        return await self._get_recent_json(
            self._cust_path("/search"), params={"query": query, "page": page}
        )

    async def _get_toc(self) -> Dict[str, Any]:
        """TOC endpoint, falling back to the structure ROOT if /code/toc is
        not available for this customer (the endpoint is absent from the older
        OpenAPI spec, so treat 404 as 'use structure instead')."""
        try:
            return await self._get_static_json(self._cust_path("/code/toc"))
        except (EcodeAPIError, EcodeNotFoundError) as e:
            # A 404 here means the endpoint is absent for this customer, not
            # that the caller asked for the wrong thing -- so it is handled,
            # never surfaced. Branch on the status code rather than string-
            # matching the message, which is what this used to do.
            if e.status_code == 404:
                logger.info("toc endpoint 404; falling back to structure ROOT")
                data = dict(
                    await self._get_static_json(
                        self._cust_path("/structure/ROOT")
                    )
                )
                data["_fallback"] = "structure/ROOT (toc endpoint unavailable)"
                return data
            raise

    @staticmethod
    def _extract_api_error(resp: httpx.Response) -> str:
        try:
            body = resp.json()
            msg = body.get("message") or body.get("exception")
            if msg:
                return str(msg)
        except Exception:
            pass
        return resp.text[:500]

    @staticmethod
    def _resolve_credentials(cfg: EcodePluginConfig) -> tuple:
        """Read api-key/api-secret from the environment, loading a local
        secrets file first if one is configured and the vars aren't set."""
        if cfg.secrets_file and (
            cfg.api_key_env not in os.environ or cfg.api_secret_env not in os.environ
        ):
            _load_env_file(cfg.secrets_file)
        return (
            (os.environ.get(cfg.api_key_env) or "").strip(),
            (os.environ.get(cfg.api_secret_env) or "").strip(),
        )

    # ── Result helpers ──────────────────────────────────────────────────

    @staticmethod
    def _ok(text: str, structured: Optional[Any] = None) -> ToolResult:
        return ToolResult(
            content=[{"type": "text", "text": text}],
            structured_content=structured,
            success=True,
        )

    @staticmethod
    def _caveats(entries: List[tuple]) -> tuple:
        """Build the structured array and the rendered lines from ONE list.

        Generating them separately is how a qualification ends up in the
        prose but not the machine-readable half, or the reverse. Tests
        assert every structured message appears in the text.
        """
        structured = [{"code": code, "message": msg} for code, msg in entries]
        lines = [f"Note: {msg}" for _code, msg in entries]
        return structured, lines

    @staticmethod
    def _err(message: str) -> ToolResult:
        return ToolResult(content=[], success=False, error_message=message)

    # ── Formatters ──────────────────────────────────────────────────────

    def _empty_search_result(
        self,
        query: str,
        page: int,
        total_count: Optional[int] = 0,
        max_pages: int = 0,
    ) -> tuple:
        """Zero rows: text plus a CONFORMING structured object.

        Mirrors _format_search deliberately. A branch that short-circuits
        with a plain string before the formatter would advertise an
        outputSchema and then return no structuredContent -- a conformance
        break invisible to happy-path tests, and a code search matching
        nothing is common here.

        `total_count` is passed IN rather than assumed to be 0. Zero rows has
        two very different causes and reporting both as a complete count of
        zero is a lie in one of them:

          * the search ran and matched nothing -> upstream reports 0, and 0
            is the honest, complete count;
          * the caller asked for a page past the end -> upstream still
            reports the real total (618 for "dog license" at page 999), and
            claiming 0 would say "no such law exists" about a query with 618
            matching sections.
        """
        entries: List[tuple] = []
        if total_count and max_pages:
            summary = (
                f"No results on page {page + 1} for query {query!r}; "
                f"{total_count} match(es) exist across {max_pages} page(s)."
            )
            entries.append(
                (
                    CAVEAT_PAGE_PAST_END,
                    f"Page {page + 1} is past the last page ({max_pages}). "
                    f"{total_count} section(s) still match {query!r} -- "
                    f"re-request a page between 0 and {max_pages - 1}.",
                )
            )
        else:
            summary = f"No results for query {query!r}."
            if total_count == 0:
                # Zero is a complete count -- the search ran and matched
                # nothing. Say what that does and does not mean; models (and
                # their users) otherwise read it as "no such law exists".
                entries.extend(self._no_match_caveats(query))
        title_caveat = self._title_number_caveat(query)
        if title_caveat:
            entries.append(title_caveat)
        proximity_caveat = self._proximity_caveat(query)
        if proximity_caveat:
            entries.append(proximity_caveat)

        caveats, caveat_lines = self._caveats(entries)
        return "\n".join([summary] + caveat_lines), {
            "query": query,
            "page": page,
            "total_count": total_count,
            "summary": summary,
            "rows": [],
            "caveats": caveats,
        }

    def _render_hits(
        self, results: List[Any], first_index: int
    ) -> Tuple[List[str], List[Dict[str, Any]]]:
        """(rendered lines, structured rows) for search hits, numbered
        from `first_index`. Shared by plain and scoped search so a hit
        reads the same in both."""
        lines: List[str] = []
        rows: List[Dict[str, Any]] = []
        for i, r in enumerate(results, first_index):
            # Search results key the node id as `id`; the snippet as `text`;
            # and both title and snippet may contain <em> highlight markup.
            # guid and ref are taken as the API returned them and never
            # re-derived from rendered text -- a citation that does not match
            # the section it came from is worse than no citation.
            guid = r.get("id") or self._guid_from_url(r.get("url", ""))
            snippet = self._strip_html(r.get("text") or r.get("snippet", ""))
            title = self._strip_html(r.get("title", "")) or "Untitled"
            ref = (r.get("number") or "").strip() or None
            is_pdf = bool(r.get("pdf"))
            url = self._result_url(r, guid)
            heading = f"{ref or ''} {title}".strip()
            lines.append(f"{i}. {heading}")
            lines.append(f"   guid: {guid or '(unknown)'}   type: {r.get('type', '')}")
            if is_pdf and r.get("type") == "law":
                lines.append(
                    "   (Ordinance PDF — an ordinance not yet worked into "
                    "the code text, not the code itself; pass the link "
                    "below to read_attachment)"
                )
            elif is_pdf:
                lines.append(
                    "   (PDF document — not code text. The excerpt may be "
                    "the start of the document, not where the words "
                    "matched: call read_attachment with the link below and "
                    "find=<a word or phrase> to list the pages it is on, "
                    "then read those pages)"
                )
            if snippet:
                lines.append(f"   …{snippet}…")
            if url:
                lines.append(f"   {url}")
            lines.append("")

            if guid:
                rows.append(
                    {
                        "guid": guid,
                        "ref": ref,
                        # Unclipped in the structured channel even though the
                        # markdown elides it -- see SECTION_OUTPUT_SCHEMA.
                        "title": title,
                        "snippet": snippet or None,
                        "url": url,
                        # The prose says "(PDF document)"; the structured half
                        # must say the same thing, or a caller acting on rows
                        # alone will send this guid to get_section and get an
                        # attachment instead of text.
                        "is_pdf": is_pdf,
                    }
                )
        return lines, rows

    def _format_search(
        self, data: Dict[str, Any], query: str = "", page: int = 0
    ) -> tuple:
        results = data.get("results", []) or []
        query = data.get("query") or query
        page = self._as_int(data.get("page", page))
        max_pages = self._as_int(data.get("maxPages", 1), default=1)

        # `totalResultCount` absent is NOT the same as zero. Only a search
        # that ran and matched nothing is a known count of 0.
        raw_total = data.get("totalResultCount")
        if raw_total is not None:
            total_count = self._as_int(raw_total)
        else:
            total_count = 0 if not results else None

        if not results:
            return self._empty_search_result(
                query, page, total_count, max_pages
            )

        lines = [
            f"{total_count if total_count is not None else len(results)} "
            f"result(s) for {query!r} "
            f"(page {page + 1} of {max_pages}, 20/page):",
            "",
        ]
        hit_lines, rows = self._render_hits(results, 1 + page * 20)
        lines.extend(hit_lines)

        entries: List[tuple] = []
        if page + 1 < max_pages:
            remaining = (
                f"{total_count - len(results) * (page + 1)} more"
                if total_count is not None
                else "more"
            )
            entries.append(
                (
                    CAVEAT_MORE_PAGES,
                    f"This is page {page + 1} of {max_pages}; {remaining} "
                    f"matching section(s) are not shown. Call search_code "
                    f"with page={page + 1} to continue.",
                )
            )

        # One section can match a query more than once. Report the distinct
        # section count whenever it differs from the hit count, so "how many
        # sections mention X" is not silently answered with a hit total.
        distinct = len({row["guid"] for row in rows})
        if distinct and distinct != len(rows):
            entries.append(
                (
                    CAVEAT_DUPLICATE_SECTIONS,
                    f"{len(rows)} hits on this page cover {distinct} distinct "
                    f"section(s); count sections, not hits.",
                )
            )

        or_caveat = self._or_flood_caveat(query, total_count)
        if or_caveat:
            entries.append(or_caveat)
        title_caveat = self._title_number_caveat(query)
        if title_caveat:
            entries.append(title_caveat)
        proximity_caveat = self._proximity_caveat(query)
        if proximity_caveat:
            entries.append(proximity_caveat)

        caveats, caveat_lines = self._caveats(entries)
        lines.extend(caveat_lines)

        summary = (
            f"{total_count if total_count is not None else len(rows)} result(s) "
            f"for {query!r}; showing {len(rows)} on page {page + 1} of {max_pages}."
        )
        return "\n".join(lines), {
            "query": query,
            "page": page,
            "total_count": total_count,
            "summary": summary,
            "rows": rows,
            "caveats": caveats,
        }

    @staticmethod
    def _query_terms(query: str) -> Tuple[List[str], bool]:
        """(the query's search terms, whether it uses AND / + / NOT).

        A term is a quoted phrase or a bare word; parentheses and OR are
        structure, not terms. Any narrowing operator means the caller
        already chose how terms combine.
        """
        terms: List[str] = []
        narrowed = False
        for tok in _QUERY_TOKEN_RE.findall(query or ""):
            if tok in ("(", ")"):
                continue
            low = tok.lower()
            if low in _QUERY_OPERATORS:
                narrowed = narrowed or low in _NARROWING_OPERATORS
                continue
            if tok.startswith(("+", "-")) and len(tok) > 1:
                narrowed = True
            terms.append(tok)
        return terms, narrowed

    def _or_flood_caveat(
        self, query: str, total_count: Optional[int]
    ) -> Optional[tuple]:
        if total_count is None or total_count < OR_CAVEAT_MIN_TOTAL:
            return None
        terms, narrowed = self._query_terms(query)
        if narrowed or len(terms) < OR_CAVEAT_MIN_TERMS:
            return None
        # A suggestion in the message only -- the query that ran is the
        # caller's, unmodified. Only the first few terms: ANDing every term
        # of a long query usually returns nothing, while its first two
        # usually return a short list with the governing section near
        # the top.
        suggestion = " AND ".join(terms[:OR_CAVEAT_SUGGEST_TERMS])
        more = len(terms) > OR_CAVEAT_SUGGEST_TERMS
        return (
            CAVEAT_TERMS_OR_COMBINED,
            f"Terms in this query are matched with OR, so {total_count} "
            "sections matched. To require terms, join them with AND, e.g. "
            f"{suggestion}"
            + (
                ", then add more terms with AND to narrow further -- "
                "requiring every term at once often returns nothing."
                if more
                else "."
            )
            + " This is guidance, not an error.",
        )

    @staticmethod
    def _title_number_caveat(query: str) -> Optional[tuple]:
        if not TITLE_NUMBER_RE.search(query or ""):
            return None
        return (
            CAVEAT_TITLE_FIELD_IS_HEADING,
            "title: matches words in a section's HEADING; it does not "
            "limit a search to a numbered Title of the code. Run the query "
            "again without the title: term and pass the `within` argument "
            "instead, e.g. within='Title 9' or within='Chapter 9.28'.",
        )

    def _no_match_caveats(self, query: str) -> List[tuple]:
        cfg = self.plugin_config
        city = cfg.city_name if cfg else "the municipality"
        library = cfg.customer_id if cfg else "?"
        entries: List[tuple] = [
            (
                CAVEAT_NO_MATCH_IN_CODE,
                "No section of the published municipal code of the "
                f"{city} (eCode360 library {library}) matched this query "
                "as written. That is not proof the text is absent: a "
                "quoted phrase must match word for word, and punctuation "
                "inside it -- such as the quotation marks around a defined "
                "term ('\"Labor agreement\" means ...') -- can prevent a "
                "match. It can also be a real answer: the rule may be "
                "state law (Alaska Statutes / Alaska Administrative Code), "
                "in a repealed chapter, or worded differently here. Next "
                "steps: try fewer or broader words, put OR between "
                "synonyms, drop the quotes, use find_definition for a "
                "defined term, or use browse_structure / "
                "get_table_of_contents to look through the relevant title.",
            )
        ]
        if STATE_LAW_RE.search(query or ""):
            lowered = (query or "").lower()
            cite = next(
                (c for name, c in STATE_LAW_CITATIONS.items() if name in lowered),
                None,
            )
            how = (
                f'search for its citation, e.g. "{cite}"'
                if cite
                else 'search for the statute citation, e.g. "AS 23.40"'
            )
            entries.append(
                (
                    CAVEAT_STATE_LAW_NAMED,
                    "This query names Alaska state law. The Alaska Statutes "
                    "are not part of the municipal code, so their text is "
                    "not searchable here. To find municipal sections that "
                    f"refer to the statute, {how}.",
                )
            )
        wildcard = self._quoted_wildcard_caveat(query)
        if wildcard:
            entries.append(wildcard)
        return entries

    async def _zero_result_followups(
        self,
        query: str,
        text: str,
        structured: Dict[str, Any],
        scope: Optional[Dict[str, Any]] = None,
    ) -> tuple:
        """For a query that is ONE quoted phrase and matched nothing: look
        the term up as a definition when the phrase reads like one, and
        retry with the phrase's words required in any order.

        Additions only. `rows` and `total_count` stay empty and 0 -- the
        phrase did not match -- and a failed follow-up is dropped, never
        allowed to sink the result the caller already has.

        With `scope` (a search limited by `within`) the relaxed retry keeps
        only hits inside the scope, checking one scan window of them. The
        definition lookup is not scoped: a definition that governs a
        chapter often sits in its title's general definitions section.
        """
        match = SINGLE_PHRASE_RE.match(query or "")
        if not match or "*" in match.group(1):
            return text, structured
        phrase = " ".join(match.group(1).split())
        definition = DEFINITION_QUERY_RE.match(phrase)
        term = (
            " ".join(_QUOTE_CHARS_RE.sub("", definition.group(1)).split())
            if definition
            else ""
        )
        words = [w for w in (re.sub(r"[^\w.$%/-]", "", tok) for tok in phrase.split()) if w]
        relaxed_query = (
            " AND ".join(f'"{w}"' for w in words)
            if RELAXED_MIN_WORDS <= len(words) <= RELAXED_MAX_WORDS
            else ""
        )

        reordered: Set[str] = set()

        async def definitions() -> Optional[List[Dict[str, Any]]]:
            if not term or len(term) > MAX_TERM_CHARS:
                return None
            data = await self._search(f'title:"{term}"', 0)
            hits = self._definition_hits(data, term)[:RELATED_DEFINITIONS]
            bodies = await self._fetch_definition_bodies(hits, reordered)
            return [
                {
                    "guid": hit["guid"],
                    "term": hit["term"],
                    "exact_match": hit["exact_match"],
                    "defined_in": hit["defined_in"],
                    "text": body or hit["snippet"],
                    "url": PUBLIC_URL.format(guid=hit["guid"]),
                }
                for hit, body in zip(hits, bodies)
            ]

        async def relaxed() -> Optional[Dict[str, Any]]:
            if not relaxed_query:
                return None
            if scope is None:
                return await self._search(relaxed_query, 0)
            hits, _total, max_pages = await self._scan_hits(relaxed_query, 0)
            kept = [h for h in hits if self._hit_in_scope(h, scope)]
            return {
                "results": kept,
                # Complete only when every hit for the query was checked.
                "totalResultCount": (
                    len(kept) if max_pages <= SCOPE_SCAN_PAGES else None
                ),
            }

        found, loose = await asyncio.gather(
            definitions(), relaxed(), return_exceptions=True
        )
        entries: List[tuple] = []
        extra: List[str] = []
        structured = dict(structured)

        if isinstance(found, BaseException):
            logger.warning("definition follow-up failed for %r: %s", query, found)
        elif found:
            structured["related_definitions"] = found
            entries.append(
                (
                    CAVEAT_DEFINITION_LOOKUP,
                    f"This query reads like a definition lookup, so the "
                    f"term {term!r} was looked up as find_definition does: "
                    f"{len(found)} definition(s) are listed below under "
                    "DEFINITIONS. A definition is its own entry in the "
                    "code -- the term is its heading and the text begins "
                    "'means ...' -- which is why the phrase did not match. "
                    "A term can be defined differently in several titles: "
                    f"call find_definition({term!r}) for all of them and "
                    "use the one for the area of law in question."
                    + (
                        " This lookup covers the whole code, not only "
                        f"{scope['display']}."
                        if scope
                        else ""
                    )
                    + (
                        " A definition below contains a list whose "
                        "position this server inferred; check the order at "
                        "its link before relying on it."
                        if reordered
                        else ""
                    ),
                )
            )
            extra += ["", "DEFINITIONS (from a definition lookup, not from the search):"]
            for item in found:
                body = item["text"] or ""
                if len(body) > RELATED_DEFINITION_CHARS:
                    body = body[:RELATED_DEFINITION_CHARS] + " …"
                extra += [
                    f"- {item['term'] or '(untitled)'}"
                    + (f" -- defined in § {item['defined_in']}" if item["defined_in"] else "")
                    + ("" if item["exact_match"] else " (related term, not an exact match)"),
                    f"  guid: {item['guid']}   {item['url']}",
                    f"  {body}",
                ]
        elif term:
            entries.append(
                (
                    CAVEAT_DEFINITION_LOOKUP,
                    f"This query reads like a definition lookup, but no "
                    f"definition headed {term!r} was found either. Try "
                    "find_definition with the singular or a shorter form "
                    "of the term.",
                )
            )

        if isinstance(loose, BaseException):
            logger.warning("relaxed follow-up failed for %r: %s", query, loose)
        elif loose and loose.get("results"):
            hit_lines, rows = self._render_hits(
                (loose.get("results") or [])[:RELAXED_MAX_ROWS], 1
            )
            raw_total = loose.get("totalResultCount")
            total = self._as_int(raw_total) if raw_total is not None else None
            where = f" within {scope['display']}" if scope else ""
            if total is not None:
                matched = f"{total} section(s) matched{where}"
            elif scope:
                matched = (
                    f"{len(loose['results'])} section(s){where} matched "
                    f"among the first {SCOPE_SCAN_PAGES * SEARCH_PAGE_SIZE} "
                    "hits checked"
                )
            else:
                matched = f"{len(rows)} section(s) matched"
            structured["relaxed_match"] = {
                "query": relaxed_query,
                "total_count": total,
                "rows": rows,
            }
            entries.append(
                (
                    CAVEAT_RELAXED_MATCH,
                    "The phrase matched nothing, so the same words were "
                    "searched again without requiring them to be adjacent "
                    f"or in order ({relaxed_query}): "
                    f"{matched}, the first {len(rows)} listed below under "
                    "RELAXED MATCHES. These are NOT matches for the phrase: "
                    "read a section with get_section before saying it "
                    "contains the wording.",
                )
            )
            extra += ["", "RELAXED MATCHES (same words, any order -- not the phrase):", ""]
            extra += hit_lines

        if not entries:
            return text, structured
        caveats, caveat_lines = self._caveats(entries)
        structured["caveats"] = list(structured.get("caveats") or []) + caveats
        return "\n".join([text] + caveat_lines + extra), structured

    @staticmethod
    def _reference_arg(args: Dict[str, Any]) -> Tuple[str, str]:
        """(the number to look for, its three-part section number)."""
        raw = args.get("section")
        text = str(raw).strip() if raw is not None else ""
        if not text:
            raise ToolInputError(
                "section is required -- pass a section number such as "
                "'9.14.040'."
            )
        if len(text) > MAX_REFERENCE_CHARS:
            raise ToolInputError(
                f"section is too long (at most {MAX_REFERENCE_CHARS} "
                "characters). Pass one section number such as '9.14.040'."
            )
        number = REFERENCE_PREFIX_RE.sub("", text).strip().rstrip(".")
        match = REFERENCE_NUMBER_RE.match(number)
        if not match:
            raise ToolInputError(
                f"{text!r} is not a section number. Pass a full section "
                "number such as '9.14.040' or '9.28.019B'. A whole chapter "
                "or title is cited too widely to list: use search_code for "
                "that."
            )
        # The code writes a subsection as "9.28.019B.2". People, and
        # other codes, write "9.28.019(B)(2)" -- and in a search query
        # parentheses are GROUPING, so "9.28.019(B)*" was read as
        # "9.28.019 OR B*": 8,289 sections for a number cited by 3. So a
        # suffix is rewritten to the code's own form before it is searched
        # for, and one that cannot be is refused, never sent as it is.
        base, suffix = match.group(1), match.group(2)
        levels = [
            part
            for part in re.split(r"[().]+", suffix)
            if part
        ]
        subsection = ".".join(levels)
        balanced = suffix.count("(") == suffix.count(")") and "()" not in suffix
        if subsection and not (
            balanced and REFERENCE_SUBSECTION_RE.match(subsection)
        ):
            raise ToolInputError(
                f"{text!r} is not a section number this tool can search "
                "for. Write a subsection as the code does, e.g. "
                "'9.28.019B.2', or as '9.28.019(B)(2)'."
            )
        if suffix and not subsection:
            raise ToolInputError(
                f"{text!r} is not a section number. Pass a full section "
                "number such as '9.14.040' or '9.28.019B'."
            )
        return base + subsection, base

    @staticmethod
    def _reference_kind(
        ref: Optional[str], title: Optional[str], is_pdf: bool
    ) -> str:
        if is_pdf:
            return "document"
        number = REFERENCE_PREFIX_RE.sub("", (ref or "").strip())
        if REFERENCE_TABLE_REF_RE.match(number):
            return "reference_table"
        # Before the table-title test: § 8.05.025 is headed "Minor offense
        # fine schedule; misdemeanor reference table" and is law, not an
        # editorial table.
        if FINE_OR_PENALTY_TITLE_RE.search(title or ""):
            return "fine_or_penalty"
        if REFERENCE_TABLE_TITLE_RE.search(title or ""):
            return "reference_table"
        return "section"

    def _format_references(
        self, number: str, base: str, data: Dict[str, Any], page: int
    ) -> tuple:
        results = data.get("results", []) or []
        # The upstream reports maxPages 0 for an empty result; "page 1 of 0"
        # reads as a fault.
        max_pages = max(1, self._as_int(data.get("maxPages", 1), default=1))
        raw_total = data.get("totalResultCount")
        if raw_total is not None:
            total_count = self._as_int(raw_total)
        else:
            total_count = 0 if not results else None

        def kind_of(r: Dict[str, Any]) -> str:
            return self._reference_kind(
                r.get("number"),
                self._strip_html(r.get("title", "")),
                bool(r.get("pdf")),
            )

        # The section's own node (and its subsection nodes) mention the
        # number too; they are not references TO it.
        def is_self(r: Dict[str, Any]) -> bool:
            own = REFERENCE_PREFIX_RE.sub("", (r.get("number") or "").strip())
            return own.lower().startswith(base.lower())

        citing = [r for r in results if isinstance(r, dict) and not is_self(r)]
        citing.sort(key=lambda r: REFERENCE_KIND_ORDER.index(kind_of(r)))
        hit_lines, rows = self._render_hits(citing, 1 + page * 20)
        for row in rows:
            row["kind"] = self._reference_kind(
                row.get("ref"), row.get("title"), row["is_pdf"]
            )

        entries: List[tuple] = []
        if not rows:
            entries.append(
                (
                    CAVEAT_NO_REFERENCES_FOUND,
                    f"No other part of the published code cites {number}"
                    + (" on this page of results" if page else "")
                    + ". That does not mean it carries no penalty: a "
                    "general penalty provision of its title or chapter may "
                    "cover it without naming it. Check the number, or look "
                    "for a penalties section with search_code "
                    "(title:penalties) or browse_structure.",
                )
            )
        if page + 1 < max_pages:
            entries.append(
                (
                    CAVEAT_MORE_REFERENCES,
                    f"This is page {page + 1} of {max_pages}. Call "
                    f"find_references with page={page + 1} for more.",
                )
            )
        caveats, caveat_lines = self._caveats(entries)

        fines = sum(1 for row in rows if row["kind"] == "fine_or_penalty")
        summary = (
            f"{len(rows)} place(s) citing {number} on page {page + 1} of "
            f"{max_pages}"
            + (
                f", {fines} of them a fine schedule or penalty section"
                if fines
                else ""
            )
            + "."
        )
        lines = [summary]
        if rows:
            lines += [
                "Fine schedules and penalty sections first, editorial "
                "reference tables last. Snippets are excerpts: read the "
                "section with get_section before quoting it.",
                "",
            ] + hit_lines
        lines.extend(caveat_lines)
        return "\n".join(lines), {
            "section": number,
            "query": f"{number}*",
            "page": page,
            "total_count": total_count,
            "references": rows,
            "summary": summary,
            "caveats": caveats,
        }

    @staticmethod
    def _proximity_caveat(query: str) -> Optional[tuple]:
        """Attached with or without hits: the search ran, but not as the
        caller meant it, and nothing else in the result would say so."""
        found = PROXIMITY_RE.search(query or "")
        if not found:
            return None
        # The words worth requiring; "of" and "the" match everything.
        words = [
            w
            for w in found.group(1).split()
            if w.lower() not in PROXIMITY_STOP_WORDS
        ]
        joined = " AND ".join(f'"{w}"' for w in words[:4])
        return (
            CAVEAT_PROXIMITY_IGNORED,
            f"The ~N after {found.group(0).rsplit('~', 1)[0]} was ignored: "
            "this search has no proximity operator, so the phrase was "
            "matched exactly, word for word. Sections that use those words "
            "near each other in another order or with words between them "
            "are NOT in this result."
            + (
                f" To find them, search for the words with AND ({joined}) "
                "and read the hits."
                if len(words) > 1
                else ""
            ),
        )

    @staticmethod
    def _quoted_wildcard_caveat(query: str) -> Optional[tuple]:
        """Only attached to an empty result: with hits the quoted form is
        merely inconsistent, and the caller has something to read."""
        quoted = QUOTED_TERM_RE.findall(query or "")
        if not any("*" in term for term in quoted):
            return None
        # A suggestion in the message only; the query that ran is unchanged.
        suggestion = QUOTED_SINGLE_WILDCARD_RE.sub(r"\1", query)
        example = (
            f" Try: {suggestion}"
            if suggestion != query
            else " Use the wildcard on a single unquoted word instead."
        )
        return (
            CAVEAT_WILDCARD_IN_QUOTES,
            "This query has a * wildcard inside double quotes. A quoted "
            "wildcard often matches nothing, especially combined with AND. "
            "Write wildcard terms without quotes (indemnif*, not "
            f'"indemnif*") and keep quotes for exact phrases.{example}',
        )

    @staticmethod
    def _as_int(value: Any, default: int = 0) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def _format_toc(self, data: Dict[str, Any]) -> tuple:
        header = []
        entries: List[tuple] = []
        if data.get("_fallback"):
            entries.append(
                (
                    CAVEAT_TOC_ENDPOINT_UNAVAILABLE,
                    f"The table-of-contents endpoint is unavailable for this "
                    f"library; this outline came from {data['_fallback']}.",
                )
            )
        title = data.get("title", "Table of Contents")
        header.append(f"Table of Contents — {title}")
        guid = data.get("guid") or data.get("id") or "ROOT"
        header.append(f"root guid: {guid}")
        header.append("")

        raw_children = data.get("children", []) or []
        counts: List[int] = [0] * TOC_DEPTH
        body, children = self._outline(raw_children, 0, counts)
        if not children:
            entries.append(
                (
                    CAVEAT_NODE_HAS_NO_CHILDREN,
                    "The outline came back empty; use browse_structure "
                    "with guid='ROOT' to walk the tree directly.",
                )
            )
        elif not any(counts[1:]):
            # The structure/ROOT fallback, or a library the API gives no
            # nested outline for: say the tree is shallow rather than let
            # one level pass for the whole outline.
            entries.append(
                (
                    CAVEAT_OUTLINE_DEPTH_LIMITED,
                    "Only the top level of the outline was available. Call "
                    "browse_structure with an entry's guid to list what is "
                    "under it.",
                )
            )

        caveats, caveat_lines = self._caveats(entries)
        text = "\n".join(
            caveat_lines
            + header
            + (body if body else ["(no entries)"])
        )
        summary = (
            f"{counts[0]} top-level entr(y/ies) under {title}"
            + (
                f", with {counts[1]} at the second level and {counts[2]} "
                "at the third (parts, titles, chapters). Sections are not "
                "listed: call browse_structure with a chapter's guid."
                if any(counts[1:])
                else "."
            )
        )
        text = text.replace(
            f"root guid: {guid}",
            f"root guid: {guid}\n{summary}",
            1,
        )
        return text, {
            "node": {
                "guid": str(guid),
                "ref": (data.get("number") or "").strip() or None,
                "title": title,
                "label": (data.get("label") or "").strip() or None,
                "type": data.get("type") or None,
            },
            "children": children,
            "summary": summary,
            "caveats": caveats,
        }

    @staticmethod
    def _part_lines(parts: List[Dict[str, Any]]) -> List[str]:
        lines = [
            "part: "
            + " ".join(p for p in (part["ref"], part["title"] or "") if p)
            + f"  [guid: {part['guid']}]"
            for part in parts[:PARTS_SHOWN]
        ]
        if len(parts) > PARTS_SHOWN:
            lines.append(
                f"(and {len(parts) - PARTS_SHOWN} more part(s): "
                "browse_structure on the chapter lists them all)"
            )
        return lines

    def _format_structure(
        self,
        data: Dict[str, Any],
        parts: Optional[List[Dict[str, Any]]] = None,
    ) -> tuple:
        parts = [dict(part) for part in parts or []]
        guid = str(data.get("guid") or data.get("id", "") or "ROOT")
        url = PUBLIC_URL.format(guid=guid) if guid and guid != "ROOT" else None
        lines = [
            " ".join(
                part
                for part in (
                    self._kind_word(data.get("label"), data.get("number")),
                    data.get("number") or "",
                    data.get("title") or "",
                )
                if part.strip()
            ),
            f"guid: {guid}   type: {data.get('type', '')}",
            f"parent: {data.get('parent') or '(none)'}",
            f"link: {url}" if url else "",
            "",
            "Children:",
        ]
        raw_children = data.get("children", []) or []
        body = self._render_children(raw_children, depth=0)
        children = self._structured_children(raw_children)

        entries: List[tuple] = []
        if not children and parts:
            entries.append(
                (
                    CAVEAT_SECTION_TEXT_IN_PARTS,
                    "This section has no children, but the outline numbers "
                    f"{len(parts)} section(s) under it, listed above as "
                    "`part`. A section set out this way is usually a "
                    "heading whose text is in those parts: read them with "
                    "get_sections.",
                )
            )
        elif not children:
            entries.append(
                (
                    CAVEAT_NODE_HAS_NO_CHILDREN,
                    "This node has no children; it is probably a leaf "
                    "section, so call get_section with its guid to read the "
                    "text.",
                )
            )

        if body:
            lines.extend(body)
        elif parts:
            lines.append("  (none)")
            lines.extend(self._part_lines(parts))
        else:
            lines.append(
                "  (none — this may be a leaf section; "
                "use get_section to read its text)"
            )
        caveats, caveat_lines = self._caveats(entries)
        lines.extend(caveat_lines)

        summary = (
            f"{len(children)} child node(s) under "
            f"{(data.get('number') or data.get('title') or guid)}."
        )
        return "\n".join(line for line in lines if line != ""), {
            "node": {
                "guid": guid,
                "ref": (data.get("number") or "").strip() or None,
                "title": (data.get("title") or "").strip() or None,
                "label": (data.get("label") or "").strip() or None,
                "type": data.get("type") or None,
            },
            "parent": data.get("parent") or None,
            "url": url,
            "children": children,
            "parts": parts,
            "summary": summary,
            "caveats": caveats,
        }

    def _outline(
        self, children: List[Any], depth: int, counts: List[int]
    ) -> Tuple[List[str], List[Dict[str, Any]]]:
        """(rendered lines, structured tree) for the outline, TOC_DEPTH
        levels deep. One walk builds both, so they cannot disagree."""
        lines: List[str] = []
        nodes: List[Dict[str, Any]] = []
        for child in children:
            if not isinstance(child, (dict, str)):
                continue
            # One child at a time: the two helpers skip different things (a
            # node with no guid has a line but no structured entry), and a
            # zip over their whole outputs would then pair the wrong ones.
            structured = self._structured_children([child])
            if not structured:
                continue
            node = structured[0]
            line = self._render_children([child], depth)[0]
            counts[depth] += 1
            below = (
                [c for c in child.get("children") or [] if isinstance(c, (dict, str))]
                if isinstance(child, dict)
                else []
            )
            if below and depth + 1 < TOC_DEPTH:
                sub_lines, sub_nodes = self._outline(below, depth + 1, counts)
                lines.append(line)
                lines.extend(sub_lines)
                node["children"] = sub_nodes
            elif below:
                node["children_not_listed"] = len(below)
                lines.append(f"{line}  (+{len(below)} below)")
            else:
                lines.append(line)
            nodes.append(node)
        return lines, nodes

    def _structured_children(self, children: List[Any]) -> List[Dict[str, Any]]:
        """Children as a tree of {guid, ref, title, type}.

        A tree, not `rows`: the whole point of the outline tools is walking
        chapters -> articles -> sections, and flattening into a table throws
        away the relation the caller came for. Bare-guid children (the API
        sometimes returns just an id) still yield a usable entry.
        """
        out: List[Dict[str, Any]] = []
        for child in children:
            if isinstance(child, str):
                out.append(
                    {
                        "guid": child,
                        "ref": None,
                        "title": None,
                        "label": None,
                        "type": None,
                    }
                )
                continue
            if not isinstance(child, dict):
                continue
            guid = child.get("guid") or child.get("id")
            if not guid:
                continue
            out.append(
                {
                    "guid": str(guid),
                    "ref": (child.get("number") or "").strip() or None,
                    "title": (child.get("title") or "").strip() or None,
                    # `label` is the kind (Title / Part / Chapter / Litem) and
                    # is the only kind field the API sets on children; `type`
                    # is absent there, so advertising it alone would have made
                    # every child's kind read as null.
                    "label": (child.get("label") or "").strip() or None,
                    "type": child.get("type") or None,
                }
            )
        return out

    @staticmethod
    def _kind_word(label: Any, number: Any) -> str:
        """The node kind, unless the number already opens with it: the
        structure endpoint numbers a chapter "Chapter 9.28" and a list item
        "Litem A", which rendered as "Chapter Chapter 9.28", "Litem Litem A"."""
        label = (label or "").strip() if isinstance(label, str) else ""
        number = (number or "").strip() if isinstance(number, str) else ""
        return "" if label and number.lower().startswith(label.lower()) else label

    def _render_children(self, children: List[Any], depth: int) -> List[str]:
        """Render a list of child nodes (each a dict, or a bare guid string)."""
        out: List[str] = []
        indent = "  " * (depth + 1)
        for child in children:
            if isinstance(child, str):
                out.append(f"{indent}- guid {child}")
                continue
            if not isinstance(child, dict):
                continue
            guid = child.get("guid") or child.get("id", "")
            # Include the node kind (Title / Part / Chapter / Litem). Several
            # real nodes carry an empty `number`, so without it a child can
            # render as nothing but a title and a guid, losing where it sits
            # in the hierarchy -- which is the entire reason to call this.
            parts = [
                self._kind_word(child.get("label"), child.get("number")),
                child.get("number", ""),
                child.get("title", ""),
            ]
            label = " ".join(p for p in parts if (p or "").strip()).strip()
            out.append(f"{indent}- {label or '(untitled)'}  [guid: {guid}]")
        return out

    # Effectively unlimited, for rendering the structured copy of a section
    # body. The markdown block is capped for readability; the machine-readable
    # half must not be, or the structured channel is strictly worse than the
    # prose it exists to replace.
    _UNCAPPED = 10**9

    def _format_content(
        self,
        data: Dict[str, Any],
        resolved_from: Optional[str] = None,
        max_chars: int = MAX_CONTENT_CHARS,
        max_structured_chars: int = MAX_STRUCTURED_CHARS,
        offset: int = 0,
    ) -> tuple:
        guid = str(data.get("id") or data.get("guid", "") or "")
        ref = (data.get("number") or "").strip() or None
        title = (data.get("title") or "").strip() or None
        url = PUBLIC_URL.format(guid=guid) if guid else ""

        images = data.get("images") or []
        attachments = data.get("attachments") or {}
        is_pdf = bool(data.get("pdf"))
        has_attachment = bool(attachments) or bool(images)
        attachment_list = self._attachment_list(attachments)
        of_title = data.get("_title_attachments") or {}
        own_urls = {item["url"] for item in attachment_list}
        title_attachments = [
            dict(item)
            for item in of_title.get("items") or []
            if item["url"] not in own_urls
        ]
        total_chars = self._content_chars(data)
        too_large = total_chars > max_structured_chars

        # Render once, whole, for structuredContent. The text block shows a
        # window of that same rendering, so a caller can read on from the
        # character where one response stopped (`offset`).
        notes: Dict[str, int] = {"inferred": 0, "uncertain": 0}
        full_body = self._render_content_node(
            data,
            depth=0,
            budget=[max_structured_chars if too_large else self._UNCAPPED],
            heading_shown=True,
            notes=notes,
        )
        if offset and offset >= len(full_body):
            if too_large:
                raise ToolInputError(
                    f"offset {offset} is past the end of the "
                    f"{len(full_body):,} characters this server returns "
                    "for this node. That is only the first part of it: "
                    f"the whole node holds about {total_chars:,} "
                    "characters of code text. Call browse_structure with "
                    f"guid {guid} and read its sections individually."
                )
            raise ToolInputError(
                f"offset {offset} is past the end of this section, whose "
                f"text is {len(full_body):,} characters long. Use the "
                "offset given in the rendered_text_truncated note, or "
                "leave it out to read from the start."
            )
        shown_end = min(len(full_body), offset + max_chars)
        if shown_end < len(full_body):
            # End on a line break when one is near, so a subsection is not
            # cut mid-sentence; the next call starts exactly there.
            line_end = full_body.rfind("\n", offset + max_chars // 2, shown_end)
            if line_end > offset:
                shown_end = line_end + 1
        display_body = full_body[offset:shown_end]

        entries: List[tuple] = []

        if resolved_from:
            # Say so: the caller passed a number, and a model chaining on
            # this result should learn the guid for next time.
            entries.append(
                (
                    CAVEAT_RESOLVED_FROM_SECTION_NUMBER,
                    f"Reference {resolved_from!r} was resolved to guid "
                    f"{guid} by exact match on its number.",
                )
            )

        if data.get("_subsection"):
            entries.append(
                (
                    CAVEAT_SUBSECTION_OF_SECTION,
                    f"Subsection {data['_subsection']} was asked for. A "
                    "subsection cannot be fetched on its own, so the whole "
                    f"of {ref or 'the section'} is returned: find "
                    f"subsection {data['_subsection']} in the text below.",
                )
            )

        # A section that is reserved, repealed or superseded is not operative
        # law, which changes how everything below it must be read. That makes
        # it a banner at the top of the response, not a footnote at the end.
        inactive = self._inactive_marker(ref, title)
        if inactive:
            entries.append(
                (
                    CAVEAT_SECTION_NOT_IN_FORCE,
                    f"This section is marked {inactive.upper()} in the "
                    f"published code -- treat the text below as a record of "
                    f"what the section says, not as operative law in force.",
                )
            )
        if is_pdf or (attachments and not full_body.strip()):
            entries.append(
                (
                    CAVEAT_BODY_IS_ATTACHMENT,
                    "The body of this section is a PDF or attachment rather "
                    "than text; read it at the ecode360.com link.",
                )
            )
        if too_large:
            entries.append(
                (
                    CAVEAT_NODE_TOO_LARGE,
                    "This node is a whole chapter or title, too large to "
                    "return in one piece. Three different lengths apply. "
                    f"(1) The whole node holds about {total_chars:,} "
                    "characters of code text. (2) This server returns "
                    f"only the first part of it: {len(full_body):,} "
                    "characters as formatted here, which is all that "
                    "structuredContent.text holds. (3) The text below is "
                    f"characters {offset + 1:,}-{shown_end:,} of those "
                    f"{len(full_body):,}. Most of the node is therefore "
                    "NOT in this result. Call browse_structure with guid "
                    f"{guid} to list its sections, then read the ones you "
                    "need with get_sections.",
                )
            )
        elif shown_end < len(full_body):
            entries.append(
                (
                    CAVEAT_RENDERED_TEXT_TRUNCATED,
                    f"INCOMPLETE: the text below is characters "
                    f"{offset + 1:,}-{shown_end:,} of {len(full_body):,} "
                    "in this section. To read on, call get_section with "
                    f"guid {guid} and offset={shown_end}. (The complete "
                    "body is also in structuredContent.text.)",
                )
            )
        elif offset:
            entries.append(
                (
                    CAVEAT_RENDERED_TEXT_TRUNCATED,
                    f"The text below is characters {offset + 1:,}-"
                    f"{shown_end:,} of {len(full_body):,}: the end of this "
                    "section, continued from an earlier call. Its start is "
                    "not repeated here.",
                )
            )

        if notes["inferred"] or notes["uncertain"]:
            parts = []
            if notes["inferred"]:
                parts.append(
                    f"In {notes['inferred']} place(s) here, the eCode360 "
                    "API gives the words before a list and the words after "
                    "it as one run of text, with the list items separate. "
                    "This server put each list back after the colon that "
                    "introduces it, so the sentence that follows a list is "
                    "shown after it. That placement is inferred, not "
                    "supplied by the publisher."
                )
            if notes["uncertain"]:
                parts.append(
                    f"In {notes['uncertain']} place(s) the position of a "
                    "list could not be worked out, and words that follow "
                    "the list in the published code may appear before it."
                )
            entries.append(
                (
                    CAVEAT_LIST_POSITION_INFERRED,
                    " ".join(parts)
                    + f" Check the order at {url} before relying on it.",
                )
            )

        if title_attachments:
            entries.append(
                (
                    CAVEAT_TITLE_ATTACHMENTS,
                    "A note in this text says that something is \"included "
                    "as an attachment to this title\". The publisher does "
                    "not link it from the section, so the "
                    f"{len(title_attachments)} PDF(s) attached to "
                    f"{of_title.get('title') or 'the title'} are listed "
                    "below as `title attachment`: pick the one the note "
                    "names and read it with read_attachment(url). It is "
                    "part of the published code but not of this text.",
                )
            )

        parts = [dict(part) for part in data.get("_parts") or []]
        if parts:
            entries.append(
                (
                    CAVEAT_SECTION_TEXT_IN_PARTS,
                    f"{ref or 'This section'} is a heading with no text of "
                    f"its own. Its text is in the {len(parts)} section(s) "
                    "numbered under it, listed below as `part`: read them "
                    "with get_sections before saying what "
                    f"{ref or 'the section'} provides.",
                )
            )

        caveats, caveat_lines = self._caveats(entries)
        history = self._parse_history(data.get("content"))

        head = [
            " ".join(p for p in [ref or "", title or ""] if p).strip()
            or "(untitled section)",
            f"guid: {guid}   type: {data.get('type', '')}",
            f"link: {url}" if url else "",
        ]
        if history and history["latest_date"]:
            count = len(history["entries"])
            head.append(
                f"history: {count} {'entry' if count == 1 else 'entries'} "
                "in the history note; the newest dated one is "
                f"{history['latest_citation']}, {history['latest_date']} "
                "(adoption date as printed)"
            )
        if history and shown_end < len(full_body):
            # The note is at the end of the section, past what is shown.
            shown_entries: List[str] = []
            used = 0
            for entry in reversed(history["entries"]):
                printed = " ".join(entry["text"].split())
                if shown_entries and used + len(printed) > HISTORY_HEAD_CHARS:
                    break
                shown_entries.append(printed)
                used += len(printed) + 2
            left_out = len(history["entries"]) - len(shown_entries)
            head.append(
                "history note (printed at the end of this section, after "
                "the text shown here; oldest first"
                + (
                    f"; the {left_out} oldest entries are left out"
                    if left_out
                    else ""
                )
                + "): "
                + "; ".join(reversed(shown_entries))
            )
        head.extend(self._part_lines(parts))
        if images:
            head.append(f"images: {len(images)} (relative links on ecode360.com)")
        for item in attachment_list:
            head.append(f"attachment: {item['name']} — {item['url']}")
        for item in title_attachments:
            head.append(f"title attachment: {item['name']} — {item['url']}")
        if attachment_list or title_attachments:
            head.append(
                "(attachments are PDFs; read one with read_attachment(url))"
            )
        head.append("")

        text = "\n".join(
            caveat_lines + [h for h in head if h != ""]
        ) + "\n" + display_body

        return text, {
            "guid": guid,
            "ref": ref,
            "title": title,
            "url": url,
            "text": full_body,
            "length": {
                "text_chars": len(full_body),
                "shown_from": offset,
                "shown_to": shown_end,
                "whole_node_chars": total_chars if too_large else None,
            },
            "history": history,
            "has_attachment": has_attachment,
            "attachments": attachment_list,
            "title_attachments": title_attachments,
            "parts": parts,
            "is_pdf": is_pdf,
            "caveats": caveats,
        }

    @staticmethod
    def _attachment_list(attachments: Any) -> List[Dict[str, str]]:
        """[{name, url}] from the API's {name: url} mapping, urls absolute."""
        if not isinstance(attachments, dict):
            return []
        out: List[Dict[str, str]] = []
        for name, url in attachments.items():
            if not isinstance(url, str) or not url.strip():
                continue
            url = url.strip()
            if url.startswith("/"):
                url = "https://ecode360.com" + url
            out.append({"name": str(name), "url": url})
        return out

    def _content_chars(self, node: Any) -> int:
        """Characters of body text under a node, without rendering it."""
        if not isinstance(node, dict):
            return 0
        return len(node.get("content") or "") + sum(
            self._content_chars(child) for child in node.get("children") or []
        )

    # ── PDF attachments ─────────────────────────────────────────────────

    def _attachment_url_arg(self, args: Dict[str, Any]) -> str:
        """Validate a caller-supplied attachment URL; return it canonical.

        The one caller-controlled URL this server fetches. Everything that
        is not an eCode360 PDF for THIS library is refused before any
        request: scheme, host, port, credentials, query, path shape.
        """
        raw = args.get("url")
        if not isinstance(raw, str) or not raw.strip():
            raise ToolInputError(
                "url is required -- pass the `url` of a search_code result "
                "with is_pdf=true, or an attachment url from get_section."
            )
        customer = self.plugin_config.customer_id
        refuse = (
            "url must be an eCode360 PDF for this code: an attachment "
            "(https://live.ecode360.com/attachment/<id>/<name>.pdf or "
            f"https://ecode360.com/documents/{customer}/<name>.pdf) or an "
            f"ordinance (https://live.ecode360.com/{customer}/laws/"
            f"LF<digits>.pdf). Got {raw.strip()!r}. Use the url exactly as "
            "search_code or get_section returned it."
        )
        try:
            parts = urlsplit(raw.strip())
            port = parts.port
        except ValueError as exc:
            raise ToolInputError(refuse) from exc
        host = (parts.hostname or "").lower()
        path = unquote(parts.path)
        document_re = re.compile(
            DOCUMENT_PATH_TEMPLATE.format(
                customer=re.escape(self.plugin_config.customer_id)
            ),
            re.IGNORECASE,
        )
        if (
            parts.scheme != "https"
            or host not in ATTACHMENT_HOSTS
            or port is not None
            or parts.username is not None
            or parts.password is not None
            or parts.query
            or ".." in path.split("/")
            or any(ord(c) < 32 for c in path)
            or not (
                ATTACHMENT_PATH_RE.match(path)
                or document_re.match(path)
                or self._is_ordinance_pdf(path)
            )
        ):
            raise ToolInputError(refuse)
        return f"https://{host}{quote(path)}"

    def _is_ordinance_pdf(self, url_or_path: str) -> bool:
        """True for an ordinance PDF of THIS library (see LAW_PATH_TEMPLATE)."""
        path = unquote(urlsplit(url_or_path).path)
        return bool(
            re.match(
                LAW_PATH_TEMPLATE.format(
                    customer=re.escape(self.plugin_config.customer_id)
                ),
                path,
                re.IGNORECASE,
            )
        )

    async def _download_attachment(self, url: str) -> bytes:
        """Fetch a validated attachment URL. No API credentials are sent,
        and a redirect is an error rather than something to follow."""
        cached = _ATTACHMENT_CACHE.get(url)
        if cached is not None:
            return cached
        timeout = self._request_timeout() or self.plugin_config.timeout
        try:
            # The download is read chunk by chunk, so the per-read httpx
            # timeout alone would let a slow trickle run past the budget.
            async with asyncio.timeout(
                self._hard_limit()
            ) as scope, httpx.AsyncClient(
                timeout=timeout,
                follow_redirects=False,
                transport=self._attachment_transport,
            ) as client:
                async with client.stream(
                    "GET", url, headers={"Accept": "application/pdf"}
                ) as resp:
                    status = resp.status_code
                    if status in (404, 410):
                        raise ToolInputError(
                            f"No attachment exists at {url} (HTTP {status}). "
                            "Take the url from a current search_code or "
                            "get_section result."
                        )
                    if status >= 400 and self._is_ordinance_pdf(url):
                        # eCode360 answers 500, not 404, for an ordinance
                        # PDF it does not have (seen 2026-11-19). That is
                        # the ordinary fate of these links: the PDF exists
                        # only until the ordinance is codified.
                        raise ToolInputError(
                            f"eCode360 has no ordinance PDF at {url} (HTTP "
                            f"{status}). The publisher keeps an ordinance "
                            "as a PDF only until it is worked into the "
                            "code text, so an older link stops working. "
                            "Look the ordinance up with find_ordinance, "
                            "which lists the sections it changed once it "
                            "is in the code, or take a current link from "
                            "recent_ordinances or search_code."
                        )
                    if status != 200:
                        raise EcodeAPIError(
                            f"eCode360 answered HTTP {status} for the "
                            f"attachment {url}.",
                            status,
                        )
                    chunks: List[bytes] = []
                    size = 0
                    async for chunk in resp.aiter_bytes():
                        size += len(chunk)
                        if size > MAX_ATTACHMENT_BYTES:
                            raise EcodeAPIError(
                                f"The attachment at {url} is larger than "
                                f"{MAX_ATTACHMENT_BYTES // (1024 * 1024)} MB "
                                "and cannot be read here; open the link.",
                                413,
                            )
                        chunks.append(chunk)
        except TimeoutError as e:
            if not scope.expired():
                raise
            raise EcodeAPIError(
                "eCode360 did not finish delivering the attachment within "
                f"this call's {TOOL_CALL_BUDGET_S:g}s budget. Try again "
                "shortly.",
                504,
            ) from e
        except httpx.TimeoutException as e:
            raise EcodeAPIError(
                f"eCode360 did not deliver the attachment within "
                f"{timeout:.0f}s ({type(e).__name__}). Try again shortly.",
                504,
            ) from e
        except httpx.TransportError as e:
            raise EcodeAPIError(
                f"Could not reach eCode360 for the attachment: "
                f"{type(e).__name__}{': ' + str(e) if str(e) else ''}",
                503,
            ) from e
        data = b"".join(chunks)
        if not data.lstrip()[:5].startswith(b"%PDF-"):
            raise EcodeAPIError(
                f"The attachment at {url} is not a PDF (it does not start "
                "with a PDF header), so its text cannot be extracted.",
                502,
            )
        while len(_ATTACHMENT_CACHE) >= ATTACHMENT_CACHE_ENTRIES:
            _ATTACHMENT_CACHE.pop(next(iter(_ATTACHMENT_CACHE)))
        _ATTACHMENT_CACHE[url] = data
        return data

    @staticmethod
    def _extract_page_texts(
        data: bytes, done: List[str], deadline: Optional[float]
    ) -> Tuple[int, List[str]]:
        """(page count, plain text of pages 1..n), continuing after the
        pages already in `done`.

        Synchronous and CPU-bound; called through asyncio.to_thread. Stops
        when the call's time budget is nearly spent, always after at least
        one more page, so every call makes progress. Whitespace is
        collapsed: a phrase broken across lines is still one phrase.
        """
        from pypdf import PdfReader

        try:
            reader = PdfReader(io.BytesIO(data))
            if reader.is_encrypted:
                reader.decrypt("")
            page_count = len(reader.pages)
        except Exception as e:
            raise EcodeAPIError(
                f"The attachment could not be opened as a PDF "
                f"({type(e).__name__}); read it at its link instead.",
                502,
            ) from e
        texts = list(done[:page_count])
        added = 0
        while len(texts) < page_count:
            if added and deadline is not None and (
                deadline - time.monotonic() < FIND_TIME_RESERVE_S
            ):
                break
            try:
                text = reader.pages[len(texts)].extract_text() or ""
            except Exception:
                text = ""
            texts.append(" ".join(text.split()))
            added += 1
        return page_count, texts

    @staticmethod
    def _attachment_text_key(url: str) -> str:
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
        return (
            f"v{SHARED_CACHE_VERSION}/{ATTACHMENT_TEXT_PREFIX}/"
            f"{digest}.json.gz"
        )

    async def _attachment_text_get(
        self, shared: Tuple[Any, str], url: str, code_date: str
    ) -> Optional[Dict[str, Any]]:
        """The stored page texts for `url`, if current. Never raises."""
        client, bucket = shared
        key = self._attachment_text_key(url)

        def read() -> bytes:
            body = client.get_object(Bucket=bucket, Key=key)["Body"]
            return body.read(SHARED_CACHE_MAX_BYTES + 1)

        try:
            raw = await asyncio.wait_for(
                asyncio.to_thread(read), SHARED_CACHE_TIMEOUT_S
            )
            if len(raw) > SHARED_CACHE_MAX_BYTES:
                raise _SharedCacheUnusable("object larger than expected")
            inflater = zlib.decompressobj(31)
            plain = inflater.decompress(raw, ATTACHMENT_TEXT_MAX_BYTES)
            if inflater.unconsumed_tail:
                raise _SharedCacheUnusable("text larger than expected")
            stored = json.loads(plain)
            age = time.time() - float(stored["fetched_at"])
            page_count = stored["page_count"]
            texts = stored["texts"]
            if (
                stored.get("version") != SHARED_CACHE_VERSION
                or stored.get("url") != url
                or not isinstance(page_count, int)
                or isinstance(page_count, bool)
                or page_count < 1
                or not isinstance(texts, list)
                or len(texts) > page_count
                or not all(isinstance(text, str) for text in texts)
                or not isinstance(stored.get("pdf_sha256"), str)
            ):
                raise _SharedCacheUnusable("unexpected object shape")
        except Exception as e:  # noqa: BLE001 - never fail a tool call
            missing = type(e).__name__ == "NoSuchKey" or "NoSuchKey" in str(e)
            logger.log(
                logging.INFO if missing else logging.WARNING,
                "shared cache read of %s: %s: %s",
                key,
                type(e).__name__,
                str(e) or "timed out",
            )
            return None
        stale = (
            stored.get("code_last_updated") != code_date
            or not -300 <= age <= SHARED_CACHE_MAX_AGE_S
        )
        logger.info(
            "shared cache hit for %s (%d of %d pages%s)",
            key,
            len(texts),
            page_count,
            "; to be checked against the PDF" if stale else "",
        )
        return {
            "page_count": page_count,
            "texts": texts,
            "pdf_sha256": stored["pdf_sha256"],
            "stale": stale,
        }

    async def _attachment_text_put(
        self,
        shared: Tuple[Any, str],
        url: str,
        code_date: str,
        entry: Dict[str, Any],
    ) -> None:
        """Store page texts for the other containers. Never raises."""
        client, bucket = shared
        key = self._attachment_text_key(url)

        def write() -> int:
            packer = zlib.compressobj(6, zlib.DEFLATED, 31)
            body = packer.compress(
                json.dumps(
                    {
                        "version": SHARED_CACHE_VERSION,
                        "url": url,
                        "code_last_updated": code_date,
                        "fetched_at": time.time(),
                        "page_count": entry["page_count"],
                        "pdf_sha256": entry["pdf_sha256"],
                        "texts": entry["texts"],
                    },
                    ensure_ascii=False,
                ).encode("utf-8")
            ) + packer.flush()
            if len(body) > SHARED_CACHE_MAX_BYTES:
                raise _SharedCacheUnusable("text too large to share")
            client.put_object(
                Bucket=bucket,
                Key=key,
                Body=body,
                ContentType="application/gzip",
            )
            return len(body)

        try:
            size = await asyncio.wait_for(
                asyncio.to_thread(write), SHARED_CACHE_TIMEOUT_S
            )
            logger.info("shared cache stored %s (%d bytes)", key, size)
        except Exception as e:  # noqa: BLE001 - never fail a tool call
            logger.warning(
                "shared cache write of %s: %s: %s",
                key,
                type(e).__name__,
                str(e) or "timed out",
            )

    async def _attachment_page_texts(self, url: str) -> Dict[str, Any]:
        """Plain text of the document's pages, as far as it has been
        extracted: {"page_count": n, "texts": [page 1, page 2, ...]}.

        Taken from this container's memory, else the shared cache, and
        extended from the PDF for as long as this call's budget allows.
        `texts` is shorter than `page_count` when the budget ran out; the
        next call continues from there. Text kept from before the code was
        last updated is used only if the PDF is still the same file.
        """
        shared = self._shared_cache_client()
        # As for the ordinance table: without the code's date there is
        # nothing to check a stored object against, so the bucket is left
        # alone.
        code_date = await self._code_last_updated() if shared else None
        entry = _ATTACHMENT_TEXT_CACHE.get(url)
        if entry is not None:
            entry = {
                **entry,
                "stale": bool(code_date)
                and entry.get("code_date") not in (None, code_date),
            }
        elif shared and code_date:
            entry = await self._attachment_text_get(shared, url, code_date)
        stale = bool(entry and entry["stale"])
        had = len(entry["texts"]) if entry else 0
        if entry is None or stale or had < entry["page_count"]:
            pdf = await self._download_attachment(url)
            digest = hashlib.sha256(pdf).hexdigest()
            if entry is not None and entry["pdf_sha256"] != digest:
                logger.info("the PDF at %s has changed; extracting again", url)
                entry, had = None, 0
            if entry is None or had < entry["page_count"]:
                page_count, texts = await asyncio.to_thread(
                    self._extract_page_texts,
                    pdf,
                    entry["texts"] if entry else [],
                    _call_deadline.get(),
                )
            else:
                page_count, texts = entry["page_count"], entry["texts"]
            entry = {
                "page_count": page_count,
                "texts": texts,
                "pdf_sha256": digest,
            }
            if (
                shared
                and code_date
                and (len(texts) > had or stale)
                and page_count >= ATTACHMENT_TEXT_SHARED_MIN_PAGES
            ):
                await self._attachment_text_put(shared, url, code_date, entry)
        entry = {
            "page_count": entry["page_count"],
            "texts": entry["texts"],
            "pdf_sha256": entry["pdf_sha256"],
            "code_date": code_date,
        }
        _ATTACHMENT_TEXT_CACHE.pop(url, None)
        while len(_ATTACHMENT_TEXT_CACHE) >= ATTACHMENT_CACHE_ENTRIES:
            _ATTACHMENT_TEXT_CACHE.pop(next(iter(_ATTACHMENT_TEXT_CACHE)))
        _ATTACHMENT_TEXT_CACHE[url] = entry
        return entry

    @staticmethod
    def _find_in_pages(
        texts: List[str], start_page: int, query: str
    ) -> Dict[str, Any]:
        """Where `query` occurs in pages start_page..len(texts).

        The phrase as written (case and line breaks ignored) if it occurs
        anywhere in that range; otherwise, for a phrase of several words,
        the pages that contain every word.
        """
        needle = query.lower()

        def snippet(text: str, lowered: str, at: int, length: int) -> str:
            # Lower-casing can change a string's length for a few
            # characters; positions then belong to the lowered text.
            source = text if len(text) == len(lowered) else lowered
            start = max(0, at - FIND_SNIPPET_BEFORE)
            end = min(len(source), at + length + FIND_SNIPPET_AFTER)
            return (
                ("... " if start else "")
                + source[start:end].strip()
                + (" ..." if end < len(source) else "")
            )

        pages = [
            (number, text, text.lower())
            for number, text in enumerate(texts, 1)
            if number >= start_page
        ]
        matches: List[Dict[str, Any]] = []
        for number, text, lowered in pages:
            count = lowered.count(needle)
            if not count:
                continue
            snippets: List[str] = []
            at = lowered.find(needle)
            while at != -1 and len(snippets) < FIND_SNIPPETS_PER_PAGE:
                snippets.append(snippet(text, lowered, at, len(needle)))
                # The next snippet starts clear of this one.
                at = lowered.find(
                    needle, at + len(needle) + FIND_SNIPPET_AFTER
                )
            matches.append(
                {"page": number, "count": count, "snippets": snippets}
            )
        if matches:
            return {"match": "phrase", "matches": matches}
        words = sorted(set(needle.split()), key=len, reverse=True)
        if len(words) < 2:
            return {"match": "phrase", "matches": []}
        patterns = [
            re.compile(rf"(?<!\w){re.escape(word)}(?!\w)") for word in words
        ]
        for number, text, lowered in pages:
            first = [pattern.search(lowered) for pattern in patterns]
            if all(first):
                matches.append(
                    {
                        "page": number,
                        "count": 1,
                        "snippets": [
                            snippet(
                                text, lowered, first[0].start(), len(words[0])
                            )
                        ],
                    }
                )
        return {
            "match": "all_words" if matches else "phrase",
            "matches": matches,
        }

    def _format_find(
        self, url: str, query: str, start_page: int, entry: Dict[str, Any]
    ) -> tuple:
        texts = entry["texts"]
        page_count = entry["page_count"]
        searched_to = len(texts)
        filename = unquote(url.rsplit("/", 1)[-1])
        searched = searched_to >= start_page
        found = (
            self._find_in_pages(texts, start_page, query)
            if searched
            else {"match": "phrase", "matches": []}
        )
        matches = found["matches"]
        listed = matches[:FIND_PAGES_LISTED]
        again = f'read_attachment again with find="{query}"'

        entries: List[tuple] = []
        if self._is_ordinance_pdf(url):
            entries.append(
                (
                    CAVEAT_ORDINANCE_PDF,
                    "This is an ORDINANCE as the publisher holds it, not "
                    "code text: it has not yet been worked into the "
                    "published code, and extraction loses the marks that "
                    "show which words it deletes. Read the page with "
                    "read_attachment before relying on a snippet; that "
                    "result explains the drafting marks.",
                )
            )
        next_page: Optional[int] = None
        if len(matches) > len(listed):
            next_page = listed[-1]["page"] + 1
            entries.append(
                (
                    CAVEAT_FIND_MORE_PAGES,
                    f"{len(matches)} pages match; the first {len(listed)} "
                    f"are listed. Call {again} and page={next_page} for "
                    "the rest, or use a more specific phrase.",
                )
            )
            if searched_to < page_count:
                entries.append(
                    (
                        CAVEAT_FIND_INCOMPLETE,
                        f"INCOMPLETE: only pages {start_page}-{searched_to} "
                        f"of {page_count} have been searched so far, so "
                        "the count above is not the whole document's. The "
                        "same call continues the search.",
                    )
                )
        elif not searched:
            next_page = start_page
            entries.append(
                (
                    CAVEAT_FIND_INCOMPLETE,
                    f"NOT SEARCHED YET: the text of this {page_count}-page "
                    f"document is still being prepared (pages 1-"
                    f"{searched_to} are ready) and page {start_page} has "
                    f"not been reached. Call {again} and page={start_page} "
                    "to continue.",
                )
            )
        elif searched_to < page_count:
            next_page = searched_to + 1
            entries.append(
                (
                    CAVEAT_FIND_INCOMPLETE,
                    f"INCOMPLETE: only pages {start_page}-{searched_to} of "
                    f"{page_count} have been searched; preparing the text "
                    "of a document this long takes more than one call. "
                    f"Call {again} and page={next_page} to search the "
                    "rest before saying what the document does not "
                    "contain.",
                )
            )
        if found["match"] == "all_words":
            entries.append(
                (
                    CAVEAT_FIND_WORDS_NOT_PHRASE,
                    f'The phrase "{query}" does not occur in the pages '
                    "searched. The pages listed contain every word of it, "
                    "but not together: they are NOT matches for the "
                    "phrase.",
                )
            )
        if listed:
            entries.append(
                (
                    CAVEAT_FIND_SNIPPETS,
                    "Snippets are extracted PDF text with line breaks "
                    "removed, to show where the phrase is. Read the page "
                    "with read_attachment(url, page=<page>) before quoting "
                    "or summarizing it.",
                )
            )
        if searched and not any(texts[start_page - 1:]):
            entries.append(
                (
                    CAVEAT_NO_TEXT_LAYER,
                    "No text could be extracted from the pages searched "
                    "-- they are probably scanned images or drawings, so "
                    "nothing can be found in them here. Read the PDF at "
                    "its link.",
                )
            )

        span = (
            f"pages {start_page}-{searched_to} of {page_count}"
            if searched
            else f"this {page_count}-page document"
        )
        if not searched:
            summary = f'{filename}: "{query}" has not been searched for yet.'
        elif not matches:
            summary = (
                f'{filename}: "{query}" was not found in {span}. PDF text '
                "can differ from the printed page (hyphenation, columns, "
                "spelling): try a shorter phrase or a single distinctive "
                "word."
            )
        elif found["match"] == "all_words":
            summary = (
                f'{filename}: the phrase "{query}" was not found in '
                f"{span}; {len(matches)} page(s) contain all of its words."
            )
        else:
            total = sum(match["count"] for match in matches)
            summary = (
                f'{filename}: "{query}" occurs {total} time(s) on '
                f"{len(matches)} page(s) in {span}."
            )
        caveats, caveat_lines = self._caveats(entries)
        lines = [summary, f"link: {url}"] + caveat_lines
        for match in listed:
            lines.append("")
            lines.append(
                f"--- page {match['page']} of {page_count} "
                f"({match['count']} match"
                f"{'' if match['count'] == 1 else 'es'}) ---"
                if found["match"] == "phrase"
                else f"--- page {match['page']} of {page_count} ---"
            )
            lines.extend(match["snippets"])
        return "\n".join(lines), {
            "url": url,
            "filename": filename,
            "page_count": page_count,
            "layout": False,
            "pages": [],
            "next_page": next_page,
            "find": {
                "query": query,
                "match": found["match"],
                "searched_from": start_page if searched else None,
                "searched_to": searched_to if searched else None,
                "pages_matched": len(matches),
                "matches": listed,
            },
            "summary": summary,
            "caveats": caveats,
        }

    @staticmethod
    def _extract_pdf(
        data: bytes, start_page: int, layout: bool, deadline: Optional[float]
    ) -> Dict[str, Any]:
        """Text of consecutive pages from `start_page` (1-indexed).

        Synchronous and CPU-bound; called through asyncio.to_thread. Stops
        at the character budget or when the call's time budget is nearly
        spent, always after at least one page.
        """
        # Imported here: only this tool needs it, and it should not slow
        # the cold start of every other request.
        from pypdf import PdfReader

        try:
            reader = PdfReader(io.BytesIO(data))
            if reader.is_encrypted:
                reader.decrypt("")
            page_count = len(reader.pages)
        except Exception as e:
            raise EcodeAPIError(
                f"The attachment could not be opened as a PDF "
                f"({type(e).__name__}); read it at its link instead.",
                502,
            ) from e
        if start_page > page_count:
            raise ToolInputError(
                f"page {start_page} is past the end: this document has "
                f"{page_count} page(s). Pages are 1-indexed."
            )

        pages: List[Dict[str, Any]] = []
        used = 0
        truncated_page: Optional[int] = None
        number = start_page
        while number <= page_count:
            if pages and deadline is not None and (
                deadline - time.monotonic() < ATTACHMENT_TIME_RESERVE_S
            ):
                break
            page = reader.pages[number - 1]
            try:
                text = (
                    page.extract_text(extraction_mode="layout")
                    if layout
                    else page.extract_text()
                )
            except Exception:
                # Layout mode is the newer code path; plain extraction of
                # the same page is better than failing the whole call.
                try:
                    text = page.extract_text()
                except Exception:
                    text = ""
            text = "\n".join(
                line.rstrip() for line in (text or "").splitlines()
            )
            text = re.sub(r"\n{3,}", "\n\n", text).strip("\n")
            if pages and used + len(text) > ATTACHMENT_TEXT_CHARS:
                break
            if len(text) > ATTACHMENT_TEXT_CHARS:
                text = text[:ATTACHMENT_TEXT_CHARS]
                truncated_page = number
            pages.append({"page": number, "text": text})
            used += len(text)
            number += 1
        return {
            "page_count": page_count,
            "pages": pages,
            "next_page": number if number <= page_count else None,
            "truncated_page": truncated_page,
        }

    def _format_attachment(
        self, url: str, layout: bool, extracted: Dict[str, Any]
    ) -> tuple:
        pages = extracted["pages"]
        page_count = extracted["page_count"]
        next_page = extracted["next_page"]
        filename = unquote(url.rsplit("/", 1)[-1])
        first, last = pages[0]["page"], pages[-1]["page"]
        span = f"page {first}" if first == last else f"pages {first}-{last}"

        entries: List[tuple] = [
            (
                CAVEAT_PDF_TEXT_EXTRACTION,
                "This text was extracted from a PDF. In a table, which "
                "column a mark belongs to is shown only by its horizontal "
                "position under the header row"
                + (
                    ""
                    if layout
                    else " -- and layout=false discards that position, so "
                    "do not read table cells from this output"
                )
                + ". Treat cell readings as provisional and tell the user "
                f"to confirm them in the PDF: {url}",
            )
        ]
        if self._is_ordinance_pdf(url):
            entries.insert(
                0,
                (
                    CAVEAT_ORDINANCE_PDF,
                    "This is an ORDINANCE as the publisher holds it, not "
                    "code text. It is here because it has not yet been "
                    "worked into the published code, so the sections it "
                    "amends may still show their earlier wording: say so. "
                    "Text extraction drops the drafting marks. In an "
                    "Anchorage ordinance new language is underlined and "
                    "deleted language is struck through inside [brackets], "
                    "usually in CAPITALS; the underlining and the strike "
                    "do not survive here, so deleted words read as if "
                    "they were law (seen: 'seven [nine][SEVEN] voting "
                    "members', where only 'seven' is the adopted text). "
                    "Treat bracketed text as deleted unless the PDF shows "
                    "otherwise, do not quote this as the current code, and "
                    "send the user to the PDF for what was added and "
                    f"removed: {url}. It may be a substitute or amended "
                    "version; its header says which.",
                ),
            )
        if next_page:
            entries.append(
                (
                    CAVEAT_MORE_ATTACHMENT_PAGES,
                    f"Showing {span} of {page_count}. Call read_attachment "
                    f"again with page={next_page} to continue. To locate a "
                    "word, phrase or section number in this document "
                    "without paging, pass find instead.",
                )
            )
        if extracted["truncated_page"]:
            entries.append(
                (
                    CAVEAT_ATTACHMENT_PAGE_TRUNCATED,
                    f"Page {extracted['truncated_page']} is longer than "
                    f"{ATTACHMENT_TEXT_CHARS:,} characters and was cut; "
                    "read the rest in the PDF"
                    + (" or retry with layout=false." if layout else "."),
                )
            )
        empty = [pg["page"] for pg in pages if not pg["text"].strip()]
        if empty:
            entries.append(
                (
                    CAVEAT_NO_TEXT_LAYER,
                    f"No text could be extracted from page(s) "
                    f"{', '.join(map(str, empty))} -- they are probably "
                    "scanned images or drawings. Read them in the PDF.",
                )
            )

        summary = f"{filename}: {span} of {page_count}."
        caveats, caveat_lines = self._caveats(entries)
        lines = [summary, f"link: {url}"] + caveat_lines
        for pg in pages:
            lines.append("")
            lines.append(f"--- page {pg['page']} of {page_count} ---")
            lines.append(pg["text"] or "(no extractable text)")
        return "\n".join(lines), {
            "url": url,
            "filename": filename,
            "page_count": page_count,
            "layout": layout,
            "pages": pages,
            "next_page": next_page,
            "summary": summary,
            "caveats": caveats,
        }

    def _format_sections(
        self, requested: List[str], outcomes: List[Any], merged: int = 0
    ) -> tuple:
        """Render a batch. Raises if NOTHING could be read, so a batch that
        failed outright is an error result rather than an empty success."""
        failures = [o for o in outcomes if isinstance(o, BaseException)]
        for exc in failures:
            # Anything that is neither a caller mistake nor a named
            # upstream failure is a bug here; never fold it into a row.
            if not isinstance(exc, (ToolInputError, EcodeAPIError)):
                raise exc
        if len(failures) == len(outcomes):
            for exc in failures:
                # An upstream fault outranks caller mistakes: it is the
                # one that needs a server-side look.
                if not isinstance(exc, ToolInputError):
                    raise exc
            raise ToolInputError(
                "None of the requested sections could be read: "
                + " | ".join(
                    f"{raw!r}: {exc}" for raw, exc in zip(requested, failures)
                )
            )

        blocks: List[str] = []
        sections: List[Dict[str, Any]] = []
        entries: List[tuple] = []
        per_section = max(
            BATCH_SECTION_CHARS,
            BATCH_TOTAL_CHARS // max(1, len(outcomes) - len(failures)),
        )
        # First at the even share; a section that did not fit is rendered
        # again below if the shorter ones left it more room.
        cut: List[int] = []
        lengths: List[int] = []
        for raw, outcome in zip(requested, outcomes):
            if isinstance(outcome, BaseException):
                message = str(outcome) or type(outcome).__name__
                entries.append(
                    (
                        CAVEAT_SECTION_UNAVAILABLE,
                        f"Requested section {raw!r} was not returned: "
                        f"{message}",
                    )
                )
                blocks.append(f"=== {raw} ===\n(not returned -- see note above)")
                sections.append(
                    {
                        "requested": raw,
                        "found": False,
                        "error": message,
                        "guid": None,
                        "ref": None,
                        "title": None,
                        "url": None,
                        "text": None,
                        "has_attachment": False,
                        "attachments": [],
                        "title_attachments": [],
                        "parts": [],
                        "is_pdf": False,
                        "caveats": [],
                    }
                )
                continue
            data, resolved_from = outcome
            text, structured = self._format_content(
                data,
                resolved_from=resolved_from,
                max_chars=per_section,
                max_structured_chars=BATCH_STRUCTURED_CHARS,
            )
            length = len(structured.get("text") or "")
            lengths.append(length)
            if length > per_section:
                cut.append(len(blocks))
            blocks.append(f"=== {raw} ===\n{text}")
            sections.append(
                {"requested": raw, "found": True, "error": None, **structured}
            )

        allowances = self._batch_allowances(lengths)
        read = [i for i, s in enumerate(sections) if s["found"]]
        for index in cut:
            allowance = allowances[read.index(index)]
            if allowance <= per_section:
                continue
            raw = requested[index]
            data, resolved_from = outcomes[index]
            text, structured = self._format_content(
                data,
                resolved_from=resolved_from,
                max_chars=allowance,
                max_structured_chars=BATCH_STRUCTURED_CHARS,
            )
            blocks[index] = f"=== {raw} ===\n{text}"
            sections[index] = {
                "requested": raw,
                "found": True,
                "error": None,
                **structured,
            }

        if merged:
            entries.append(
                (
                    CAVEAT_DUPLICATE_REQUESTS,
                    f"{merged} repeated entr(y/ies) in the request were "
                    "merged; each distinct section is returned once.",
                )
            )
        found = sum(1 for s in sections if s["found"])
        summary = f"{found} of {len(sections)} requested section(s) returned."
        caveats, caveat_lines = self._caveats(entries)
        text_out = "\n\n".join(["\n".join([summary] + caveat_lines)] + blocks)
        return text_out, {
            "sections": sections,
            "summary": summary,
            "caveats": caveats,
        }

    @staticmethod
    def _batch_allowances(lengths: List[int]) -> List[int]:
        """Characters of rendered text each section of a batch may show,
        given the full length of each.

        Shortest first: a section takes its even share of what is left, or
        its whole length if that is less, and what it leaves goes to the
        ones after it. No section gets less than BATCH_SECTION_CHARS."""
        allowances = [0] * len(lengths)
        left = BATCH_TOTAL_CHARS
        order = sorted(range(len(lengths)), key=lambda i: lengths[i])
        for position, index in enumerate(order):
            share = max(
                BATCH_SECTION_CHARS, left // (len(order) - position)
            )
            allowances[index] = share
            left = max(0, left - min(lengths[index], share))
        return allowances

    def _format_definitions(
        self,
        term: str,
        data: Dict[str, Any],
        hits: List[Dict[str, Any]],
        bodies: List[Optional[str]],
        reordered: Optional[Set[str]] = None,
    ) -> tuple:
        raw_total = data.get("totalResultCount")
        total_count = (
            self._as_int(raw_total) if raw_total is not None else None
        )
        max_pages = self._as_int(data.get("maxPages", 1), default=1)
        shown = hits[: len(bodies)]

        entries: List[tuple] = []
        definitions: List[Dict[str, Any]] = []
        lines: List[str] = []
        from_snippet = 0
        for i, (hit, body) in enumerate(zip(shown, bodies), 1):
            text = body or hit["snippet"]
            if not body:
                from_snippet += 1
            url = PUBLIC_URL.format(guid=hit["guid"])
            where = (
                f"§ {hit['defined_in']}" if hit["defined_in"] else "(unknown)"
            )
            related = "" if hit["exact_match"] else "  [related term]"
            lines.append(f"{i}. {hit['term'] or '(untitled)'}{related}")
            lines.append(f"   defined in: {where}   guid: {hit['guid']}")
            if text:
                lines.extend(f"   {ln}" for ln in text.splitlines())
            lines.append(f"   {url}")
            lines.append("")
            definitions.append(
                {
                    "guid": hit["guid"],
                    "term": hit["term"],
                    "exact_match": hit["exact_match"],
                    "defined_in": hit["defined_in"],
                    "text": text,
                    "url": url,
                }
            )

        exact = sum(1 for d in definitions if d["exact_match"])
        if not definitions:
            summary = f"No published definition found for {term!r}."
            entries.append(
                (
                    CAVEAT_NO_DEFINITION_FOUND,
                    f"The code has no separately published definition "
                    f"titled {term!r}. That does not mean the term is "
                    "undefined: it may be defined inline in a section, "
                    "under a different form of the word (try the singular "
                    "or a shorter phrase), or by state law. Next step: "
                    f'search_code with "{term}" AND means, or '
                    f'title:Definitions AND "{term}".',
                )
            )
        else:
            summary = (
                f"{len(definitions)} definition(s) for {term!r}"
                f" ({exact} exact match(es))."
            )
            if len(definitions) > 1:
                entries.append(
                    (
                        CAVEAT_MULTIPLE_DEFINITIONS,
                        "This term is defined in more than one place. Each "
                        "definition applies only within the chapter or "
                        "title that contains it -- use the one for the "
                        "area of law the question concerns, cite its "
                        "section, and call get_section on `defined_in` if "
                        "the scope is unclear.",
                    )
                )
        if len(hits) > len(shown) or (definitions and max_pages > 1):
            of_total = (
                total_count if total_count is not None else "an unknown number"
            )
            entries.append(
                (
                    CAVEAT_MORE_DEFINITIONS,
                    f"Showing {len(definitions)} definition(s); more may "
                    "exist (only the first 20 title matches were "
                    f"examined, of {of_total}). To see the rest, use "
                    f'search_code with title:"{term}" and page through.',
                )
            )
        if from_snippet:
            entries.append(
                (
                    CAVEAT_DEFINITION_TEXT_FROM_SNIPPET,
                    f"{from_snippet} definition(s) show only a search "
                    "excerpt because the full text could not be fetched; "
                    "read them in full with get_section before quoting.",
                )
            )

        placed = [
            str(i)
            for i, (hit, body) in enumerate(zip(shown, bodies), 1)
            if body and hit["guid"] in (reordered or ())
        ]
        if placed:
            entries.append(
                (
                    CAVEAT_LIST_POSITION_INFERRED,
                    f"Definition(s) {', '.join(placed)} above contain a "
                    "list. The eCode360 API gives the words before a list "
                    "and the words after it as one run of text, with the "
                    "list items separate, so this server put the list "
                    "after the colon that introduces it where it could. "
                    "That placement is inferred, not supplied by the "
                    "publisher, and words that follow the list in the "
                    "published code may appear before it. Check the order "
                    "at the definition's ecode360.com link before relying "
                    "on it.",
                )
            )

        caveats, caveat_lines = self._caveats(entries)
        text_out = "\n".join([summary, ""] + lines + caveat_lines).strip()
        return text_out, {
            "term": term,
            "total_count": total_count,
            "definitions": definitions,
            "summary": summary,
            "caveats": caveats,
        }

    @staticmethod
    def _inactive_marker(ref: Optional[str], title: Optional[str]) -> Optional[str]:
        """Whether a section's heading marks it as not in force.

        Heading only. "repealed" turns up constantly in the BODY of sections
        that are perfectly in force -- an ordinance repealing something else
        -- so matching content would cry wolf on ordinary law.
        """
        for heading in (title, ref):
            match = INACTIVE_TITLE_RE.search(" ".join((heading or "").split()))
            if match:
                return (
                    match.group("paren") or match.group("whole") or match.group("lead")
                ).lower()
        return None

    @staticmethod
    def _same_heading(a: Optional[str], b: Optional[str]) -> bool:
        def norm(text: Optional[str]) -> str:
            return " ".join((text or "").split()).rstrip(".").lower()

        return bool(norm(a)) and norm(a) == norm(b)

    @staticmethod
    def _split_list_text(text: str) -> Tuple[str, str, Optional[str]]:
        """(words before the list, words after it, how that was decided).

        `text` is a parent node's own text with any history note removed.
        See LIST_LEAD_IN_RE for why this is needed and how it decides.
        """
        if not text or text.endswith(":"):
            return text, "", None
        cuts = [
            m
            for m in LIST_LEAD_IN_RE.finditer(text)
            if len(text[: m.start()].split()) >= LEAD_IN_MIN_WORDS
        ]
        if not cuts:
            return text, "", None
        if len(cuts) > 1:
            return text, "", "uncertain"
        return text[: cuts[0].start() + 1], text[cuts[0].end():], "inferred"

    @staticmethod
    def _term_opens_text(title: str, text: str) -> bool:
        """Whether `title` is a quoted term that `text` starts with."""
        return (
            len(title) > 2
            and title[0] in _TERM_QUOTES
            and title[-1] in _TERM_QUOTES
            and text.startswith(title)
        )

    @staticmethod
    def _notes_on_own_lines(text: str) -> str:
        """Put each editor's note whose end is known on a line of its own.

        See EDITOR_NOTE_RE. A note whose end cannot be told from the text
        is left where it is: guessing would put words of the law inside
        the note, or the reverse.
        """
        if "ote" not in text:
            return text
        lines: List[str] = []
        done = 0
        for match in EDITOR_NOTE_RE.finditer(text):
            before = text[: match.start()]
            if before.count("(") > before.count(")"):
                # Inside a parenthesis, which is the history note: that
                # stays one run of text, as _parse_history reads it.
                continue
            lines += [text[done : match.start()], match.group(1)]
            done = match.end()
            # The footnote mark stood before the sentence's own stop
            # ("as depicted below[1]."): keep that stop with its sentence
            # rather than opening the next line with it.
            if lines[-2].strip() and text[done : done + 1] in (".", ",", ";"):
                lines[-2] = lines[-2].rstrip() + text[done]
                done += 1
        if not done:
            return text
        lines.append(text[done:])
        return "\n".join(line.strip() for line in lines if line.strip())

    def _render_content_node(
        self,
        node: Dict[str, Any],
        depth: int,
        budget: List[int],
        heading_shown: bool = False,
        inherited_title: Optional[str] = None,
        notes: Optional[Dict[str, int]] = None,
    ) -> str:
        """Render a node and its descendants in DOCUMENT order: the node's
        lead-in text, its children, the text that follows them, then the
        history note.

        `heading_shown` says the caller prints this node's heading itself
        (the "§ number title" line of get_section), so the copy of it that
        opens the body is dropped. `inherited_title` is the heading of the
        section this node is a subsection of: the API stamps that heading on
        every subsection, where repeating it ("A. Unfair labor practices.",
        "1. Unfair labor practices.") only buries the text.
        """
        if budget[0] <= 0:
            return ""
        indent = "  " * depth
        number = (node.get("number") or "").strip()
        title = (node.get("title") or "").strip()
        text = (node.get("content") or "").strip()
        children = [c for c in node.get("children") or [] if isinstance(c, dict)]
        inherited = self._same_heading(title, inherited_title)
        own_heading = depth > 0 and bool(title) and not inherited

        if title and not inherited and (heading_shown or own_heading):
            # Only a whole heading: "Fees." opening "Fees. A fee of ..." is
            # the heading, but "Fees" opening "Fees shall be paid ..." is the
            # first word of the law.
            rest = text[len(title):]
            if text.startswith(title) and (
                not rest or (rest[0].isspace() and title[-1] in ".:;?!)")
            ):
                text = rest.lstrip()

        lead, after, history = text, "", ""
        if children:
            note = HISTORY_NOTE_RE.search(text)
            if note:
                history = note.group(1)
                text = text[: note.start()].rstrip()
            lead, after, how = self._split_list_text(text)
            if how and notes is not None:
                notes[how] += 1

        def take(piece: str) -> str:
            if not piece or budget[0] <= 0:
                return ""
            if len(piece) > budget[0]:
                piece = piece[: budget[0]] + " …[truncated]"
            budget[0] -= len(piece)
            return piece

        def block(piece: str, first_prefix: str = "") -> str:
            rows = piece.splitlines() or [""]
            out = [f"{indent}{first_prefix}{rows[0]}".rstrip()]
            out += [f"{indent}{row}" for row in rows[1:]]
            return "\n".join(out)

        lead = self._notes_on_own_lines(lead)
        after = self._notes_on_own_lines(after)

        parts: List[str] = []
        lead = take(lead)
        if own_heading and self._term_opens_text(title, lead):
            # A defined term: its heading is the quoted term and its text
            # opens with the same quoted term, so the heading line only
            # printed every term twice.
            parts.append(block(lead, f"{number} " if number else ""))
        elif own_heading:
            parts.append(f"{indent}{' '.join(p for p in [number, title] if p)}")
            if lead:
                parts.append(block(lead))
        elif depth > 0 and number:
            # A subsection: its label, then its own text, on one line.
            parts.append(block(lead, f"{number} ") if lead else f"{indent}{number}")
        elif lead:
            parts.append(block(lead))

        child_title = title if (depth == 0 or own_heading) else inherited_title
        for child in children:
            if budget[0] <= 0:
                parts.append(f"{indent}…[output truncated; fetch child guids "
                             "individually with get_section]")
                break
            parts.append(
                self._render_content_node(
                    child,
                    depth + 1,
                    budget,
                    inherited_title=child_title,
                    notes=notes,
                )
            )
        for tail in (take(after), take(history)):
            if tail:
                parts.append(block(tail))
        return "\n".join(p for p in parts if p)

    # ── History notes and the ordinance list ────────────────────────────

    @staticmethod
    def _iso_date(printed: Optional[str]) -> Optional[str]:
        """'6-25-2024' -> '2024-06-25'; None for anything not a real date."""
        match = HISTORY_DATE_RE.fullmatch((printed or "").strip())
        if not match:
            return None
        month, day, year = (int(g) for g in match.groups())
        try:
            return datetime.date(year, month, day).isoformat()
        except ValueError:
            return None

    @staticmethod
    def _split_outside_parens(text: str, sep: str = ";") -> List[str]:
        parts: List[str] = []
        depth = 0
        current: List[str] = []
        for char in text:
            if char == "(":
                depth += 1
            elif char == ")":
                depth = max(0, depth - 1)
            if char == sep and depth == 0:
                parts.append("".join(current))
                current = []
            else:
                current.append(char)
        parts.append("".join(current))
        return [" ".join(part.split()) for part in parts if part.strip()]

    def _parse_history(self, content: Any) -> Optional[Dict[str, Any]]:
        """The node's own history note as entries, or None if it has none.

        Parsing only labels what is printed: every entry keeps its text,
        and a piece that does not fit the pattern is still an entry.
        """
        if not isinstance(content, str):
            return None
        note = HISTORY_NOTE_RE.search(content.strip())
        if not note:
            return None
        entries: List[Dict[str, Any]] = []
        for raw in self._split_outside_parens(note.group(1)[1:-1]):
            effective = HISTORY_EFFECTIVE_RE.search(raw)
            dated = None
            for found in HISTORY_DATE_RE.finditer(raw):
                if effective and found.start() == effective.start(1):
                    continue
                dated = self._iso_date(found.group(0))
                if dated:
                    break
            entries.append(
                {
                    "citation": raw.split(",", 1)[0].strip(),
                    "date": dated,
                    "effective_date": (
                        self._iso_date(effective.group(1))
                        if effective
                        else None
                    ),
                    "text": raw[:HISTORY_ENTRY_CHARS],
                }
            )
        if not entries:
            return None
        latest = max(
            (e for e in entries if e["date"]),
            key=lambda e: e["date"],
            default=None,
        )
        return {
            "entries": entries,
            "latest_date": latest["date"] if latest else None,
            "latest_citation": latest["citation"] if latest else None,
        }

    @staticmethod
    def _ordinance_scope_arg(args: Dict[str, Any]) -> Optional[str]:
        """`within` as a chapter prefix ('21', '21.05', 'R21', 'Charter')."""
        raw = args.get("within")
        if raw is None or not str(raw).strip():
            return None
        text = " ".join(str(raw).split())
        if len(text) > MAX_WITHIN_CHARS:
            raise ToolInputError(
                f"within is too long (at most {MAX_WITHIN_CHARS} "
                "characters). Pass a title or chapter such as 'Title 21'."
            )
        if text.lower().rstrip(".") in ("charter", "cha", "the charter"):
            return "Charter"
        match = ORDINANCE_WITHIN_RE.match(text)
        if not match:
            raise ToolInputError(
                f"within {text!r} is not a title or chapter. Pass "
                "'Title 21', 'Chapter 21.05', '21', 'R21' or 'Charter'; "
                "leave it out to list every recent ordinance."
            )
        # The table records chapters, so a section narrows to its chapter.
        return ".".join(match.group(1).upper().split(".")[:2])

    @staticmethod
    def _ordinance_in_scope(chapters: List[str], scope: str) -> bool:
        if "." in scope or scope == "Charter":
            return scope in chapters
        return any(ch.split(".")[0] == scope for ch in chapters)

    async def _fetch_ordinance_list(self) -> Optional[Dict[str, Any]]:
        """The Ordinance List section's content, or None when the code has
        no such section."""
        return await self._fetch_section_titled(ORDINANCE_LIST_TITLE)

    async def _fetch_section_titled(
        self, wanted: str, static: bool = False
    ) -> Optional[Dict[str, Any]]:
        """The content of the outline's section with this exact title
        (lower case, no final full stop), or None when there is none.
        Found by title, never by a guid written here: guids change when
        the publisher republishes. `static` keeps the content for
        STATIC_CACHE_TTL_S (per container) instead of RECENT_CACHE_TTL_S."""
        toc = await self._get_toc()
        found: Optional[str] = None
        stack = [toc]
        while stack:
            node = stack.pop()
            if not isinstance(node, dict):
                continue
            kids = node.get("children") or []
            title = " ".join((node.get("title") or "").split())
            if not kids and title.lower().rstrip(".") == wanted:
                found = str(node.get("guid") or node.get("id") or "")
                break
            stack.extend(reversed(kids))
        if not found:
            return None
        path = self._cust_path(f"/code/content/{quote(found, safe='')}")
        if static:
            return await self._get_shared_static_json(path)
        return await self._get_recent_json(path)

    def _parse_ordinance_rows(self, content: str) -> List[Dict[str, Any]]:
        text = " ".join((content or "").split())
        starts = []
        for match in ORDINANCE_ROW_RE.finditer(text):
            words = [w.lower() for w in text[: match.start()].split()[-2:]]
            if words and words[-1] == "no.":
                words = words[:-1]
                if not words:
                    words = [""]
            else:
                words = words[-1:]
            if words and words[-1] in ORDINANCE_MENTION_WORDS:
                continue
            starts.append(match)
        rows: List[Dict[str, Any]] = []
        for match, following in zip(starts, starts[1:] + [None]):
            end = following.start() if following else len(text)
            body = text[match.end():end].strip()
            subject, chapters, supplement = body, [], None
            tail = ORDINANCE_TAIL_RE.search(body)
            if tail:
                subject = body[: tail.start()].strip()
                supplement = tail.group(2)
                for place in tail.group(1).split("; "):
                    place = re.sub(r"^Chs?\. ", "", place)
                    chapters.append("Charter" if place == "CHA" else place)
            number = match.group(1)
            base = re.match(r"\d{4}-\d+", number).group(0)
            rows.append(
                {
                    "ordinance": number,
                    "adopted": self._iso_date(match.group(2)),
                    "subject": subject or None,
                    "chapters": chapters,
                    "supplement": supplement,
                    "search_query": self._ordinance_search_query(base),
                    "text": text[match.start():end].strip(),
                }
            )
        return rows

    def _format_ordinances(
        self,
        node: Optional[Dict[str, Any]],
        scope: Optional[str],
        limit: int,
        updated: Optional[str],
    ) -> tuple:
        empty = {
            "ordinances": [],
            "total_count": 0,
            "within": scope,
            "covers": None,
            "source": None,
            "code_last_updated": updated,
        }
        rows = (
            self._parse_ordinance_rows(node.get("content") or "")
            if isinstance(node, dict)
            else []
        )
        if not rows:
            summary = "The list of recent ordinances could not be read."
            why = (
                "This code's outline has no 'Ordinance List and Disposition "
                "Table' section."
                if node is None
                else "The Ordinance List and Disposition Table was found "
                "but no rows could be read from it; its layout may have "
                "changed."
            )
            caveats, lines = self._caveats(
                [
                    (
                        CAVEAT_ORDINANCE_LIST_UNAVAILABLE,
                        f"{why} This is not evidence that nothing changed. "
                        "To check one section, read it with get_section and "
                        "look at its history; to look for an ordinance, "
                        "search_code for its number in quotes, e.g. "
                        '"2026-40".',
                    )
                ]
            )
            return "\n".join([summary] + lines), {
                **empty,
                "summary": summary,
                "caveats": caveats,
            }

        guid = str(node.get("id") or node.get("guid") or "")
        ref = (node.get("number") or "").strip() or None
        source = {
            "guid": guid,
            "ref": ref,
            "title": (node.get("title") or "").strip() or None,
            "url": PUBLIC_URL.format(guid=guid),
        }
        dates = sorted(r["adopted"] for r in rows if r["adopted"])
        covers = {
            "count": len(rows),
            "earliest_adopted": dates[0] if dates else None,
            "latest_adopted": dates[-1] if dates else None,
        }
        kept = [
            r
            for r in rows
            if scope is None or self._ordinance_in_scope(r["chapters"], scope)
        ]
        # Stable: newest first, undated rows after them in table order.
        kept.sort(key=lambda r: r["adopted"] or "", reverse=True)
        shown = kept[:limit]

        if scope is None:
            where = ""
        elif scope == "Charter":
            where = " codified in the Charter"
        else:
            kind = "Chapter" if "." in scope else "Title"
            where = f" codified in {kind} {scope}"
        span = (
            f"{covers['earliest_adopted']} to {covers['latest_adopted']}"
            if dates
            else "dates not given"
        )
        if kept:
            summary = (
                f"{len(kept)} recent ordinance(s){where}; showing "
                f"{len(shown)}, newest first."
            )
        else:
            summary = f"No recent ordinance(s){where} in the list."
        entries: List[tuple] = [
            (
                CAVEAT_ORDINANCE_LIST_SCOPE,
                f"Source: the publisher's Ordinance List and Disposition "
                f"Table ({ref or 'guid ' + guid}, {source['url']}), which "
                f"lists {covers['count']} ordinance(s) adopted {span}. It "
                "covers only ordinances already worked into the published "
                "code: one adopted since the code was last updated is not "
                "here, and neither is the code's earlier history (look an "
                "older ordinance up with find_ordinance, or read a "
                "section's own history with get_section). Dates are adoption dates; an "
                "ordinance may take effect later. The table names the "
                "chapters an ordinance went into, not the sections: pass a "
                "row's search_query to search_code to list the sections "
                "that cite it, then read them."
                + (
                    " An ordinance with no listed chapter is never matched "
                    "by `within`."
                    if scope is not None
                    else ""
                ),
            )
        ]
        if len(kept) > len(shown):
            entries.append(
                (
                    CAVEAT_MORE_ORDINANCES,
                    f"{len(kept) - len(shown)} older ordinance(s) are not "
                    f"shown. Call again with limit={min(len(kept), MAX_ORDINANCES)} "
                    "to see them.",
                )
            )
        undated = sum(1 for r in shown if not r["adopted"])
        if undated:
            entries.append(
                (
                    CAVEAT_ORDINANCE_ROWS_UNDATED,
                    f"{undated} row(s) have no adoption date in the table "
                    "and are listed last; their place in the order is "
                    "unknown.",
                )
            )

        lines = [summary, ""]
        for i, row in enumerate(shown, 1):
            lines.append(
                f"{i}. AO {row['ordinance']} -- adopted "
                f"{row['adopted'] or '(date not given)'}"
            )
            if row["subject"]:
                lines.append(f"   {row['subject']}")
            lines.append(
                "   codified in: "
                + (", ".join(row["chapters"]) or "(not given)")
                + (
                    f"   supplement: {row['supplement']}"
                    if row["supplement"]
                    else ""
                )
            )
            lines.append(f"   sections: search_code({row['search_query']})")
            lines.append("")
        caveats, caveat_lines = self._caveats(entries)
        text = "\n".join(lines + caveat_lines).strip()
        if updated:
            text += f"\n\nPublished code last updated {updated}."
        return text, {
            "ordinances": shown,
            "total_count": len(kept),
            "within": scope,
            "covers": covers,
            "source": source,
            "code_last_updated": updated,
            "summary": summary,
            "caveats": caveats,
        }

    @staticmethod
    def _ordinance_search_query(base: str) -> str:
        """The search that lists sections citing an ordinance: its number,
        quoted. Not '"AO No. <number>"': older history notes write
        "AO 2013-117", which that phrase misses (1 section against 68)."""
        return f'"{base}"'

    async def _citing_sections(
        self, base: str
    ) -> Optional[Tuple[List[Dict[str, Any]], bool]]:
        """(sections that cite the ordinance, whether every hit was
        checked), or None when the search could not be made. Decorates a
        result and never fails one."""
        cited = re.compile(
            ORDINANCE_CITATION_TEMPLATE.format(base=re.escape(base))
        )
        rows: List[Dict[str, Any]] = []
        complete = True
        try:
            for page in range(ORDINANCE_CITING_PAGES):
                data = await self._search(
                    self._ordinance_search_query(base), page
                )
                results = (data or {}).get("results") or []
                for hit in results:
                    if hit.get("pdf") or hit.get("type") in ("doc", "law"):
                        continue
                    found = cited.search(
                        self._strip_html(hit.get("text") or "")
                    )
                    if not found:
                        continue  # an ordinance table, or another mention
                    guid = str(
                        hit.get("id")
                        or self._guid_from_url(hit.get("url", ""))
                    )
                    if not guid or any(r["guid"] == guid for r in rows):
                        continue
                    ref = " ".join((hit.get("number") or "").split()) or None
                    title = self._strip_html(hit.get("title") or "").strip()
                    cited_as = re.sub(
                        r"\s+([,;.)])", r"\1", " ".join(found.group(0).split())
                    ).rstrip(" ,.")
                    # A snippet can join two excerpts with "..."; the
                    # citation ends at its date when it has one.
                    cited_as = cited_as.split("...")[0].rstrip(" ,.")
                    dated = re.match(r".*?\d{1,2}-\d{1,2}-\d{4}", cited_as)
                    if dated:
                        cited_as = dated.group(0)
                    rows.append(
                        {
                            "guid": guid,
                            "ref": ref,
                            "title": title or None,
                            "url": PUBLIC_URL.format(guid=guid),
                            "cited_as": cited_as or None,
                        }
                    )
                if len(results) < 20:
                    break
            else:
                total = self._as_int((data or {}).get("totalResultCount"))
                complete = total <= ORDINANCE_CITING_PAGES * 20
        except (EcodeAPIError, ToolInputError) as e:
            logger.warning("could not search for sections citing %s: %s", base, e)
            return None
        return rows, complete

    def _with_citing_sections(
        self,
        text: str,
        structured: Dict[str, Any],
        citing: Optional[Tuple[List[Dict[str, Any]], bool]],
    ) -> tuple:
        rows, complete = citing if citing else ([], True)
        structured = {
            **structured,
            "citing_sections": rows,
            "citing_sections_complete": complete,
        }
        if not rows:
            return text, structured
        count = f"{len(rows)}{'' if complete else ' or more'}"
        old = structured.get("summary") or ""
        lead = (
            f"AO {structured['listing']['ordinance']} is in the list of "
            "recent ordinances; the Ordinance Disposition Table does not "
            "list it yet, but"
            if structured.get("listing")
            else f"AO {structured['ordinance']} is not in the Ordinance "
            "Disposition Table, but"
        )
        summary = f"{lead} {count} section(s) of the code cite it."
        if old and text.startswith(old):
            text = summary + text[len(old):]
        structured["summary"] = summary
        listed = "\n".join(
            f"- {r['ref'] or 'guid ' + r['guid']}"
            + (f" {r['title']}" if r["title"] else "")
            + (f" -- {r['cited_as']}" if r["cited_as"] else "")
            for r in rows
        )
        text = self._append_note(
            text,
            structured,
            CAVEAT_ORDINANCE_CITING_SECTIONS,
            f"These {count} section(s) cite the ordinance, found by "
            f"searching the code for {structured['search_query']}. A "
            "section cites an ordinance in its history note, so these are "
            "the sections it enacted or amended; the citation is shown as "
            "printed, where '\u00a7 n' is the ordinance's own section and "
            "the date is its adoption date. It does not say what changed: "
            "read the sections with get_sections."
            + (
                ""
                if complete
                else " Only the first "
                f"{ORDINANCE_CITING_PAGES * 20} search hits were checked: "
                "pass the same query to search_code for the rest."
            )
            + f"\n{listed}",
        )
        return text, structured

    # ── Ordinances held as PDFs: adopted, not yet codified ───────────────

    async def _pending_ordinances(self) -> Optional[List[Dict[str, str]]]:
        """[{ordinance, url}] for the ordinances the publisher holds as
        PDFs (see PENDING_ORDINANCE_QUERY), or None when that could not be
        checked. It decorates a result and must never fail one."""
        found: List[Dict[str, str]] = []
        try:
            for page in range(PENDING_ORDINANCE_PAGES):
                data = await self._search(PENDING_ORDINANCE_QUERY, page)
                results = (data or {}).get("results") or []
                for hit in results:
                    if hit.get("type") != "law" or not hit.get("pdf"):
                        continue
                    try:
                        # Only a link read_attachment will accept.
                        url = self._attachment_url_arg(
                            {"url": str(hit.get("url") or "").split("?")[0]}
                        )
                    except ToolInputError:
                        continue
                    if not self._is_ordinance_pdf(url):
                        continue
                    title = " ".join(
                        self._strip_html(hit.get("title") or "").split()
                    )
                    if title and all(f["url"] != url for f in found):
                        found.append({"ordinance": title, "url": url})
                if len(results) < 20:
                    break
        except (EcodeAPIError, ToolInputError) as e:
            logger.warning("could not list the ordinance PDFs: %s", e)
            return None
        await self._add_ordinance_titles(found)
        return found

    async def _add_ordinance_titles(self, found: List[Dict[str, Any]]) -> None:
        """Set `subject` and `amends` on each ordinance, from its PDF.
        Bounded and never raises: an unread title is simply null."""
        titles: List[Any] = [None] * len(found)
        try:
            titles = await asyncio.wait_for(
                asyncio.gather(
                    *(self._ordinance_title(o["url"]) for o in found),
                    return_exceptions=True,
                ),
                ORDINANCE_TITLES_TIMEOUT_S,
            )
        except Exception as e:  # noqa: BLE001 - decorates a result
            logger.warning(
                "ordinance titles not read: %s: %s",
                type(e).__name__,
                str(e) or "timed out",
            )
        for ordinance, title in zip(found, titles):
            subject = title if isinstance(title, str) and title else None
            ordinance["subject"] = subject
            ordinance["amends"] = list(
                dict.fromkeys(
                    n.upper()
                    for n in ORDINANCE_TITLE_NUMBER_RE.findall(subject or "")
                )
            )

    async def _ordinance_title(self, url: str) -> Optional[str]:
        """The title of the ordinance at `url` ('' when its first page has
        none), from memory, the shared cache, or the PDF itself. None when
        the PDF could not be read; that is not remembered."""
        if url in _ORDINANCE_TITLE_CACHE:
            return _ORDINANCE_TITLE_CACHE[url]
        path = "/laws-title/" + url.rsplit("/", 1)[-1]
        shared = self._shared_cache_client()
        code_date = await self._code_last_updated() if shared else None
        title: Optional[str] = None
        if shared and code_date:
            stored = await self._shared_cache_get(shared, path, code_date)
            if stored is not None:
                title = stored["content"]
        if title is None:
            try:
                pdf = await self._download_attachment(url)
                page = await asyncio.to_thread(self._first_page_text, pdf)
            except (EcodeAPIError, ToolInputError) as e:
                logger.warning("ordinance title of %s not read: %s", url, e)
                return None
            title = self._ordinance_title_from_page(page) or ""
            if shared and code_date:
                await self._shared_cache_put(
                    shared, path, code_date, {"content": title}
                )
        while len(_ORDINANCE_TITLE_CACHE) >= ORDINANCE_TITLE_CACHE_ENTRIES:
            _ORDINANCE_TITLE_CACHE.pop(next(iter(_ORDINANCE_TITLE_CACHE)))
        _ORDINANCE_TITLE_CACHE[url] = title
        return title

    @staticmethod
    def _first_page_text(data: bytes) -> str:
        """Synchronous; called through asyncio.to_thread."""
        from pypdf import PdfReader

        try:
            reader = PdfReader(io.BytesIO(data))
            if reader.is_encrypted:
                reader.decrypt("")
            return reader.pages[0].extract_text() or ""
        except Exception as e:
            raise EcodeAPIError(
                f"The ordinance PDF could not be opened ({type(e).__name__}).",
                502,
            ) from e

    @staticmethod
    def _ordinance_title_from_page(page_text: str) -> Optional[str]:
        """'AN ORDINANCE ... .' from the first page, as printed, with the
        drafting line numbers at the ends of its lines removed. None when
        the page does not have that shape: nothing is guessed."""
        start = ORDINANCE_TITLE_START_RE.search(page_text or "")
        if not start:
            return None
        lines = page_text[start.start():start.start() + 4000].split("\n")
        expected: Optional[int] = None
        kept: List[str] = []
        for line in lines:
            line = line.rstrip()
            numbered = re.search(r"(?:^|\s)(\d{1,2})$", line)
            if numbered:
                value = int(numbered.group(1))
                # Line numbers count up from the title's first line; a
                # trailing number that does not fit is part of the title
                # ("... CHAPTER 9" with the line number missing).
                if (expected is None and value <= 3) or value == expected:
                    expected = value + 1
                    line = line[: numbered.start()].rstrip()
            kept.append(line)
        text = "\n".join(kept)
        end = ORDINANCE_TITLE_END_RE.search(text)
        if not end:
            return None
        title = text[: end.start()]
        if text[end.start()] == ".":
            title += "."
        title = " ".join(title.split())
        if not 20 <= len(title) <= ORDINANCE_TITLE_MAX_CHARS:
            return None
        return title

    @staticmethod
    def _append_note(
        text: str, structured: Dict[str, Any], code: str, message: str
    ) -> str:
        """One more caveat on a finished result, in both halves: the
        structured array and a 'Note:' line above the code-date trailer."""
        structured["caveats"] = list(structured.get("caveats") or []) + [
            {"code": code, "message": message}
        ]
        trailer = "\n\nPublished code last updated "
        head, sep, tail = text.partition(trailer)
        return f"{head}\nNote: {message}{sep}{tail}"

    @staticmethod
    def _title_names_scope(ordinance: Dict[str, Any], scope: str) -> bool:
        """Does the ordinance's title name this title or chapter?"""
        if scope == "Charter":
            return "CHARTER" in (ordinance.get("subject") or "").upper()
        for number in ordinance.get("amends") or []:
            if "." in scope:
                if number == scope or number.startswith(scope + "."):
                    return True
            elif number.split(".")[0] == scope:
                return True
        return False

    def _with_pending_ordinances(
        self,
        text: str,
        structured: Dict[str, Any],
        pending: Optional[List[Dict[str, Any]]],
        scope: Optional[str] = None,
    ) -> tuple:
        """recent_ordinances: add what is newer than the whole list."""
        listed_items: List[Dict[str, Any]] = []
        for ordinance in pending or []:
            item = dict(ordinance)
            item.setdefault("subject", None)
            item.setdefault("amends", [])
            item["in_scope"] = (
                self._title_names_scope(item, scope)
                if scope and item["subject"]
                else None
            )
            listed_items.append(item)
        # Stable: those whose title names the scope first.
        listed_items.sort(key=lambda o: o["in_scope"] is not True)
        structured = {**structured, "not_yet_codified": listed_items}
        if listed_items:
            rows = "\n".join(
                f"- {'[names it] ' if o['in_scope'] else ''}"
                f"{o['ordinance']} ({o['url']}): "
                + (o["subject"] or "(title not read)")
                for o in listed_items
            )
            if scope:
                kind = (
                    "the Charter"
                    if scope == "Charter"
                    else f"{'Chapter' if '.' in scope else 'Title'} {scope}"
                )
                naming = sum(1 for o in listed_items if o["in_scope"])
                where = (
                    f" `within` does not filter them. Judged from the "
                    f"titles alone, {naming} of them name(s) {kind} (marked "
                    "[names it]); a title lists what its drafter chose to, "
                    "so one that does not name it can still amend it."
                )
            else:
                where = ""
            text = self._append_note(
                text,
                structured,
                CAVEAT_ORDINANCES_NOT_YET_CODIFIED,
                f"NEWER than everything listed above: {len(listed_items)} "
                "ordinance(s) adopted but not yet worked into the code "
                "text, held by the publisher as PDFs. The sections they "
                "amend still show their earlier wording. Each is shown "
                "with its own title, read from the first page of its PDF; "
                "words in [brackets] are deletions from an earlier version "
                "of the title. Use the titles to decide which to read with "
                "read_attachment, and mention them when asked what changed "
                f"recently.{where}\n{rows}",
            )
        return text, structured

    def _with_ordinance_pdfs(
        self,
        text: str,
        structured: Dict[str, Any],
        base: str,
        pending: Optional[List[Dict[str, str]]],
    ) -> tuple:
        """find_ordinance: add the ordinance itself when it is held as a
        PDF. Matched on the number in the PDF's TITLE: a search for the
        number alone also returns PDFs that merely mention it."""
        wanted = re.compile(rf"(?<![\d-]){re.escape(base)}(?!\d)")
        pdfs = [o for o in pending or [] if wanted.search(o["ordinance"])]
        structured = {**structured, "ordinance_pdfs": pdfs}
        if not pdfs:
            return text, structured
        pdfs = [
            {
                "ordinance": o["ordinance"],
                "url": o["url"],
                "subject": o.get("subject"),
                "amends": list(o.get("amends") or []),
            }
            for o in pdfs
        ]
        structured["ordinance_pdfs"] = pdfs
        listed = "; ".join(
            f"{o['ordinance']} ({o['url']})"
            + (f" -- its title: {o['subject']}" if o["subject"] else "")
            for o in pdfs
        )
        if not structured.get("found"):
            summary = (
                f"{pdfs[0]['ordinance']} has been adopted but is not in the "
                "Ordinance Disposition Table yet; the publisher holds the "
                "ordinance itself as a PDF."
            )
            old = structured.get("summary") or ""
            if old and text.startswith(old):
                text = summary + text[len(old):]
            structured["summary"] = summary
        text = self._append_note(
            text,
            structured,
            CAVEAT_ORDINANCE_PDF_AVAILABLE,
            f"The ordinance itself is available as a PDF: {listed}. Read "
            "it with read_attachment to see what it changes. It has not "
            "been worked into the code text yet, so the sections it amends "
            "may still show their earlier wording. Words in [brackets] in "
            "a title are deletions from an earlier version of it.",
        )
        return text, structured

    # ── One ordinance: the Ordinance Disposition Table ──────────────────

    @staticmethod
    def _ordinance_number_arg(args: Dict[str, Any]) -> Tuple[str, str]:
        """('2025-115', '2025-115(S-2)') from 'AO No. 2025-115(S-2)'.

        The first is what rows are matched on; the table lists a
        substitute or amended version under the same base number.
        """
        raw = args.get("ordinance")
        example = "Pass an ordinance number such as '2025-94' or 'AO 85-8'."
        if raw is None or isinstance(raw, (bool, list, dict)):
            raise ToolInputError(f"ordinance is required. {example}")
        text = " ".join(str(raw).split())
        if not text:
            raise ToolInputError(f"ordinance is required. {example}")
        if len(text) > MAX_ORDINANCE_ARG_CHARS:
            raise ToolInputError(
                f"ordinance is too long (at most {MAX_ORDINANCE_ARG_CHARS} "
                f"characters). {example}"
            )
        found = ORDINANCE_ASKED_RE.findall(text)
        if not found:
            raise ToolInputError(
                f"{text!r} is not an ordinance number. {example} To search "
                "by subject instead, use search_code or recent_ordinances."
            )
        if len({(a, b) for a, b, _suffix in found}) > 1:
            raise ToolInputError(
                f"{text!r} names more than one ordinance; pass one number "
                "per call."
            )
        if ORDINANCE_RESOLUTION_RE.search(text):
            # Resolutions are numbered like ordinances: AR 2025-94 and
            # AO 2025-94 are different acts, and only the AO is here.
            raise ToolInputError(
                f"{text!r} is an Assembly resolution (AR). The Ordinance "
                "Disposition Table lists ordinances (AO) only, and "
                "resolutions are not part of the code. If you mean the "
                "ordinance with that number, pass it as 'AO "
                f"{found[0][0]}-{found[0][1]}'."
            )
        first, second, suffix = found[0]
        # Before 2000 the year is written with two digits everywhere: in
        # the disposition table and in every history note ("AO No. 94-113").
        # "1994-113" is that ordinance, and looked up as typed it matched
        # nothing, table or citations.
        century = re.fullmatch(r"19(\d\d)", first)
        if century:
            first = century.group(1)
        # "2025-094" is 2025-94; the table never pads.
        base = f"{first.lstrip('0') or '0'}-{second.lstrip('0') or '0'}"
        return base, base + suffix.replace(" ", "")

    def _parse_ordinance_table(self, content: str) -> List[Dict[str, Any]]:
        """§ CCT.020 as [{ordinance, base, note, entries}], in table order.

        See the comment at ORDINANCE_TABLE_TITLE for the layout. Nothing
        is guessed: a cell that does not read as a section number or a
        date is left in the disposition, as printed.
        """
        text = content or ""
        at = text.find(ORDINANCE_TABLE_HEADER)
        if at < 0:
            return []
        tokens = text[at + len(ORDINANCE_TABLE_HEADER):].split(" ")
        empty = ORDINANCE_EMPTY_CELL

        starts: List[int] = []
        for i, token in enumerate(tokens):
            if not ORDINANCE_NUMBER_RE.match(token):
                continue
            prev = tokens[i - 1] if i else ""
            after = tokens[i + 1] if i + 1 < len(tokens) else ""
            if i and (
                prev == empty  # the section cell of a continuation row
                or prev.endswith(",")  # inside a disposition ("4.c, 11-17")
                or (starts and starts[-1] == i - 1)
            ):
                continue
            # A row goes on with its section cell: a number, or empty.
            if after != empty and not after[:1].isdigit():
                continue
            starts.append(i)

        blocks: List[Dict[str, Any]] = []
        for start, following in zip(starts, starts[1:] + [len(tokens)]):
            cells = tokens[start + 1:following]
            note = None
            if tokens[max(start - 2, 0):start] == ["Amd.", "of"]:
                note = "Amd. of"
            if cells[-2:] == ["Amd.", "of"]:
                cells = cells[:-2]  # belongs to the NEXT ordinance's row
            match = ORDINANCE_NUMBER_RE.match(tokens[start])
            printed, glued = tokens[start], None
            stuck = re.fullmatch(r"(.*\))(\d+)", printed)
            if stuck:
                # "2000-127(S)1": the section cell typed onto the number.
                printed, glued = stuck.group(1), stuck.group(2)
            entries = self._parse_ordinance_cells(cells)
            if glued and entries and not entries[0]["ordinance_section"]:
                entries[0]["ordinance_section"] = glued
            blocks.append(
                {
                    # "Amd. of 2014-137(S)" is a later ordinance amending
                    # that one, listed under its number: kept apart.
                    "ordinance": f"{note} {printed}" if note else printed,
                    "base": match.group(1),
                    "entries": entries,
                }
            )
        return blocks

    def _parse_ordinance_cells(self, cells: List[str]) -> List[Dict[str, Any]]:
        """The rows of ONE ordinance, from the cells after its number."""
        empty = ORDINANCE_EMPTY_CELL
        entries: List[Dict[str, Any]] = []
        i, n = 0, len(cells)
        first = True
        while i < n:
            if not first and cells[i] == empty:
                i += 1  # the empty ordinance cell of a continuation row
            first = False
            section: List[str] = []
            if i < n and cells[i] == empty:
                i += 1
            else:
                while i < n:
                    cell = cells[i]
                    if (
                        section
                        and cell.startswith("(")
                        and not cell.startswith("(§")
                    ):
                        # "4 (Exh. A)", "1 (Prop. 9)"
                        j = i
                        while j < n - 1 and not cells[j].endswith(")"):
                            j += 1
                        section.extend(cells[i:j + 1])
                        i = j + 1
                        continue
                    if ORDINANCE_SECTION_CELL_RE.match(
                        cell
                    ) and not ORDINANCE_DATE_CELL_RE.match(cell):
                        section.append(cell.rstrip(empty))
                        i += 1
                        continue
                    break
            date: List[str] = []
            if i < n and cells[i] == empty:
                i += 1
            elif i < n and ORDINANCE_DATE_CELL_RE.match(cells[i]):
                date.append(cells[i])
                i += 1
                # "7-4-2016, 9-12-2016, 1-1-2017";
                # "12-8-2020/election of 4-6-2021"
                while (
                    i < n
                    and cells[i] != empty
                    and (
                        date[-1].endswith((",", "/"))
                        or cells[i] == "of"
                        or date[-1] == "of"
                    )
                ):
                    date.append(cells[i])
                    i += 1
            rest: List[str] = []
            while i < n and cells[i] != empty:
                rest.append(cells[i])
                i += 1
            action = None
            if rest and rest[0] in ORDINANCE_ACTIONS:
                action = ORDINANCE_ACTIONS[rest[0]]
                rest = rest[1:]
            disposition = " ".join(rest).replace(empty, " ").strip()
            printed_date = " ".join(date) or None
            if not (disposition or section or printed_date):
                continue  # a stray run of empty cells
            if (
                rest
                and rest[0] == "as"
                and action is None
                and entries
                and entries[-1]["action"] == "renumbered"
                and not section
            ):
                # "Rnbd § 3.30.172F.7—21" / "as § 3.30.172F.8—22": one
                # change printed on two rows.
                entries[-1]["disposition"] += f" {disposition}"
                continue
            entries.append(
                {
                    "ordinance_section": " ".join(section) or None,
                    "effective_date": self._iso_date(printed_date),
                    "effective_as_printed": printed_date,
                    "action": action,
                    "disposition": disposition,
                }
            )
        return entries

    @staticmethod
    def _outline_section_numbers(toc: Any) -> Set[str]:
        """Every section-level number in the outline ('21.05.070',
        'R21.20.002', '23.10.104.15'): a number with at least two dots."""
        numbers: Set[str] = set()
        stack = [toc]
        while stack:
            node = stack.pop()
            if not isinstance(node, dict):
                continue
            number = "".join(
                str(node.get("number") or "").replace("\u00a7", "").split()
            )
            if number.count(".") >= 2:
                numbers.add(number)
            stack.extend(node.get("children") or [])
        return numbers

    @staticmethod
    def _ordinance_code_sections(
        disposition: str, numbers: Set[str]
    ) -> Tuple[List[str], List[str]]:
        """(sections of the current code, section numbers that are not).

        A disposition cites a provision ('§ 21.05.070D.13b.vii(C)',
        '§ 23.10.104.15.1A.1'); the section it belongs to is the longest
        leading part that is a section in the outline (21.05.070,
        23.10.104.15 -- Title 23 numbers run to five parts). One that
        matches nothing was repealed or renumbered after the ordinance,
        or is mistyped in the table ('§ 21.050.030B'); it is reported at
        the usual three parts. With no outline to check against, every
        number is reported in the first list at three parts.
        """
        current: List[str] = []
        gone: List[str] = []
        for cited in ORDINANCE_CITED_RE.findall(disposition):
            cited = cited.rstrip(".,;(")
            found = None
            for end in range(len(cited), 0, -1):
                if cited[:end] in numbers and not cited[end:end + 1].isdigit():
                    found = cited[:end]
                    break
            target = current
            if found is None:
                short = ORDINANCE_CODE_SECTION_RE.match(f"\u00a7{cited}")
                found = short.group(1) if short else cited
                if numbers:
                    target = gone
            if found and found not in current and found not in gone:
                target.append(found)
        return current, gone

    def _format_ordinance_lookup(
        self,
        base: str,
        asked: str,
        table: Optional[Dict[str, Any]],
        listing_node: Optional[Dict[str, Any]],
        updated: Optional[str],
        numbers: Optional[Set[str]] = None,
    ) -> tuple:
        blocks = (
            self._parse_ordinance_table(table.get("content") or "")
            if isinstance(table, dict)
            else []
        )
        numbers = numbers or set()
        # The table writes years before 2000 with two digits: "1985-8"
        # is listed as 85-8. Only tried when the number as given is absent.
        long_year = re.fullmatch(r"19(\d\d)-(\d+)", base)
        if long_year and not any(b["base"] == base for b in blocks):
            short = f"{long_year.group(1)}-{long_year.group(2)}"
            if any(b["base"] == short for b in blocks):
                base = short
        search_query = self._ordinance_search_query(base)

        listing = None
        if isinstance(listing_node, dict):
            for row in self._parse_ordinance_rows(
                listing_node.get("content") or ""
            ):
                if re.match(r"\d+-\d+", row["ordinance"]).group(0) == base:
                    listing = {
                        key: row[key]
                        for key in (
                            "ordinance",
                            "adopted",
                            "subject",
                            "chapters",
                            "supplement",
                        )
                    }
                    break

        source = covers = None
        if blocks:
            guid = str(table.get("id") or table.get("guid") or "")
            source = {
                "guid": guid,
                "ref": (table.get("number") or "").strip() or None,
                "title": (table.get("title") or "").strip() or None,
                "url": PUBLIC_URL.format(guid=guid),
            }
            covers = {
                "count": len({b["ordinance"] for b in blocks}),
                "first_ordinance": blocks[0]["ordinance"],
                "last_ordinance": blocks[-1]["ordinance"],
            }

        # A number listed in two places, or once per version, becomes one
        # match per printed form, rows in table order.
        matches: List[Dict[str, Any]] = []
        for block in blocks:
            if block["base"] != base:
                continue
            match = next(
                (m for m in matches if m["ordinance"] == block["ordinance"]),
                None,
            )
            if match is None:
                match = {"ordinance": block["ordinance"], "entries": []}
                matches.append(match)
            match["entries"].extend(block["entries"])
        total = sum(len(m["entries"]) for m in matches)
        room = MAX_ORDINANCE_ENTRIES
        for match in matches:
            entries = match["entries"]
            dated = next(
                (e for e in entries if e["effective_as_printed"]), None
            )
            match["effective_date"] = dated["effective_date"] if dated else None
            match["effective_as_printed"] = (
                dated["effective_as_printed"] if dated else None
            )
            match["entry_count"] = len(entries)
            sections: List[str] = []
            missing: List[str] = []
            for entry in entries:
                (
                    entry["sections"],
                    entry["sections_not_in_code"],
                ) = self._ordinance_code_sections(entry["disposition"], numbers)
                for number in entry["sections"]:
                    if number not in sections:
                        sections.append(number)
                for number in entry["sections_not_in_code"]:
                    if number not in missing:
                        missing.append(number)
            match["sections"] = sections
            match["sections_not_in_code"] = [
                n for n in missing if n not in sections
            ]
            match["entries"] = entries[:room]
            room -= len(match["entries"])
        shown = sum(len(m["entries"]) for m in matches)

        entries_caveats: List[tuple] = []
        lines: List[str] = []
        table_name = (
            f"Ordinance Disposition Table ({source['ref'] or 'guid ' + source['guid']}, "
            f"{source['url']})"
            if source
            else "Ordinance Disposition Table"
        )
        if matches:
            first = matches[0]
            when = (
                f", effective {first['effective_date'] or first['effective_as_printed']}"
                if first["effective_as_printed"]
                else ""
            )
            summary = (
                f"AO {first['ordinance']}{when}: {total} "
                f"entr{'y' if total == 1 else 'ies'} in the Ordinance "
                "Disposition Table"
                + (
                    f", under {len(matches)} forms of the number."
                    if len(matches) > 1
                    else "."
                )
            )
        elif listing:
            summary = (
                f"AO {listing['ordinance']} is in the list of recent "
                "ordinances, but the Ordinance Disposition Table does not "
                "list its sections."
            )
        else:
            summary = (
                f"AO {asked} is not listed in the Ordinance Disposition "
                "Table."
                if blocks
                else "The Ordinance Disposition Table could not be read."
            )
        lines.append(summary)
        if listing:
            lines.append("")
            lines.append(
                f"Adopted {listing['adopted'] or '(date not given)'}"
                + (f": {listing['subject']}" if listing["subject"] else "")
            )
            if listing["chapters"]:
                lines.append("Codified in: " + ", ".join(listing["chapters"]))

        for match in matches:
            lines.append("")
            head = match["ordinance"]
            if not head.startswith("Amd."):
                head = f"AO {head}"
            if match["effective_as_printed"]:
                head += (
                    " -- effective "
                    + (match["effective_date"] or match["effective_as_printed"])
                )
            lines.append(head)
            first_date = match["effective_as_printed"]
            labelled = False
            for entry in match["entries"]:
                if entry["ordinance_section"]:
                    label = f"ord. § {entry['ordinance_section']}"
                    labelled = True
                else:
                    # Ditto under a numbered row; a plain bullet where the
                    # table gives no ordinance sections at all (pre-2000).
                    label = "       \"" if labelled else "-"
                own_date = entry["effective_as_printed"]
                lines.append(
                    f"  {label}: "
                    + (f"{entry['action']} " if entry["action"] else "")
                    + (entry["disposition"] or "(nothing printed)")
                    + (
                        f"  [effective {entry['effective_date'] or own_date}]"
                        if own_date and own_date != first_date
                        else ""
                    )
                )
            if match["sections"]:
                lines.append(
                    f"  Sections named ({len(match['sections'])}): "
                    + ", ".join(match["sections"])
                )
            if match["sections_not_in_code"]:
                lines.append(
                    "  Named but not sections of the code today "
                    f"({len(match['sections_not_in_code'])}): "
                    + ", ".join(match["sections_not_in_code"])
                )

        if matches:
            entries_caveats.append(
                (
                    CAVEAT_ORDINANCE_TABLE_SCOPE,
                    f"Source: the publisher's {table_name}. It is a finding "
                    "aid, not the ordinance: it says where each section of "
                    "the ordinance went in the code when it was codified. "
                    "A section named here may have been amended, renumbered "
                    "or repealed since; read it with get_sections for what "
                    "the code says now, and check its history. Dates are "
                    "EFFECTIVE dates as printed, not adoption dates; a row "
                    "with no date of its own shares the date printed above "
                    "it. A row not marked added, repealed, amended or "
                    "renumbered changed the provision named, and the table "
                    "does not say how. 'Old' marks a section of the "
                    "superseded Title 21. A range (§ a—§ b) is listed by its "
                    "two ends only. A number listed as not a section of the "
                    "code today was repealed or renumbered after the "
                    "ordinance, or is mistyped in the table; get_sections "
                    "will not find it.",
                )
            )
            if len(matches) > 1:
                entries_caveats.append(
                    (
                        CAVEAT_ORDINANCE_SEVERAL_VERSIONS,
                        "The table lists this number under more than one "
                        "form ("
                        + ", ".join(m["ordinance"] for m in matches)
                        + "): (S) is a substitute version, 'am' an amended "
                        "one, and 'Amd. of' a later ordinance amending it. "
                        "They are shown separately, as the table lists "
                        "them.",
                    )
                )
            if shown < total:
                entries_caveats.append(
                    (
                        CAVEAT_ORDINANCE_ENTRIES_TRUNCATED,
                        f"INCOMPLETE: {shown} of {total} entries are shown. "
                        f"The rest are in the table itself: {source['url']}.",
                    )
                )
        elif blocks:
            newer = (
                " The list of recent ordinances has it (above), so it is "
                "probably too new for this table: its last entry is AO "
                f"{covers['last_ordinance']}."
                if listing
                else f" The table runs from AO {covers['first_ordinance']} "
                f"to AO {covers['last_ordinance']} and leaves out "
                "ordinances that were repealed, superseded or not of a "
                "general and permanent nature (budgets, rezonings, one-time "
                "actions), so this does not show the ordinance does not "
                "exist. Check the number: from 1977 it is year then "
                "number ('85-8' is no. 8 of 1985; four-digit years from "
                "2000), and for 1975-76 number then year ('13-75')."
            )
            entries_caveats.append(
                (
                    CAVEAT_ORDINANCE_NOT_IN_TABLE,
                    f"The {table_name} has no row for {base}.{newer} To "
                    "find the sections whose history cites it, call "
                    f"search_code with {search_query}; a recent ordinance "
                    "may also come back from that search as a PDF of the "
                    "ordinance itself, which read_attachment reads. For "
                    "ordinances adopted recently, see recent_ordinances.",
                )
            )
        else:
            why = (
                "This code's outline has no 'Ordinance Disposition Table' "
                "section."
                if table is None
                else "The Ordinance Disposition Table was found but no rows "
                "could be read from it; its layout may have changed."
            )
            entries_caveats.append(
                (
                    CAVEAT_ORDINANCE_TABLE_UNAVAILABLE,
                    f"{why} This says nothing about the ordinance. To find "
                    "the sections whose history cites it, call search_code "
                    f"with {search_query}.",
                )
            )

        caveats, caveat_lines = self._caveats(entries_caveats)
        text = "\n".join(lines + [""] + caveat_lines).strip()
        if updated:
            text += f"\n\nPublished code last updated {updated}."
        return text, {
            "ordinance": asked,
            "found": bool(matches),
            "matches": matches,
            "total_entries": total,
            "listing": listing,
            "search_query": search_query,
            "covers": covers,
            "source": source,
            "code_last_updated": updated,
            "summary": summary,
            "caveats": caveats,
        }

    def _format_customer(self, data: Dict[str, Any]) -> tuple:
        results = data.get("results", data) if isinstance(data, dict) else {}
        if not isinstance(results, dict):
            results = {}

        def val(key: str) -> Any:
            # "" means absent in this API (govsubtype, mapUrl); report null.
            v = results.get(key)
            return None if v == "" else v

        counties = results.get("counties")
        if isinstance(counties, list):
            counties = [str(c) for c in counties if c not in (None, "")]
        else:
            counties = None
        population = val("population")
        if not isinstance(population, (int, str)) or isinstance(
            population, bool
        ):
            population = None
        pdf_only = val("pdfOnly")
        if not isinstance(pdf_only, (bool, str)):
            pdf_only = None

        def text(key: str) -> Optional[str]:
            v = val(key)
            return None if v is None else str(v)

        structured = {
            "customer_id": text("id"),
            "name": text("name"),
            "short_name": text("shortName"),
            "state": text("state"),
            "government_type": text("govtype"),
            "counties": counties or None,
            "population": population,
            "last_updated": text("updatedDate"),
            "pdf_only": pdf_only,
            "url": text("url"),
        }

        fields = [
            ("Name", structured["name"]),
            ("Short name", structured["short_name"]),
            ("Customer ID", structured["customer_id"]),
            ("State", structured["state"]),
            ("Government type", structured["government_type"]),
            (
                "Counties",
                ", ".join(counties) if counties else None,
            ),
            ("Population", population),
            ("Last updated", structured["last_updated"]),
            ("PDF only", pdf_only),
            ("URL", structured["url"]),
        ]
        text_out = "\n".join(
            f"{label}: {value}" for label, value in fields if value is not None
        )
        summary = (
            f"{structured['name'] or structured['customer_id'] or 'Code library'}"
            + (
                f", code last updated {structured['last_updated']}."
                if structured["last_updated"]
                else "."
            )
        )
        structured["summary"] = summary
        structured["caveats"] = []
        return text_out, structured

    # ── Small utilities ─────────────────────────────────────────────────

    @staticmethod
    def _result_url(result: Dict[str, Any], guid: str) -> str:
        """The link to cite for a search hit.

        Two different situations, and using one rule for both gets one of
        them wrong:

        * An ordinary code SECTION. The API's `url` is a live.ecode360.com
          address carrying the transient search-highlight query string
          (?highlight=fence#8682677). That is a link to this search, not to
          the law, so the canonical https://ecode360.com/<guid> form is the
          better citation and is what gets rendered.

        * A DOCUMENT hit (type "doc" / pdf true). Here the API's `url` is the
          PDF itself, e.g.
          live.ecode360.com/documents/AN6998/public/753252538.pdf. There is
          no code section behind it, so synthesizing
          https://ecode360.com/<guid> would cite a different resource than
          the one that actually matched -- a citation that looks right and
          is not. Use what the API returned.
        """
        url = (result.get("url") or "").strip()
        is_document = bool(result.get("pdf")) or result.get("type") == "doc"
        if is_document and url:
            return url
        return PUBLIC_URL.format(guid=guid) if guid else url

    @staticmethod
    def _guid_from_url(url: str) -> str:
        """Extract the trailing guid from an ecode360.com URL."""
        if not url:
            return ""
        return url.rstrip("/").rsplit("/", 1)[-1]

    @staticmethod
    def _strip_html(text: str) -> str:
        # Tags out, then entities: search snippets of PDF documents carry
        # "&nbsp;" and "&amp;" (seen live in find_references 21.05.010).
        plain = html.unescape(re.sub(r"<[^>]+>", "", text or ""))
        return plain.replace(" ", " ").strip()


def _load_env_file(path: str) -> None:
    """Load simple KEY=VALUE lines from a local env file into os.environ.

    Only sets vars that are not already present, so real environment / Lambda
    env vars always win. Silently no-ops if the file is missing.
    """
    try:
        # utf-8-sig tolerates a BOM (e.g. files saved by Windows Notepad).
        with open(path, "r", encoding="utf-8-sig") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
    except FileNotFoundError:
        logger.warning("secrets_file not found: %s", path)
    except Exception as e:
        logger.warning("failed reading secrets_file %s: %s", path, e)
