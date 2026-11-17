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
import io
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple
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

# Cap on the rendered section text in one response. It was 14,000 until
# 2026-11-17: prod logs showed 5 of 151 attorney calls cut short (the DUI
# section § 9.28.020 is 27,585 characters, the fine schedule § 9.48.130
# 18,745) with no follow-up read, and a client that shows the model only
# the text block never sees structuredContent.text. 40,000 returns every
# section seen cut in those logs whole; anything longer is read on with
# get_section's `offset`.
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

# ── Batch reads and definitions ─────────────────────────────────────────
#
# get_sections exists because models read a chapter one get_section at a
# time (79 bursts in 14 days of prod logs, the longest 19 calls), each a
# separate Lambda invocation.
MAX_BATCH_SECTIONS = 10
# Upstream requests in flight at once for one tool call.
BATCH_CONCURRENCY = 5
# Rendered text in a batch: the sections read share BATCH_TOTAL_CHARS
# evenly, and none gets less than BATCH_SECTION_CHARS. Two sections get
# 40,000 each, ten get 8,000 each. A section cut short names the
# get_section offset to read on from; the full text is in structuredContent.
BATCH_TOTAL_CHARS = 80_000
BATCH_SECTION_CHARS = 8000

# eCode360 publishes each defined term as its own node, numbered as the
# defining section plus an index in braces: "8.05.015A{9}", titled with the
# term in quotes. title:"term" finds them (verified live 2026-11-15).
DEFINITION_NUMBER_RE = re.compile(r"^(.*)\{\d+\}$")
MAX_DEFINITIONS = 8
MAX_TERM_CHARS = 100

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
CAVEAT_NO_TEXT_LAYER = "no_text_layer"

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
# the caller wrote them. Measured on the attorney queries (2026-11-13): the
# first two ANDed returned results for 4 of 5, while ANDing all of them, or
# the "most specific" (longest) phrases, returned 0 for most.
OR_CAVEAT_SUGGEST_TERMS = 2

# One search token: an optionally +/- prefixed, optionally field-qualified
# quoted phrase (with any ~N suffix), a parenthesis, or a bare word.
_QUERY_TOKEN_RE = re.compile(
    r'[+-]?(?:\w+:)?"[^"]*"(?:~\d+)?|[()]|[^\s()"]+'
)
_QUERY_OPERATORS = frozenset({"and", "or", "not", "&&", "||"})
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
# headings containing "9" and `x AND title:9*` matches nothing (seen three
# times from attorney sessions, 2026-11-15/16). There is no Title filter.
TITLE_NUMBER_RE = re.compile(r'(?i)\btitle:"?\d')

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
        "rows": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "guid": {"type": "string"},
                    "ref": {"type": ["string", "null"]},
                    "title": {"type": ["string", "null"]},
                    "snippet": {"type": ["string", "null"]},
                    "url": {
                        "type": "string",
                        "description": (
                            "As returned by the API where it supplies one "
                            "(document hits link to a PDF on "
                            "live.ecode360.com); otherwise the canonical "
                            "https://ecode360.com/<guid> form."
                        ),
                    },
                    "is_pdf": {
                        "type": "boolean",
                        "description": (
                            "The hit is a PDF document, not a code section: "
                            "pass its `url` to read_attachment to read it. "
                            "get_section cannot open it."
                        ),
                    },
                },
                "required": ["guid", "url", "is_pdf"],
            },
        },
        "caveats": _CAVEAT_SCHEMA,
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
        "has_attachment": {"type": "boolean"},
        "attachments": _ATTACHMENTS_SCHEMA,
        "is_pdf": {"type": "boolean"},
        "caveats": _CAVEAT_SCHEMA,
    },
    "required": [
        "guid",
        "url",
        "text",
        "has_attachment",
        "is_pdf",
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
                    "has_attachment": {"type": "boolean"},
                    "attachments": _ATTACHMENTS_SCHEMA,
                    "is_pdf": {"type": "boolean"},
                    "caveats": _CAVEAT_SCHEMA,
                },
                "required": ["requested", "found", "error", "caveats"],
            },
        },
        "summary": {"type": "string"},
        "caveats": _CAVEAT_SCHEMA,
    },
    "required": ["sections", "summary", "caveats"],
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
                "Pass as `page` to continue; null when the last page of "
                "the document has been returned."
            ),
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
        "summary": {"type": "string"},
        "caveats": _CAVEAT_SCHEMA,
    },
    "required": ["node", "children", "summary", "caveats"],
}

TOC_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "node": _NODE_SCHEMA,
        "children": {"type": "array", "items": _NODE_SCHEMA},
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
    plugin_version = "1.6.0"

    def __init__(self, config: Dict[str, Any]) -> None:
        super().__init__(config)
        self.plugin_config: Optional[EcodePluginConfig] = None
        self.client: Optional[httpx.AsyncClient] = None
        # Serialises the first fetch of a cached document so a batch of
        # chapter numbers triggers one table-of-contents download, not ten.
        self._static_lock = asyncio.Lock()
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
                    "municipality; a trailing * is a wildcard (liab*); "
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
                    "'Title 9', 'Chapter 9.28', or a guid) to keep only "
                    "hits under that title, chapter or section. The server "
                    "checks up to 200 hits for the query per call and "
                    "returns those inside the scope; if the query has "
                    "more, a scope_scan_partial note says so and `page` "
                    "moves to the next 200. PDF documents are left out of "
                    "a scoped search.\n\n"
                    "No results means this code does not contain that "
                    "text, which can itself be the answer (the rule may be "
                    "state law, repealed, or worded differently); broaden "
                    "the words or browse with get_table_of_contents before "
                    "concluding."
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
                                "chapter or section: 'Title 9', 'Chapter "
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
                    f"Get the top-level table of contents of {city}'s municipal code "
                    "(titles/chapters down to the section level, without body text). "
                    "Each entry has a guid you can pass to browse_structure or "
                    "get_section. Start here to orient before drilling in."
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
                    "then walk down using child guids. Use this to navigate the "
                    "outline; use get_section to read actual text."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "guid": {
                            "type": "string",
                            "description": "Node guid, or 'ROOT' for the top",
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
                    f"Up to {MAX_CONTENT_CHARS:,} characters of text are "
                    "returned per call; a longer section says so and "
                    "gives the `offset` to call again with for the rest "
                    "-- read it all before summarizing the section."
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
                    "With layout=true (the default) columns are kept "
                    "aligned with spaces: to read a table cell, line the "
                    "mark up with the column headers at the top of the "
                    "page, and tell the user to confirm against the PDF. "
                    "Only eCode360 attachment urls for this code are "
                    "accepted."
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
                    "code text is."
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
                query = (arguments.get("query") or "").strip()
                if not query:
                    raise ToolInputError(
                        "query is required -- pass the words or phrase to search "
                        "the municipal code for (e.g. 'dog license')."
                    )
                page = self._int_arg(arguments, "page", 0)
                if page < 0:
                    raise ToolInputError(
                        f"page must be 0 or greater (got {page}); results are "
                        "0-indexed, 20 per page."
                    )
                within = arguments.get("within")
                if within is not None and str(within).strip():
                    return self._ok(
                        *await self._scoped_search(
                            query, page, str(within).strip()
                        )
                    )
                data = await self._search(query, page)
                return self._ok(*self._format_search(data, query, page))

            if tool_name == "get_table_of_contents":
                data = await self._get_toc()
                return self._ok(*self._format_toc(data))

            if tool_name == "browse_structure":
                guid = self._guid_arg(arguments, default="ROOT")
                data = await self._get_json(
                    self._cust_path(f"/structure/{quote(guid, safe='')}")
                )
                # structure responses wrap the node in an {error, results} envelope
                node = data.get("results", data) if isinstance(data, dict) else data
                return self._ok(*self._format_structure(node))

            if tool_name == "get_section":
                offset = self._int_arg(arguments, "offset", 0)
                if offset < 0:
                    raise ToolInputError(
                        f"offset must be 0 or greater (got {offset}); it is "
                        "the character position to read on from, as given "
                        "in a rendered_text_truncated note."
                    )
                data, resolved_from = await self._fetch_section(
                    arguments.get("guid")
                )
                return self._ok(
                    *self._format_content(
                        data, resolved_from=resolved_from, offset=offset
                    )
                )

            if tool_name == "get_sections":
                requested, merged = self._sections_arg(arguments)
                outcomes = await self._fetch_sections(requested)
                return self._ok(
                    *self._format_sections(requested, outcomes, merged)
                )

            if tool_name == "find_definition":
                term = self._term_arg(arguments)
                data = await self._search(f'title:"{term}"', 0)
                hits = self._definition_hits(data, term)
                bodies = await self._fetch_definition_bodies(
                    hits[:MAX_DEFINITIONS]
                )
                return self._ok(
                    *self._format_definitions(term, data, hits, bodies)
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
        """
        raw = args.get(name, default)
        try:
            return int(raw)
        except (TypeError, ValueError) as exc:
            raise ToolInputError(
                f"{name} must be an integer (got {raw!r})"
            ) from exc

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
                # where a guid would be; models pass it on (seen in prod
                # for Table 21.05-1) and used to be told it "looks like a
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
        parsed = self._parse_ref(raw)
        if parsed:
            kind, number, charter_only = parsed
            shown = " ".join(str(raw).split())
            guid = await self._resolve_reference(
                kind, number, charter_only, shown
            )
            resolved_from: Optional[str] = (
                number if kind is None and not charter_only else shown
            )
        else:
            guid = self._guid_arg({"guid": raw})
            resolved_from = None
        data = await self._get_json(
            self._cust_path(f"/code/content/{quote(guid, safe='')}")
        )
        return data, resolved_from

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
        self, hits: List[Dict[str, Any]]
    ) -> List[Optional[str]]:
        """Full text per definition, or None where the fetch failed.

        The search snippet is elided mid-sentence, so the body is read from
        the content endpoint. A failed fetch must not sink the lookup: the
        caller falls back to the snippet and says so.
        """
        gate = asyncio.Semaphore(BATCH_CONCURRENCY)

        async def one(guid: str) -> Optional[str]:
            async with gate:
                try:
                    node = await self._get_json(
                        self._cust_path(
                            f"/code/content/{quote(guid, safe='')}"
                        )
                    )
                except (EcodeAPIError, ToolInputError) as e:
                    logger.warning(
                        "definition body fetch failed for %s: %s", guid, e
                    )
                    return None
                body = self._render_content_node(
                    node, depth=0, budget=[self._UNCAPPED]
                )
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
        match = REF_WITH_KIND_RE.match(raw.strip())
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
        if parsed is not None:
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
        elif GUID_RE.match(within) and within.upper() != "ROOT":
            guid = within
        else:
            raise ToolInputError(
                f"within {within!r} is not a title, chapter, section or "
                "guid." + hint
            )

        in_scope: set = set()
        all_guids: set = set()
        by_number: Dict[str, set] = {}
        found: List[Dict[str, Any]] = []

        def walk(node: Any, inside: bool) -> None:
            if not isinstance(node, dict):
                return
            node_guid = str(node.get("guid") or "")
            if node_guid == guid and not found:
                found.append(node)
                inside = True
            if node_guid:
                all_guids.add(node_guid)
                if inside:
                    in_scope.add(node_guid)
                if node.get("type") == "section":
                    number = self._norm_number(node.get("number"))
                    if number:
                        by_number.setdefault(number, set()).add(node_guid)
            for child in node.get("children") or []:
                walk(child, inside)

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
        }

    def _hit_in_scope(
        self, hit: Dict[str, Any], scope: Dict[str, Any]
    ) -> Optional[bool]:
        """True / False when the hit can be placed, None when it cannot
        (a PDF document, or a node unknown to the outline)."""
        if hit.get("pdf") or hit.get("type") == "doc":
            return None
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
        return self._format_scoped_search(
            query, page, scope, hits, query_total, max_pages
        )

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
        unknown = 0
        for hit in hits:
            placed = self._hit_in_scope(hit, scope)
            if placed is None:
                if hit.get("pdf") or hit.get("type") == "doc":
                    documents += 1
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
                    f"{documents} PDF document hit(s)" if documents else "",
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
                    "were left out. Search without `within` to see them.",
                )
            )
        title_caveat = self._title_number_caveat(query)
        if title_caveat:
            entries.append(title_caveat)

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
            raise EcodeAPIError(
                f"eCode360 did not answer within this call's "
                f"{TOOL_CALL_BUDGET_S:g}s budget. The service may be slow "
                "right now; try again shortly.",
                504,
            )
        if remaining < self.plugin_config.timeout:
            return remaining
        return None

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
                resp = await self.client.get(path, **kwargs)
                resp.raise_for_status()
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
                if status >= 500 and self._may_retry(attempt, started):
                    await self._backoff(attempt, path, f"HTTP {status}")
                    continue
                if status in (401, 403):
                    # Our credentials, not the caller's request. This is
                    # where a bad or rotated key shows up now that
                    # initialize() no longer probes.
                    raise EcodeAPIError(
                        f"eCode360 rejected this server's credentials "
                        f"(HTTP {status}) for {path}: {detail}. This is a "
                        "server configuration problem (check "
                        "ECODE_API_KEY / ECODE_API_SECRET), not a problem "
                        "with the request.",
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
                raise EcodeAPIError(
                    f"eCode360 returned a non-JSON response for {path} "
                    f"(content-type "
                    f"{resp.headers.get('content-type', '?')}).",
                    502,
                ) from e

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
        async with self._static_lock:
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

    async def _search(self, query: str, page: int) -> Dict[str, Any]:
        return await self._get_json(
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
            if is_pdf:
                lines.append(
                    "   (PDF document — not code text; pass the link below "
                    "to read_attachment)"
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
        # of a long query usually returns nothing (the attorney's 6-term
        # notice-of-claim query -> 0), while its first two -> 25 with
        # § 1.50.010 near the top.
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
                "This exact text does not appear in the published "
                f"municipal code of the {city} (eCode360 library "
                f"{library}). That can be a real answer: the rule may be "
                "state law (Alaska Statutes / Alaska Administrative Code), "
                "in a repealed chapter, or worded differently here. Next "
                "steps: try fewer or broader words, put OR between "
                "synonyms, drop the quotes, or use browse_structure / "
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
        return entries

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
        body = self._render_children(raw_children, depth=0)
        children = self._structured_children(raw_children)
        if not children:
            entries.append(
                (
                    CAVEAT_NODE_HAS_NO_CHILDREN,
                    "The outline came back empty; use browse_structure "
                    "with guid='ROOT' to walk the tree directly.",
                )
            )

        caveats, caveat_lines = self._caveats(entries)
        text = "\n".join(
            caveat_lines
            + header
            + (body if body else ["(no entries)"])
        )
        summary = f"{len(children)} top-level entr(y/ies) under {title}."
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

    def _format_structure(self, data: Dict[str, Any]) -> tuple:
        guid = str(data.get("guid") or data.get("id", "") or "ROOT")
        url = PUBLIC_URL.format(guid=guid) if guid and guid != "ROOT" else None
        lines = [
            f"{data.get('label', '')} {data.get('number', '')} "
            f"{data.get('title', '')}".strip(),
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
        if not children:
            entries.append(
                (
                    CAVEAT_NODE_HAS_NO_CHILDREN,
                    "This node has no children; it is probably a leaf "
                    "section, so call get_section with its guid to read the "
                    "text.",
                )
            )

        lines.extend(body or ["  (none — this may be a leaf section; "
                              "use get_section to read its text)"])
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
            "summary": summary,
            "caveats": caveats,
        }

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

    def _render_children(self, children: List[Any], depth: int) -> List[str]:
        """Render a list of child nodes (each a dict, or a bare guid string)."""
        out: List[str] = []
        indent = "  " * (depth + 1)
        for child in children:
            if isinstance(child, str):
                out.append(f"{indent}- guid {child}")
                continue
            guid = child.get("guid") or child.get("id", "")
            # Include the node kind (Title / Part / Chapter / Litem). Several
            # real nodes carry an empty `number`, so without it a child can
            # render as nothing but a title and a guid, losing where it sits
            # in the hierarchy -- which is the entire reason to call this.
            parts = [
                child.get("label", ""),
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
        total_chars = self._content_chars(data)
        too_large = total_chars > max_structured_chars

        # Render once, whole, for structuredContent. The text block shows a
        # window of that same rendering, so a caller can read on from the
        # character where one response stopped (`offset`).
        full_body = self._render_content_node(
            data,
            depth=0,
            budget=[max_structured_chars if too_large else self._UNCAPPED],
        )
        if offset and offset >= len(full_body):
            raise ToolInputError(
                f"offset {offset} is past the end of this section, which "
                f"is {len(full_body):,} characters long. Use the offset "
                "given in the rendered_text_truncated note, or leave it "
                "out to read from the start."
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
                    f"This node is a whole chapter or title of about "
                    f"{total_chars:,} characters -- too large to return "
                    f"in one piece. The text below is cut at "
                    f"{max_chars:,} characters and structuredContent.text "
                    f"at {max_structured_chars:,}. Call browse_structure "
                    f"with guid {guid} to list its sections, then read "
                    "the ones you need with get_sections.",
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

        caveats, caveat_lines = self._caveats(entries)

        head = [
            " ".join(p for p in [ref or "", title or ""] if p).strip()
            or "(untitled section)",
            f"guid: {guid}   type: {data.get('type', '')}",
            f"link: {url}" if url else "",
        ]
        if images:
            head.append(f"images: {len(images)} (relative links on ecode360.com)")
        for item in attachment_list:
            head.append(f"attachment: {item['name']} — {item['url']}")
        if attachment_list:
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
            "has_attachment": has_attachment,
            "attachments": attachment_list,
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
        refuse = (
            "url must be an eCode360 PDF attachment for this code, e.g. "
            "https://live.ecode360.com/attachment/<id>/<name>.pdf or "
            "https://ecode360.com/documents/"
            f"{self.plugin_config.customer_id}/<name>.pdf (got {raw.strip()!r}). "
            "Use the url exactly as search_code or get_section returned it."
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
            or not (ATTACHMENT_PATH_RE.match(path) or document_re.match(path))
        ):
            raise ToolInputError(refuse)
        return f"https://{host}{quote(path)}"

    async def _download_attachment(self, url: str) -> bytes:
        """Fetch a validated attachment URL. No API credentials are sent,
        and a redirect is an error rather than something to follow."""
        cached = _ATTACHMENT_CACHE.get(url)
        if cached is not None:
            return cached
        timeout = self._request_timeout() or self.plugin_config.timeout
        try:
            async with httpx.AsyncClient(
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
        if next_page:
            entries.append(
                (
                    CAVEAT_MORE_ATTACHMENT_PAGES,
                    f"Showing {span} of {page_count}. Call read_attachment "
                    f"again with page={next_page} to continue.",
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
            blocks.append(f"=== {raw} ===\n{text}")
            sections.append(
                {"requested": raw, "found": True, "error": None, **structured}
            )

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

    def _format_definitions(
        self,
        term: str,
        data: Dict[str, Any],
        hits: List[Dict[str, Any]],
        bodies: List[Optional[str]],
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
        heading = f"{ref or ''} {title or ''}".lower()
        for marker in INACTIVE_TITLE_MARKERS:
            if marker in heading:
                return marker
        return None

    def _render_content_node(
        self, node: Dict[str, Any], depth: int, budget: List[int]
    ) -> str:
        if budget[0] <= 0:
            return ""
        indent = "  " * depth
        parts: List[str] = []
        heading = " ".join(
            p for p in [node.get("number", ""), node.get("title", "")] if p
        ).strip()
        if depth > 0 and heading:
            parts.append(f"{indent}{heading}")
        text = (node.get("content") or "").strip()
        if text:
            if len(text) > budget[0]:
                text = text[: budget[0]] + " …[truncated]"
            budget[0] -= len(text)
            wrapped = "\n".join(f"{indent}{ln}" for ln in text.splitlines())
            parts.append(wrapped)
        for child in node.get("children", []) or []:
            if budget[0] <= 0:
                parts.append(f"{indent}…[output truncated; fetch child guids "
                             "individually with get_section]")
                break
            if isinstance(child, dict):
                parts.append(self._render_content_node(child, depth + 1, budget))
        return "\n".join(p for p in parts if p)

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
        return re.sub(r"<[^>]+>", "", text or "").strip()


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
