"""Universal HTTP handler for OpenContext MCP server.

This handler provides cloud-agnostic HTTP request processing that can be
used by any cloud provider adapter (AWS Lambda, GCP Cloud Functions, Azure Functions, etc.).
"""

import json
import logging
import os
import time
import uuid
from typing import Any, Dict, Optional, Tuple

from core.logging_utils import (
    configure_json_logging,
    format_request_log,
    format_response_log,
)
from core.mcp_server import ERA_MODERN, MCPServer
from core.plugin_manager import PluginManager
from core.validators import (
    ConfigurationError,
    get_logging_config,
    load_and_validate_config,
)


def _packaged_config_path() -> str:
    """Path to the config.yaml shipped inside the deployment package.

    On Lambda the package is extracted to ``$LAMBDA_TASK_ROOT`` (/var/task),
    so resolve relative to that rather than the process CWD; fall back to the
    CWD for local runs. The full config (including the ``instructions`` block)
    is delivered as this packaged file rather than the ``OPENCONTEXT_CONFIG``
    env var, which AWS caps at 4KB.
    """
    root = os.environ.get("LAMBDA_TASK_ROOT") or "."
    return os.path.join(root, "config.yaml")


# Configure JSON logging globally (must be called before other loggers are created)
# Try to get log level from config, but default to INFO if config not available yet
try:
    # Try loading config to get log level
    if os.environ.get("OPENCONTEXT_CONFIG"):
        config_json = os.environ.get("OPENCONTEXT_CONFIG")
        config = json.loads(config_json)
        logging_config = get_logging_config(config)
        log_level = logging_config.get("level", "INFO")
    else:
        # Fall back to the packaged config.yaml.
        config = load_and_validate_config(_packaged_config_path())
        logging_config = get_logging_config(config)
        log_level = logging_config.get("level", "INFO")
except Exception:
    # If config loading fails, use default
    log_level = "INFO"

configure_json_logging(level=log_level, pretty=False)  # Compact JSON for CloudWatch
logger = logging.getLogger(__name__)

# Global variables for container reuse (warm starts)
_plugin_manager: Optional[PluginManager] = None
_mcp_server: Optional[MCPServer] = None
_config: Optional[Dict[str, Any]] = None


def _load_config() -> Dict[str, Any]:
    """Load configuration from environment or embedded config.

    Returns:
        Configuration dictionary
    """
    global _config

    if _config is not None:
        return _config

    # Try to load from environment variable (set by Terraform)
    config_json = os.environ.get("OPENCONTEXT_CONFIG")
    if config_json:
        try:
            _config = json.loads(config_json)
            logger.info("Loaded configuration from environment variable")
            return _config
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse config from environment: {e}")
            raise

    # Fall back to the packaged config.yaml (the primary path on Lambda now
    # that the full config exceeds the 4KB OPENCONTEXT_CONFIG env-var limit).
    try:
        _config = load_and_validate_config(_packaged_config_path())
        logger.info("Loaded configuration from packaged config.yaml")
        return _config
    except FileNotFoundError:
        logger.error(
            "No configuration found. Set OPENCONTEXT_CONFIG environment variable "
            "or ensure config.yaml exists."
        )
        raise


async def _initialize_server() -> None:
    """Initialize plugin manager and MCP server.

    This function is called on first request (cold start) and reuses
    the initialized instances for subsequent requests (warm starts).
    """
    global _plugin_manager, _mcp_server

    if _plugin_manager is not None and _mcp_server is not None:
        return

    try:
        config = _load_config()

        # Initialize Plugin Manager
        _plugin_manager = PluginManager(config)

        # Load plugins (validates ONE plugin enabled)
        await _plugin_manager.load_plugins()

        # Initialize MCP Server
        _mcp_server = MCPServer(_plugin_manager)

        logger.info("OpenContext MCP server initialized successfully")

    except ConfigurationError as e:
        # Log error and crash
        logger.error(f"Configuration error: {e}")
        raise RuntimeError(f"Configuration error: {e}") from e
    except Exception as e:
        logger.error(f"Failed to initialize server: {e}", exc_info=True)
        raise


class UniversalHTTPHandler:
    """Universal HTTP handler for cloud-agnostic request processing."""

    # Browser origins allowed to call /mcp via cross-origin requests.
    # Native MCP clients (Claude Desktop, Claude Code, server-side
    # integrations) do not enforce CORS, so they are unaffected by this
    # list. Add real consumers here as they show up in CloudWatch.
    ALLOWED_ORIGINS = frozenset(
        {
            "https://claude.ai",
            "https://claude.com",
            "https://console.anthropic.com",
            "http://localhost:6274",
            "http://127.0.0.1:6274",
        }
    )
    # Default origin returned when no Origin header is sent or the
    # caller's origin is not on the allowlist. claude.ai is the primary
    # legitimate browser consumer today; reflecting it ensures the most
    # common case keeps working without a request-header round-trip.
    DEFAULT_CORS_ORIGIN = "https://claude.ai"

    # Request paths that carry MCP JSON-RPC. The hardened GCC route
    # (/mcp-gcc) shares this same Lambda handler as the public /mcp route;
    # its API-key requirement is enforced at API Gateway, not here, so the
    # handler simply needs to accept the path.
    MCP_PATHS = frozenset({"/mcp", "/mcp-gcc"})

    def __init__(self) -> None:
        """Initialize the universal HTTP handler."""
        logger.info("UniversalHTTPHandler initialized")

    @classmethod
    def _get_cors_headers(
        cls, request_origin: Optional[str] = None
    ) -> Dict[str, str]:
        """Build CORS response headers, reflecting allowlisted origins.

        Args:
            request_origin: The Origin header from the incoming request,
                if any. If it matches the allowlist, it's echoed back so
                the browser will accept the response. Otherwise the
                default (claude.ai) is sent — non-browser clients ignore
                CORS, so they are unaffected.

        Returns:
            Dictionary of CORS headers.
        """
        if request_origin and request_origin in cls.ALLOWED_ORIGINS:
            allow_origin = request_origin
        else:
            allow_origin = cls.DEFAULT_CORS_ORIGIN
        return {
            "Access-Control-Allow-Origin": allow_origin,
            "Vary": "Origin",
            "Access-Control-Allow-Methods": "POST, OPTIONS",
            "Access-Control-Allow-Headers": (
                "content-type, accept, mcp-session-id, mcp-protocol-version, "
                "mcp-method, mcp-name"
            ),
            "Access-Control-Expose-Headers": "x-request-id, mcp-session-id",
        }

    @classmethod
    def _origin_rejected(cls, request_origin: Optional[str]) -> bool:
        """Whether a request must be refused because of its Origin.

        The MCP HTTP transport requires servers to answer 403 Forbidden for
        an invalid Origin -- the defence against DNS-rebinding attacks, which
        matters here because /mcp is anonymous and CloudWatch already shows
        it being probed. Echoing CORS headers only for allowlisted origins,
        which is all this handler did before, is NOT the same thing: the
        request was still served, and a non-browser attacker never reads the
        CORS headers at all.

        Only a *present* Origin can be invalid: native MCP clients (Claude
        Desktop, Claude Code, the claude.ai backend connector, curl, the
        Lambda console) send none, and are unaffected.

        Args:
            request_origin: The Origin header from the request, if any.

        Returns:
            True if the request should be rejected with 403.
        """
        return bool(request_origin) and request_origin not in cls.ALLOWED_ORIGINS

    async def handle_request(
        self,
        method: str,
        path: str,
        body: str,
        headers: Dict[str, str],
        request_id: Optional[str] = None,
    ) -> Tuple[int, Dict[str, str], str]:
        """Handle a universal HTTP request.

        Args:
            method: HTTP method (e.g., "POST", "GET")
            path: Request path (e.g., "/mcp")
            body: Request body as JSON string
            headers: Request headers as dictionary
            request_id: Optional request ID for logging/tracing

        Returns:
            Tuple of (status_code, response_headers, response_body)
        """
        start_time = time.perf_counter()
        request_id = request_id or "unknown"

        # Pull incoming MCP session ID (sent by clients on every request after
        # the initial handshake). For `initialize` itself we generate one below.
        incoming_session_id = headers.get("mcp-session-id") if headers else None

        # Pull Origin header so CORS responses can reflect allowlisted origins.
        request_origin = headers.get("origin") if headers else None

        # Reject disallowed browser origins outright (DNS-rebinding defence)
        # before the request reaches any routing or plugin code.
        if self._origin_rejected(request_origin):
            duration_ms = (time.perf_counter() - start_time) * 1000
            error_body = json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {
                        "code": -32600,
                        "message": "Forbidden",
                        "data": "Origin not allowed",
                    },
                }
            )
            logger.warning(
                f"403 error: Origin '{request_origin}' not allowed",
                extra={
                    "request_id": request_id,
                    "request_path": path,
                    "http_method": method,
                    "request_origin": request_origin,
                    "duration_ms": duration_ms,
                },
            )
            error_headers = {"Content-Type": "application/json"}
            error_headers.update(self._get_cors_headers(None))
            return (403, error_headers, error_body)

        # Validate path - must be an MCP route
        if path not in self.MCP_PATHS:
            duration_ms = (time.perf_counter() - start_time) * 1000
            error_body = json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {
                        "code": -32601,
                        "message": "Not Found",
                        "data": f"Path '{path}' not found. Expected '/mcp' or '/mcp-gcc'",
                    },
                }
            )
            logger.warning(
                f"404 error: Path '{path}' not found",
                extra={
                    "request_id": request_id,
                    "request_path": path,
                    "http_method": method,
                    "duration_ms": duration_ms,
                },
            )
            error_headers = {"Content-Type": "application/json"}
            error_headers.update(self._get_cors_headers(request_origin))
            return (
                404,
                error_headers,
                error_body,
            )

        # Validate method - must be POST
        if method != "POST":
            duration_ms = (time.perf_counter() - start_time) * 1000
            error_body = json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {
                        "code": -32601,
                        "message": "Method Not Allowed",
                        "data": f"Method '{method}' not allowed. Expected 'POST'",
                    },
                }
            )
            logger.warning(
                f"405 error: Method '{method}' not allowed",
                extra={
                    "request_id": request_id,
                    "request_path": path,
                    "http_method": method,
                    "duration_ms": duration_ms,
                },
            )
            error_headers = {"Content-Type": "application/json", "Allow": "POST"}
            error_headers.update(self._get_cors_headers(request_origin))
            return (
                405,
                error_headers,
                error_body,
            )

        # Validate the MCP-Protocol-Version header, which clients have been
        # required to send on every post-handshake request since 2025-06-18.
        # Absent means an older client: the spec says assume 2025-03-26, so
        # we let it through untouched. Both legacy (2025-11-25 and earlier,
        # initialize handshake) and modern (2026-07-28+, per-request _meta)
        # revisions are served -- see core/mcp_server.py for the split; the
        # modern era's header/body consistency checks happen there, once
        # the body has been parsed.
        #
        # A version we implement in neither era gets the modern
        # UnsupportedProtocolVersionError (-32022) listing what we do
        # support: a modern client retries with one of those, and a
        # legacy client at least sees a version it can ask for on
        # initialize. (This used to be a bare -32600 on purpose, so that
        # dual-era clients would fall back to initialize; now that we are
        # dual-era ourselves that fallback would cost them a round trip
        # for nothing.)
        declared_version = (
            headers.get("mcp-protocol-version") if headers else None
        )
        if (
            declared_version
            and declared_version not in MCPServer.SUPPORTED_PROTOCOL_VERSIONS
        ):
            duration_ms = (time.perf_counter() - start_time) * 1000
            error_body = json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {
                        "code": -32022,
                        "message": "Unsupported protocol version",
                        "data": {
                            "supported": list(
                                MCPServer.SUPPORTED_PROTOCOL_VERSIONS
                            ),
                            "requested": declared_version,
                        },
                    },
                }
            )
            logger.warning(
                f"400 error: unsupported MCP-Protocol-Version "
                f"'{declared_version}'",
                extra={
                    "request_id": request_id,
                    "request_path": path,
                    "http_method": method,
                    "mcp_protocol_version": declared_version,
                    "duration_ms": duration_ms,
                },
            )
            error_headers = {"Content-Type": "application/json"}
            error_headers.update(self._get_cors_headers(request_origin))
            return (400, error_headers, error_body)

        # Parse JSON to check if this is an initialize request
        # NOTE: This is intentionally parsing the JSON body separately from the
        # later parsing in _mcp_server.handle_http_request(). This early parsing
        # allows us to detect initialize requests and generate session IDs without
        # affecting error handling if the JSON is invalid. The body will be parsed
        # again later, which is an acceptable trade-off for error handling isolation.
        try:
            request_json = json.loads(body)
            is_initialize = request_json.get("method") == "initialize"
            is_modern = (
                MCPServer.request_era(request_json, headers) == ERA_MODERN
            )
        except (json.JSONDecodeError, AttributeError):
            is_initialize = False
            is_modern = False

        # Generate session ID for initialize requests
        # NOTE: This session ID is for logging and tracing purposes only.
        # It is NOT implementing true session management - there is no persistent
        # session storage. The session ID is included in response headers to
        # help correlate logs and trace requests, but it does not maintain
        # any server-side session state.
        session_id = None
        if is_initialize:
            session_id = str(uuid.uuid4())
            logger.info(
                f"Initialize request detected, generating session ID: {session_id}",
                extra={"request_id": request_id, "mcp_session_id": session_id},
            )

        # Modern-era (2026-07-28) requests have no session: an
        # Mcp-Session-Id a client sends anyway is ignored, never echoed or
        # logged as if it correlated anything.
        effective_session_id = (
            None if is_modern else (session_id or incoming_session_id)
        )

        # Log request details
        request_log_data = format_request_log(
            request_id=request_id,
            http_method=method,
            request_path=path,
            headers=headers,
            body=body,
            lambda_context=None,  # Not available in universal handler
        )
        if effective_session_id:
            request_log_data["mcp_session_id"] = effective_session_id
        logger.info("Incoming HTTP request", extra=request_log_data)

        try:
            # Initialize server on first request
            await _initialize_server()

            # Handle request
            response = await _mcp_server.handle_http_request(body, headers)

            # Extract status code and body from response
            status_code = response.get("statusCode", 200)
            response_body = response.get("body", "")
            response_headers = response.get("headers", {}).copy()

            # Add session ID to response headers if this was an initialize request
            if session_id:
                response_headers["Mcp-Session-Id"] = session_id

            # Add request ID to response headers for tracing
            response_headers["X-Request-ID"] = request_id

            # Ensure Content-Type is set
            if "Content-Type" not in response_headers:
                response_headers["Content-Type"] = "application/json"

            # Add CORS headers
            response_headers.update(self._get_cors_headers(request_origin))

            # Calculate duration
            duration_ms = (time.perf_counter() - start_time) * 1000

            # Log response details
            response_log_data = format_response_log(
                request_id=request_id,
                status_code=status_code,
                headers=response_headers,
                body=response_body,
                duration_ms=duration_ms,
                success=True,
            )
            if effective_session_id:
                response_log_data["mcp_session_id"] = effective_session_id
            logger.info("HTTP request processed successfully", extra=response_log_data)

            return (status_code, response_headers, response_body)

        except ConfigurationError as e:
            # Configuration errors should crash
            duration_ms = (time.perf_counter() - start_time) * 1000
            # Don't leak config details to the client; full exception is in
            # CloudWatch via logger.error(..., exc_info=True) below.
            error_body = json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {
                        "code": -32603,
                        "message": "Server configuration error",
                        "data": f"Request ID: {request_id}",
                    },
                }
            )

            # Log error response
            error_headers = {"Content-Type": "application/json"}
            error_headers.update(self._get_cors_headers(request_origin))
            response_log_data = format_response_log(
                request_id=request_id,
                status_code=500,
                headers=error_headers,
                body=error_body,
                duration_ms=duration_ms,
                success=False,
            )
            if effective_session_id:
                response_log_data["mcp_session_id"] = effective_session_id
            logger.error(
                f"Configuration error in request {request_id}: {e}",
                extra={**response_log_data, "error_type": "ConfigurationError"},
                exc_info=True,
            )

            return (500, error_headers, error_body)

        except Exception as e:
            duration_ms = (time.perf_counter() - start_time) * 1000
            # Don't leak exception details to the client; full traceback is in
            # CloudWatch via logger.error(..., exc_info=True) below.
            error_body = json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {
                        "code": -32603,
                        "message": "Internal error",
                        "data": f"Request ID: {request_id}",
                    },
                }
            )

            # Log error response
            error_headers = {"Content-Type": "application/json"}
            error_headers.update(self._get_cors_headers(request_origin))
            response_log_data = format_response_log(
                request_id=request_id,
                status_code=500,
                headers=error_headers,
                body=error_body,
                duration_ms=duration_ms,
                success=False,
            )
            if effective_session_id:
                response_log_data["mcp_session_id"] = effective_session_id
            logger.error(
                f"Error processing request {request_id}: {e}",
                extra={**response_log_data, "error_type": type(e).__name__},
                exc_info=True,
            )

            return (500, error_headers, error_body)

    def handle_options(
        self,
        request_id: Optional[str] = None,
        request_origin: Optional[str] = None,
    ) -> Tuple[int, Dict[str, str], str]:
        """Handle CORS preflight OPTIONS request.

        Args:
            request_id: Optional request ID for logging/tracing
            request_origin: Origin header from the preflight, used to
                reflect allowlisted origins back to the browser.

        Returns:
            Tuple of (status_code, response_headers, response_body)
        """
        request_id = request_id or "unknown"

        # Refuse the preflight outright for a disallowed origin, so the
        # browser never issues the actual request.
        if self._origin_rejected(request_origin):
            logger.warning(
                f"403 preflight: Origin '{request_origin}' not allowed",
                extra={
                    "request_id": request_id,
                    "request_origin": request_origin,
                },
            )
            return (
                403,
                {
                    **self._get_cors_headers(None),
                    "Content-Type": "application/json",
                    "X-Request-ID": request_id,
                },
                "",
            )

        cors_headers = {
            **self._get_cors_headers(request_origin),
            "Access-Control-Max-Age": "86400",
            "Content-Type": "application/json",
            "X-Request-ID": request_id,
        }

        logger.info(
            "CORS preflight OPTIONS request handled",
            extra={"request_id": request_id},
        )

        return (200, cors_headers, "")
