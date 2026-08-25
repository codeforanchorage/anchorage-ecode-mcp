"""eCode360 (General Code) municipal code plugin for OpenContext.

Provides conversational, read-only access to a single municipality's published
municipal code hosted on the eCode360 platform (the "EcodeGateway" API at
https://api.ecode360.com). The municipality is fixed per deployment by
``customer_id`` in config (e.g. ``AN6998`` for the Municipality of Anchorage),
so the tools never ask the model for a customer id.

API reference (v2.7): two API-key headers (``api-key`` / ``api-secret``) on
every request; all endpoints are GET under ``/v1`` and return JSON.
"""

import logging
import os
import re
from typing import Any, Dict, List, Optional
from urllib.parse import quote

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

# Cap rendered section text so a deeply nested chapter cannot blow up a response.
MAX_CONTENT_CHARS = 14000

# Node guids are opaque alphanumeric identifiers (in this library, all
# numeric -- e.g. 49401992). ROOT is the reserved alias for the top of the
# structure tree. Section NUMBERS (21.05.010) are a different namespace and
# are never accepted as guids by the API.
GUID_RE = re.compile(r"^[A-Za-z0-9]+$")

# eCode360 statuses that mean the CALLER asked for something wrong rather
# than the server failing: 404 (no such node) and 400 (malformed request).
# 401/403 are our credentials and 5xx is upstream -- both genuine faults.
CALLER_ERROR_STATUSES = frozenset({400, 404})

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
                            "The hit is a PDF document, so get_section will "
                            "return an attachment rather than code text."
                        ),
                    },
                },
                "required": ["guid", "url", "is_pdf"],
            },
        },
        "caveats": _CAVEAT_SCHEMA,
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
                "clipped: the markdown block caps its rendering for "
                "readability, but truncating the machine-readable half would "
                "defeat its purpose."
            ),
        },
        "has_attachment": {"type": "boolean"},
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
    plugin_version = "1.0.0"

    def __init__(self, config: Dict[str, Any]) -> None:
        super().__init__(config)
        self.plugin_config: Optional[EcodePluginConfig] = None
        self.client: Optional[httpx.AsyncClient] = None

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

            # Verify connectivity + credentials against the (cheap) customer
            # metadata endpoint. 401/403 here means a bad key or no access.
            resp = await self.client.get(self._cust_path(""))
            resp.raise_for_status()

            self._initialized = True
            logger.info(
                "eCode plugin initialized for %s (customer %s)",
                self.plugin_config.city_name,
                self.plugin_config.customer_id,
            )
            return True

        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            hint = (
                " (check ECODE_API_KEY / ECODE_API_SECRET)"
                if status in (401, 403)
                else ""
            )
            logger.error(
                "eCode plugin init failed: HTTP %s%s: %s",
                status,
                hint,
                e.response.text[:500],
            )
            return False
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
                    f"Full-text search of {city}'s municipal code. Returns up to 20 "
                    "matching sections per page, each with its title, reference "
                    "number, a highlighted snippet, and a guid. Use the guid with "
                    "get_section to read the full text. Supports eCode360 advanced "
                    'syntax, e.g. proximity: "city council"~3. Page through results '
                    "with the page argument (0-indexed)."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Search term or advanced-syntax query",
                        },
                        "page": {
                            "type": "integer",
                            "description": "0-indexed result page (20 results/page)",
                            "default": 0,
                            "minimum": 0,
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
                    "get_table_of_contents, or browse_structure. Also surfaces any "
                    "images/attachments and the public ecode360.com link."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "guid": {
                            "type": "string",
                            "description": "Section/node guid (e.g. '8682677')",
                        },
                    },
                    "required": ["guid"],
                },
                output_schema=SECTION_OUTPUT_SCHEMA,
                annotations=read_only,
            ),
            ToolDefinition(
                name="get_customer_info",
                description=(
                    f"Get metadata about {city}'s code library: official name, "
                    "state, government type, last-updated date, and public URL."
                ),
                input_schema={"type": "object", "properties": {}},
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
                guid = self._guid_arg(arguments)
                data = await self._get_json(
                    self._cust_path(f"/code/content/{quote(guid, safe='')}")
                )
                return self._ok(*self._format_content(data))

            if tool_name == "get_customer_info":
                data = await self._get_json(self._cust_path(""))
                return self._ok(self._format_customer(data))

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
            return self._err(str(e) or "Tool execution failed")

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
            raise ToolInputError(
                f"{name} must be an alphanumeric node id such as '49401992' "
                f"(got {guid!r})." + hint + " Use search_code to find the "
                "section and read its guid from the result."
            )
        return guid

    # ── HTTP helpers ────────────────────────────────────────────────────

    def _cust_path(self, suffix: str) -> str:
        """Path under /v1/customer/{customer_id}{suffix}."""
        cust = self.plugin_config.customer_id
        return f"/customer/{cust}{suffix}"

    async def _get_json(
        self, path: str, params: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        try:
            resp = await self.client.get(path, params=params)
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            detail = self._extract_api_error(e.response)
            if status in CALLER_ERROR_STATUSES:
                # The caller named something that is not in this code library.
                # A caller mistake, so it must not log as a server fault.
                raise EcodeNotFoundError(
                    f"eCode360 has no such entry (HTTP {status}): {detail}. "
                    "Check the guid -- use search_code to look the section up "
                    "and read its guid from the result.",
                    status,
                ) from e
            raise EcodeAPIError(
                f"eCode API error (HTTP {status}) for {path}: {detail}",
                status,
            ) from e
        return resp.json()

    async def _search(self, query: str, page: int) -> Dict[str, Any]:
        return await self._get_json(
            self._cust_path("/search"), params={"query": query, "page": page}
        )

    async def _get_toc(self) -> Dict[str, Any]:
        """TOC endpoint, falling back to the structure ROOT if /code/toc is
        not available for this customer (the endpoint is absent from the older
        OpenAPI spec, so treat 404 as 'use structure instead')."""
        try:
            return await self._get_json(self._cust_path("/code/toc"))
        except (EcodeAPIError, EcodeNotFoundError) as e:
            # A 404 here means the endpoint is absent for this customer, not
            # that the caller asked for the wrong thing -- so it is handled,
            # never surfaced. Branch on the status code rather than string-
            # matching the message, which is what this used to do.
            if e.status_code == 404:
                logger.info("toc endpoint 404; falling back to structure ROOT")
                data = await self._get_json(self._cust_path("/structure/ROOT"))
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

        caveats, caveat_lines = self._caveats(entries)
        return "\n".join([summary] + caveat_lines), {
            "query": query,
            "page": page,
            "total_count": total_count,
            "summary": summary,
            "rows": [],
            "caveats": caveats,
        }

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

        rows: List[Dict[str, Any]] = []
        lines = [
            f"{total_count if total_count is not None else len(results)} "
            f"result(s) for {query!r} "
            f"(page {page + 1} of {max_pages}, 20/page):",
            "",
        ]
        for i, r in enumerate(results, 1 + page * 20):
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
                lines.append("   (PDF document — the body is a PDF, not code text)")
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

    def _format_content(self, data: Dict[str, Any]) -> tuple:
        guid = str(data.get("id") or data.get("guid", "") or "")
        ref = (data.get("number") or "").strip() or None
        title = (data.get("title") or "").strip() or None
        url = PUBLIC_URL.format(guid=guid) if guid else ""

        images = data.get("images") or []
        attachments = data.get("attachments") or {}
        is_pdf = bool(data.get("pdf"))
        has_attachment = bool(attachments) or bool(images)

        # Render twice: once capped for the human block, once uncapped for
        # structuredContent.
        display_body = self._render_content_node(
            data, depth=0, budget=[MAX_CONTENT_CHARS]
        )
        full_body = self._render_content_node(
            data, depth=0, budget=[self._UNCAPPED]
        )

        entries: List[tuple] = []

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
        if len(full_body) > len(display_body):
            entries.append(
                (
                    CAVEAT_RENDERED_TEXT_TRUNCATED,
                    f"The text below is capped at {MAX_CONTENT_CHARS:,} "
                    f"characters ({len(full_body):,} in full). The complete "
                    f"body is in structuredContent.text, or fetch child "
                    f"guids individually with get_section.",
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
        if attachments:
            head.append(f"attachments: {', '.join(attachments.keys())}")
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
            "is_pdf": is_pdf,
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

    def _format_customer(self, data: Dict[str, Any]) -> str:
        results = data.get("results", data) if isinstance(data, dict) else {}
        if not isinstance(results, dict):
            results = {}
        fields = [
            ("Name", results.get("name")),
            ("Short name", results.get("shortName")),
            ("Customer ID", results.get("id")),
            ("State", results.get("state")),
            ("Government type", results.get("govtype")),
            ("Population", results.get("population")),
            ("Last updated", results.get("updatedDate")),
            ("PDF only", results.get("pdfOnly")),
            ("URL", results.get("url")),
        ]
        return "\n".join(
            f"{label}: {value}" for label, value in fields if value not in (None, "")
        )

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
