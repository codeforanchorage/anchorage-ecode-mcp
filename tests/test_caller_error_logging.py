"""Caller mistakes must not be reported, or logged, as server faults.

A -32603 plus a traceback is a claim that the server broke. Spending one on
"that guid isn't valid" both misleads the operator and tells the model
nothing it can act on. Every case pinned here was reproduced against the
live production deployment before the fix.

ToolInputError is an explicit marker rather than an inference from
ValueError; the tests below pin the case where that inference would be
wrong.
"""

import ast
import inspect
import json
import logging
import pathlib
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from core.interfaces import (
    InvalidToolParamsError,
    ToolInputError,
    ToolResult,
    UnknownToolError,
)
from core.mcp_server import MCPServer
from core.plugin_manager import PluginManager
from plugins.ecode.config_schema import EcodePluginConfig
from plugins.ecode.plugin import EcodeAPIError, EcodeNotFoundError, EcodePlugin

PLUGIN_PATH = pathlib.Path(inspect.getfile(EcodePlugin))


@pytest.fixture
def ecode_config():
    return {
        "enabled": True,
        "base_url": "https://api.ecode360.com",
        "customer_id": "AN6998",
        "city_name": "Municipality of Anchorage",
        "timeout": 20,
    }


@pytest.fixture
def plugin(ecode_config):
    p = EcodePlugin(ecode_config)
    p.plugin_config = EcodePluginConfig(**ecode_config)
    return p


# --------------------------------------------------------------------------
# B1 -- unknown tool
# --------------------------------------------------------------------------


class TestUnknownTool:
    """Naming a tool that does not exist is -32602, not -32603."""

    @pytest.mark.asyncio
    async def test_unknown_tool_returns_invalid_params(self):
        """Prod returned -32603 "Internal error" for ecode__no_such_tool."""
        plugin_manager = MagicMock(spec=PluginManager)
        plugin_manager.execute_tool = AsyncMock(
            side_effect=UnknownToolError(
                "ecode__no_such_tool", "ecode__search_code, ecode__get_section"
            )
        )
        server = MCPServer(plugin_manager)

        response = await server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "ecode__no_such_tool", "arguments": {}},
            }
        )

        # The tools spec's own example for this case.
        assert response["error"]["code"] == -32602
        assert response["error"]["message"] == "Unknown tool: ecode__no_such_tool"
        # The available list rides in `data` so a model can self-correct.
        assert "ecode__search_code" in response["error"]["data"]

    @pytest.mark.asyncio
    async def test_unknown_tool_logs_warning_without_traceback(self, caplog):
        plugin_manager = MagicMock(spec=PluginManager)
        plugin_manager.execute_tool = AsyncMock(
            side_effect=UnknownToolError("ecode__nope", "ecode__search_code")
        )
        server = MCPServer(plugin_manager)

        with caplog.at_level(logging.WARNING):
            await server.handle_request(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "ecode__nope", "arguments": {}},
                }
            )

        recs = [r for r in caplog.records if "tools/call" in r.getMessage()]
        assert recs, "expected a log record"
        assert all(r.levelno == logging.WARNING for r in recs)
        # exc_info=False leaves the attribute False rather than None;
        # what matters is that no traceback is attached.
        assert not any(r.exc_info for r in recs)

    @pytest.mark.asyncio
    async def test_genuine_server_fault_stays_internal_error(self):
        """The caller-error mapping must not swallow real failures."""
        plugin_manager = MagicMock(spec=PluginManager)
        plugin_manager.execute_tool = AsyncMock(
            side_effect=RuntimeError("upstream exploded")
        )
        server = MCPServer(plugin_manager)

        response = await server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "ecode__search_code", "arguments": {}},
            }
        )

        assert response["error"]["code"] == -32603
        assert response["error"]["message"] == "Internal error"

    @pytest.mark.asyncio
    async def test_genuine_server_fault_still_logs_traceback(self, caplog):
        plugin_manager = MagicMock(spec=PluginManager)
        plugin_manager.execute_tool = AsyncMock(
            side_effect=RuntimeError("upstream exploded")
        )
        server = MCPServer(plugin_manager)

        with caplog.at_level(logging.WARNING):
            await server.handle_request(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "ecode__search_code", "arguments": {}},
                }
            )

        errs = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert errs, "a real fault must still log at ERROR"
        assert any(r.exc_info is not None for r in errs), "with a traceback"

    def test_unknown_tool_error_is_a_valueerror(self):
        """Subclassing ValueError keeps every existing handler working."""
        assert isinstance(UnknownToolError("x"), ValueError)


# --------------------------------------------------------------------------
# B2 -- malformed tools/call
# --------------------------------------------------------------------------


class TestMalformedToolsCall:
    """Requests that fail the CallToolRequest schema are -32602."""

    @pytest.mark.asyncio
    async def test_missing_name_returns_invalid_params(self):
        plugin_manager = MagicMock(spec=PluginManager)
        server = MCPServer(plugin_manager)

        response = await server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"arguments": {}},
            }
        )

        assert response["error"]["code"] == -32602
        assert response["error"]["message"] == "Invalid params"
        assert "name" in response["error"]["data"]

    @pytest.mark.asyncio
    async def test_non_object_arguments_returns_invalid_params(self):
        """Prod returned "'str' object has no attribute 'get'" as a RESULT.

        The plugin was reached with a string, called .get() on it, and the
        AttributeError came back as a tool result with isError: true -- as
        though the tool had run and failed.
        """
        plugin_manager = MagicMock(spec=PluginManager)
        plugin_manager.execute_tool = AsyncMock()
        server = MCPServer(plugin_manager)

        for bad in ("query=dogs", [1, 2], 5):
            response = await server.handle_request(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "ecode__search_code", "arguments": bad},
                }
            )
            assert response["error"]["code"] == -32602, bad
            assert "arguments" in response["error"]["data"], bad
            assert type(bad).__name__ in response["error"]["data"], bad

        # A plugin must never be reached with a malformed request.
        plugin_manager.execute_tool.assert_not_called()

    @pytest.mark.asyncio
    async def test_omitted_arguments_is_valid_and_defaults_to_empty(self):
        """Omitting `arguments` entirely IS valid per the schema."""
        plugin_manager = MagicMock(spec=PluginManager)
        plugin_manager.execute_tool = AsyncMock(
            return_value=ToolResult(
                content=[{"type": "text", "text": "ok"}], success=True
            )
        )
        server = MCPServer(plugin_manager)

        response = await server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "ecode__get_table_of_contents"},
            }
        )

        assert "error" not in response
        plugin_manager.execute_tool.assert_awaited_once_with(
            "ecode__get_table_of_contents", {}
        )

    def test_invalid_tool_params_error_is_a_valueerror(self):
        assert isinstance(InvalidToolParamsError("x"), ValueError)


# --------------------------------------------------------------------------
# B3 -- caller input inside the plugin
# --------------------------------------------------------------------------


class TestPluginCallerInput:
    @pytest.mark.asyncio
    async def test_non_numeric_page_names_the_argument_and_the_value(self, plugin):
        """Prod returned "invalid literal for int() with base 10: 'two'"."""
        result = await plugin.execute_tool(
            "search_code", {"query": "dog", "page": "two"}
        )

        assert result.success is False
        assert "page" in result.error_message
        assert "two" in result.error_message
        assert "invalid literal" not in result.error_message

    @pytest.mark.asyncio
    async def test_negative_page_is_rejected(self, plugin):
        result = await plugin.execute_tool(
            "search_code", {"query": "dog", "page": -3}
        )
        assert result.success is False
        assert "page" in result.error_message

    @pytest.mark.asyncio
    async def test_missing_query_is_a_caller_error(self, plugin):
        result = await plugin.execute_tool("search_code", {})
        assert result.success is False
        assert "query" in result.error_message

    @pytest.mark.asyncio
    async def test_blank_query_is_a_caller_error(self, plugin):
        result = await plugin.execute_tool("search_code", {"query": "   "})
        assert result.success is False
        assert "query" in result.error_message

    @pytest.mark.asyncio
    async def test_missing_guid_points_back_at_search_code(self, plugin):
        result = await plugin.execute_tool("get_section", {})
        assert result.success is False
        assert "search_code" in result.error_message

    @pytest.mark.asyncio
    async def test_section_number_as_guid_is_named_as_such(self, plugin):
        """The predicted failure: a model passes the section NUMBER.

        Prod turned this into an opaque upstream 404 with a percent-encoded
        path in the message.
        """
        for bad in ("§ 21.05.010", "21.05.010"):
            result = await plugin.execute_tool("get_section", {"guid": bad})
            assert result.success is False, bad
            assert "section NUMBER" in result.error_message, bad
            assert "search_code" in result.error_message, bad

    @pytest.mark.asyncio
    async def test_valid_guid_shape_is_accepted(self, plugin):
        """A well-formed guid must reach the API, not be rejected locally."""
        plugin.client = MagicMock()
        plugin.client.get = AsyncMock(
            return_value=MagicMock(
                status_code=200,
                json=MagicMock(return_value={"id": "49401992", "title": "T"}),
                raise_for_status=MagicMock(),
            )
        )
        result = await plugin.execute_tool("get_section", {"guid": "49401992"})
        assert result.success is True

    @pytest.mark.asyncio
    async def test_browse_structure_defaults_to_root(self, plugin):
        plugin.client = MagicMock()
        plugin.client.get = AsyncMock(
            return_value=MagicMock(
                status_code=200,
                json=MagicMock(return_value={"results": {"guid": "ROOT"}}),
                raise_for_status=MagicMock(),
            )
        )
        result = await plugin.execute_tool("browse_structure", {})
        assert result.success is True

    @pytest.mark.asyncio
    async def test_caller_error_logs_warning_without_traceback(
        self, plugin, caplog
    ):
        with caplog.at_level(logging.WARNING):
            await plugin.execute_tool("get_section", {"guid": "21.05.010"})

        recs = [r for r in caplog.records if "get_section" in r.getMessage()]
        assert recs, "expected a log record"
        assert all(r.levelno == logging.WARNING for r in recs)
        assert not any(r.exc_info for r in recs), "no traceback for caller errors"

    @pytest.mark.asyncio
    async def test_server_fault_in_plugin_still_logs_error_with_traceback(
        self, plugin, caplog
    ):
        """The quiet path must not swallow genuine failures."""
        plugin.client = MagicMock()
        plugin.client.get = AsyncMock(side_effect=RuntimeError("socket exploded"))

        with caplog.at_level(logging.WARNING):
            result = await plugin.execute_tool("get_customer_info", {})

        assert result.success is False
        errs = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert errs, "a real fault must still log at ERROR"
        assert any(r.exc_info is not None for r in errs), "with a traceback"


class TestUpstreamStatusClassification:
    """404/400 from eCode360 are the caller's fault; 401/403/5xx are not."""

    def _response(self, status):
        return httpx.Response(
            status_code=status,
            json={"message": "Could not find Content"},
            request=httpx.Request("GET", "https://api.ecode360.com/v1/x"),
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", [400, 404])
    async def test_caller_statuses_raise_tool_input_error(self, plugin, status):
        plugin.client = MagicMock()
        resp = self._response(status)
        plugin.client.get = AsyncMock(return_value=resp)

        with pytest.raises(EcodeNotFoundError) as exc:
            await plugin._get_json("/customer/AN6998/code/content/99999999")

        assert isinstance(exc.value, ToolInputError)
        assert exc.value.status_code == status
        assert "search_code" in str(exc.value)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", [401, 403, 500, 503])
    async def test_server_statuses_stay_server_faults(self, plugin, status):
        plugin.client = MagicMock()
        plugin.client.get = AsyncMock(return_value=self._response(status))

        with pytest.raises(EcodeAPIError) as exc:
            await plugin._get_json("/customer/AN6998")

        assert not isinstance(exc.value, ToolInputError)
        assert exc.value.status_code == status

    @pytest.mark.asyncio
    async def test_toc_404_still_falls_back_to_structure_root(self, plugin):
        """The fallback branches on status, not on a substring of the message.

        It used to match "HTTP 404" in str(e); reclassifying 404 as a caller
        error would have silently broken it.
        """
        calls = []

        async def fake_get(path, params=None):
            calls.append(path)
            if path.endswith("/code/toc"):
                return self._response(404)
            return httpx.Response(
                status_code=200,
                json={"guid": "ROOT", "title": "Code", "children": []},
                request=httpx.Request("GET", "https://api.ecode360.com/v1/x"),
            )

        plugin.client = MagicMock()
        plugin.client.get = AsyncMock(side_effect=fake_get)

        data = await plugin._get_toc()

        assert data["_fallback"].startswith("structure/ROOT")
        assert any("structure/ROOT" in c for c in calls)


# --------------------------------------------------------------------------
# B3 traps -- the classification must not drift
# --------------------------------------------------------------------------


class TestClassificationDoesNotDrift:
    def test_json_decode_error_is_not_a_tool_input_error(self):
        """json.JSONDecodeError subclasses ValueError.

        If ValueError were the marker, a malformed eCode360 payload would be
        misfiled as a caller mistake and silently lose its stack trace.
        """
        try:
            json.loads("{not json")
        except json.JSONDecodeError as e:
            assert isinstance(e, ValueError)
            assert not isinstance(e, ToolInputError)
        else:
            pytest.fail("expected JSONDecodeError")

    def test_no_plain_valueerror_raises_in_the_plugin(self):
        """Every raise site must be classified deliberately.

        The plugin currently raises no bare ValueError at all: caller input
        raises ToolInputError, upstream faults raise EcodeAPIError. A new
        plain `raise ValueError` would be unclassified -- it would inherit
        the generic ERROR + traceback path whether or not it deserves it.
        """
        src = PLUGIN_PATH.read_text(encoding="utf-8")
        assert src.count("raise ValueError(") == 0, (
            "a plain `raise ValueError` was added to the eCode plugin -- "
            "classify it as ToolInputError (caller) or EcodeAPIError "
            "(upstream) rather than letting it default"
        )

    def test_no_unconverted_numeric_coercion_of_caller_args(self):
        """AST sweep, not grep.

        A line-oriented search misses inline forms such as
        min(int(arguments.get("limit", 20)), 100), which is how nine sites
        were missed in the sibling repo.
        """
        tree = ast.parse(PLUGIN_PATH.read_text(encoding="utf-8"))
        arg_names = {"arguments", "args", "kwargs", "params"}
        offenders = []

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            if not (isinstance(fn, ast.Name) and fn.id in {"int", "float"}):
                continue
            if not node.args:
                continue
            if any(
                isinstance(sub, ast.Name) and sub.id in arg_names
                for sub in ast.walk(node.args[0])
            ):
                offenders.append(f"line {node.lineno}: {ast.unparse(node)}")

        assert not offenders, (
            "int()/float() applied directly to caller arguments; route "
            "through _int_arg so the error names the argument and the "
            "offending value:\n  " + "\n  ".join(offenders)
        )


# --------------------------------------------------------------------------
# B4 -- malformed request JSON
# --------------------------------------------------------------------------


class TestMalformedRequestBody:
    @pytest.mark.asyncio
    async def test_malformed_json_logs_warning_not_error(self, caplog):
        """-32700 already tells the caller; our parse traceback adds nothing."""
        server = MCPServer(MagicMock(spec=PluginManager))

        with caplog.at_level(logging.WARNING):
            resp = await server.handle_http_request("{not json")

        assert json.loads(resp["body"])["error"]["code"] == -32700
        recs = [r for r in caplog.records if "Invalid JSON" in r.getMessage()]
        assert recs
        assert all(r.levelno == logging.WARNING for r in recs)
        # exc_info=False leaves the attribute False rather than None;
        # what matters is that no traceback is attached.
        assert not any(r.exc_info for r in recs)


# --------------------------------------------------------------------------
# plugin_manager
# --------------------------------------------------------------------------


class TestPluginManagerRaisesUnknownToolError:
    @pytest.mark.asyncio
    async def test_unknown_tool_name(self):
        manager = PluginManager({"plugins": {}})
        manager._initialized = True
        manager.tools = {"ecode__search_code": ("ecode", "search_code")}

        with pytest.raises(UnknownToolError) as exc:
            await manager.execute_tool("ecode__nonexistent", {})

        assert str(exc.value) == "Unknown tool: ecode__nonexistent"
        assert exc.value.tool_name == "ecode__nonexistent"
        assert "ecode__search_code" in exc.value.available
        assert isinstance(exc.value, ValueError)
