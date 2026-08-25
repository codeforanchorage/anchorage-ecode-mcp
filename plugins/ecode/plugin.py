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

    def get_tools(self) -> List[ToolDefinition]:
        city = (
            self.plugin_config.city_name if self.plugin_config else "the municipality"
        )
        read_only = {"readOnlyHint": True, "openWorldHint": True}
        return [
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
                return self._ok(self._format_search(data))

            if tool_name == "get_table_of_contents":
                data = await self._get_toc()
                return self._ok(self._format_toc(data))

            if tool_name == "browse_structure":
                guid = self._guid_arg(arguments, default="ROOT")
                data = await self._get_json(
                    self._cust_path(f"/structure/{quote(guid, safe='')}")
                )
                # structure responses wrap the node in an {error, results} envelope
                node = data.get("results", data) if isinstance(data, dict) else data
                return self._ok(self._format_structure(node))

            if tool_name == "get_section":
                guid = self._guid_arg(arguments)
                data = await self._get_json(
                    self._cust_path(f"/code/content/{quote(guid, safe='')}")
                )
                return self._ok(self._format_content(data))

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
    def _ok(text: str) -> ToolResult:
        return ToolResult(content=[{"type": "text", "text": text}], success=True)

    @staticmethod
    def _err(message: str) -> ToolResult:
        return ToolResult(content=[], success=False, error_message=message)

    # ── Formatters ──────────────────────────────────────────────────────

    def _format_search(self, data: Dict[str, Any]) -> str:
        results = data.get("results", []) or []
        total = data.get("totalResultCount", len(results))
        page = self._as_int(data.get("page", 0))
        max_pages = self._as_int(data.get("maxPages", 1), default=1)
        if not results:
            return f"No results for query {data.get('query', '')!r}."

        lines = [
            f"{total} result(s) for {data.get('query', '')!r} "
            f"(page {page + 1} of {max_pages}, 20/page):",
            "",
        ]
        for i, r in enumerate(results, 1 + page * 20):
            # Search results key the node id as `id`; the snippet as `text`;
            # and both title and snippet may contain <em> highlight markup.
            guid = r.get("id") or self._guid_from_url(r.get("url", ""))
            snippet = self._strip_html(r.get("text") or r.get("snippet", ""))
            title = self._strip_html(r.get("title", "")) or "Untitled"
            heading = f"{r.get('number', '')} {title}".strip()
            lines.append(f"{i}. {heading}")
            lines.append(f"   guid: {guid or '(unknown)'}   type: {r.get('type', '')}")
            if r.get("pdf"):
                lines.append("   (PDF document)")
            if snippet:
                lines.append(f"   …{snippet}…")
            if guid:
                lines.append(f"   {PUBLIC_URL.format(guid=guid)}")
            lines.append("")
        if page + 1 < max_pages:
            lines.append(f"More results: call search_code with page={page + 1}.")
        return "\n".join(lines)

    @staticmethod
    def _as_int(value: Any, default: int = 0) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def _format_toc(self, data: Dict[str, Any]) -> str:
        header = []
        if data.get("_fallback"):
            header.append(f"(Note: {data['_fallback']})")
        title = data.get("title", "Table of Contents")
        header.append(f"Table of Contents — {title}")
        guid = data.get("guid") or data.get("id")
        if guid:
            header.append(f"root guid: {guid}")
        header.append("")
        body = self._render_children(data.get("children", []) or [], depth=0)
        return "\n".join(header) + ("\n".join(body) if body else "(no entries)")

    def _format_structure(self, data: Dict[str, Any]) -> str:
        guid = data.get("guid") or data.get("id", "")
        lines = [
            f"{data.get('label', '')} {data.get('number', '')} "
            f"{data.get('title', '')}".strip(),
            f"guid: {guid}   type: {data.get('type', '')}",
            f"parent: {data.get('parent') or '(none)'}",
            f"link: {PUBLIC_URL.format(guid=guid)}" if guid and guid != "ROOT" else "",
            "",
            "Children:",
        ]
        children = data.get("children", []) or []
        body = self._render_children(children, depth=0)
        lines.extend(body or ["  (none — this may be a leaf section; "
                              "use get_section to read its text)"])
        return "\n".join(line for line in lines if line != "")

    def _render_children(self, children: List[Any], depth: int) -> List[str]:
        """Render a list of child nodes (each a dict, or a bare guid string)."""
        out: List[str] = []
        indent = "  " * (depth + 1)
        for child in children:
            if isinstance(child, str):
                out.append(f"{indent}- guid {child}")
                continue
            guid = child.get("guid") or child.get("id", "")
            label = " ".join(
                p for p in [child.get("number", ""), child.get("title", "")] if p
            ).strip()
            out.append(f"{indent}- {label or '(untitled)'}  [guid: {guid}]")
        return out

    def _format_content(
        self, data: Dict[str, Any], _budget: Optional[List[int]] = None
    ) -> str:
        if _budget is None:
            _budget = [MAX_CONTENT_CHARS]
            guid = data.get("id") or data.get("guid", "")
            head = [
                " ".join(
                    p for p in [data.get("number", ""), data.get("title", "")] if p
                ).strip()
                or "(untitled section)",
                f"guid: {guid}   type: {data.get('type', '')}",
                f"link: {PUBLIC_URL.format(guid=guid)}" if guid else "",
            ]
            images = data.get("images") or []
            attachments = data.get("attachments") or {}
            if images:
                head.append(f"images: {len(images)} (relative links on ecode360.com)")
            if attachments:
                head.append(f"attachments: {', '.join(attachments.keys())}")
            head.append("")
            body = self._render_content_node(data, depth=0, budget=_budget)
            return "\n".join(h for h in head if h != "") + "\n" + body

        return self._render_content_node(data, depth=0, budget=_budget)

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
