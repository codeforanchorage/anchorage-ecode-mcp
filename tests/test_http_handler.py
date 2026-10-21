"""Comprehensive tests for Universal HTTP Handler.

These tests verify HTTP request processing, path/method validation,
CORS handling, error handling, and server initialization.
"""

import pytest
import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

from core.mcp_server import MCPServer
from server.http_handler import UniversalHTTPHandler, _initialize_server, _load_config
from core.validators import ConfigurationError


class TestPathValidation:
    """Test path validation."""

    @pytest.mark.asyncio
    async def test_valid_path_mcp_succeeds(self):
        """Test that /mcp path succeeds."""
        handler = UniversalHTTPHandler()

        with (
            patch("server.http_handler._initialize_server") as mock_init,
            patch("server.http_handler._mcp_server") as mock_mcp_server,
        ):
            mock_mcp_server.handle_http_request = AsyncMock(
                return_value={
                    "statusCode": 200,
                    "headers": {},
                    "body": json.dumps({"result": "success"}),
                }
            )

            status, headers, body = await handler.handle_request(
                method="POST",
                path="/mcp",
                body=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}),
                headers={},
            )

            assert status == 200
            mock_init.assert_called_once()

    @pytest.mark.asyncio
    async def test_valid_path_mcp_gcc_succeeds(self):
        """The hardened GCC route shares this handler and must be accepted
        (its API-key requirement is enforced upstream at API Gateway)."""
        handler = UniversalHTTPHandler()

        with (
            patch("server.http_handler._initialize_server") as mock_init,
            patch("server.http_handler._mcp_server") as mock_mcp_server,
        ):
            mock_mcp_server.handle_http_request = AsyncMock(
                return_value={
                    "statusCode": 200,
                    "headers": {},
                    "body": json.dumps({"result": "success"}),
                }
            )

            status, headers, body = await handler.handle_request(
                method="POST",
                path="/mcp-gcc",
                body=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}),
                headers={},
            )

            assert status == 200
            mock_init.assert_called_once()

    @pytest.mark.asyncio
    async def test_invalid_path_returns_404(self):
        """Test that invalid path returns 404."""
        handler = UniversalHTTPHandler()

        status, headers, body = await handler.handle_request(
            method="POST",
            path="/invalid",
            body=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}),
            headers={},
        )

        assert status == 404
        assert headers["Content-Type"] == "application/json"
        error_body = json.loads(body)
        assert error_body["error"]["code"] == -32601
        assert error_body["error"]["message"] == "Not Found"
        assert "/invalid" in error_body["error"]["data"]

    @pytest.mark.asyncio
    async def test_root_path_returns_404(self):
        """Test that root path returns 404."""
        handler = UniversalHTTPHandler()

        status, headers, body = await handler.handle_request(
            method="POST",
            path="/",
            body=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}),
            headers={},
        )

        assert status == 404

    @pytest.mark.asyncio
    async def test_mcp_with_trailing_slash_returns_404(self):
        """Test that /mcp/ (with trailing slash) returns 404."""
        handler = UniversalHTTPHandler()

        status, headers, body = await handler.handle_request(
            method="POST",
            path="/mcp/",
            body=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}),
            headers={},
        )

        assert status == 404


class TestMethodValidation:
    """Test HTTP method validation."""

    @pytest.mark.asyncio
    async def test_post_method_succeeds(self):
        """Test that POST method succeeds."""
        handler = UniversalHTTPHandler()

        with (
            patch("server.http_handler._initialize_server"),
            patch("server.http_handler._mcp_server") as mock_mcp_server,
        ):
            mock_mcp_server.handle_http_request = AsyncMock(
                return_value={
                    "statusCode": 200,
                    "headers": {},
                    "body": json.dumps({"result": "success"}),
                }
            )

            status, headers, body = await handler.handle_request(
                method="POST",
                path="/mcp",
                body=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}),
                headers={},
            )

            assert status == 200

    @pytest.mark.asyncio
    async def test_get_method_returns_405(self):
        """Test that GET method returns 405."""
        handler = UniversalHTTPHandler()

        status, headers, body = await handler.handle_request(
            method="GET",
            path="/mcp",
            body="",
            headers={},
        )

        assert status == 405
        assert headers["Allow"] == "POST"
        error_body = json.loads(body)
        assert error_body["error"]["code"] == -32601
        assert error_body["error"]["message"] == "Method Not Allowed"

    @pytest.mark.asyncio
    async def test_put_method_returns_405(self):
        """Test that PUT method returns 405."""
        handler = UniversalHTTPHandler()

        status, headers, body = await handler.handle_request(
            method="PUT",
            path="/mcp",
            body="",
            headers={},
        )

        assert status == 405

    @pytest.mark.asyncio
    async def test_delete_method_returns_405(self):
        """Test that DELETE method returns 405."""
        handler = UniversalHTTPHandler()

        status, headers, body = await handler.handle_request(
            method="DELETE",
            path="/mcp",
            body="",
            headers={},
        )

        assert status == 405


class TestCORS:
    """Test CORS handling."""

    @pytest.mark.asyncio
    async def test_cors_headers_added_to_response(self):
        """Test that CORS headers are added to response."""
        handler = UniversalHTTPHandler()

        with (
            patch("server.http_handler._initialize_server"),
            patch("server.http_handler._mcp_server") as mock_mcp_server,
        ):
            mock_mcp_server.handle_http_request = AsyncMock(
                return_value={
                    "statusCode": 200,
                    "headers": {},
                    "body": json.dumps({"result": "success"}),
                }
            )

            status, headers, body = await handler.handle_request(
                method="POST",
                path="/mcp",
                body=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}),
                headers={},
            )

            # No Origin sent → default-origin (claude.ai) is reflected.
            assert headers["Access-Control-Allow-Origin"] == "https://claude.ai"
            assert headers["Vary"] == "Origin"
            assert (
                headers["Access-Control-Allow-Methods"]
                == "POST, OPTIONS"
            )
            assert (
                headers["Access-Control-Allow-Headers"]
                == (
                "content-type, accept, mcp-session-id, mcp-protocol-version, "
                "mcp-method, mcp-name"
            )
            )

    def test_handle_options_returns_cors_headers(self):
        """Test that OPTIONS handler returns CORS headers."""
        handler = UniversalHTTPHandler()

        status, headers, body = handler.handle_options()

        assert status == 200
        # No Origin sent → default-origin reflected.
        assert headers["Access-Control-Allow-Origin"] == "https://claude.ai"
        assert headers["Vary"] == "Origin"
        assert (
            headers["Access-Control-Allow-Methods"]
            == "POST, OPTIONS"
        )
        assert (
            headers["Access-Control-Allow-Headers"]
            == (
                "content-type, accept, mcp-session-id, mcp-protocol-version, "
                "mcp-method, mcp-name"
            )
        )
        assert headers["Access-Control-Max-Age"] == "86400"
        assert body == ""

    def test_handle_options_reflects_allowlisted_origin(self):
        """Allowlisted origins are reflected back in Access-Control-Allow-Origin."""
        handler = UniversalHTTPHandler()

        _status, headers, _body = handler.handle_options(
            request_origin="https://claude.ai"
        )
        assert headers["Access-Control-Allow-Origin"] == "https://claude.ai"

        _status, headers, _body = handler.handle_options(
            request_origin="https://console.anthropic.com"
        )
        assert (
            headers["Access-Control-Allow-Origin"]
            == "https://console.anthropic.com"
        )

    def test_handle_options_rejects_unknown_origin(self):
        """A preflight from a non-allowlisted origin is refused with 403.

        Previously this returned 200 with the default origin echoed, so the
        browser merely failed the CORS check client-side and the real request
        was still served to anything that ignores CORS.
        """
        handler = UniversalHTTPHandler()

        status, headers, _body = handler.handle_options(
            request_origin="https://attacker.example.com"
        )
        assert status == 403
        assert "attacker.example.com" not in headers["Access-Control-Allow-Origin"]


class TestOriginValidation:
    """Disallowed Origins are refused with 403, not silently served.

    This is the Streamable HTTP transport's DNS-rebinding defence, and it is
    a MUST. Echoing CORS headers only for allowlisted origins -- the previous
    behaviour -- is not the same thing: the request still ran.
    """

    @pytest.mark.asyncio
    async def test_disallowed_origin_returns_403(self):
        """A POST carrying a non-allowlisted Origin is refused."""
        handler = UniversalHTTPHandler()

        status, headers, body = await handler.handle_request(
            method="POST",
            path="/mcp",
            body=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}),
            headers={"origin": "https://attacker.example.com"},
        )

        assert status == 403
        error_body = json.loads(body)
        assert error_body["error"]["message"] == "Forbidden"
        assert "attacker.example.com" not in headers["Access-Control-Allow-Origin"]

    @pytest.mark.asyncio
    async def test_disallowed_origin_never_reaches_the_server(self):
        """The refusal happens before any plugin or routing code runs."""
        handler = UniversalHTTPHandler()

        with (
            patch("server.http_handler._initialize_server") as mock_init,
            patch("server.http_handler._mcp_server") as mock_mcp_server,
        ):
            mock_mcp_server.handle_http_request = AsyncMock()

            status, _headers, _body = await handler.handle_request(
                method="POST",
                path="/mcp",
                body=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}),
                headers={"origin": "https://attacker.example.com"},
            )

        assert status == 403
        mock_init.assert_not_called()
        mock_mcp_server.handle_http_request.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_origin_is_allowed(self):
        """Native clients send no Origin at all and must not be blocked."""
        handler = UniversalHTTPHandler()

        with (
            patch("server.http_handler._initialize_server"),
            patch("server.http_handler._mcp_server") as mock_mcp_server,
        ):
            mock_mcp_server.handle_http_request = AsyncMock(
                return_value={
                    "statusCode": 200,
                    "headers": {},
                    "body": json.dumps({"result": {}}),
                }
            )

            status, _headers, _body = await handler.handle_request(
                method="POST",
                path="/mcp",
                body=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}),
                headers={},
            )

        assert status == 200

    @pytest.mark.asyncio
    async def test_allowlisted_origin_is_allowed(self):
        """An allowlisted browser origin passes through and is reflected."""
        handler = UniversalHTTPHandler()

        with (
            patch("server.http_handler._initialize_server"),
            patch("server.http_handler._mcp_server") as mock_mcp_server,
        ):
            mock_mcp_server.handle_http_request = AsyncMock(
                return_value={
                    "statusCode": 200,
                    "headers": {},
                    "body": json.dumps({"result": {}}),
                }
            )

            status, headers, _body = await handler.handle_request(
                method="POST",
                path="/mcp",
                body=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}),
                headers={"origin": "https://claude.ai"},
            )

        assert status == 200
        assert headers["Access-Control-Allow-Origin"] == "https://claude.ai"

    def test_claude_com_is_allowlisted(self):
        """claude.ai and claude.com are both first-party browser origins."""
        assert "https://claude.com" in UniversalHTTPHandler.ALLOWED_ORIGINS
        assert "https://claude.ai" in UniversalHTTPHandler.ALLOWED_ORIGINS

    def test_mcp_inspector_origins_still_allowed(self):
        """Local MCP Inspector must keep working against a local server."""
        assert "http://localhost:6274" in UniversalHTTPHandler.ALLOWED_ORIGINS
        assert "http://127.0.0.1:6274" in UniversalHTTPHandler.ALLOWED_ORIGINS


class TestProtocolVersionHeader:
    """MCP-Protocol-Version has been required post-handshake since 2025-06-18."""

    @pytest.mark.asyncio
    async def test_unsupported_version_returns_400(self):
        """A declared revision we implement in neither era is refused with
        400 and the modern UnsupportedProtocolVersionError, listing every
        version we do serve so the client can retry with one."""
        handler = UniversalHTTPHandler()

        status, _headers, body = await handler.handle_request(
            method="POST",
            path="/mcp",
            body=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}),
            headers={"mcp-protocol-version": "1999-01-01"},
        )

        assert status == 400
        error_body = json.loads(body)
        assert error_body["error"]["code"] == -32022
        assert error_body["error"]["data"]["requested"] == "1999-01-01"
        assert error_body["error"]["data"]["supported"][0] == "2026-07-28"
        assert "2025-11-25" in error_body["error"]["data"]["supported"]
        # The spec's own UnsupportedProtocolVersionError example echoes
        # the request id; a client with several requests in flight needs
        # it to know which one was refused. This used to be null.
        assert error_body["id"] == 1

    @pytest.mark.asyncio
    async def test_unsupported_version_without_id_keeps_null_id(self):
        """A notification (no id) or an unparseable body cannot be
        correlated, so the error keeps a null id rather than inventing
        one."""
        handler = UniversalHTTPHandler()

        status, _headers, body = await handler.handle_request(
            method="POST",
            path="/mcp",
            body=json.dumps(
                {"jsonrpc": "2.0", "method": "notifications/initialized"}
            ),
            headers={"mcp-protocol-version": "1999-01-01"},
        )
        assert status == 400
        assert json.loads(body)["id"] is None

        status, _headers, body = await handler.handle_request(
            method="POST",
            path="/mcp",
            body="{not json",
            headers={"mcp-protocol-version": "1999-01-01"},
        )
        assert status == 400
        assert json.loads(body)["id"] is None

    @pytest.mark.asyncio
    async def test_modern_version_header_is_accepted(self):
        """2026-07-28 is served (dual-era), not bounced to a 400 that used
        to cost every claude.ai / Claude Code session a fallback round
        trip. The header alone is not enough -- the body must carry the
        modern _meta too -- so a bare-header request is a header
        mismatch, which is still a modern (-32020) error, not a legacy
        refusal."""
        handler = UniversalHTTPHandler()

        with patch("server.http_handler._initialize_server"):
            from server import http_handler

            plugin_manager = MagicMock()
            plugin_manager.config = {}
            plugin_manager.get_all_tools.return_value = []
            http_handler._mcp_server = MCPServer(plugin_manager)

            status, headers, body = await handler.handle_request(
                method="POST",
                path="/mcp",
                body=json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "server/discover",
                        "params": {
                            "_meta": {
                                "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                                "io.modelcontextprotocol/clientCapabilities": {},
                            }
                        },
                    }
                ),
                headers={
                    "mcp-protocol-version": "2026-07-28",
                    "mcp-method": "server/discover",
                    "mcp-session-id": "stale-legacy-session",
                },
            )

        assert status == 200
        assert json.loads(body)["result"]["supportedVersions"][0] == "2026-07-28"
        # No session in the modern era: nothing minted, nothing echoed.
        assert "Mcp-Session-Id" not in headers

    @pytest.mark.asyncio
    async def test_supported_version_passes_through(self):
        """A supported declared revision is served normally."""
        handler = UniversalHTTPHandler()

        with (
            patch("server.http_handler._initialize_server"),
            patch("server.http_handler._mcp_server") as mock_mcp_server,
        ):
            mock_mcp_server.handle_http_request = AsyncMock(
                return_value={
                    "statusCode": 200,
                    "headers": {},
                    "body": json.dumps({"result": {}}),
                }
            )

            status, _headers, _body = await handler.handle_request(
                method="POST",
                path="/mcp",
                body=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}),
                headers={"mcp-protocol-version": "2025-11-25"},
            )

        assert status == 200

    @pytest.mark.asyncio
    async def test_absent_version_passes_through(self):
        """No header means an older client; the spec says assume a default."""
        handler = UniversalHTTPHandler()

        with (
            patch("server.http_handler._initialize_server"),
            patch("server.http_handler._mcp_server") as mock_mcp_server,
        ):
            mock_mcp_server.handle_http_request = AsyncMock(
                return_value={
                    "statusCode": 200,
                    "headers": {},
                    "body": json.dumps({"result": {}}),
                }
            )

            status, _headers, _body = await handler.handle_request(
                method="POST",
                path="/mcp",
                body=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}),
                headers={},
            )

        assert status == 200


class TestSessionID:
    """Test session ID generation."""

    @pytest.mark.asyncio
    async def test_initialize_request_generates_session_id(self):
        """Test that initialize request generates session ID."""
        handler = UniversalHTTPHandler()

        with (
            patch("server.http_handler._initialize_server"),
            patch("server.http_handler._mcp_server") as mock_mcp_server,
        ):
            mock_mcp_server.handle_http_request = AsyncMock(
                return_value={
                    "statusCode": 200,
                    "headers": {},
                    "body": json.dumps({"result": "success"}),
                }
            )

            status, headers, body = await handler.handle_request(
                method="POST",
                path="/mcp",
                body=json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {},
                    }
                ),
                headers={},
            )

            assert "Mcp-Session-Id" in headers
            assert headers["Mcp-Session-Id"] is not None
            assert len(headers["Mcp-Session-Id"]) > 0

    @pytest.mark.asyncio
    async def test_non_initialize_request_no_session_id(self):
        """Test that non-initialize request doesn't generate session ID."""
        handler = UniversalHTTPHandler()

        with (
            patch("server.http_handler._initialize_server"),
            patch("server.http_handler._mcp_server") as mock_mcp_server,
        ):
            mock_mcp_server.handle_http_request = AsyncMock(
                return_value={
                    "statusCode": 200,
                    "headers": {},
                    "body": json.dumps({"result": "success"}),
                }
            )

            status, headers, body = await handler.handle_request(
                method="POST",
                path="/mcp",
                body=json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "ping",
                        "params": {},
                    }
                ),
                headers={},
            )

            assert "Mcp-Session-Id" not in headers


class TestRequestID:
    """Test request ID handling."""

    @pytest.mark.asyncio
    async def test_request_id_added_to_response_headers(self):
        """Test that request ID is added to response headers."""
        handler = UniversalHTTPHandler()

        with (
            patch("server.http_handler._initialize_server"),
            patch("server.http_handler._mcp_server") as mock_mcp_server,
        ):
            mock_mcp_server.handle_http_request = AsyncMock(
                return_value={
                    "statusCode": 200,
                    "headers": {},
                    "body": json.dumps({"result": "success"}),
                }
            )

            status, headers, body = await handler.handle_request(
                method="POST",
                path="/mcp",
                body=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}),
                headers={},
                request_id="test-request-id-123",
            )

            assert headers["X-Request-ID"] == "test-request-id-123"

    @pytest.mark.asyncio
    async def test_default_request_id_when_not_provided(self):
        """Test that default request ID is used when not provided."""
        handler = UniversalHTTPHandler()

        with (
            patch("server.http_handler._initialize_server"),
            patch("server.http_handler._mcp_server") as mock_mcp_server,
        ):
            mock_mcp_server.handle_http_request = AsyncMock(
                return_value={
                    "statusCode": 200,
                    "headers": {},
                    "body": json.dumps({"result": "success"}),
                }
            )

            status, headers, body = await handler.handle_request(
                method="POST",
                path="/mcp",
                body=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}),
                headers={},
                # No request_id provided
            )

            assert "X-Request-ID" in headers
            assert headers["X-Request-ID"] == "unknown"


class TestErrorHandling:
    """Test error handling."""

    @pytest.mark.asyncio
    async def test_configuration_error_returns_500(self):
        """Test that ConfigurationError returns 500."""
        handler = UniversalHTTPHandler()

        with patch("server.http_handler._initialize_server") as mock_init:
            mock_init.side_effect = ConfigurationError("Config error")

            status, headers, body = await handler.handle_request(
                method="POST",
                path="/mcp",
                body=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}),
                headers={},
            )

            assert status == 500
            error_body = json.loads(body)
            assert error_body["error"]["code"] == -32603
            assert error_body["error"]["message"] == "Server configuration error"
            # Error data must not leak internal config details. It should
            # only echo a request ID for support correlation.
            assert "Config error" not in error_body["error"]["data"]
            assert "Request ID:" in error_body["error"]["data"]

    @pytest.mark.asyncio
    async def test_general_exception_returns_500(self):
        """Test that general exceptions return 500."""
        handler = UniversalHTTPHandler()

        with patch("server.http_handler._initialize_server") as mock_init:
            mock_init.side_effect = Exception("Unexpected error")

            status, headers, body = await handler.handle_request(
                method="POST",
                path="/mcp",
                body=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}),
                headers={},
            )

            assert status == 500
            error_body = json.loads(body)
            assert error_body["error"]["code"] == -32603
            assert error_body["error"]["message"] == "Internal error"


class TestConfigLoading:
    """Test configuration loading."""

    def test_load_config_from_environment_variable(self):
        """Test loading config from environment variable."""
        config_data = {
            "server_name": "TestServer",
            "plugins": {
                "ckan": {"enabled": True, "base_url": "https://data.example.com"}
            },
        }

        with patch.dict(os.environ, {"OPENCONTEXT_CONFIG": json.dumps(config_data)}):
            # Clear cached config
            import server.http_handler

            server.http_handler._config = None

            config = _load_config()
            assert config["server_name"] == "TestServer"
            assert config["plugins"]["ckan"]["enabled"] is True

    def test_load_config_from_file_when_env_not_set(self, tmp_path):
        """Test loading config from file when environment variable not set."""
        config_data = {
            "server_name": "TestServer",
            "plugins": {
                "ckan": {"enabled": True, "base_url": "https://data.example.com"}
            },
        }

        config_file = tmp_path / "config.yaml"
        import yaml

        with open(config_file, "w") as f:
            yaml.dump(config_data, f)

        with (
            patch.dict(os.environ, {}, clear=True),
            patch("server.http_handler.load_and_validate_config") as mock_load,
        ):
            mock_load.return_value = config_data

            # Clear cached config
            import server.http_handler

            server.http_handler._config = None

            _load_config()
            import server.http_handler as _h

            mock_load.assert_called_once_with(_h._packaged_config_path())

    def test_load_config_raises_on_invalid_json(self):
        """Test that invalid JSON in environment variable raises error."""
        with patch.dict(os.environ, {"OPENCONTEXT_CONFIG": "invalid json"}):
            # Clear cached config
            import server.http_handler

            server.http_handler._config = None

            with pytest.raises((ValueError, json.JSONDecodeError)):
                _load_config()

    def test_load_config_caches_result(self):
        """Test that config is cached after first load."""
        config_data = {
            "server_name": "TestServer",
            "plugins": {"ckan": {"enabled": True}},
        }

        with patch.dict(os.environ, {"OPENCONTEXT_CONFIG": json.dumps(config_data)}):
            # Clear cached config
            import server.http_handler

            server.http_handler._config = None

            config1 = _load_config()
            config2 = _load_config()

            # Should return same object (cached)
            assert config1 is config2


class TestServerInitialization:
    """Test server initialization."""

    @pytest.mark.asyncio
    async def test_initialize_server_creates_plugin_manager_and_mcp_server(self):
        """Test that server initialization creates plugin manager and MCP server."""
        config = {
            "server_name": "TestServer",
            "plugins": {
                "ckan": {"enabled": True, "base_url": "https://data.example.com"}
            },
        }

        with (
            patch("server.http_handler._load_config") as mock_load_config,
            patch("server.http_handler.PluginManager") as mock_pm_class,
            patch("server.http_handler.MCPServer") as mock_mcp_class,
        ):
            mock_load_config.return_value = config
            mock_pm = MagicMock()
            mock_pm.load_plugins = AsyncMock()
            mock_pm_class.return_value = mock_pm
            mock_mcp = MagicMock()
            mock_mcp_class.return_value = mock_mcp

            # Clear global state
            import server.http_handler

            server.http_handler._plugin_manager = None
            server.http_handler._mcp_server = None

            await _initialize_server()

            mock_pm_class.assert_called_once_with(config)
            mock_pm.load_plugins.assert_called_once()
            mock_mcp_class.assert_called_once_with(mock_pm)

    @pytest.mark.asyncio
    async def test_initialize_server_reuses_existing_instances(self):
        """Test that server initialization reuses existing instances."""
        config = {"plugins": {"ckan": {"enabled": True}}}

        import server.http_handler

        with (
            patch("server.http_handler._load_config") as mock_load_config,
            patch("server.http_handler.PluginManager") as mock_pm_class,
            # patch.object restores both globals on exit. Assigning them
            # directly used to leave a non-awaitable MagicMock behind for
            # whatever ran next -- see tests/conftest.py.
            patch.object(server.http_handler, "_plugin_manager", MagicMock()),
            patch.object(server.http_handler, "_mcp_server", MagicMock()),
        ):
            mock_load_config.return_value = config

            await _initialize_server()

            # Should not create new instances
            mock_pm_class.assert_not_called()

    @pytest.mark.asyncio
    async def test_initialize_server_raises_on_configuration_error(self):
        """Test that server initialization raises on configuration error."""
        with (
            patch("server.http_handler._load_config") as mock_load_config,
            patch("server.http_handler.PluginManager") as mock_pm_class,
        ):
            from core.validators import ConfigurationError

            mock_load_config.return_value = {"plugins": {}}
            mock_pm = MagicMock()
            mock_pm.load_plugins = AsyncMock(
                side_effect=ConfigurationError("Config error")
            )
            mock_pm_class.return_value = mock_pm

            # Clear global state
            import server.http_handler

            server.http_handler._plugin_manager = None
            server.http_handler._mcp_server = None

            with pytest.raises(RuntimeError) as exc_info:
                await _initialize_server()

            assert "Configuration error" in str(exc_info.value)
